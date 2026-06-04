from datetime import UTC, datetime

from sqlalchemy import text

from alpaca_data import get_daily_asset_metrics
from db import engine
from market_scanner import load_assets


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
                day_volume_score FLOAT NOT NULL,
                week_volume_score FLOAT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def update_market_data():
    assets = load_assets()
    symbols = [asset["symbol"] for asset in assets]
    metrics, source = get_daily_asset_metrics(symbols)
    if source != "alpaca" or not metrics:
        print("No se pudo actualizar desde Alpaca. Revisa claves o plan de datos.")
        return 1

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
                "day_volume_score": metric["day_volume_score"],
                "week_volume_score": metric["week_volume_score"],
                "updated_at": updated_at,
            }
        )

    if not rows:
        print("Alpaca respondio, pero no se generaron filas.")
        return 1

    with engine.begin() as connection:
        ensure_snapshot_table(connection)
        for row in rows:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        """
                        INSERT INTO asset_snapshots
                        (symbol, name, sector, market, price, money_volume,
                         day_volume_score, week_volume_score, updated_at)
                        VALUES
                        (:symbol, :name, :sector, :market, :price, :money_volume,
                         :day_volume_score, :week_volume_score, :updated_at)
                        ON CONFLICT (symbol) DO UPDATE SET
                          name = EXCLUDED.name,
                          sector = EXCLUDED.sector,
                          market = EXCLUDED.market,
                          price = EXCLUDED.price,
                          money_volume = EXCLUDED.money_volume,
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
                         day_volume_score, week_volume_score, updated_at)
                        VALUES
                        (:symbol, :name, :sector, :market, :price, :money_volume,
                         :day_volume_score, :week_volume_score, :updated_at)
                        """
                    ),
                    row,
                )

    print(f"Snapshots actualizados: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(update_market_data())
