import csv
from pathlib import Path


DATA_PATH = Path(__file__).resolve().parent / "data" / "assets.csv"


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
                    "day_volume_score": float(row["day_volume_score"]),
                    "week_volume_score": float(row["week_volume_score"]),
                }
            )
        return assets


def available_sectors(assets):
    return ["Todos"] + sorted({asset["sector"] for asset in assets})


def available_markets(assets):
    return ["Todos"] + sorted({asset["market"] for asset in assets})


def filter_assets(filters, assets=None):
    assets = assets or load_assets()
    filtered = assets

    if filters["sector"] != "Todos":
        filtered = [asset for asset in filtered if asset["sector"] == filters["sector"]]

    if filters["market"] != "Todos":
        filtered = [asset for asset in filtered if asset["market"] == filters["market"]]

    min_volume = filters["min_money_volume"] * 1_000_000
    filtered = [asset for asset in filtered if asset["money_volume"] >= min_volume]

    min_day_score = min(5, max(1, filters["day_volume_window"]))
    min_week_score = min(5, max(1, filters["week_volume_window"]))
    filtered = [
        asset
        for asset in filtered
        if asset["day_volume_score"] >= min_day_score
        and asset["week_volume_score"] >= min_week_score
    ]

    filtered = sorted(
        filtered,
        key=lambda asset: (
            asset["money_volume"],
            asset["day_volume_score"],
            asset["week_volume_score"],
        ),
        reverse=True,
    )
    return filtered[: filters["limit"]]
