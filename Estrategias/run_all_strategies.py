"""
Ejecuta todas las estrategias de esta carpeta.

Cada estrategia se encarga de escribir su propio TXT dentro de salidas_txt/.
Este script solo las lanza una a una y muestra un resumen final.
"""

from pathlib import Path
import json
import subprocess
import sys
from datetime import UTC, datetime


BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "strategy_run_status.json"
OUTPUT_DIR = BASE_DIR / "salidas_txt"


STRATEGIES = [
    {"name": "Momentum", "file": "Momentum.py", "txt": "Momentum.txt"},
    {"name": "Swing Trading", "file": "SwingTrading.py", "txt": "SwingTrading.txt"},
    {"name": "BreaKout", "file": "BreaKout.py", "txt": "BreaKout.txt"},
    {"name": "Mean Reversion", "file": "Mean Reversion.py", "txt": "Mean_Reversion.txt"},
    {"name": "Value Trading", "file": "ValueTrading.py", "txt": "ValueTrading.txt"},
    {"name": "Dividend Growth", "file": "DividenGrowth.py", "txt": "DividenGrowth.txt"},
    {"name": "Trend Following", "file": "TrendFollowing.py", "txt": "TrendFollowing.txt"},
    {"name": "Pairs Trading", "file": "PairsTrading.py", "txt": "PairsTrading.txt"},
    {"name": "Sector Rotation", "file": "SectorRotation.py", "txt": "SectorRotation.txt"},
    {"name": "Quality Investing", "file": "QualityInvesting.py", "txt": "QualityInvesting.txt"},
    {"name": "Opening Range BreaKout", "file": "OpeningRangeBreaKout.py", "txt": "OpeningRangeBreaKout.txt"},
    {"name": "VWAP Reversion", "file": "VWAP Reversion.py", "txt": "VWAP_Reversion.txt"},
    {"name": "Momentum Intradia", "file": "MomentumIntradia.py", "txt": "MomentumIntradia.txt"},
    {"name": "Scalping The PullBacks", "file": "ScalpingThePullBacKs.py", "txt": "ScalpingThePullBacKs.txt"},
    {"name": "Gap and Go", "file": "Gap and Go.py", "txt": "Gap_and_Go.txt"},
]


def run_strategy(strategy):
    """
    Ejecuta una estrategia y devuelve si fue bien o fallo.
    """
    filename = strategy["file"]
    path = BASE_DIR / filename
    txt_path = OUTPUT_DIR / strategy["txt"]
    previous_mtime = txt_path.stat().st_mtime if txt_path.exists() else None

    if not path.exists():
        return {
            "name": strategy["name"],
            "file": filename,
            "txt": strategy["txt"],
            "ok": False,
            "txt_updated": False,
            "returncode": None,
            "error": "Archivo no encontrado",
            "ran_at": datetime.now(UTC).isoformat(),
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

    txt_updated = output_txt_updated(txt_path, previous_mtime)
    errors = []
    if completed.returncode != 0:
        errors.append(completed.stderr.strip() or f"Return code {completed.returncode}")
    if completed.returncode == 0 and not txt_updated:
        errors.append(f"El codigo termino sin error, pero no actualizo {strategy['txt']}.")

    return {
        "name": strategy["name"],
        "file": filename,
        "txt": strategy["txt"],
        "ok": completed.returncode == 0 and txt_updated,
        "txt_updated": txt_updated,
        "returncode": completed.returncode,
        "error": " | ".join(error for error in errors if error),
        "ran_at": datetime.now(UTC).isoformat(),
    }


def output_txt_updated(path, previous_mtime):
    if not path.exists() or not path.is_file():
        return False
    current_mtime = path.stat().st_mtime
    if previous_mtime is None:
        return True
    return current_mtime > previous_mtime


def write_status_file(results, started_at, finished_at):
    status = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "strategies": {
            result["name"]: {
                "file": result["file"],
                "txt": result["txt"],
                "ok": result["ok"],
                "txt_updated": result["txt_updated"],
                "returncode": result["returncode"],
                "error": result["error"][-800:],
                "ran_at": result["ran_at"],
            }
            for result in results
        },
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    started_at = datetime.now(UTC)
    print(f"Inicio ejecucion: {started_at.isoformat()}")

    results = [
        run_strategy(strategy)
        for strategy in STRATEGIES
    ]
    finished_at = datetime.now(UTC)
    write_status_file(results, started_at, finished_at)

    ok_count = sum(1 for result in results if result["ok"])
    fail_count = len(results) - ok_count

    print("\n=== Resumen final ===")
    print(f"Estrategias ejecutadas correctamente: {ok_count}")
    print(f"Estrategias con error: {fail_count}")

    for result in results:
        status = "OK" if result["ok"] else "ERROR"
        txt_status = "TXT OK" if result["txt_updated"] else "TXT NO ACTUALIZADO"
        print(f"{status} - {result['name']} ({result['file']}) | {txt_status}")
    print(f"Estado guardado en: {STATUS_FILE}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
