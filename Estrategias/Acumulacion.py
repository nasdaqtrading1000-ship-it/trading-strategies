"""
Estrategia Acumulacion.

Misma idea que Acumula Metales, pero aplicada a todo el universo
filtrado de tickers.txt:

- Precio por debajo de SMA180 diaria.
- Precio por debajo de SMA120 semanal.
- RSI14 diario menor que 30.

La operativa propuesta es LONG de acumulacion:
- Apertura: precio actual usado por el calculo.
- Sin cierre automatico de momento.
- Cierre y stop se dejan como referencias informativas.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca_request import get_stock_bars_data
from analysis_debug import log_strategy_summary, log_symbol_decision
from env_loader import load_env
from txt_output import write_results_to_txt


load_env()

STRATEGY_NAME = "Acumulacion"
BASE_DIR = Path(__file__).resolve().parent
TICKERS_FILE = BASE_DIR / "tickers.txt"
LOOKBACK_DAYS = 950
SMA_DAILY = 180
SMA_WEEKLY = 120
RSI_WINDOW = 14
TOP_N = 20

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    with open(path, "r", encoding="utf-8") as file:
        return sorted(
            {
                line.strip().upper()
                for line in file
                if line.strip() and not line.strip().startswith("#")
            }
        )


def get_daily_bars(client, symbols):
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


def rsi(series, window=14):
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(window).mean()
    avg_loss = losses.rolling(window).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def weekly_sma(df, window=20):
    weekly = df["close"].resample("W-FRI").last().dropna()
    if len(weekly) < window:
        return None
    return float(weekly.rolling(window).mean().iloc[-1])


def analyze_symbol(symbol, df):
    if len(df) < SMA_DAILY + RSI_WINDOW + 2:
        return None

    df = df.copy()
    df["sma180"] = df["close"].rolling(SMA_DAILY).mean()
    df["rsi14"] = rsi(df["close"], RSI_WINDOW)

    latest = df.iloc[-1]
    price = float(latest["close"])
    sma180 = float(latest["sma180"])
    rsi14 = float(latest["rsi14"])
    sma120_weekly = weekly_sma(df, SMA_WEEKLY)

    if price <= 0 or sma120_weekly is None:
        return None

    if not (price < sma180 and price < sma120_weekly and rsi14 < 30):
        return None

    distance_daily = ((sma180 / price) - 1) * 100
    distance_weekly = ((sma120_weekly / price) - 1) * 100
    oversold_points = 30 - rsi14
    score = distance_daily * 0.35 + distance_weekly * 0.35 + oversold_points * 0.30

    target_by_weekly = sma120_weekly
    target_by_percent = price * 1.12
    target = min(target_by_weekly, target_by_percent)
    if target <= price:
        target = price * 1.08

    return {
        "symbol": symbol,
        "price": price,
        "sma180": sma180,
        "sma120_weekly": sma120_weekly,
        "rsi14": rsi14,
        "distance_daily_pct": distance_daily,
        "distance_weekly_pct": distance_weekly,
        "target": target,
        "stop_loss": price * 0.90,
        "score": score,
    }


def find_accumulation_candidates():
    symbols = load_tickers(TICKERS_FILE)
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    data = get_daily_bars(client, symbols)

    candidates = []
    with_data_count = 0
    accepted_count = 0

    for symbol in symbols:
        df = data.get(symbol)
        if df is None or df.empty:
            log_symbol_decision(STRATEGY_NAME, symbol, "SIN DATOS", "No hay velas diarias")
            continue

        with_data_count += 1
        result = analyze_symbol(symbol, df)
        if result:
            accepted_count += 1
            candidates.append(result)
            log_symbol_decision(STRATEGY_NAME, symbol, "OK", format_candidate(result))
        else:
            log_symbol_decision(
                STRATEGY_NAME,
                symbol,
                "DESCARTADO",
                "No cumple precio < SMA180 diaria, precio < SMA120 semanal y RSI14 < 30",
            )

    selected = sorted(candidates, key=lambda item: item["score"], reverse=True)[:TOP_N]
    log_strategy_summary(STRATEGY_NAME, len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def format_candidate(candidate):
    return (
        f"{candidate['symbol']} | "
        f"Direccion: LONG | "
        f"Precio actual: {candidate['price']:.2f} | "
        f"Apertura: {candidate['price']:.2f} | "
        f"Cierre: {candidate['target']:.2f} | "
        f"Stop Loss: {candidate['stop_loss']:.2f} | "
        f"Cierre automatico: NO | "
        f"SMA180 diaria: {candidate['sma180']:.2f} | "
        f"SMA120 semanal: {candidate['sma120_weekly']:.2f} | "
        f"RSI14: {candidate['rsi14']:.2f} | "
        f"Dist SMA180: {candidate['distance_daily_pct']:.2f}% | "
        f"Dist SMA120 sem: {candidate['distance_weekly_pct']:.2f}% | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_accumulation_candidates()
    output_path, output_count = write_results_to_txt(STRATEGY_NAME, results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Acumulacion con los filtros actuales.")
    else:
        print("Candidatos Acumulacion:")
        for candidate in results:
            print(format_candidate(candidate))
