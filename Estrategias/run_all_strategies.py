"""
Ejecuta todas las estrategias de esta carpeta.

Cada estrategia se encarga de escribir su propio TXT dentro de salidas_txt/.
Este script solo las lanza una a una y muestra un resumen final.
"""

from pathlib import Path
import json
import subprocess
import sys
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "strategy_run_status.json"


STRATEGIES = [
    {"name": "Momentum", "file": "Momentum.py"},
    {"name": "Swing Trading", "file": "SwingTrading.py"},
    {"name": "BreaKout", "file": "BreaKout.py"},
    {"name": "Mean Reversion", "file": "Mean Reversion.py"},
    {"name": "Value Trading", "file": "ValueTrading.py"},
    {"name": "Dividend Growth", "file": "DividenGrowth.py"},
    {"name": "Trend Following", "file": "TrendFollowing.py"},
    {"name": "Pairs Trading", "file": "PairsTrading.py"},
    {"name": "Sector Rotation", "file": "SectorRotation.py"},
    {"name": "Quality Investing", "file": "QualityInvesting.py"},
    {"name": "Opening Range BreaKout", "file": "OpeningRangeBreaKout.py"},
    {"name": "VWAP Reversion", "file": "VWAP Reversion.py"},
    {"name": "Momentum Intradia", "file": "MomentumIntradia.py"},
    {"name": "Scalping The PullBacks", "file": "ScalpingThePullBacKs.py"},
    {"name": "Gap and Go", "file": "Gap and Go.py"},
]


def run_strategy(strategy):
    """
    Ejecuta una estrategia y devuelve si fue bien o fallo.
    """
    filename = strategy["file"]
    path = BASE_DIR / filename

    if not path.exists():
        return {
            "name": strategy["name"],
            "file": filename,
            "ok": False,
            "returncode": None,
            "error": "Archivo no encontrado",
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"\n=== Ejecutando {filename} ===")

    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
    )

    if completed.stdout:
        print(completed.stdout.strip())

    if completed.stderr:
        print(completed.stderr.strip())

    return {
        "name": strategy["name"],
        "file": filename,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "error": completed.stderr.strip(),
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def write_status_file(results, started_at, finished_at):
    status = {
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "strategies": {
            result["name"]: {
                "file": result["file"],
                "ok": result["ok"],
                "returncode": result["returncode"],
                "error": result["error"][-800:],
                "ran_at": result["ran_at"],
            }
            for result in results
        },
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    started_at = datetime.now()
    print(f"Inicio ejecucion: {started_at.strftime('%Y-%m-%d %H:%M:%S')}")

    results = [
        run_strategy(strategy)
        for strategy in STRATEGIES
    ]
    finished_at = datetime.now()
    write_status_file(results, started_at, finished_at)

    ok_count = sum(1 for result in results if result["ok"])
    fail_count = len(results) - ok_count

    print("\n=== Resumen final ===")
    print(f"Estrategias ejecutadas correctamente: {ok_count}")
    print(f"Estrategias con error: {fail_count}")

    for result in results:
        status = "OK" if result["ok"] else "ERROR"
        print(f"{status} - {result['name']} ({result['file']})")
    print(f"Estado guardado en: {STATUS_FILE}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
