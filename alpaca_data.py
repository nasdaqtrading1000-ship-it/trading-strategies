import os
from datetime import UTC, datetime, timedelta


def alpaca_credentials_available():
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def get_daily_asset_metrics(symbols, lookback_days=90):
    if not alpaca_credentials_available():
        return {}, "csv"

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError:
        return {}, "csv"

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_SECRET_KEY"],
    )
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(UTC) - timedelta(days=lookback_days),
        end=datetime.now(UTC),
    )

    try:
        bars = client.get_stock_bars(request)
    except Exception:
        return {}, "csv"

    metrics = {}
    for symbol, symbol_bars in bars.data.items():
        if not symbol_bars:
            continue

        sorted_bars = sorted(symbol_bars, key=lambda bar: bar.timestamp)
        latest = sorted_bars[-1]
        money_volumes = [float(bar.close) * float(bar.volume) for bar in sorted_bars]
        day_window = money_volumes[-5:] or money_volumes
        week_window = money_volumes[-25:] or money_volumes

        avg_money_volume = sum(money_volumes) / len(money_volumes)
        latest_money_volume = float(latest.close) * float(latest.volume)
        day_score = _score(latest_money_volume, sum(day_window) / len(day_window))
        week_score = _score(latest_money_volume, sum(week_window) / len(week_window))

        metrics[symbol] = {
            "price": float(latest.close),
            "money_volume": avg_money_volume,
            "day_volume_score": day_score,
            "week_volume_score": week_score,
        }
    return metrics, "alpaca"


def _score(current, average):
    if average <= 0:
        return 1.0
    ratio = current / average
    return round(max(1.0, min(5.0, ratio * 3)), 1)
