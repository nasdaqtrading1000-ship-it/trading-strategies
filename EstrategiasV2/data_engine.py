from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from indicators import (
    atr,
    average_dollar_volume,
    bollinger_bands,
    distance_pct,
    dollar_volume,
    ema,
    last_float,
    macd,
    pct_change,
    rolling_high,
    rolling_low,
    rsi,
    safe_ratio,
    sma,
    slope_pct,
    vwap,
)
from models import TickerData


NY_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def load_tickers(path: Path, benchmark: str = "QQQ", max_tickers: int | None = None) -> list[str]:
    tickers = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip().upper()
        if not line or line.startswith("#"):
            continue
        symbol = line.split()[0].strip().upper()
        if symbol and symbol not in tickers:
            tickers.append(symbol)
    if benchmark and benchmark.upper() not in tickers:
        tickers.append(benchmark.upper())
    if max_tickers:
        limited = tickers[:max_tickers]
        if benchmark and benchmark.upper() not in limited:
            limited.append(benchmark.upper())
        return limited
    return tickers


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def resolve_adjustment(Adjustment, config: dict):
    adjustment_name = str(config.get("data_adjustment", "ALL")).upper()
    return getattr(Adjustment, adjustment_name, Adjustment.ALL)


def fetch_daily_data(symbols: list[str], config: dict) -> dict[str, pd.DataFrame]:
    try:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as error:
        raise RuntimeError("Falta alpaca-py para descargar datos.") from error

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Faltan ALPACA_API_KEY y ALPACA_SECRET_KEY.")

    client = StockHistoricalDataClient(api_key, secret_key)
    lookback_days = int(config.get("lookback_days", 240))
    batch_size = int(config.get("batch_size", 120))
    feed_name = str(config.get("data_feed", "IEX")).upper()
    feed = DataFeed.IEX if feed_name == "IEX" else DataFeed.SIP
    adjustment = resolve_adjustment(Adjustment, config)

    result: dict[str, pd.DataFrame] = {}
    for batch_number, batch in enumerate(chunked(symbols, batch_size), start=1):
        print(f"Datos diarios | tanda {batch_number} | {len(batch)} activos | ajuste={adjustment}")
        try:
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                adjustment=adjustment,
                start=datetime.now(UTC) - timedelta(days=lookback_days),
                end=datetime.now(UTC),
                feed=feed,
            )
            bars = client.get_stock_bars(request).data
            result.update(bars_to_dataframes(bars))
        except Exception as error:
            print(f"Datos diarios | tanda {batch_number} | ERROR lote: {error}")
            result.update(fetch_daily_data_individual(client, batch, lookback_days, feed, adjustment, StockBarsRequest, TimeFrame))
    return result


def fetch_intraday_data(symbols: list[str], config: dict) -> dict[str, pd.DataFrame]:
    try:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as error:
        raise RuntimeError("Falta alpaca-py para descargar datos intradia.") from error

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Faltan ALPACA_API_KEY y ALPACA_SECRET_KEY.")

    client = StockHistoricalDataClient(api_key, secret_key)
    lookback_days = int(config.get("intraday_lookback_days", 5))
    batch_size = int(config.get("batch_size", 120))
    feed_name = str(config.get("data_feed", "IEX")).upper()
    feed = DataFeed.IEX if feed_name == "IEX" else DataFeed.SIP
    adjustment = resolve_adjustment(Adjustment, config)

    result: dict[str, pd.DataFrame] = {}
    for batch_number, batch in enumerate(chunked(symbols, batch_size), start=1):
        print(f"Datos intradia | tanda {batch_number} | {len(batch)} activos | ajuste={adjustment}")
        try:
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Minute,
                adjustment=adjustment,
                start=datetime.now(UTC) - timedelta(days=lookback_days),
                end=datetime.now(UTC),
                feed=feed,
            )
            bars = client.get_stock_bars(request).data
            result.update(bars_to_dataframes(bars))
        except Exception as error:
            print(f"Datos intradia | tanda {batch_number} | ERROR lote: {error}")
            result.update(fetch_intraday_data_individual(client, batch, lookback_days, feed, adjustment, StockBarsRequest, TimeFrame))
    return result


def fetch_daily_data_individual(client, symbols, lookback_days, feed, adjustment, StockBarsRequest, TimeFrame):
    result = {}
    for symbol in symbols:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Day,
                adjustment=adjustment,
                start=datetime.now(UTC) - timedelta(days=lookback_days),
                end=datetime.now(UTC),
                feed=feed,
            )
            result.update(bars_to_dataframes(client.get_stock_bars(request).data))
        except Exception as error:
            print(f"Datos diarios | {symbol} | OMITIDO: {error}")
    return result


