"""
Actualiza activos y datos de mercado desde este PC y los guarda en PostgreSQL.

Render queda solo para mostrar la web.

Usos:
    python run_local_market_update.py --assets
    python run_local_market_update.py --market-full
    python run_local_market_update.py --all

Necesita en .env:
    DATABASE_URL=postgresql://...
    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from config_env import load_local_env
from market_scanner import save_universe_assets, universe_count, snapshot_count
from update_assets import build_assets_from_alpaca, write_assets
from update_market_data import update_market_data


BASE_DIR = Path(__file__).resolve().parent


def main():
    load_local_env()
    warn_if_not_postgres()

    parser = argparse.ArgumentParser(description="Actualizacion local de mercado para la web.")
    parser.add_argument("--assets", action="store_true", help="Actualiza CSV/universo de activos desde Alpaca.")
    parser.add_argument("--market-full", action="store_true", help="Actualiza datos de mercado completos.")
    parser.add_argument("--market-batch", action="store_true", help="Actualiza solo una tanda de mercado.")
    parser.add_argument("--all", action="store_true", help="Ejecuta activos + mercado completo.")
    parser.add_argument("--max-symbols", type=int, default=None, help="Limite de simbolos para tanda/mercado.")
    args = parser.parse_args()

    if not any([args.assets, args.market_full, args.market_batch, args.all]):
        parser.print_help()
        return 1

    if args.assets or args.all:
        update_assets_universe()

    if args.market_full or args.all:
        update_market(full=True, max_symbols=args.max_symbols)
    elif args.market_batch:
        update_market(full=False, max_symbols=args.max_symbols)

    print("")
    print(f"Universo en DB: {universe_count()}")
    print(f"Snapshots mercado en DB: {snapshot_count()}")
    mirror_postgres_to_sqlite()
    return 0


def update_assets_universe():
    print("Actualizando universo de activos desde Alpaca...")
    rows, source = build_assets_from_alpaca()
    write_assets(rows)
    save_universe_assets(rows)
    print(f"Activos actualizados: {len(rows)}. Fuente: {source}.")


def update_market(full, max_symbols):
    label = "completo" if full else "por tanda"
    print(f"Actualizando mercado {label}...")
    result = update_market_data(full=full, max_symbols=max_symbols)
    print(f"OK: {result.get('ok')}")
    print(f"Guardados: {result.get('saved_rows', 0)}")
    print(f"Universo total: {result.get('total_universe', 0)}")
    print(f"Modo: {result.get('update_mode', '')}")
    print(f"Error: {result.get('last_error', '') or 'Sin error'}")
    if not result.get("ok"):
        raise SystemExit(1)


def warn_if_not_postgres():
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        print("DATABASE_URL PostgreSQL detectada. La web de Render vera estos datos.")
        return
    print("AVISO: no hay DATABASE_URL PostgreSQL. Esto actualizara SQLite local, Render no vera los cambios.")


def mirror_postgres_to_sqlite():
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        return
    sync_script = BASE_DIR / "sync_postgres_to_sqlite.py"
    if not sync_script.exists():
        print("Copia SQLite omitida: no existe sync_postgres_to_sqlite.py")
        return
    print("")
    print("Actualizando copia SQLite local desde PostgreSQL...")
    result = subprocess.run([sys.executable, str(sync_script)], cwd=str(BASE_DIR), text=True)
    if result.returncode != 0:
        print(f"Copia SQLite termino con codigo {result.returncode}.")


if __name__ == "__main__":
    raise SystemExit(main())
