"""
Estrategia Follow The Money.

Objetivo:
- Buscar donde esta entrando dinero hoy.
- Comparar el volumen monetario del ultimo dia frente a las medias
  de 1, 2 y 3 meses.
- Devolver los 10 activos con mayor expansion de volumen monetario.

La operativa propuesta es LONG:
- Apertura: precio actual usado por el calculo.
- Cierre: +10% sobre apertura.
- Stop Loss: -10% sobre apertura.
"""

import math
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

BASE_DIR = Path(__file__).resolve().parent
TICKERS_FILE = BASE_DIR / "tickers.txt"
LOOKBACK_DAYS = 120
TOP_N = 10
MIN_LATEST_DOLLAR_VOLUME = 10_000_000
WINDOW_1M = 21
WINDOW_2M = 42
WINDOW_3M = 63

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


def average_dollar_volume(dollar_volume, window):
    if len(dollar_volume) < window + 1:
        return None
    previous = dollar_volume.iloc[-window - 1:-1]
    if previous.empty:
        return None
    average = float(previous.mean())
    return average if average > 0 else None


def safe_ratio(value, base):
    if base is None or base <= 0:
        return None
    return float(value) / float(base)


def analyze_symbol(symbol, df):
    if len(df) < WINDOW_3M + 1:
        return None

    df = df.copy()
    df["dollar_volume"] = df["close"] * df["volume"]

    latest = df.iloc[-1]
    price = float(latest["close"])
    latest_dollar_volume = float(latest["dollar_volume"])

    if price <= 0 or latest_dollar_volume < MIN_LATEST_DOLLAR_VOLUME:
        return None

    avg_1m = average_dollar_volume(df["dollar_volume"], WINDOW_1M)
    avg_2m = average_dollar_volume(df["dollar_volume"], WINDOW_2M)
    avg_3m = average_dollar_volume(df["dollar_volume"], WINDOW_3M)

    ratio_1m = safe_ratio(latest_dollar_volume, avg_1m)
    ratio_2m = safe_ratio(latest_dollar_volume, avg_2m)
    ratio_3m = safe_ratio(latest_dollar_volume, avg_3m)

    if ratio_1m is None or ratio_2m is None or ratio_3m is None:
        return None

    # Buscamos expansion real: el dinero de hoy debe estar claramente
    # por encima de su media mensual.
    if ratio_1m < 1.25:
        return None

    score = (
        ratio_1m * 0.5
        + ratio_2m * 0.3
        + ratio_3m * 0.2
        + math.log10(max(latest_dollar_volume, 1)) * 0.05
    )

    return {
        "symbol": symbol,
        "price": price,
        "open_price": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "latest_dollar_volume": latest_dollar_volume,
        "avg_dollar_volume_1m": avg_1m,
        "avg_dollar_volume_2m": avg_2m,
        "avg_dollar_volume_3m": avg_3m,
        "ratio_1m": ratio_1m,
        "ratio_2m": ratio_2m,
        "ratio_3m": ratio_3m,
        "target": price * 1.10,
        "stop_loss": price * 0.90,
        "score": score,
    }


def find_follow_the_money_candidates():
    symbols = load_tickers(TICKERS_FILE)
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    data = get_daily_bars(client, symbols)

    candidates = []
    with_data_count = 0
    accepted_count = 0

    for symbol in symbols:
        df = data.get(symbol)
        if df is None or df.empty:
            log_symbol_decision("Follow The Money", symbol, "SIN DATOS", "No hay velas diarias")
            continue

        with_data_count += 1
        result = analyze_symbol(symbol, df)
        if result:
            accepted_count += 1
            candidates.append(result)
            log_symbol_decision("Follow The Money", symbol, "OK", format_candidate(result))
        else:
            log_symbol_decision(
                "Follow The Money",
                symbol,
                "DESCARTADO",
                "No supera expansion minima de volumen monetario o liquidez",
            )

    selected = sorted(candidates, key=lambda item: item["score"], reverse=True)[:TOP_N]
    log_strategy_summary("Follow The Money", len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def fmt_money(value):
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def format_candidate(candidate):
    return (
        f"{candidate['symbol']} | "
        f"Direccion: LONG | "
        f"Precio actual: {candidate['price']:.2f} | "
        f"Apertura: {candidate['price']:.2f} | "
        f"Cierre: {candidate['target']:.2f} | "
        f"Stop Loss: {candidate['stop_loss']:.2f} | "
        f"Vol$ dia: {fmt_money(candidate['latest_dollar_volume'])} | "
        f"Media Vol$ 1M: {fmt_money(candidate['avg_dollar_volume_1m'])} | "
        f"Media Vol$ 2M: {fmt_money(candidate['avg_dollar_volume_2m'])} | "
        f"Media Vol$ 3M: {fmt_money(candidate['avg_dollar_volume_3m'])} | "
        f"Ratio 1M: {candidate['ratio_1m']:.2f}x | "
        f"Ratio 2M: {candidate['ratio_2m']:.2f}x | "
        f"Ratio 3M: {candidate['ratio_3m']:.2f}x | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_follow_the_money_candidates()
    output_path, output_count = write_results_to_txt("Follow The Money", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Follow The Money con los filtros actuales.")
    else:
        print("Candidatos Follow The Money:")
        for candidate in results:
            print(format_candidate(candidate))
