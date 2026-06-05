"""
Estrategia Trend Following.

Objetivo:
- Seguir tendencias alcistas ya confirmadas.
- Evitar intentar adivinar suelos.
- Comprar fuerza con control de riesgo.
- Usar stop dinámico basado en ATR.

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


# Archivo con tickers.
TICKERS_FILE = "tickers.txt"

# Días hacia atrás para descargar datos.
LOOKBACK_DAYS = 220

# Medias móviles de tendencia.
SMA_FAST = 50
SMA_SLOW = 200

# Ruptura de máximos.
BREAKOUT_LOOKBACK = 55

# ATR para stop.
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 3.0

# Volumen monetario mínimo.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Máximo de candidatos.
TOP_N = 20

# Claves Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde archivo de texto.
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


def calculate_atr(df, period):
    """
    Calcula ATR.

    ATR mide volatilidad.
    Se usa para colocar un stop dinámico:
    cuanto más volátil el activo, más margen necesita.
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
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def pct_change(series, window):
    """
    Calcula rentabilidad porcentual en una ventana.
    """
    if len(series) <= window:
        return None

    start_price = float(series.iloc[-window - 1])
    end_price = float(series.iloc[-1])

    if start_price <= 0:
        return None

    return ((end_price / start_price) - 1) * 100


def analyze_symbol(symbol, df):
    """
    Analiza si un activo cumple condiciones Trend Following.

    Condiciones:
    - Precio por encima de SMA 200.
    - SMA 50 por encima de SMA 200.
    - SMA 200 con pendiente positiva.
    - Precio rompe máximos de 55 días.
    - Volumen monetario suficiente.
    """
    min_required = max(SMA_SLOW, BREAKOUT_LOOKBACK, ATR_PERIOD) + 10

    if len(df) < min_required:
        return None

    df = df.copy()

    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    df = df.dropna()

    if df.empty:
        return None

    latest = df.iloc[-1]

    price = float(latest["close"])
    sma_fast = float(latest["sma_fast"])
    sma_slow = float(latest["sma_slow"])
    atr = float(latest["atr"])

    if sma_slow <= 0 or atr <= 0:
        return None

    # Pendiente de la SMA 200:
    # comparamos la SMA 200 actual con la de hace 20 sesiones.
    if len(df) < 25:
        return None

    sma_slow_20d_ago = float(df["sma_slow"].iloc[-21])
    sma_slow_slope_pct = ((sma_slow / sma_slow_20d_ago) - 1) * 100

    # Máximo de los últimos 55 días,
    # excluyendo la vela actual.
    previous_window = df.iloc[-BREAKOUT_LOOKBACK - 1:-1]
    breakout_level = float(previous_window["high"].max())

    breakout_pct = ((price / breakout_level) - 1) * 100

    avg_dollar_volume = average_dollar_volume(df, 20)

    trend_ok = (
        price > sma_slow
        and sma_fast > sma_slow
        and sma_slow_slope_pct > 0
    )

    breakout_ok = price > breakout_level

    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    if not all([trend_ok, breakout_ok, liquidity_ok]):
        return None

    # Stop tipo trend following:
    # precio menos 3 ATR.
    atr_stop = price - (atr * ATR_STOP_MULTIPLIER)

    # También podemos usar SMA 50 como stop más lento.
    stop_loss = max(atr_stop, sma_fast)

    if stop_loss >= price:
        return None

    risk_per_share = price - stop_loss

    # Objetivos orientativos.
    # En trend following a veces se deja correr sin TP fijo,
    # pero damos referencias.
    take_profit_1 = price + risk_per_share * 2
    take_profit_2 = price + risk_per_share * 4

    trend_return_3m = pct_change(df["close"], 63)

    if trend_return_3m is None:
        trend_return_3m = 0

    # Score:
    # prioriza ruptura, tendencia de 3 meses y pendiente de SMA 200.
    score = (
        breakout_pct * 0.30
        + trend_return_3m * 0.40
        + sma_slow_slope_pct * 0.30
    )

    return {
        "symbol": symbol,
        "price": price,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "sma_slow_slope_pct": sma_slow_slope_pct,
        "breakout_level": breakout_level,
        "breakout_pct": breakout_pct,
        "atr": atr,
        "avg_dollar_volume": avg_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "trend_return_3m": trend_return_3m,
        "score": score,
    }


def find_trend_following_candidates():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga datos.
    3. Analiza tendencia.
    4. Ordena por score.
    5. Devuelve top candidatos.
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
    Formatea un candidato para imprimir o enviar por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Breakout: {candidate['breakout_level']:.2f} | "
        f"Ruptura: {candidate['breakout_pct']:.2f}% | "
        f"Tend 3M: {candidate['trend_return_3m']:.2f}% | "
        f"SMA200 slope: {candidate['sma_slow_slope_pct']:.2f}% | "
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1: {candidate['take_profit_1']:.2f} | "
        f"TP2: {candidate['take_profit_2']:.2f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_trend_following_candidates()
    output_path, output_count = write_results_to_txt("TrendFollowing", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Trend Following con los filtros actuales.")
    else:
        print("Candidatos Trend Following:")
        for candidate in results:
            print(format_candidate(candidate))
