"""
Estrategia Mean Reversion.

Objetivo:
- Detectar activos que se han alejado demasiado de su media.
- Buscar posible rebote hacia el equilibrio.
- Filtrar por liquidez, RSI y volatilidad.
- Proponer entrada, stop y objetivos.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# Archivo con los tickers a analizar.
TICKERS_FILE = "tickers.txt"

# Días hacia atrás que pedimos a Alpaca.
LOOKBACK_DAYS = 120

# Media principal para medir desviación.
SMA_MEAN = 20

# Media de tendencia más larga para evitar activos destruidos.
SMA_TREND = 100

# RSI para detectar sobreventa.
RSI_PERIOD = 14

# Bollinger Bands.
# Usamos desviación estándar para medir extremo estadístico.
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2

# ATR para medir volatilidad y colocar stop.
ATR_PERIOD = 14

# Distancia mínima por debajo de la SMA 20.
# Ejemplo: -4 significa que el precio está al menos 4% bajo la media.
MIN_DISTANCE_BELOW_SMA_PCT = -4

# RSI máximo para considerar sobreventa.
MAX_RSI = 35

# Evitamos activos que estén demasiado rotos:
# precio debe estar por encima de SMA 100 o no demasiado lejos de ella.
MAX_DISTANCE_BELOW_TREND_SMA_PCT = -12

# Volumen monetario mínimo.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Máximo de candidatos a mostrar.
TOP_N = 20

# Claves de Alpaca desde variables de entorno.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde un archivo de texto.

    Ignora líneas vacías y comentarios.
    """
    with open(path, "r", encoding="utf-8") as file:
        return sorted(
            {
                line.strip().upper()
                for line in file
                if line.strip() and not line.strip().startswith("#")
            }
        )


