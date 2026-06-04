import os
from datetime import UTC, datetime, timedelta


def alpaca_credentials_available():
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def get_daily_asset_metrics(symbols, lookback_days=90, batch_size=100):
    diagnostics = {
        "requested_symbols": len(symbols),
        "batch_size": batch_size,
        "batches": 0,
        "failed_batches": 0,
        "last_error": "",
    }
    if not alpaca_credentials_available():
        diagnostics["last_error"] = "Faltan ALPACA_API_KEY o ALPACA_SECRET_KEY."
        return {}, "csv", diagnostics

    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError:
        diagnostics["last_error"] = "No esta instalado alpaca-py."
        return {}, "csv", diagnostics

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_SECRET_KEY"],
    )

    metrics = {}
    for batch in _chunks(symbols, batch_size):
        diagnostics["batches"] += 1
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
            diagnostics["failed_batches"] += 1
            diagnostics["last_error"] = str(exc)
            print(f"Alpaca batch omitido ({len(batch)} simbolos): {exc}")
            continue

        for symbol, symbol_bars in bars.data.items():
            metric = _bars_to_metrics(symbol_bars)
            if metric:
                metrics[symbol] = metric

    diagnostics["metrics"] = len(metrics)
    return metrics, "alpaca" if metrics else "csv", diagnostics


def _bars_to_metrics(symbol_bars):
    if not symbol_bars:
        return None

    sorted_bars = sorted(symbol_bars, key=lambda bar: bar.timestamp)
    latest = sorted_bars[-1]
    money_volumes = [float(bar.close) * float(bar.volume) for bar in sorted_bars]
    day_1 = _window_average(money_volumes, 1)
    day_2 = _window_average(money_volumes, 2)
    day_3 = _window_average(money_volumes, 3)
    day_4 = _window_average(money_volumes, 4)
    day_5 = _window_average(money_volumes, 5)
    week_1 = _window_average(money_volumes, 5)
    week_2 = _window_average(money_volumes, 10)
    week_3 = _window_average(money_volumes, 15)
    week_4 = _window_average(money_volumes, 20)
    week_5 = _window_average(money_volumes, 25)
    month_1 = _window_average(money_volumes, 21)
    month_2 = _window_average(money_volumes, 42)
    month_3 = _window_average(money_volumes, 63)

    avg_money_volume = sum(money_volumes) / len(money_volumes)
    latest_money_volume = float(latest.close) * float(latest.volume)
    day_score = _score(latest_money_volume, day_5)
    week_score = _score(latest_money_volume, week_3)

    return {
        "price": float(latest.close),
        "money_volume": avg_money_volume,
        "money_volume_1m": month_1,
        "money_volume_2m": month_2,
        "money_volume_3m": month_3,
        "day_money_volume_1d": day_1,
        "day_money_volume_2d": day_2,
        "day_money_volume_3d": day_3,
        "day_money_volume_4d": day_4,
        "day_money_volume_5d": day_5,
        "week_money_volume_1w": week_1,
        "week_money_volume_2w": week_2,
        "week_money_volume_3w": week_3,
        "week_money_volume_4w": week_4,
        "week_money_volume_5w": week_5,
        "day_money_volume": day_5,
        "week_money_volume": week_5,
        "day_volume_score": day_score,
        "week_volume_score": week_score,
    }


def _window_average(values, size):
    window = values[-size:] or values
    return sum(window) / len(window)


def _score(current, average):
    if average <= 0:
        return 1.0
    ratio = current / average
    return round(max(1.0, min(5.0, ratio * 3)), 1)


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]