def fetch_intraday_data_individual(client, symbols, lookback_days, feed, adjustment, StockBarsRequest, TimeFrame):
    result = {}
    for symbol in symbols:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                adjustment=adjustment,
                start=datetime.now(UTC) - timedelta(days=lookback_days),
                end=datetime.now(UTC),
                feed=feed,
            )
            result.update(bars_to_dataframes(client.get_stock_bars(request).data))
        except Exception as error:
            print(f"Datos intradia | {symbol} | OMITIDO: {error}")
    return result


def bars_to_dataframes(bars: dict) -> dict[str, pd.DataFrame]:
    result = {}
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
        if not rows:
            continue
        df = pd.DataFrame(rows).sort_values("timestamp")
        df.set_index("timestamp", inplace=True)
        result[str(symbol).upper()] = df
    return result


def build_ticker_dataset(
    daily_data: dict[str, pd.DataFrame],
    intraday_data: dict[str, pd.DataFrame],
    config: dict,
) -> dict[str, TickerData]:
    benchmark = str(config.get("benchmark", "QQQ")).upper()
    benchmark_df = daily_data.get(benchmark)
    benchmark_returns = {
        20: pct_change(benchmark_df["close"], 20) if benchmark_df is not None else None,
        60: pct_change(benchmark_df["close"], 60) if benchmark_df is not None else None,
        120: pct_change(benchmark_df["close"], 120) if benchmark_df is not None else None,
    }
    dataset = {}
    for symbol, daily in daily_data.items():
        metrics = calculate_common_metrics(symbol, daily, intraday_data.get(symbol), benchmark_returns)
        dataset[symbol] = TickerData(
            symbol=symbol,
            daily=daily,
            intraday=intraday_data.get(symbol),
            metrics=metrics,
        )
    apply_dataset_ranks(dataset)
    return dataset


