"""
Estrategia Swing Trading tendencial.

Objetivo:
- Buscar activos en tendencia alcista.
- Esperar un retroceso razonable.
- Detectar recuperación de fuerza.
- Proponer entrada, stop loss y objetivos.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca_request import get_stock_bars_data
from alpaca.data.timeframe import TimeFrame
from analysis_debug import log_strategy_summary, log_symbol_decision


# Archivo con los tickers a analizar.
TICKERS_FILE = "tickers.txt"

# Días hacia atrás que pedimos a Alpaca.
LOOKBACK_DAYS = 120

# Medias móviles para detectar tendencia.
SMA_FAST = 20
SMA_SLOW = 50

# RSI para evitar comprar demasiado sobrecomprado o demasiado débil.
RSI_PERIOD = 14

# Buscamos el máximo de los últimos 20 días
# y medimos cuánto ha retrocedido desde ahí.
PULLBACK_LOOKBACK = 20

# Retroceso mínimo y máximo aceptado.
# Ejemplo:
# 3% = pequeño retroceso.
# 12% = retroceso importante pero todavía controlado.
MIN_PULLBACK_PCT = 3
MAX_PULLBACK_PCT = 12

# Para confirmar recuperación,
# pedimos que el cierre supere máximos recientes.
BREAKOUT_LOOKBACK = 3

# Volumen monetario mínimo medio.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Número máximo de candidatos a mostrar.
TOP_N = 20

# Claves de Alpaca desde variables de entorno.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde un archivo de texto.

    Ignora:
    - líneas vacías
    - comentarios que empiezan por #
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

    Devuelve:
    {
        "AAPL": DataFrame,
        "NVDA": DataFrame,
        ...
    }
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        adjustment=Adjustment.RAW,
        start=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
        end=datetime.now(UTC),
        feed=DataFeed.IEX,
    )

    bars = get_stock_bars_data(client, request)
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

    RSI bajo: activo débil o en retroceso.
    RSI alto: activo fuerte o sobrecomprado.

    Para swing tendencial nos interesa una zona media,
    por ejemplo 40-60, donde puede estar recuperando.
    """
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def average_dollar_volume(df, window=20):
    """
    Calcula el volumen monetario medio.

    Volumen monetario = cierre x volumen.

    Se usa para evitar activos demasiado ilíquidos.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def pct_change(series, window):
    """
    Calcula la rentabilidad porcentual en una ventana.

    Ejemplo:
    Si hace 50 días valía 100 y hoy vale 120:
    devuelve 20.0
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
    Analiza un activo y decide si cumple las condiciones swing.

    Condiciones:
    - Tendencia alcista.
    - Retroceso desde máximos recientes.
    - RSI en zona razonable.
    - Recuperación de fuerza.
    - Volumen suficiente.
    """
    if len(df) < SMA_SLOW + 10:
        return None

    df = df.copy()

    # Medias móviles.
    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()

    # RSI.
    df["rsi"] = calculate_rsi(df["close"], RSI_PERIOD)

    df = df.dropna()

    if df.empty:
        return None

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    price = float(latest["close"])
    sma_fast = float(latest["sma_fast"])
    sma_slow = float(latest["sma_slow"])
    rsi = float(latest["rsi"])

    # Tendencia de los últimos 50 días.
    trend_return = pct_change(df["close"], 50)

    if trend_return is None:
        return None

    # Máximo de los últimos 20 días.
    highest_20d = float(df["high"].tail(PULLBACK_LOOKBACK).max())

    # Retroceso actual desde ese máximo.
    pullback_pct = ((highest_20d - price) / highest_20d) * 100

    # Máximo reciente antes de la vela actual.
    # Se usa para comprobar recuperación.
    recent_high_before_today = float(
        df["high"].iloc[-BREAKOUT_LOOKBACK - 1:-1].max()
    )

    avg_dollar_volume = average_dollar_volume(df, 20)

    # Tendencia alcista:
    # precio por encima de SMA lenta,
    # SMA rápida por encima de SMA lenta,
    # y rentabilidad de 50 días positiva.
    trend_ok = (
        price > sma_slow
        and sma_fast > sma_slow
        and trend_return > 0
    )

    # Queremos que haya retrocedido,
    # pero no que se haya desplomado demasiado.
    pullback_ok = (
        MIN_PULLBACK_PCT <= pullback_pct <= MAX_PULLBACK_PCT
    )

    # Evitamos RSI demasiado bajo o demasiado alto.
    rsi_ok = 40 <= rsi <= 60

    # Confirmación de recuperación:
    # el cierre actual supera máximos recientes
    # y cierra mejor que ayer.
    recovery_ok = (
        price > recent_high_before_today
        and price > float(previous["close"])
    )

    volume_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    if not all([trend_ok, pullback_ok, rsi_ok, recovery_ok, volume_ok]):
        return None

    # Score para ordenar candidatos.
    score = (
        trend_return * 0.35
        + (MAX_PULLBACK_PCT - pullback_pct) * 0.25
        + (60 - abs(50 - rsi)) * 0.20
        + ((price / sma_slow - 1) * 100) * 0.20
    )

    # Stop orientativo:
    # mínimo de los últimos 5 días o SMA lenta.
    stop_loss = min(
        float(df["low"].tail(5).min()),
        sma_slow
    )

    # Objetivos orientativos por múltiplos de riesgo.
    risk_per_share = price - stop_loss

    take_profit_1 = price + risk_per_share * 1.5
    take_profit_2 = price + risk_per_share * 2.5

    return {
        "symbol": symbol,
        "price": price,
        "trend_return_pct": trend_return,
        "pullback_pct": pullback_pct,
        "rsi": rsi,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "avg_dollar_volume": avg_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_swing_candidates():
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
    with_data_count = 0
    accepted_count = 0

    for symbol in symbols:
        df = data.get(symbol)

        if df is None or df.empty:
            log_symbol_decision("Swing Trading", symbol, "SIN DATOS", "Alpaca no devolvio velas")
            continue

        with_data_count += 1
        result = analyze_symbol(symbol, df)

        if result:
            accepted_count += 1
            log_symbol_decision("Swing Trading", symbol, "OK", format_candidate(result))
            candidates.append(result)
        else:
            log_symbol_decision("Swing Trading", symbol, "DESCARTADO", "No cumple tendencia, pullback, RSI, recuperacion o volumen")

    candidates = sorted(
        candidates,
        key=lambda item: item["score"],
        reverse=True,
    )

    selected = candidates[:TOP_N]
    log_strategy_summary("Swing Trading", len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def format_candidate(candidate):
    """
    Formatea un candidato para imprimirlo o mandarlo por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Tendencia 50d: {candidate['trend_return_pct']:.2f}% | "
        f"Pullback: {candidate['pullback_pct']:.2f}% | "
        f"RSI: {candidate['rsi']:.1f} | "
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1: {candidate['take_profit_1']:.2f} | "
        f"TP2: {candidate['take_profit_2']:.2f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_swing_candidates()
    output_path, output_count = write_results_to_txt("SwingTrading", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos swing con los filtros actuales.")
    else:
        print("Candidatos Swing Trading:")
        for candidate in results:
            print(format_candidate(candidate))
