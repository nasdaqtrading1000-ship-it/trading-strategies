"""
Estrategia Reversion RSI 5.

Reglas:
- SHORT cuando RSI14 > 80 y el precio esta mas de 5% por encima
  de la media de las dos ultimas horas.
- LONG cuando RSI14 < 20 y el precio esta mas de 5% por debajo
  de la media de las dos ultimas horas.

Gestion:
- No cierra perdidas.
- Acumula varias entradas del mismo ticker y direccion.
- El simulador cerrara todas las posiciones acumuladas de ese ticker
  y direccion cuando el beneficio conjunto sea >= 5% de lo invertido.
"""

import os
from datetime import UTC, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

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

STRATEGY_NAME = "Reversion RSI 5"
BASE_DIR = Path(__file__).resolve().parent
TICKERS_FILE = BASE_DIR / "tickers.txt"
NY_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
LOOKBACK_DAYS = 3
MEAN_WINDOW_MINUTES = 120
RSI_WINDOW = 14
MIN_DISTANCE_PCT = 5.0
SHORT_RSI = 80
LONG_RSI = 20
TAKE_PROFIT_PCT = 5.0
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


def get_market_times_for_today():
    now = datetime.now(NY_TZ)
    market_open = datetime.combine(now.date(), MARKET_OPEN, tzinfo=NY_TZ)
    market_close = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=NY_TZ)
    return market_open, market_close


def get_intraday_bars(client, symbols):
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
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
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df.set_index("timestamp", inplace=True)
            data[symbol] = df
    return data


def only_today_regular_session(df):
    market_open, market_close = get_market_times_for_today()
    data = df.copy()
    ny_time = data.index.tz_convert(NY_TZ)
    mask = (ny_time >= market_open) & (ny_time <= market_close)
    return data.loc[mask]


def rsi(series, window=14):
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(window).mean()
    avg_loss = losses.rolling(window).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def analyze_symbol(symbol, df):
    session = only_today_regular_session(df)
    if len(session) < MEAN_WINDOW_MINUTES + RSI_WINDOW:
        return None

    session = session.copy()
    session["rsi14"] = rsi(session["close"], RSI_WINDOW)
    recent = session.tail(MEAN_WINDOW_MINUTES)
    latest = session.iloc[-1]

    price = float(latest["close"])
    mean_2h = float(recent["close"].mean())
    rsi14 = float(latest["rsi14"])
    if price <= 0 or mean_2h <= 0:
        return None

    distance_pct = ((price / mean_2h) - 1) * 100
    direction = ""
    target = 0.0

    if rsi14 > SHORT_RSI and distance_pct > MIN_DISTANCE_PCT:
        direction = "SHORT"
        target = price * (1 - TAKE_PROFIT_PCT / 100)
    elif rsi14 < LONG_RSI and distance_pct < -MIN_DISTANCE_PCT:
        direction = "LONG"
        target = price * (1 + TAKE_PROFIT_PCT / 100)
    else:
        return None

    score = abs(distance_pct) * 0.65 + abs(rsi14 - 50) * 0.35

    return {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "mean_2h": mean_2h,
        "rsi14": rsi14,
        "distance_pct": distance_pct,
        "target": target,
        "score": score,
    }


def find_reversion_candidates():
    symbols = load_tickers(TICKERS_FILE)
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    data = get_intraday_bars(client, symbols)

    candidates = []
    with_data_count = 0
    accepted_count = 0

    for symbol in symbols:
        df = data.get(symbol)
        if df is None or df.empty:
            log_symbol_decision(STRATEGY_NAME, symbol, "SIN DATOS", "No hay velas intradia")
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
                "No cumple RSI extremo y distancia > 5% frente a media 2h",
            )

    selected = sorted(candidates, key=lambda item: item["score"], reverse=True)[:TOP_N]
    log_strategy_summary(STRATEGY_NAME, len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def format_candidate(candidate):
    return (
        f"{candidate['symbol']} | "
        f"Direccion: {candidate['direction']} | "
        f"Precio actual: {candidate['price']:.2f} | "
        f"Apertura: {candidate['price']:.2f} | "
        f"Cierre: {candidate['target']:.2f} | "
        f"Stop Loss: NO | "
        f"Cierre perdidas: NO | "
        f"Cierre grupo: beneficio conjunto 5% | "
        f"Media 2h: {candidate['mean_2h']:.2f} | "
        f"Dist media 2h: {candidate['distance_pct']:.2f}% | "
        f"RSI14: {candidate['rsi14']:.2f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_reversion_candidates()
    output_path, output_count = write_results_to_txt(STRATEGY_NAME, results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Reversion RSI 5 con los filtros actuales.")
    else:
        print("Candidatos Reversion RSI 5:")
        for candidate in results:
            print(format_candidate(candidate))