def calculate_common_metrics(
    symbol: str,
    daily: pd.DataFrame,
    intraday: pd.DataFrame | None,
    benchmark_returns: dict[int, float | None],
) -> dict:
    df = daily.copy()
    df["daily_sma20"] = sma(df["close"], 20)
    df["daily_sma50"] = sma(df["close"], 50)
    df["daily_sma100"] = sma(df["close"], 100)
    df["daily_sma120"] = sma(df["close"], 120)
    df["daily_sma180"] = sma(df["close"], 180)
    df["daily_sma200"] = sma(df["close"], 200)
    df["daily_rsi14"] = rsi(df["close"], 14)
    df["daily_atr14"] = atr(df, 14)
    df["daily_macd"], df["daily_macd_signal"], df["daily_macd_hist"] = macd(df["close"])
    df["daily_bollinger_lower20"], df["daily_bollinger_mid20"], df["daily_bollinger_upper20"] = bollinger_bands(df["close"], 20, 2.0)

    price = last_float(df["close"])
    previous_close = float(df["close"].iloc[-2]) if len(df) >= 2 else None
    previous_low = float(df["low"].iloc[-2]) if len(df) >= 2 else None
    latest_open = last_float(df["open"])
    momentum_20d = pct_change(df["close"], 20)
    momentum_50d = pct_change(df["close"], 50)
    momentum_60d = pct_change(df["close"], 60)
    momentum_120d = pct_change(df["close"], 120)
    daily_return_1d = pct_change(df["close"], 1)
    daily_return_5d = pct_change(df["close"], 5)
    daily_return_10d = pct_change(df["close"], 10)
    avg_dollar_volume_20d = average_dollar_volume(df, 20)
    avg_dollar_volume_5d = average_dollar_volume(df, 5)
    avg_dollar_volume_10d = average_dollar_volume(df, 10)
    avg_dollar_volume_21d = average_dollar_volume(df, 21)
    avg_dollar_volume_42d = average_dollar_volume(df, 42)
    avg_dollar_volume_63d = average_dollar_volume(df, 63)
    avg_dollar_volume_120d = average_dollar_volume(df, 120)
    dollar_volume_series = df["close"] * df["volume"]
    prev_avg_dollar_volume_21d = previous_average(dollar_volume_series, 21)
    prev_avg_dollar_volume_42d = previous_average(dollar_volume_series, 42)
    prev_avg_dollar_volume_63d = previous_average(dollar_volume_series, 63)
    avg_volume_20d = last_float(df["volume"].rolling(20).mean())
    avg_volume_50d = last_float(df["volume"].rolling(50).mean())
    current_dollar_volume = dollar_volume(df)
    resistance_20d = rolling_high(df, 20, exclude_current=True)
    resistance_50d = rolling_high(df, 50, exclude_current=True)
    resistance_55d = rolling_high(df, 55, exclude_current=True)
    high_20d_including_current = float(df["high"].tail(20).max()) if len(df) >= 20 else None
    recent_high_10d = rolling_high(df, 10, exclude_current=True)
    recent_low_5d = float(df["low"].tail(5).min()) if len(df) >= 5 else None
    support_20d = rolling_low(df, 20, exclude_current=True)
    support_50d = rolling_low(df, 50, exclude_current=True)
    relative_strength_20d = (
        momentum_20d - benchmark_returns.get(20)
        if momentum_20d is not None and benchmark_returns.get(20) is not None
        else None
    )
    relative_strength_60d = (
        momentum_60d - benchmark_returns.get(60)
        if momentum_60d is not None and benchmark_returns.get(60) is not None
        else None
    )
    relative_strength_120d = (
        momentum_120d - benchmark_returns.get(120)
        if momentum_120d is not None and benchmark_returns.get(120) is not None
        else None
    )
    weekly_metrics = calculate_weekly_metrics(df)
    intraday_metrics = calculate_intraday_metrics(intraday)

    metrics = {
        "symbol": symbol,
        "price": price,
        "open": latest_open,
        "high": last_float(df["high"]),
        "low": last_float(df["low"]),
        "previous_close": previous_close,
        "previous_low": previous_low,
        "daily_gap_pct": distance_pct(latest_open, previous_close),
        "daily_change_from_open_pct": distance_pct(price, latest_open),
        "daily_return_1d_pct": daily_return_1d,
        "daily_return_5d_pct": daily_return_5d,
        "daily_return_10d_pct": daily_return_10d,
        "volume": last_float(df["volume"]),
        "avg_volume_20d": avg_volume_20d,
        "avg_volume_50d": avg_volume_50d,
        "volume_ratio_vs_20d": safe_ratio(last_float(df["volume"]), avg_volume_20d),
        "volume_ratio_vs_50d": safe_ratio(last_float(df["volume"]), avg_volume_50d),
        "current_dollar_volume": current_dollar_volume,
        "avg_dollar_volume_5d": avg_dollar_volume_5d,
        "avg_dollar_volume_10d": avg_dollar_volume_10d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "avg_dollar_volume_21d": avg_dollar_volume_21d,
        "avg_dollar_volume_42d": avg_dollar_volume_42d,
        "avg_dollar_volume_63d": avg_dollar_volume_63d,
        "avg_dollar_volume_120d": avg_dollar_volume_120d,
        "prev_avg_dollar_volume_21d": prev_avg_dollar_volume_21d,
        "prev_avg_dollar_volume_42d": prev_avg_dollar_volume_42d,
        "prev_avg_dollar_volume_63d": prev_avg_dollar_volume_63d,
        "dollar_volume_ratio_vs_5d": safe_ratio(current_dollar_volume, avg_dollar_volume_5d),
        "dollar_volume_ratio_vs_20d": safe_ratio(current_dollar_volume, avg_dollar_volume_20d),
        "dollar_volume_ratio_vs_21d": safe_ratio(current_dollar_volume, avg_dollar_volume_21d),
        "dollar_volume_ratio_vs_42d": safe_ratio(current_dollar_volume, avg_dollar_volume_42d),
        "dollar_volume_ratio_vs_63d": safe_ratio(current_dollar_volume, avg_dollar_volume_63d),
        "dollar_volume_ma5_vs_ma120": safe_ratio(avg_dollar_volume_5d, avg_dollar_volume_120d),
        "dollar_volume_ratio_vs_prev_21d": safe_ratio(current_dollar_volume, prev_avg_dollar_volume_21d),
        "dollar_volume_ratio_vs_prev_42d": safe_ratio(current_dollar_volume, prev_avg_dollar_volume_42d),
        "dollar_volume_ratio_vs_prev_63d": safe_ratio(current_dollar_volume, prev_avg_dollar_volume_63d),
        "daily_rsi14": last_float(df["daily_rsi14"]),
        "daily_atr14": last_float(df["daily_atr14"]),
        "daily_sma20": last_float(df["daily_sma20"]),
        "daily_sma50": last_float(df["daily_sma50"]),
        "daily_sma100": last_float(df["daily_sma100"]),
        "daily_sma120": last_float(df["daily_sma120"]),
        "daily_sma180": last_float(df["daily_sma180"]),
        "daily_sma200": last_float(df["daily_sma200"]),
        "daily_sma50_slope_20d_pct": slope_pct(df["daily_sma50"], 20),
        "daily_sma200_slope_20d_pct": slope_pct(df["daily_sma200"], 20),
        "distance_daily_sma20_pct": distance_pct(price, last_float(df["daily_sma20"])),
        "distance_daily_sma50_pct": distance_pct(price, last_float(df["daily_sma50"])),
        "distance_daily_sma100_pct": distance_pct(price, last_float(df["daily_sma100"])),
        "distance_daily_sma120_pct": distance_pct(price, last_float(df["daily_sma120"])),
        "distance_daily_sma180_pct": distance_pct(price, last_float(df["daily_sma180"])),
        "distance_daily_sma200_pct": distance_pct(price, last_float(df["daily_sma200"])),
        "daily_macd": last_float(df["daily_macd"]),
        "daily_macd_signal": last_float(df["daily_macd_signal"]),
        "daily_macd_hist": last_float(df["daily_macd_hist"]),
        "daily_bollinger_lower20": last_float(df["daily_bollinger_lower20"]),
        "daily_bollinger_mid20": last_float(df["daily_bollinger_mid20"]),
        "daily_bollinger_upper20": last_float(df["daily_bollinger_upper20"]),
        "distance_daily_bollinger_lower20_pct": distance_pct(price, last_float(df["daily_bollinger_lower20"])),
        "distance_daily_bollinger_upper20_pct": distance_pct(price, last_float(df["daily_bollinger_upper20"])),
        "momentum_20d_pct": momentum_20d,
        "momentum_50d_pct": momentum_50d,
        "momentum_60d_pct": momentum_60d,
        "momentum_120d_pct": momentum_120d,
        "benchmark_return_20d_pct": benchmark_returns.get(20),
        "benchmark_return_60d_pct": benchmark_returns.get(60),
        "benchmark_return_120d_pct": benchmark_returns.get(120),
        "relative_strength_20d_pct": relative_strength_20d,
        "relative_strength_60d_pct": relative_strength_60d,
        "relative_strength_120d_pct": relative_strength_120d,
        "resistance_20d": resistance_20d,
        "resistance_50d": resistance_50d,
        "resistance_55d": resistance_55d,
        "high_20d_including_current": high_20d_including_current,
        "recent_high_10d": recent_high_10d,
        "recent_low_5d": recent_low_5d,
        "support_20d": support_20d,
        "support_50d": support_50d,
        "breakout_20d_pct": distance_pct(price, resistance_20d),
        "daily_bars_loaded": len(df),
        "intraday_1m_bars_loaded": len(intraday) if intraday is not None else 0,
    }
    metrics.update(weekly_metrics)
    metrics.update(intraday_metrics)
    metrics.update(fundamental_placeholders())
    return metrics


