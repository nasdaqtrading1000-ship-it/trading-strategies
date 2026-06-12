"""
Ejecuta todas las estrategias de esta carpeta.

Cada estrategia se encarga de escribir su propio TXT dentro de salidas_txt/.
Este script solo las lanza una a una y muestra un resumen final.
"""

from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "strategy_run_status.json"
OUTPUT_DIR = BASE_DIR / "salidas_txt"
LOG_DIR = BASE_DIR / "logs"
SELECTION_FILE = BASE_DIR / "estrategias_a_ejecutar.txt"
PROJECT_DIR = BASE_DIR.parent
MARKET_UPDATE_SCRIPT = PROJECT_DIR / "run_local_market_update.py"
MADRID_TZ = ZoneInfo("Europe/Madrid")
DEFAULT_START_TIME = "15:30"
DEFAULT_END_TIME = "22:00"
DEFAULT_INTERVAL_MINUTES = 60
MARKET_UPDATE_INTERVAL_MINUTES = int(os.environ.get("LOCAL_MARKET_UPDATE_INTERVAL_MINUTES", "240"))
LOOP_SLEEP_SECONDS = int(os.environ.get("LOCAL_TRADING_LOOP_SLEEP_SECONDS", "30"))


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


def selected_strategy_schedules():
    selected = selected_strategies_from_file(include_schedule=True)
    if selected is None:
        selected = [
            {
                **strategy,
                "start": DEFAULT_START_TIME,
                "end": DEFAULT_END_TIME,
                "interval": DEFAULT_INTERVAL_MINUTES,
            }
            for strategy in selected_strategies()
        ]
    return selected


def selected_strategies_from_file(include_schedule=False):
    selection_path = Path(os.environ.get("TRADING_STRATEGY_SELECTION_FILE", SELECTION_FILE))
    if not selection_path.exists() or not selection_path.is_file():
        return None

    requested = []
    for raw_line in selection_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        requested.append(parse_selection_line(line))

    if not requested:
        return None

    selected = []
    for item in requested:
        strategy = find_strategy(item["key"])
        if strategy:
            selected.append({**strategy, **item} if include_schedule else strategy)

    missing = [
        item["key"]
        for item in requested
        if not find_strategy(item["key"])
    ]
    for value in missing:
        print(f"No se encontro estrategia para: {value}", file=sys.stderr)

    return selected


def parse_selection_line(line):
    parts = [part.strip() for part in line.split("|")]
    name = parts[0]
    return {
        "key": normalize_key(name),
        "start": parts[1] if len(parts) > 1 and parts[1] else DEFAULT_START_TIME,
        "end": parts[2] if len(parts) > 2 and parts[2] else DEFAULT_END_TIME,
        "interval": parse_int(parts[3], DEFAULT_INTERVAL_MINUTES) if len(parts) > 3 else DEFAULT_INTERVAL_MINUTES,
    }


def find_strategy(key):
    for strategy in STRATEGIES:
        if key in {
            normalize_key(strategy["name"]),
            normalize_key(strategy["file"]),
            normalize_key(Path(strategy["file"]).stem),
            normalize_key(strategy["txt"]),
        }:
            return strategy
    return None


def parse_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def normalize_key(value):
    return "".join(char.lower() for char in str(value) if char.isalnum())


def run_selected_once(strategies=None):
    started_at = datetime.now(UTC)
    print(f"Inicio ejecucion: {started_at.isoformat()}")

    strategies = strategies or selected_strategies()
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


def run_loop():
    schedules = selected_strategy_schedules()
    if not schedules:
        print("No hay estrategias configuradas para ejecutar.")
        return 1

    print("Modo automatico local iniciado.")
    for strategy in schedules:
        print(f"- {strategy['name']} | {strategy['start']} - {strategy['end']} | cada {strategy['interval']} min")

    last_strategy_runs = {}
    last_market_update = None
    run_market_update()
    last_market_update = datetime.now(MADRID_TZ)

    while True:
        now = datetime.now(MADRID_TZ)
        if last_market_update is None or now - last_market_update >= timedelta(minutes=MARKET_UPDATE_INTERVAL_MINUTES):
            run_market_update()
            last_market_update = datetime.now(MADRID_TZ)

        due = due_strategies(schedules, now, last_strategy_runs)
        for strategy in due:
            result_code = run_selected_once([strategy])
            last_strategy_runs[strategy["name"]] = datetime.now(MADRID_TZ)
            if result_code != 0:
                print(f"{strategy['name']} termino con aviso/error, se continua con la siguiente.")

        time.sleep(LOOP_SLEEP_SECONDS)


def due_strategies(schedules, now, last_strategy_runs):
    due = []
    for strategy in schedules:
        if not within_time_window(now, strategy["start"], strategy["end"]):
            continue
        last_run = last_strategy_runs.get(strategy["name"])
        if last_run is None or now - last_run >= timedelta(minutes=strategy["interval"]):
            due.append(strategy)
    return due


def within_time_window(now, start_value, end_value):
    start = time_for_today(now, start_value)
    end = time_for_today(now, end_value)
    if end < start:
        end += timedelta(days=1)
    return start <= now <= end


def time_for_today(now, value):
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
    except (TypeError, ValueError):
        hour, minute = 15, 30
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_market_update():
    if os.environ.get("LOCAL_SKIP_MARKET_UPDATE") == "1":
        print("Actualizacion de mercado omitida por LOCAL_SKIP_MARKET_UPDATE=1.")
        return
    if not MARKET_UPDATE_SCRIPT.exists():
        print("Actualizacion de mercado omitida: no existe run_local_market_update.py")
        return

    print("\n=== Actualizando activos y mercado desde local ===")
    completed = subprocess.run(
        [sys.executable, str(MARKET_UPDATE_SCRIPT), "--all"],
        cwd=str(PROJECT_DIR),
        text=True,
    )
    if completed.returncode != 0:
        print(f"Actualizacion de mercado con aviso/error. Codigo {completed.returncode}. Se continua.")


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


def main():
    parser = argparse.ArgumentParser(description="Ejecuta estrategias locales y sincroniza PostgreSQL.")
    parser.add_argument("--loop", action="store_true", help="Modo automatico: respeta horarios e intervalos del TXT.")
    args = parser.parse_args()
    if args.loop:
        return run_loop()
    return run_selected_once()


if __name__ == "__main__":
    raise SystemExit(main())
