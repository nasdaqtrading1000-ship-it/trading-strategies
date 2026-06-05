"""
Estrategia Breakout de resistencias.

Objetivo:
- Detectar acciones que rompen una resistencia reciente.
- Confirmar que la ruptura se produce con volumen.
- Evitar rupturas débiles o en activos sin tendencia.

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

# Ventana para calcular resistencia.
# Ejemplo: máximo de los últimos 20 días.
RESISTANCE_LOOKBACK = 20

# Medias móviles para confirmar tendencia.
SMA_FAST = 20
SMA_SLOW = 50

# Volumen medio para comparar la ruptura.
VOLUME_LOOKBACK = 20

# La vela de ruptura debe tener al menos este multiplicador
# respecto al volumen medio.
# 1.5 = 50% más volumen que la media.
MIN_VOLUME_MULTIPLIER = 1.5

# Margen mínimo por encima de la resistencia.
# 0.2 significa que el cierre debe superar la resistencia un 0.2%.
MIN_BREAKOUT_PCT = 0.2

# Volumen monetario mínimo medio.
# Evita activos ilíquidos.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Máximo de candidatos a mostrar.
TOP_N = 20

# Claves de Alpaca desde variables de entorno.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde un archivo de texto.

    Ignora:
    - líneas vacías
    - líneas que empiezan por #
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


def average_dollar_volume(df, window=20):
    """
    Calcula volumen monetario medio.

    Volumen monetario = cierre x volumen.

    Sirve para filtrar activos que realmente mueven dinero.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando ruptura de resistencia.

    Condiciones:
    - Suficientes datos.
    - Tendencia alcista.
    - Cierre actual rompe resistencia.
    - Volumen actual superior al volumen medio.
    - Volumen monetario suficiente.
    """
    min_required = max(SMA_SLOW, RESISTANCE_LOOKBACK, VOLUME_LOOKBACK) + 5

    if len(df) < min_required:
        return None

    df = df.copy()

    # Medias móviles.
    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()

    # Volumen medio.
    df["avg_volume"] = df["volume"].rolling(VOLUME_LOOKBACK).mean()

    df = df.dropna()

    if df.empty:
        return None

    latest = df.iloc[-1]

    price = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])
    volume = float(latest["volume"])
    sma_fast = float(latest["sma_fast"])
    sma_slow = float(latest["sma_slow"])
    avg_volume = float(latest["avg_volume"])

    # Resistencia:
    # máximo de los últimos X días,
    # excluyendo la vela actual para no compararla consigo misma.
    previous_window = df.iloc[-RESISTANCE_LOOKBACK - 1:-1]
    resistance = float(previous_window["high"].max())

    if resistance <= 0 or avg_volume <= 0:
        return None

    # Porcentaje por encima de la resistencia.
    breakout_pct = ((price / resistance) - 1) * 100

    # Relación de volumen:
    # volumen actual dividido entre volumen medio.
    volume_ratio = volume / avg_volume

    avg_dollar_volume = average_dollar_volume(df, 20)

    # Confirmación de tendencia.
    trend_ok = (
        price > sma_slow
        and sma_fast > sma_slow
    )

    # Confirmación de ruptura.
    breakout_ok = breakout_pct >= MIN_BREAKOUT_PCT

    # Confirmación de volumen.
    volume_ok = volume_ratio >= MIN_VOLUME_MULTIPLIER

    # Confirmación de liquidez.
    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    if not all([trend_ok, breakout_ok, volume_ok, liquidity_ok]):
        return None

    # Stop orientativo:
    # debajo de la resistencia rota o mínimo reciente.
    recent_low = float(df["low"].tail(5).min())
    stop_loss = min(resistance, recent_low)

    risk_per_share = price - stop_loss

    if risk_per_share <= 0:
        return None

    # Objetivos orientativos por múltiplos de riesgo.
    take_profit_1 = price + risk_per_share * 1.5
    take_profit_2 = price + risk_per_share * 2.5

    # Score:
    # prioriza ruptura limpia, volumen fuerte y tendencia.
    score = (
        breakout_pct * 0.35
        + volume_ratio * 0.35
        + ((price / sma_slow - 1) * 100) * 0.30
    )

    return {
        "symbol": symbol,
        "price": price,
        "high": high,
        "low": low,
        "resistance": resistance,
        "breakout_pct": breakout_pct,
        "volume": volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "avg_dollar_volume": avg_dollar_volume,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_breakout_candidates():
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
    Formatea un candidato para imprimirlo o mandarlo por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Resistencia: {candidate['resistance']:.2f} | "
        f"Ruptura: {candidate['breakout_pct']:.2f}% | "
        f"Vol xMedia: {candidate['volume_ratio']:.2f}x | "
        f"Vol$: {candidate['avg_dollar_volume'] / 1_000_000:.1f}M | "
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1: {candidate['take_profit_1']:.2f} | "
        f"TP2: {candidate['take_profit_2']:.2f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_breakout_candidates()
    output_path, output_count = write_results_to_txt("BreaKout", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos breakout con los filtros actuales.")
    else:
        print("Candidatos Breakout:")
        for candidate in results:
            print(format_candidate(candidate))
