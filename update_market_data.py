import argparse
import csv
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from alpaca_data import get_daily_asset_metrics
from db import engine
from market_scanner import load_universe_assets


MARKET_DATA_CSV_PATH = Path(__file__).resolve().parent / "data" / "market_data.csv"
STRATEGY_TICKERS_PATH = Path(__file__).resolve().parent / "Estrategias" / "tickers.txt"


def ensure_snapshot_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS asset_snapshots (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sector TEXT NOT NULL,
                market TEXT NOT NULL,
                price FLOAT NOT NULL,
                money_volume FLOAT NOT NULL,
                money_volume_1m FLOAT NOT NULL DEFAULT 0,
                money_volume_2m FLOAT NOT NULL DEFAULT 0,
                money_volume_3m FLOAT NOT NULL DEFAULT 0,
                day_money_volume FLOAT NOT NULL DEFAULT 0,
                week_money_volume FLOAT NOT NULL DEFAULT 0,
                day_money_volume_1d FLOAT NOT NULL DEFAULT 0,
                day_money_volume_2d FLOAT NOT NULL DEFAULT 0,
                day_money_volume_3d FLOAT NOT NULL DEFAULT 0,
                day_money_volume_4d FLOAT NOT NULL DEFAULT 0,
                day_money_volume_5d FLOAT NOT NULL DEFAULT 0,
                week_money_volume_1w FLOAT NOT NULL DEFAULT 0,
                week_money_volume_2w FLOAT NOT NULL DEFAULT 0,
                week_money_volume_3w FLOAT NOT NULL DEFAULT 0,
                week_money_volume_4w FLOAT NOT NULL DEFAULT 0,
                week_money_volume_5w FLOAT NOT NULL DEFAULT 0,
                day_volume_score FLOAT NOT NULL,
                week_volume_score FLOAT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for column_name in SNAPSHOT_EXTRA_COLUMNS:
        add_column_if_missing(connection, column_name)


def add_column_if_missing(connection, column_name):
    if snapshot_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE asset_snapshots ADD COLUMN {column_name} FLOAT NOT NULL DEFAULT 0"
        )
    )


def snapshot_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'asset_snapshots'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(asset_snapshots)")).fetchall()
    return any(row[1] == column_name for row in rows)


SNAPSHOT_EXTRA_COLUMNS = [
    "money_volume_1m",
    "money_volume_2m",
    "money_volume_3m",
    "day_money_volume",
    "week_money_volume",
    "day_money_volume_1d",
    "day_money_volume_2d",
    "day_money_volume_3d",
    "day_money_volume_4d",
    "day_money_volume_5d",
    "week_money_volume_1w",
    "week_money_volume_2w",
    "week_money_volume_3w",
    "week_money_volume_4w",
    "week_money_volume_5w",
]


def metric_value(metric, key, fallback=0):
    return metric.get(key) or metric.get("money_volume") or fallback