def apply_dataset_ranks(dataset: dict[str, TickerData]) -> None:
    ranking_fields = [
        "current_dollar_volume",
        "avg_dollar_volume_20d",
        "avg_dollar_volume_21d",
        "avg_dollar_volume_42d",
        "avg_dollar_volume_63d",
        "avg_dollar_volume_120d",
        "dollar_volume_ma5_vs_ma120",
        "dollar_volume_ratio_vs_20d",
        "dollar_volume_ratio_vs_21d",
        "dollar_volume_ratio_vs_42d",
        "dollar_volume_ratio_vs_63d",
        "dollar_volume_ratio_vs_prev_21d",
        "dollar_volume_ratio_vs_prev_42d",
        "dollar_volume_ratio_vs_prev_63d",
        "relative_strength_20d_pct",
        "relative_strength_60d_pct",
        "relative_strength_120d_pct",
    ]
    for field in ranking_fields:
        values = [
            (symbol, ticker.metrics.get(field))
            for symbol, ticker in dataset.items()
            if ticker.metrics.get(field) is not None
        ]
        values.sort(key=lambda item: item[1], reverse=True)
        total = len(values)
        for rank, (symbol, _value) in enumerate(values, start=1):
            dataset[symbol].metrics[f"{field}_rank"] = rank
            dataset[symbol].metrics[f"{field}_percentile"] = round((1 - ((rank - 1) / total)) * 100, 4) if total else None


