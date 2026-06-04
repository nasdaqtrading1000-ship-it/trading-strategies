import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from db import engine


DATA_PATH = Path(__file__).resolve().parent / "data" / "assets.csv"


def _float_or_zero(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def load_assets(path=DATA_PATH):
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assets = []
        for row in reader:
            assets.append(
                {
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "sector": row["sector"],
                    "market": row["market"],
                    "price": float(row["price"]),
                    "money_volume": float(row["money_volume"]),
                    "money_volume_1m": _float_or_zero(row.get("money_volume_1m")),
                    "money_volume_2m": _float_or_zero(row.get("money_volume_2m")),
                    "money_volume_3m": _float_or_zero(row.get("money_volume_3m")),
                    "day_money_volume": _float_or_zero(row.get("day_money_volume")),
                    "week_money_volume": _float_or_zero(row.get("week_money_volume")),
                    "day_money_volume_1d": _float_or_zero(row.get("day_money_volume_1d")),
                    "day_money_volume_2d": _float_or_zero(row.get("day_money_volume_2d")),
                    "day_money_volume_3d": _float_or_zero(row.get("day_money_volume_3d")),
                    "day_money_volume_4d": _float_or_zero(row.get("day_money_volume_4d")),
                    "day_money_volume_5d": _float_or_zero(row.get("day_money_volume_5d")),
                    "week_money_volume_1w": _float_or_zero(row.get("week_money_volume_1w")),
                    "week_money_volume_2w": _float_or_zero(row.get("week_money_volume_2w")),
                    "week_money_volume_3w": _float_or_zero(row.get("week_money_volume_3w")),
                    "week_money_volume_4w": _float_or_zero(row.get("week_money_volume_4w")),
                    "week_money_volume_5w": _float_or_zero(row.get("week_money_volume_5w")),
                    "day_volume_score": float(row["day_volume_score"]),
                    "week_volume_score": float(row["week_volume_score"]),
                }
            )
        return assets


def load_universe_assets():
    database_assets = load_database_universe_assets()
    return database_assets or load_assets()


def load_database_universe_assets():
    try:
        with engine.connect() as connection:
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
                           day_volume_score, week_volume_score
                    FROM asset_universe
                    ORDER BY symbol
                    """
                )
            ).mappings().fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


def universe_count():
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT COUNT(*) FROM asset_universe")).scalar_one()
    except Exception:
        return 0


def save_universe_assets(rows):
    with engine.begin() as connection:
        ensure_universe_table(connection)
        connection.execute(text("DELETE FROM asset_universe"))
        for row in rows:
            complete = normalize_asset_row(row)
            connection.execute(
                text(
                    """
                    INSERT INTO asset_universe
                    (symbol, name, sector, market, price, money_volume,
                     money_volume_1m, money_volume_2m, money_volume_3m,
                     day_money_volume, week_money_volume,
                     day_money_volume_1d, day_money_volume_2d, day_money_volume_3d,
                     day_money_volume_4d, day_money_volume_5d,
                     week_money_volume_1w, week_money_volume_2w, week_money_volume_3w,
                     week_money_volume_4w, week_money_volume_5w,
                     day_volume_score, week_volume_score)
                    VALUES
                    (:symbol, :name, :sector, :market, :price, :money_volume,
                     :money_volume_1m, :money_volume_2m, :money_volume_3m,
                     :day_money_volume, :week_money_volume,
                     :day_money_volume_1d, :day_money_volume_2d, :day_money_volume_3d,
                     :day_money_volume_4d, :day_money_volume_5d,
                     :week_money_volume_1w, :week_money_volume_2w, :week_money_volume_3w,
                     :week_money_volume_4w, :week_money_volume_5w,
                     :day_volume_score, :week_volume_score)
                    """
                ),
                complete,
            )


def normalize_asset_row(row):
    money_volume = float(row.get("money_volume") or 0)
    return {
        "symbol": row["symbol"],
        "name": row.get("name") or row["symbol"],
        "sector": row.get("sector") or "Sin clasificar",
        "market": row.get("market") or "Otro",
        "price": float(row.get("price") or 0),
        "money_volume": money_volume,
        "money_volume_1m": _float_or_zero(row.get("money_volume_1m")),
        "money_volume_2m": _float_or_zero(row.get("money_volume_2m")),
        "money_volume_3m": _float_or_zero(row.get("money_volume_3m")),
        "day_money_volume": _float_or_zero(row.get("day_money_volume")),
        "week_money_volume": _float_or_zero(row.get("week_money_volume")),
        "day_money_volume_1d": _float_or_zero(row.get("day_money_volume_1d")),
        "day_money_volume_2d": _float_or_zero(row.get("day_money_volume_2d")),
        "day_money_volume_3d": _float_or_zero(row.get("day_money_volume_3d")),
        "day_money_volume_4d": _float_or_zero(row.get("day_money_volume_4d")),
        "day_money_volume_5d": _float_or_zero(row.get("day_money_volume_5d")),
        "week_money_volume_1w": _float_or_zero(row.get("week_money_volume_1w")),
        "week_money_volume_2w": _float_or_zero(row.get("week_money_volume_2w")),
        "week_money_volume_3w": _float_or_zero(row.get("week_money_volume_3w")),
        "week_money_volume_4w": _float_or_zero(row.get("week_money_volume_4w")),
        "week_money_volume_5w": _float_or_zero(row.get("week_money_volume_5w")),
        "day_volume_score": float(row.get("day_volume_score") or 1),
        "week_volume_score": float(row.get("week_volume_score") or 1),
    }


def ensure_universe_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS asset_universe (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sector TEXT NOT NULL,
                market TEXT NOT NULL,
                price FLOAT NOT NULL DEFAULT 0,
                money_volume FLOAT NOT NULL DEFAULT 0,
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
                day_volume_score FLOAT NOT NULL DEFAULT 1,
                week_volume_score FLOAT NOT NULL DEFAULT 1
            )
            """
        )
    )
    for column_name in [
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
    ]:
        add_float_column_if_missing(connection, "asset_universe", column_name)