def update_market_data(max_symbols=None, full=False):
    base_assets = load_universe_assets()
    strategy_assets = load_strategy_ticker_assets()
    assets = merge_assets(base_assets, strategy_assets)
    max_symbols = resolve_max_symbols(max_symbols)
    total_universe = len(assets)
    if not assets:
        return {
            "ok": False,
            "message": "No hay universo de activos cargado.",
            "loaded_assets": 0,
            "source": "database",
            "last_error": "Pulsa primero Actualizar CSV.",
        }

    with engine.begin() as connection:
        ensure_update_state_table(connection)
        batch_offset = get_batch_offset(connection, total_universe)

    full_update = full or max_symbols <= 0 or max_symbols >= total_universe
    if full_update:
        batch_offset = 0
        assets = assets
        next_offset = 0
        update_mode = "completa"
    else:
        assets = select_asset_batch(assets, batch_offset, max_symbols)
        next_offset = next_batch_offset(batch_offset, max_symbols, total_universe)
        update_mode = "tanda"

    symbols = [asset["symbol"] for asset in assets]
    metrics, source, diagnostics = get_daily_asset_metrics(symbols)
    diagnostics["loaded_assets"] = len(assets)
    diagnostics["total_universe"] = total_universe
    diagnostics["base_universe"] = len(base_assets)
    diagnostics["strategy_tickers"] = len(strategy_assets)
    diagnostics["added_strategy_tickers"] = max(0, len(assets) - len(base_assets))
    diagnostics["batch_offset"] = batch_offset
    diagnostics["next_batch_offset"] = next_offset
    diagnostics["max_symbols"] = max_symbols
    diagnostics["update_mode"] = update_mode
    diagnostics["source"] = source
    if source != "alpaca" or not metrics:
        print("No se pudo actualizar desde Alpaca. Revisa claves o plan de datos.")
        return {
            "ok": False,
            "message": "No se pudo actualizar desde Alpaca.",
            **diagnostics,
        }

    updated_at = datetime.now(UTC)
    rows = []
    for asset in assets:
        metric = metrics.get(asset["symbol"])
        if not metric:
            continue
        rows.append(
            {
                "symbol": asset["symbol"],
                "name": asset["name"],
                "sector": asset["sector"],
                "market": asset["market"],
                "price": metric["price"],
                "money_volume": metric["money_volume"],
                "money_volume_1m": metric_value(metric, "money_volume_1m"),
                "money_volume_2m": metric_value(metric, "money_volume_2m"),
                "money_volume_3m": metric_value(metric, "money_volume_3m"),
                "day_money_volume": metric_value(metric, "day_money_volume"),
                "week_money_volume": metric_value(metric, "week_money_volume"),
                "day_money_volume_1d": metric_value(metric, "day_money_volume_1d"),
                "day_money_volume_2d": metric_value(metric, "day_money_volume_2d"),
                "day_money_volume_3d": metric_value(metric, "day_money_volume_3d"),
                "day_money_volume_4d": metric_value(metric, "day_money_volume_4d"),
                "day_money_volume_5d": metric_value(metric, "day_money_volume_5d"),
                "week_money_volume_1w": metric_value(metric, "week_money_volume_1w"),
                "week_money_volume_2w": metric_value(metric, "week_money_volume_2w"),
                "week_money_volume_3w": metric_value(metric, "week_money_volume_3w"),
                "week_money_volume_4w": metric_value(metric, "week_money_volume_4w"),
                "week_money_volume_5w": metric_value(metric, "week_money_volume_5w"),
                "day_volume_score": metric["day_volume_score"],
                "week_volume_score": metric["week_volume_score"],
                "updated_at": updated_at,
            }
        )

    if not rows:
        print("Alpaca respondio, pero no se generaron filas.")
        return {
            "ok": False,
            "message": "Alpaca respondio, pero no se generaron filas.",
            **diagnostics,
        }

    with engine.begin() as connection:
        ensure_snapshot_table(connection)
        for row in rows:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        """
                        INSERT INTO asset_snapshots
                        (symbol, name, sector, market, price, money_volume,
                         money_volume_1m, money_volume_2m, money_volume_3m,
                         day_money_volume, week_money_volume,
                         day_money_volume_1d, day_money_volume_2d, day_money_volume_3d,
                         day_money_volume_4d, day_money_volume_5d,
                         week_money_volume_1w, week_money_volume_2w, week_money_volume_3w,
                         week_money_volume_4w, week_money_volume_5w,
                         day_volume_score, week_volume_score, updated_at)
                        VALUES
                        (:symbol, :name, :sector, :market, :price, :money_volume,
                         :money_volume_1m, :money_volume_2m, :money_volume_3m,
                         :day_money_volume, :week_money_volume,
                         :day_money_volume_1d, :day_money_volume_2d, :day_money_volume_3d,
                         :day_money_volume_4d, :day_money_volume_5d,
                         :week_money_volume_1w, :week_money_volume_2w, :week_money_volume_3w,
                         :week_money_volume_4w, :week_money_volume_5w,
                         :day_volume_score, :week_volume_score, :updated_at)
                        ON CONFLICT (symbol) DO UPDATE SET
                          name = EXCLUDED.name,
                          sector = EXCLUDED.sector,
                          market = EXCLUDED.market,
                          price = EXCLUDED.price,
                          money_volume = EXCLUDED.money_volume,
                          money_volume_1m = EXCLUDED.money_volume_1m,
                          money_volume_2m = EXCLUDED.money_volume_2m,
                          money_volume_3m = EXCLUDED.money_volume_3m,
                          day_money_volume = EXCLUDED.day_money_volume,
                          week_money_volume = EXCLUDED.week_money_volume,
                          day_money_volume_1d = EXCLUDED.day_money_volume_1d,
                          day_money_volume_2d = EXCLUDED.day_money_volume_2d,
                          day_money_volume_3d = EXCLUDED.day_money_volume_3d,
                          day_money_volume_4d = EXCLUDED.day_money_volume_4d,
                          day_money_volume_5d = EXCLUDED.day_money_volume_5d,
                          week_money_volume_1w = EXCLUDED.week_money_volume_1w,
                          week_money_volume_2w = EXCLUDED.week_money_volume_2w,
                          week_money_volume_3w = EXCLUDED.week_money_volume_3w,
                          week_money_volume_4w = EXCLUDED.week_money_volume_4w,
                          week_money_volume_5w = EXCLUDED.week_money_volume_5w,
                          day_volume_score = EXCLUDED.day_volume_score,
                          week_volume_score = EXCLUDED.week_volume_score,
                          updated_at = EXCLUDED.updated_at
                        """
                    ),
                    row,
                )
            else:
                connection.execute(
                    text(
                        """
                        INSERT OR REPLACE INTO asset_snapshots
                        (symbol, name, sector, market, price, money_volume,
                         money_volume_1m, money_volume_2m, money_volume_3m,
                         day_money_volume, week_money_volume,
                         day_money_volume_1d, day_money_volume_2d, day_money_volume_3d,
                         day_money_volume_4d, day_money_volume_5d,
                         week_money_volume_1w, week_money_volume_2w, week_money_volume_3w,
                         week_money_volume_4w, week_money_volume_5w,
                         day_volume_score, week_volume_score, updated_at)
                        VALUES
                        (:symbol, :name, :sector, :market, :price, :money_volume,
                         :money_volume_1m, :money_volume_2m, :money_volume_3m,
                         :day_money_volume, :week_money_volume,
                         :day_money_volume_1d, :day_money_volume_2d, :day_money_volume_3d,
                         :day_money_volume_4d, :day_money_volume_5d,
                         :week_money_volume_1w, :week_money_volume_2w, :week_money_volume_3w,
                         :week_money_volume_4w, :week_money_volume_5w,
                         :day_volume_score, :week_volume_score, :updated_at)
                        """
                    ),
                    row,
                )
        set_batch_offset(connection, diagnostics["next_batch_offset"])

    csv_rows = export_market_data_csv()
    print(f"Snapshots actualizados: {len(rows)}")
    return {
        "ok": True,
        "message": f"Snapshots actualizados: {len(rows)}",
        "saved_rows": len(rows),
        "csv_rows": csv_rows,
        **diagnostics,
    }


