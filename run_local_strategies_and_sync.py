"""
Ejecuta las estrategias en este PC y sincroniza los avisos con PostgreSQL.

Uso:
    python run_local_strategies_and_sync.py

Variables utiles:
    TRADING_ACTIVE_STRATEGIES='["Momentum", "Gap and Go"]'
    DATABASE_URL=postgresql://...
"""

import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
RUNNER = BASE_DIR / "Estrategias" / "run_all_strategies.py"
SYNC = BASE_DIR / "sync_signals_to_db.py"


def main():
    if not RUNNER.exists():
        print(f"No existe: {RUNNER}")
        return 1

    print("Ejecutando estrategias en local...")
    run_result = subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=str(RUNNER.parent),
        text=True,
    )

    print("")
    print("Sincronizando avisos con PostgreSQL...")
    sync_result = subprocess.run(
        [sys.executable, str(SYNC)],
        cwd=str(BASE_DIR),
        text=True,
    )

    if run_result.returncode != 0:
        print("")
        print(f"Las estrategias terminaron con codigo {run_result.returncode}.")
    if sync_result.returncode != 0:
        print(f"La sincronizacion termino con codigo {sync_result.returncode}.")

    return run_result.returncode or sync_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