def get_daily_bars(client, symbols):
    """
    Descarga velas diarias desde Alpaca.

    Devuelve un diccionario:
    {
        "AAPL": DataFrame,
        "NVDA": DataFrame,
        ...
    }
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
        end=datetime.now(UTC),
        feed=DataFeed.IEX,
    )

    bars = client.get_stock_bars(request).data
    data = {}

    for symbol, symbol_bars in bars.items():
        rows = []

        for bar in symbol_bars:
            rows.append(
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )

        if rows:
            df = pd.DataFrame(rows).sort_values("timestamp")
            df.set_index("timestamp", inplace=True)
            data[symbol] = df

    return data


def calculate_rsi(close, period):
    """
    Calcula RSI clásico.

    RSI bajo suele indicar sobreventa.
    """
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_atr(df, period):
    """
    Calcula ATR, una medida de volatilidad.

    ATR ayuda a colocar stops adaptados al movimiento normal del activo.
    """
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    true_range = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    ).max(axis=1)

    return true_range.rolling(period).mean()


def average_dollar_volume(df, window=20):
    """
    Calcula volumen monetario medio.

    Volumen monetario = cierre x volumen.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando mean reversion.

    Condiciones:
    - Precio muy por debajo de SMA 20.
    - Precio cerca o por debajo de banda inferior de Bollinger.
    - RSI en sobreventa.
    - Liquidez suficiente.
    - No demasiado destruido frente a SMA 100.
    - Posible primera señal de estabilización.
    """
    min_required = max(SMA_TREND, BOLLINGER_PERIOD, ATR_PERIOD, RSI_PERIOD) + 5

    if len(df) < min_required:
        return None

    df = df.copy()

    # Medias.
    df["sma_mean"] = df["close"].rolling(SMA_MEAN).mean()
    df["sma_trend"] = df["close"].rolling(SMA_TREND).mean()

    # RSI.
    df["rsi"] = calculate_rsi(df["close"], RSI_PERIOD)

    # Bollinger Bands.
    rolling_mean = df["close"].rolling(BOLLINGER_PERIOD).mean()
    rolling_std = df["close"].rolling(BOLLINGER_PERIOD).std()

    df["bollinger_mid"] = rolling_mean
    df["bollinger_lower"] = rolling_mean - (rolling_std * BOLLINGER_STD)
    df["bollinger_upper"] = rolling_mean + (rolling_std * BOLLINGER_STD)

    # ATR.
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    df = df.dropna()

    if df.empty:
        return None

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    price = float(latest["close"])
    previous_close = float(previous["close"])

    sma_mean = float(latest["sma_mean"])
    sma_trend = float(latest["sma_trend"])
    rsi = float(latest["rsi"])
    atr = float(latest["atr"])

    bollinger_lower = float(latest["bollinger_lower"])
    bollinger_mid = float(latest["bollinger_mid"])

    if sma_mean <= 0 or sma_trend <= 0 or atr <= 0:
        return None

    # Distancia del precio respecto a la SMA 20.
    distance_mean_pct = ((price / sma_mean) - 1) * 100

    # Distancia respecto a SMA 100.
    distance_trend_pct = ((price / sma_trend) - 1) * 100

    avg_dollar_volume = average_dollar_volume(df, 20)

    # Condición de sobreventa respecto a media.
    distance_ok = distance_mean_pct <= MIN_DISTANCE_BELOW_SMA_PCT

    # Condición de Bollinger:
    # precio en o bajo banda inferior.
    bollinger_ok = price <= bollinger_lower

    # RSI bajo.
    rsi_ok = rsi <= MAX_RSI

    # Evitamos activos demasiado rotos.
    trend_damage_ok = distance_trend_pct >= MAX_DISTANCE_BELOW_TREND_SMA_PCT

    # Liquidez.
    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    # Pequeña estabilización:
    # hoy no cierra peor que ayer o deja mínimo superior.
    stabilization_ok = (
        price >= previous_close
        or float(latest["low"]) > float(previous["low"])
    )

    if not all([
        distance_ok,
        bollinger_ok,
        rsi_ok,
        trend_damage_ok,
        liquidity_ok,
        stabilization_ok,
    ]):
        return None

    # Stop orientativo:
    # debajo del mínimo reciente menos 1 ATR.
    recent_low = float(df["low"].tail(5).min())
    stop_loss = recent_low - atr

    if stop_loss <= 0 or stop_loss >= price:
        return None

    # Objetivo 1: volver a SMA 20.
    take_profit_1 = sma_mean

    # Objetivo 2: volver a media de Bollinger.
    take_profit_2 = bollinger_mid

    # Score:
    # prioriza mayor desviación, RSI más bajo y cercanía al daño máximo permitido.
    score = (
        abs(distance_mean_pct) * 0.40
        + (MAX_RSI - rsi) * 0.30
        + abs(min(distance_trend_pct, 0)) * 0.15
        + ((take_profit_1 / price - 1) * 100) * 0.15
    )

    return {
        "symbol": symbol,
        "price": price,
        "sma_mean": sma_mean,
        "sma_trend": sma_trend,
        "distance_mean_pct": distance_mean_pct,
        "distance_trend_pct": distance_trend_pct,
        "rsi": rsi,
        "bollinger_lower": bollinger_lower,
        "bollinger_mid": bollinger_mid,
        "atr": atr,
        "avg_dollar_volume": avg_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_mean_reversion_candidates():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga datos.
    3. Analiza cada activo.
    4. Ordena por score.
    5. Devuelve el top.
    """
    symbols = load_tickers(TICKERS_FILE)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_daily_bars(client, symbols)

    candidates = []

    for symbol in symbols:
        df = data.get(symbol)

        if df is None or df.empty:
            continue

        result = analyze_symbol(symbol, df)

        if result:
            candidates.append(result)

    candidates = sorted(
        candidates,
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates[:TOP_N]


def format_candidate(candidate):
    """
    Formatea un candidato para imprimirlo o enviarlo por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Dist SMA20: {candidate['distance_mean_pct']:.2f}% | "
        f"RSI: {candidate['rsi']:.1f} | "
        f"Banda baja: {candidate['bollinger_lower']:.2f} | "
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1 SMA20: {candidate['take_profit_1']:.2f} | "
        f"TP2 Media Boll: {candidate['take_profit_2']:.2f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_mean_reversion_candidates()
    output_path, output_count = write_results_to_txt("Mean_Reversion", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos mean reversion con los filtros actuales.")
    else:
        print("Candidatos Mean Reversion:")
        for candidate in results:
            print(format_candidate(candidate))