def add_float_column_if_missing(connection, table_name, column_name):
    if column_exists(connection, table_name, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} FLOAT NOT NULL DEFAULT 0"
        )
    )


def column_exists(connection, table_name, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = :table_name
                  AND column_name = :column_name
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(row[1] == column_name for row in rows)


def csv_updated_at(path=DATA_PATH):
    if not path.exists():
        return "No disponible"
    updated_at = datetime.fromtimestamp(path.stat().st_mtime)
    return updated_at.strftime("%d/%m/%Y %H:%M")


def load_snapshot_assets():
    with engine.connect() as connection:
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
                       day_volume_score, week_volume_score
                FROM asset_snapshots
                ORDER BY money_volume DESC
                """
            )
        ).mappings().fetchall()
    return [dict(row) for row in rows]


def snapshot_count():
    with engine.connect() as connection:
        result = connection.execute(text("SELECT COUNT(*) FROM asset_snapshots"))
        return result.scalar_one()


def available_sectors(assets):
    return ["Todos"] + sorted({asset["sector"] for asset in assets})


def available_markets(assets):
    return ["Todos"] + sorted({asset["market"] for asset in assets})


def filter_assets(filters, assets=None):
    source = "csv"
    universe_total = len(assets) if assets else 0
    if filters.get("data_source") == "database":
        snapshot_assets = load_snapshot_assets()
        if snapshot_assets:
            assets = snapshot_assets
            source = "database"
            universe_total = len(snapshot_assets)
        else:
            assets = []
            source = "database_empty"
            universe_total = 0

    assets = assets if assets is not None else load_assets()
    if not universe_total:
        universe_total = len(assets)
    filtered = assets

    if filters["sector"] != "Todos":
        filtered = [asset for asset in filtered if asset["sector"] == filters["sector"]]

    if filters["market"] != "Todos":
        filtered = [asset for asset in filtered if asset["market"] == filters["market"]]

    filtered = [add_selected_metrics(asset, filters) for asset in filtered]

    min_volume = filters["min_money_volume"] * 1_000_000
    filtered = [asset for asset in filtered if asset["money_volume_selected"] >= min_volume]

    sort_by = filters.get("sort_by", "money_volume_selected")

    filtered = sorted(
        filtered,
        key=lambda asset: asset.get(sort_by, 0),
        reverse=True,
    )
    return filtered[: filters["limit"]], source, universe_total


def add_selected_metrics(asset, filters):
    month_key = f"money_volume_{filters['month_window']}m"
    day_key = f"day_money_volume_{filters['day_volume_window']}d"
    week_key = f"week_money_volume_{filters['week_volume_window']}w"
    money_volume = asset.get(month_key) or 0
    day_money_volume = asset.get(day_key) or 0
    week_money_volume = asset.get(week_key) or 0
    ratio = day_money_volume / money_volume if money_volume else 0
    return {
        **asset,
        "money_volume_selected": money_volume,
        "day_money_volume_selected": day_money_volume,
        "week_money_volume_selected": week_money_volume,
        "day_to_month_volume_ratio": ratio,
    }
