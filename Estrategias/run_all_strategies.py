"""
Ejecuta todas las estrategias de esta carpeta.

Cada estrategia se encarga de escribir su propio TXT dentro de salidas_txt/.
Este script solo las lanza una a una y muestra un resumen final.
"""

from pathlib import Path
import json
import os
import subprocess
import sys
from datetime import UTC, datetime


BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "strategy_run_status.json"
OUTPUT_DIR = BASE_DIR / "salidas_txt"
LOG_DIR = BASE_DIR / "logs"
SELECTION_FILE = BASE_DIR / "estrategias_a_ejecutar.txt"


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

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{safe_log_name(strategy['name'])}.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"=== Ejecutando {filename} ===\n")
        log_file.flush()
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=BASE_DIR,
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    txt_updated = output_txt_updated(txt_path, previous_mtime)
    errors = []
    if completed.returncode != 0:
        log_tail = read_tail(log_path)
        errors.append(log_tail or f"Return code {completed.returncode}")

    return {
        "name": strategy["name"],
        "file": filename,
        "txt": strategy["txt"],
        "ok": completed.returncode == 0,
        "txt_updated": txt_updated,
        "returncode": completed.returncode,
        "error": " | ".join(error for error in errors if error),
        "log": str(log_path),
        "ran_at": datetime.now(UTC).isoformat(),
    }


def run_strategy_safely(strategy):
    try:
        return run_strategy(strategy)
    except Exception as error:
        return {
            "name": strategy.get("name", "Estrategia desconocida"),
            "file": strategy.get("file", ""),
            "txt": strategy.get("txt", ""),
            "ok": False,
            "txt_updated": False,
            "returncode": None,
            "error": f"Error inesperado en runner: {error}",
            "log": "",
            "ran_at": datetime.now(UTC).isoformat(),
        }


def safe_log_name(value):
    cleaned = "".join(char if char.isalnum() else "_" for char in str(value))
    return cleaned.strip("_") or "strategy"


def read_tail(path, max_chars=1200):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip()[-max_chars:]


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
                "log": result.get("log", ""),
                "ran_at": result["ran_at"],
            }
            for result in results
        },
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def selected_strategies():
    active_names_raw = os.environ.get("TRADING_ACTIVE_STRATEGIES")
    if active_names_raw is None:
        selected_from_file = selected_strategies_from_file()
        if selected_from_file is not None:
            return selected_from_file
        return STRATEGIES

    try:
        active_names = set(json.loads(active_names_raw))
    except json.JSONDecodeError:
        print("No se pudo leer TRADING_ACTIVE_STRATEGIES. Se ejecutan todas.", file=sys.stderr)
        return STRATEGIES

    return [
        strategy
        for strategy in STRATEGIES
        if strategy["name"] in active_names
    ]


def selected_strategies_from_file():
    selection_path = Path(os.environ.get("TRADING_STRATEGY_SELECTION_FILE", SELECTION_FILE))
    if not selection_path.exists() or not selection_path.is_file():
        return None

    requested = []
    for raw_line in selection_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            requested.append(normalize_key(line))

    if not requested:
        return None

    selected = [
        strategy
        for strategy in STRATEGIES
        if normalize_key(strategy["name"]) in requested
        or normalize_key(strategy["file"]) in requested
        or normalize_key(Path(strategy["file"]).stem) in requested
        or normalize_key(strategy["txt"]) in requested
    ]

    missing = [
        value
        for value in requested
        if not any(
            value in {
                normalize_key(strategy["name"]),
                normalize_key(strategy["file"]),
                normalize_key(Path(strategy["file"]).stem),
                normalize_key(strategy["txt"]),
            }
            for strategy in STRATEGIES
        )
    ]
    for value in missing:
        print(f"No se encontro estrategia para: {value}", file=sys.stderr)

    return selected


def normalize_key(value):
    return "".join(char.lower() for char in str(value) if char.isalnum())


def main():
    started_at = datetime.now(UTC)
    print(f"Inicio ejecucion: {started_at.isoformat()}")

    strategies = selected_strategies()
    print(f"Estrategias seleccionadas: {len(strategies)}")
    for strategy in strategies:
        print(f"- {strategy['name']}")

    results = []
    for strategy in strategies:
        result = run_strategy_safely(strategy)
        results.append(result)
        write_status_file(results, started_at, datetime.now(UTC))
        sync_to_database()

    finished_at = datetime.now(UTC)
    write_status_file(results, started_at, finished_at)
    sync_to_database()

    ok_count = sum(1 for result in results if result["ok"])
    fail_count = len(results) - ok_count

    print("\n=== Resumen final ===")
    print(f"Estrategias ejecutadas correctamente: {ok_count}")
    print(f"Estrategias con error: {fail_count}")

    for result in results:
        status = "OK" if result["ok"] else "ERROR"
        txt_status = "TXT ACTUALIZADO" if result["txt_updated"] else "TXT SIN CAMBIOS"
        print(f"{status} - {result['name']} ({result['file']}) | {txt_status}")
    print(f"Estado guardado en: {STATUS_FILE}")

    return 0 if fail_count == 0 else 1


def sync_to_database():
    if os.environ.get("TRADING_SKIP_DB_SYNC") == "1":
        return

    sync_script = BASE_DIR.parent / "sync_signals_to_db.py"
    if not sync_script.exists():
        print("Sincronizacion PostgreSQL omitida: no existe sync_signals_to_db.py")
        return

    completed = subprocess.run(
        [sys.executable, str(sync_script)],
        cwd=str(BASE_DIR.parent),
        text=True,
    )
    if completed.returncode != 0:
        print(f"Sincronizacion PostgreSQL con avisos: codigo {completed.returncode}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
