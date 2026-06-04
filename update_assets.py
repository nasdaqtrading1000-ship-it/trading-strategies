import argparse
import csv
from pathlib import Path


OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "assets.csv"


SEED_ASSETS = [
    ("AAPL", "Apple Inc.", "Tecnologia", "Nasdaq", 196.45, 7200000000, 4.1, 4.3),
    ("MSFT", "Microsoft Corp.", "Tecnologia", "Nasdaq", 442.57, 6800000000, 3.8, 4.0),
    ("NVDA", "NVIDIA Corp.", "Tecnologia", "Nasdaq", 125.61, 8600000000, 4.9, 4.7),
    ("AVGO", "Broadcom Inc.", "Tecnologia", "Nasdaq", 142.88, 3100000000, 3.3, 3.1),
    ("AMD", "Advanced Micro Devices Inc.", "Tecnologia", "Nasdaq", 158.32, 2900000000, 4.4, 4.0),
    ("INTC", "Intel Corp.", "Tecnologia", "Nasdaq", 30.74, 1100000000, 2.4, 2.1),
    ("QCOM", "Qualcomm Inc.", "Tecnologia", "Nasdaq", 205.73, 1400000000, 2.8, 2.7),
    ("ADBE", "Adobe Inc.", "Tecnologia", "Nasdaq", 475.21, 950000000, 2.1, 2.4),
    ("AMZN", "Amazon.com Inc.", "Consumo", "Nasdaq", 184.26, 5100000000, 3.5, 3.7),
    ("TSLA", "Tesla Inc.", "Consumo", "Nasdaq", 177.29, 4900000000, 5.0, 4.8),
    ("COST", "Costco Wholesale Corp.", "Consumo", "Nasdaq", 812.63, 850000000, 2.0, 2.2),
    ("SBUX", "Starbucks Corp.", "Consumo", "Nasdaq", 83.41, 620000000, 1.9, 1.8),
    ("HD", "Home Depot Inc.", "Consumo", "NYSE", 336.12, 980000000, 2.2, 2.1),
    ("NKE", "Nike Inc.", "Consumo", "NYSE", 94.57, 720000000, 2.5, 2.3),
    ("META", "Meta Platforms Inc.", "Comunicacion", "Nasdaq", 503.24, 4300000000, 3.1, 3.4),
    ("GOOGL", "Alphabet Inc. Class A", "Comunicacion", "Nasdaq", 176.73, 3900000000, 2.9, 3.2),
    ("GOOG", "Alphabet Inc. Class C", "Comunicacion", "Nasdaq", 178.12, 2800000000, 2.6, 3.0),
    ("NFLX", "Netflix Inc.", "Comunicacion", "Nasdaq", 650.84, 1300000000, 2.7, 2.9),
    ("DIS", "Walt Disney Co.", "Comunicacion", "NYSE", 102.33, 880000000, 2.1, 2.0),
    ("JPM", "JPMorgan Chase & Co.", "Financiero", "NYSE", 199.68, 2200000000, 2.2, 2.5),
    ("BAC", "Bank of America Corp.", "Financiero", "NYSE", 39.82, 1500000000, 2.6, 2.4),
    ("GS", "Goldman Sachs Group Inc.", "Financiero", "NYSE", 458.77, 710000000, 1.8, 2.0),
    ("MS", "Morgan Stanley", "Financiero", "NYSE", 98.16, 620000000, 1.7, 1.8),
    ("V", "Visa Inc.", "Financiero", "NYSE", 274.91, 840000000, 1.8, 2.1),
    ("MA", "Mastercard Inc.", "Financiero", "NYSE", 452.62, 760000000, 1.9, 2.0),
    ("XOM", "Exxon Mobil Corp.", "Energia", "NYSE", 114.12, 1800000000, 1.9, 2.1),
    ("CVX", "Chevron Corp.", "Energia", "NYSE", 158.23, 990000000, 1.6, 1.9),
    ("COP", "ConocoPhillips", "Energia", "NYSE", 113.74, 690000000, 1.7, 1.8),
    ("SLB", "Schlumberger Ltd.", "Energia", "NYSE", 47.61, 520000000, 1.5, 1.6),
    ("LLY", "Eli Lilly and Co.", "Salud", "NYSE", 807.43, 1700000000, 2.4, 2.2),
    ("UNH", "UnitedHealth Group Inc.", "Salud", "NYSE", 505.12, 1200000000, 2.0, 2.1),
    ("JNJ", "Johnson & Johnson", "Salud", "NYSE", 147.52, 790000000, 1.5, 1.7),
    ("PFE", "Pfizer Inc.", "Salud", "NYSE", 28.41, 670000000, 1.8, 1.6),
    ("MRK", "Merck & Co. Inc.", "Salud", "NYSE", 128.35, 830000000, 1.7, 1.9),
    ("ABBV", "AbbVie Inc.", "Salud", "NYSE", 165.71, 760000000, 1.6, 1.7),
    ("CAT", "Caterpillar Inc.", "Industrial", "NYSE", 329.84, 760000000, 1.8, 2.0),
    ("BA", "Boeing Co.", "Industrial", "NYSE", 183.76, 930000000, 2.9, 2.6),
    ("GE", "GE Aerospace", "Industrial", "NYSE", 163.42, 640000000, 2.0, 2.2),
    ("HON", "Honeywell International Inc.", "Industrial", "Nasdaq", 199.27, 430000000, 1.5, 1.6),
    ("DE", "Deere & Co.", "Industrial", "NYSE", 381.64, 520000000, 1.7, 1.8),
]


FIELDS = [
    "symbol",
    "name",
    "sector",
    "market",
    "price",
    "money_volume",
    "day_volume_score",
    "week_volume_score",
]


def parse_csv_list(value):
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def build_assets(markets=None, sectors=None, min_money_volume=0):
    rows = []
    for asset in SEED_ASSETS:
        row = dict(zip(FIELDS, asset))
        if markets and row["market"] not in markets:
            continue
        if sectors and row["sector"] not in sectors:
            continue
        if row["money_volume"] < min_money_volume:
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: (row["market"], row["sector"], row["symbol"]))


def write_assets(rows, output_path=OUTPUT_PATH):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Regenera data/assets.csv.")
    parser.add_argument("--markets", help="Mercados separados por coma. Ej: Nasdaq,NYSE")
    parser.add_argument("--sectors", help="Sectores separados por coma. Ej: Tecnologia,Salud")
    parser.add_argument(
        "--min-money-volume",
        type=float,
        default=0,
        help="Volumen monetario minimo en USD.",
    )
    args = parser.parse_args()

    rows = build_assets(
        markets=parse_csv_list(args.markets),
        sectors=parse_csv_list(args.sectors),
        min_money_volume=args.min_money_volume,
    )
    write_assets(rows)
    print(f"CSV actualizado: {OUTPUT_PATH}")
    print(f"Activos escritos: {len(rows)}")


if __name__ == "__main__":
    main()