def load_strategy_ticker_assets(path=STRATEGY_TICKERS_PATH):
    if not path.exists():
        return []

    assets = []
    seen = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        symbol = raw_line.strip().upper()
        if not symbol or symbol.startswith("#") or symbol in seen:
            continue
        seen.add(symbol)
        assets.append(
            {
                "symbol": symbol,
                "name": symbol,
                "sector": "Sin clasificar",
                "market": "Otro",
                "price": 0,
                "money_volume": 0,
                "day_volume_score": 1,
                "week_volume_score": 1,
            }
        )
    return assets


def merge_assets(base_assets, extra_assets):
    merged = {asset["symbol"].upper(): dict(asset) for asset in base_assets}
    for asset in extra_assets:
        symbol = asset["symbol"].upper()
        if symbol not in merged:
            merged[symbol] = dict(asset)
    return sorted(merged.values(), key=lambda item: item["symbol"])


def resolve_max_symbols(max_symbols):
    if max_symbols is not None:
        return int(max_symbols)
    try:
        return int(os.environ.get("MARKET_DATA_MAX_SYMBOLS", "1000"))
    except ValueError:
        return 1000


def export_market_data_csv(path=MARKET_DATA_CSV_PATH):
    rows = load_snapshot_rows_for_export()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=MARKET_DATA_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def load_snapshot_rows_for_export():
    with engine.connect() as connection:
        ensure_snapshot_table(connection)
        rows = connection.execute(
            text(
                """
                SELECT symbol, name, sector, market, price, money_volume,
                       money_volume_1m, money_volume_2m, money_volume_3m,
                       day_money_volume, week_money_volume,
                       day_money_volume_1d, day_money_volume_2d, day_money_volume_3d,
                       day_money_volume_4d, day_money_volume_5d,
                       week_money_volume_1w, week_money_volume_2w, week_money_volume_3w,
                       week_money_volume_4w, week_money_volume_5w,
                       day_volume_score, week_volume_score, updated_at
                FROM asset_snapshots
                ORDER BY money_volume DESC
                """
            )
        ).mappings().fetchall()
    return [dict(row) for row in rows]


MARKET_DATA_CSV_COLUMNS = [
    "symbol",
    "name",
    "sector",
    "market",
    "price",
    "money_volume",
    "money_volume_1m",
    "money_volume_2m",
    "money_volume_3m",
    "day_money_volume",
    "week_money_volume",
    "day_money_volume_1d",
    "day_money_volume_2d",
    "day_money_volume_3d",
    "day_money_volume_4d",
    "day_money_volume_5d",
    "week_money_volume_1w",
    "week_money_volume_2w",
    "week_money_volume_3w",
    "week_money_volume_4w",
    "week_money_volume_5w",
    "day_volume_score",
    "week_volume_score",
    "updated_at",
]


def ensure_update_state_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS market_update_state (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
    )


def get_batch_offset(connection, total_universe):
    row = connection.execute(
        text("SELECT value FROM market_update_state WHERE key = 'batch_offset'")
    ).fetchone()
    if not row or total_universe <= 0:
        return 0
    return int(row[0]) % total_universe


def set_batch_offset(connection, offset):
    if engine.dialect.name == "postgresql":
        connection.execute(
            text(
                """
                INSERT INTO market_update_state (key, value)
                VALUES ('batch_offset', :offset)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """
            ),
            {"offset": offset},
        )
    else:
        connection.execute(
            text(
                """
                INSERT OR REPLACE INTO market_update_state (key, value)
                VALUES ('batch_offset', :offset)
                """
            ),
            {"offset": offset},
        )


def select_asset_batch(assets, offset, limit):
    if limit >= len(assets):
        return assets
    end = offset + limit
    if end <= len(assets):
        return assets[offset:end]
    return assets[offset:] + assets[: end - len(assets)]


def next_batch_offset(offset, limit, total):
    if total <= 0:
        return 0
    return (offset + limit) % total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Valora todo el universo de activos en una sola ejecucion.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="Numero maximo de simbolos a valorar. Usa 0 para todos.",
    )
    args = parser.parse_args()
    result = update_market_data(max_symbols=args.max_symbols, full=args.full)
    print(result)
    raise SystemExit(0 if result["ok"] else 1)