def previous_average(series: pd.Series, window: int) -> float | None:
    if len(series) < window + 1:
        return None
    previous = series.iloc[-window - 1:-1]
    if previous.empty:
        return None
    value = float(previous.mean())
    return value if value > 0 else None


def calculate_weekly_metrics(df: pd.DataFrame) -> dict:
    weekly = (
        df[["open", "high", "low", "close", "volume"]]
        .resample("W-FRI")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    if weekly.empty:
        return {
            "weekly_sma20": None,
            "weekly_sma50": None,
            "weekly_sma120": None,
            "weekly_rsi14": None,
            "distance_weekly_sma120_pct": None,
            "weekly_bars_loaded": 0,
        }
    weekly["weekly_sma20"] = sma(weekly["close"], 20)
    weekly["weekly_sma50"] = sma(weekly["close"], 50)
    weekly["weekly_sma120"] = sma(weekly["close"], 120)
    weekly["weekly_rsi14"] = rsi(weekly["close"], 14)
    price = last_float(df["close"])
    weekly_sma120 = last_float(weekly["weekly_sma120"])
    return {
        "weekly_sma20": last_float(weekly["weekly_sma20"]),
        "weekly_sma50": last_float(weekly["weekly_sma50"]),
        "weekly_sma120": weekly_sma120,
        "weekly_rsi14": last_float(weekly["weekly_rsi14"]),
        "distance_weekly_sma120_pct": distance_pct(price, weekly_sma120),
        "weekly_bars_loaded": len(weekly),
    }


def calculate_intraday_metrics(intraday: pd.DataFrame | None) -> dict:
    empty = {
        "intraday_1m_data_date": None,
        "intraday_1m_last_timestamp": None,
        "intraday_1m_rsi5": None,
        "intraday_1m_open": None,
        "intraday_1m_high": None,
        "intraday_1m_low": None,
        "intraday_1m_previous_close": None,
        "intraday_1m_vwap": None,
        "intraday_1m_rsi14": None,
        "intraday_1m_ema9": None,
        "intraday_1m_ema21": None,
        "intraday_1m_sma20": None,
        "intraday_1m_sma120": None,
        "intraday_1m_distance_vwap_pct": None,
        "intraday_1m_distance_sma120_pct": None,
        "intraday_1m_momentum_15m_pct": None,
        "intraday_1m_momentum_30m_pct": None,
        "intraday_1m_recent_high_20m": None,
        "intraday_1m_recent_low_20m": None,
        "intraday_1m_recent_high_5m": None,
        "intraday_1m_recent_low_5m": None,
        "intraday_1m_breakout_high_20m_pct": None,
        "intraday_1m_breakdown_low_20m_pct": None,
        "intraday_1m_volume_ratio_20m": None,
        "intraday_day_dollar_volume": None,
        "opening_range_15m_high": None,
        "opening_range_15m_low": None,
        "opening_range_15m_dollar_volume": None,
        "opening_range_15m_breakout_pct": None,
        "opening_range_15m_breakdown_pct": None,
    }
    if intraday is None or intraday.empty:
        return empty

    data = intraday.copy()
    if data.index.tz is None:
        data.index = pd.to_datetime(data.index, utc=True)
    data = data.sort_index()
    ny_index = data.index.tz_convert(NY_TZ)
    latest_session = ny_index[-1].date()
    market_open = datetime.combine(latest_session, MARKET_OPEN, tzinfo=NY_TZ)
    market_close = datetime.combine(latest_session, MARKET_CLOSE, tzinfo=NY_TZ)
    session_data = data[(ny_index >= market_open) & (ny_index <= market_close)].copy()
    if session_data.empty:
        return empty

    session_data["intraday_1m_rsi14"] = rsi(session_data["close"], 14)
    session_data["intraday_1m_rsi5"] = rsi(session_data["close"], 5)
    session_data["intraday_1m_ema9"] = ema(session_data["close"], 9)
    session_data["intraday_1m_ema21"] = ema(session_data["close"], 21)
    session_data["intraday_1m_sma20"] = sma(session_data["close"], 20)
    session_data["intraday_1m_sma120"] = sma(session_data["close"], 120)
    session_data["intraday_1m_avg_volume_20m"] = session_data["volume"].rolling(20).mean()
    session_data["intraday_1m_vwap"] = cumulative_vwap(session_data)

    price = last_float(session_data["close"])
    volume = last_float(session_data["volume"])
    avg_volume_20m = last_float(session_data["intraday_1m_avg_volume_20m"])
    recent_high_20m = rolling_high(session_data, 20, exclude_current=True)
    recent_low_20m = rolling_low(session_data, 20, exclude_current=True)
    recent_high_5m = rolling_high(session_data, 5, exclude_current=True)
    recent_low_5m = rolling_low(session_data, 5, exclude_current=True)
    vwap_value = last_float(session_data["intraday_1m_vwap"])
    sma120_value = last_float(session_data["intraday_1m_sma120"])

    opening_end = market_open + timedelta(minutes=15)
    opening_range = session_data[(session_data.index.tz_convert(NY_TZ) >= market_open) & (session_data.index.tz_convert(NY_TZ) < opening_end)]
    opening_high = float(opening_range["high"].max()) if not opening_range.empty else None
    opening_low = float(opening_range["low"].min()) if not opening_range.empty else None
    opening_dollar_volume = float((opening_range["close"] * opening_range["volume"]).sum()) if not opening_range.empty else None

    return {
        "intraday_1m_data_date": latest_session.isoformat(),
        "intraday_1m_last_timestamp": session_data.index[-1].tz_convert(NY_TZ).isoformat(),
        "intraday_1m_open": last_float(session_data["open"]),
        "intraday_1m_high": last_float(session_data["high"]),
        "intraday_1m_low": last_float(session_data["low"]),
        "intraday_1m_previous_close": float(session_data["close"].iloc[-2]) if len(session_data) >= 2 else None,
        "intraday_1m_rsi5": last_float(session_data["intraday_1m_rsi5"]),
        "intraday_1m_vwap": vwap_value,
        "intraday_1m_rsi14": last_float(session_data["intraday_1m_rsi14"]),
        "intraday_1m_ema9": last_float(session_data["intraday_1m_ema9"]),
        "intraday_1m_ema21": last_float(session_data["intraday_1m_ema21"]),
        "intraday_1m_sma20": last_float(session_data["intraday_1m_sma20"]),
        "intraday_1m_sma120": sma120_value,
        "intraday_1m_distance_vwap_pct": distance_pct(price, vwap_value),
        "intraday_1m_distance_sma120_pct": distance_pct(price, sma120_value),
        "intraday_1m_momentum_15m_pct": pct_change(session_data["close"], 15),
        "intraday_1m_momentum_30m_pct": pct_change(session_data["close"], 30),
        "intraday_1m_recent_high_20m": recent_high_20m,
        "intraday_1m_recent_low_20m": recent_low_20m,
        "intraday_1m_recent_high_5m": recent_high_5m,
        "intraday_1m_recent_low_5m": recent_low_5m,
        "intraday_1m_breakout_high_20m_pct": distance_pct(price, recent_high_20m),
        "intraday_1m_breakdown_low_20m_pct": distance_pct(recent_low_20m, price),
        "intraday_1m_volume_ratio_20m": safe_ratio(volume, avg_volume_20m),
        "intraday_day_dollar_volume": float((session_data["close"] * session_data["volume"]).sum()),
        "opening_range_15m_high": opening_high,
        "opening_range_15m_low": opening_low,
        "opening_range_15m_dollar_volume": opening_dollar_volume,
        "opening_range_15m_breakout_pct": distance_pct(price, opening_high),
        "opening_range_15m_breakdown_pct": distance_pct(opening_low, price),
    }


def cumulative_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_volume = df["volume"].cumsum()
    return (typical * df["volume"]).cumsum() / cumulative_volume.replace(0, pd.NA)


def fundamental_placeholders() -> dict:
    return {
        "fmp_company_name": None,
        "fmp_sector": None,
        "fmp_industry": None,
        "fmp_beta": None,
        "fmp_pe_ratio": None,
        "fmp_pb_ratio": None,
        "fmp_ps_ratio": None,
        "fmp_roe_pct": None,
        "fmp_roic_pct": None,
        "fmp_roa_pct": None,
        "fmp_debt_to_equity": None,
        "fmp_revenue_growth_pct": None,
        "fmp_eps_growth_pct": None,
        "fmp_gross_margin_pct": None,
        "fmp_operating_margin_pct": None,
        "fmp_net_margin_pct": None,
        "fmp_dividend_yield_pct": None,
        "fmp_payout_ratio_pct": None,
        "fmp_dividend_growth_3y_pct": None,
        "fmp_dividend_growth_3y_total_pct": None,
        "fmp_latest_annual_dividend": None,
        "fmp_error": None,
    }
