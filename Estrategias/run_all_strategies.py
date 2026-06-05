"""
Ejecuta todas las estrategias de esta carpeta.

Cada estrategia se encarga de escribir su propio TXT dentro de salidas_txt/.
Este script solo las lanza una a una y muestra un resumen final.
"""

from pathlib import Path
import subprocess
import sys
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent


STRATEGY_FILES = [
    "Momentum.py",
    "SwingTrading.py",
    "BreaKout.py",
    "Mean Reversion.py",
    "ValueTrading.py",
    "DividenGrowth.py",
    "TrendFollowing.py",
    "PairsTrading.py",
    "SectorRotation.py",
    "QualityInvesting.py",
    "OpeningRangeBreaKout.py",
    "VWAP Reversion.py",
    "MomentumIntradia.py",
    "ScalpingThePullBacKs.py",
    "Gap and Go.py",
]


def run_strategy(filename):
    """
    Ejecuta una estrategia y devuelve si fue bien o fallo.
    """
    path = BASE_DIR / filename

    if not path.exists():
        return {
            "file": filename,
            "ok": False,
            "returncode": None,
            "error": "Archivo no encontrado",
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
        "file": filename,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "error": completed.stderr.strip(),
    }


def main():
    print(f"Inicio ejecucion: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = [
        run_strategy(filename)
        for filename in STRATEGY_FILES
    ]

    ok_count = sum(1 for result in results if result["ok"])
    fail_count = len(results) - ok_count

    print("\n=== Resumen final ===")
    print(f"Estrategias ejecutadas correctamente: {ok_count}")
    print(f"Estrategias con error: {fail_count}")

    for result in results:
        status = "OK" if result["ok"] else "ERROR"
        print(f"{status} - {result['file']}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
