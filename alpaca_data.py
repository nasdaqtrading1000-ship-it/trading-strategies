import os
from datetime import UTC, datetime, timedelta


def alpaca_credentials_available():
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def get_daily_asset_metrics(symbols, lookback_days=90, batch_size=100):
    if not alpaca_credentials_available():
        return {}, "csv"

    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError:
        return {}, "csv"

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_SECRET_KEY"],
    )

    metrics = {}
    for batch in _chunks(symbols, batch_size):
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            start=datetime.now(UTC) - timedelta(days=lookback_days),
            end=datetime.now(UTC),
            feed=DataFeed.IEX,
        )

        try:
            bars = client.get_stock_bars(request)
        except Exception as exc:
            print(f"Alpaca batch omitido ({len(batch)} simbolos): {exc}")
            continue

        for symbol, symbol_bars in bars.data.items():
            metric = _bars_to_metrics(symbol_bars)
            if metric:
                metrics[symbol] = metric

    return metrics, "alpaca" if metrics else "csv"


def _bars_to_metrics(symbol_bars):
    if not symbol_bars:
        return None

    sorted_bars = sorted(symbol_bars, key=lambda bar: bar.timestamp)
    latest = sorted_bars[-1]
    money_volumes = [float(bar.close) * float(bar.volume) for bar in sorted_bars]
    day_window = money_volumes[-5:] or money_volumes
    week_window = money_volumes[-25:] or money_volumes

    avg_money_volume = sum(money_volumes) / len(money_volumes)
    latest_money_volume = float(latest.close) * float(latest.volume)
    day_score = _score(latest_money_volume, sum(day_window) / len(day_window))
    week_score = _score(latest_money_volume, sum(week_window) / len(week_window))

    return {
        "price": float(latest.close),
        "money_volume": avg_money_volume,
        "day_money_volume": sum(day_window) / len(day_window),
        "week_money_volume": sum(week_window) / len(week_window),
        "day_volume_score": day_score,
        "week_volume_score": week_score,
    }


def _score(current, average):
    if average <= 0:
        return 1.0
    ratio = current / average
    return round(max(1.0, min(5.0, ratio * 3)), 1)


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]
