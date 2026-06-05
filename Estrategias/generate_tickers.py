"""
Genera tickers.txt con activos filtrados buenos desde Alpaca.

Objetivo:
- Descargar el universo de activos de Alpaca.
- Quedarse solo con acciones US activas y tradables.
- Excluir simbolos raros, warrants, units, preferreds, etc.
- Descargar datos diarios recientes.
- Filtrar por precio y volumen monetario.
- Guardar tickers.txt para que lo lean las estrategias.

Este script NO compra ni vende.
Solo crea un archivo de tickers.
"""

import argparse
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import os
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from env_loader import load_env


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "tickers.txt"

DEFAULT_MARKETS = {"NASDAQ", "NYSE", "AMEX"}

# Simbolos simples: AAPL, MSFT, BRK.B.
# Excluye cosas raras con /, -, espacios, etc.
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# Sufijos habituales que suelen indicar warrants, units, rights, preferreds, etc.
BAD_SYMBOL_SUFFIXES = (
    "W",
    "WS",
    "WT",
    "U",
    "UN",
    "R",
    "RT",
    "P",
    "PR",
)

BAD_NAME_KEYWORDS = (
    "warrant",
    "unit",
    "right",
    "preferred",
    "preference",
    "depositary",
    "notes",
    "note due",
)


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable {name}")
    return value


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=2000.0)
    parser.add_argument("--min-dollar-volume", type=float, default=10_000_000.0)
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--volume-window", type=int, default=20)
    parser.add_argument("--max-assets", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--feed", default=os.environ.get("ALPACA_DATA_FEED", "iex"))
    parser.add_argument("--include-etfs", action="store_true")
    parser.add_argument("--markets", default="NASDAQ,NYSE,AMEX")
    return parser.parse_args()


def get_clients():
    load_env()
    api_key = require_env("ALPACA_API_KEY")
    secret_key = require_env("ALPACA_SECRET_KEY")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    return (
        TradingClient(api_key=api_key, secret_key=secret_key, paper=paper),
        StockHistoricalDataClient(api_key=api_key, secret_key=secret_key),
    )


def load_alpaca_assets(trading_client, markets, include_etfs):
    """
    Descarga activos y aplica filtros de universo.
    """
    request = GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=AssetStatus.ACTIVE,
    )
    assets = trading_client.get_all_assets(request)

    output = []
    for asset in assets:
        exchange = (asset.exchange or "").upper()
        symbol = asset.symbol.upper()
        name = (asset.name or "").lower()

        if exchange not in markets:
            continue
        if not asset.tradable:
            continue
        if not SYMBOL_PATTERN.match(symbol):
            continue
        if has_bad_symbol_suffix(symbol):
            continue
        if any(keyword in name for keyword in BAD_NAME_KEYWORDS):
            continue
        if not include_etfs and " etf" in f" {name}":
            continue

        output.append(
            {
                "symbol": symbol,
                "name": asset.name or symbol,
                "exchange": exchange,
            }
        )

    return sorted(output, key=lambda item: item["symbol"])


def has_bad_symbol_suffix(symbol):
    """
    Filtro conservador para evitar instrumentos no comunes.
    """
    compact = symbol.replace(".", "")
    if len(compact) <= 1:
        return False
    return any(compact.endswith(suffix) for suffix in BAD_SYMBOL_SUFFIXES)


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def get_market_metrics(data_client, symbols, args):
    """
    Descarga barras diarias y calcula precio y volumen monetario medio.
    """
    feed = DataFeed(args.feed)
    metrics = {}

    for batch in chunks(symbols, args.batch_size):
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            start=datetime.now(UTC) - timedelta(days=args.lookback_days),
            end=datetime.now(UTC),
            feed=feed,
        )

        try:
            bars = data_client.get_stock_bars(request).data
        except Exception as exc:
            print(f"Lote omitido ({len(batch)} activos): {exc}")
            time.sleep(1)
            continue

        for symbol, symbol_bars in bars.items():
            metric = bars_to_metric(symbol_bars, args.volume_window)
            if metric:
                metrics[symbol] = metric

        time.sleep(0.15)

    return metrics


def bars_to_metric(symbol_bars, volume_window):
    if len(symbol_bars) < max(5, volume_window):
        return None

    sorted_bars = sorted(symbol_bars, key=lambda bar: bar.timestamp)
    latest = sorted_bars[-1]
    recent = sorted_bars[-volume_window:]

    price = float(latest.close)
    avg_dollar_volume = sum(
        float(bar.close) * float(bar.volume)
        for bar in recent
    ) / len(recent)

    return {
        "price": price,
        "avg_dollar_volume": avg_dollar_volume,
    }


def filter_assets(assets, metrics, args):
    """
    Aplica filtros de precio y volumen.
    """
    filtered = []

    for asset in assets:
        metric = metrics.get(asset["symbol"])
        if not metric:
            continue

        price = metric["price"]
        avg_dollar_volume = metric["avg_dollar_volume"]

        if price < args.min_price or price > args.max_price:
            continue
        if avg_dollar_volume < args.min_dollar_volume:
            continue

        filtered.append(
            {
                **asset,
                **metric,
            }
        )

    return sorted(
        filtered,
        key=lambda item: item["avg_dollar_volume"],
        reverse=True,
    )


def write_tickers(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(row["symbol"] for row in rows) + "\n",
        encoding="utf-8",
    )


def main():
    args = parse_args()
    markets = {
        market.strip().upper()
        for market in args.markets.split(",")
        if market.strip()
    } or DEFAULT_MARKETS

    trading_client, data_client = get_clients()

    print("Descargando universo de activos...")
    assets = load_alpaca_assets(
        trading_client=trading_client,
        markets=markets,
        include_etfs=args.include_etfs,
    )

    if args.max_assets > 0:
        assets = assets[: args.max_assets]

    symbols = [asset["symbol"] for asset in assets]
    print(f"Activos candidatos antes de mercado: {len(symbols)}")

    print("Descargando precio y volumen...")
    metrics = get_market_metrics(data_client, symbols, args)
    print(f"Activos con datos de mercado: {len(metrics)}")

    filtered = filter_assets(assets, metrics, args)
    write_tickers(args.output, filtered)

    print(f"tickers.txt generado: {args.output}")
    print(f"Activos finales: {len(filtered)}")
    print("Top 20 por volumen monetario:")
    for row in filtered[:20]:
        print(
            f"{row['symbol']:6} "
            f"{row['exchange']:6} "
            f"${row['price']:8.2f} "
            f"Vol$ {row['avg_dollar_volume'] / 1_000_000:10.1f}M "
            f"{row['name']}"
        )


if __name__ == "__main__":
    main()
