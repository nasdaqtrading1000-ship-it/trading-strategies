"""
Ejecuta todas las estrategias de esta carpeta.

Ultima actualizacion del runner: 2026-06-15 09:44:27 Europe/Madrid.

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

from env_loader import load_env


load_env()

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

STATUS_FILE = BASE_DIR / "strategy_run_status.json"
OUTPUT_DIR = BASE_DIR / "salidas_txt"
LOG_DIR = BASE_DIR / "logs"
SELECTION_FILE = BASE_DIR / "estrategias_a_ejecutar.txt"
CONFIG_FILE = BASE_DIR / "runner_config.txt"
TICKER_GENERATOR_SCRIPT = BASE_DIR / "generate_tickers.py"
SIMULATE_OPERATIONS_SCRIPT = BASE_DIR / "simulate_operations.py"
MADRID_TZ = ZoneInfo("Europe/Madrid")
DEFAULT_START_TIME = "15:30"
DEFAULT_END_TIME = "22:00"
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_LOOP_SLEEP_SECONDS = 30


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
    {"name": "Follow The Money", "file": "FollowTheMoney.py", "txt": "Follow_The_Money.txt"},
    {"name": "Acumula Metales", "file": "AcumulaMetales.py", "txt": "Acumula_Metales.txt"},
    {"name": "Acumulacion", "file": "Acumulacion.py", "txt": "Acumulacion.txt"},
    {"name": "Reversion RSI 5", "file": "ReversionRSI5.py", "txt": "Reversion_RSI_5.txt"},
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
    log_path = daily_log_path()
    completed = run_command_with_tee(
        [sys.executable, str(path)],
        cwd=BASE_DIR,
        log_path=log_path,
        title=f"Estrategia: {strategy['name']} | Archivo: {filename}",
        output_prefix=strategy["name"],
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


def daily_log_path():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"trading_log_{datetime.now(MADRID_TZ).strftime('%Y-%m-%d')}.txt"


def run_command_with_tee(command, cwd, log_path, title="", output_prefix=""):
    """
    Ejecuta un comando mostrando su salida en pantalla y anadiendola al TXT diario.
    """
    started_at = datetime.now(MADRID_TZ)
    header = [
        "=" * 90,
        title or "Ejecucion",
        f"Inicio: {started_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Comando: {' '.join(str(part) for part in command)}",
        f"Carpeta: {cwd}",
        "=" * 90,
        "",
    ]

    files = [log_path.open("a", encoding="utf-8", errors="replace")]

    try:
        for line in header:
            write_tee_line(line, files)

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip("\n")
            if output_prefix and text:
                text = f"{output_prefix} | {text}"
            write_tee_line(text, files)

        returncode = process.wait()
        finished_at = datetime.now(MADRID_TZ)
        footer = [
            "",
            "=" * 90,
            f"Fin: {finished_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"Duracion: {finished_at - started_at}",
            f"Codigo de salida: {returncode}",
            f"Log guardado en: {log_path}",
            "=" * 90,
        ]
        for line in footer:
            write_tee_line(line, files)

        return subprocess.CompletedProcess(command, returncode)
    finally:
        for file in files:
            file.close()


def write_tee_line(line, files):
    print(line, flush=True)
    for file in files:
        file.write(f"{line}\n")
        file.flush()


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


def write_running_status_file(strategy, started_at):
    status = {
        "started_at": started_at.isoformat(),
        "finished_at": "",
        "strategies": {
            strategy["name"]: {
                "file": strategy["file"],
                "txt": strategy["txt"],
                "running": True,
                "ok": False,
                "txt_updated": False,
                "returncode": None,
                "error": "",
                "log": "",
                "ran_at": "",
            }
        },
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def selected_strategies():
    active_names_raw = os.environ.get("TRADING_ACTIVE_STRATEGIES")
    if active_names_raw is None:
        selected_from_file = selected_strategies_from_file()
        selected = selected_from_file if selected_from_file is not None else STRATEGIES
        return filter_by_database_active_strategies(selected)

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


def filter_by_database_active_strategies(strategies):
    active_names = active_strategy_names_from_database()
    if active_names is None:
        return strategies

    filtered = [
        strategy
        for strategy in strategies
        if strategy["name"] in active_names
    ]
    skipped = [
        strategy["name"]
        for strategy in strategies
        if strategy["name"] not in active_names
    ]
    if skipped:
        print(
            "Estrategias omitidas por estar inactivas en PostgreSQL/admin: "
            + ", ".join(skipped),
            flush=True,
        )
    return filtered


def active_strategy_names_from_database():
    if os.environ.get("TRADING_RESPECT_DB_ACTIVE", "1").lower() in {"0", "false", "no"}:
        return None
    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        from sqlalchemy import text
        from db import engine

        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT name FROM strategies WHERE is_active = 1")
            ).mappings().fetchall()
        return {row["name"] for row in rows}
    except Exception as error:
        print(
            f"No se pudieron leer estrategias activas de PostgreSQL/admin: {error}. "
            "Se usa la seleccion local.",
            file=sys.stderr,
            flush=True,
        )
        return None


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
        "start": normalize_time_value(parts[1] if len(parts) > 1 and parts[1] else DEFAULT_START_TIME, DEFAULT_START_TIME),
        "end": normalize_time_value(parts[2] if len(parts) > 2 and parts[2] else DEFAULT_END_TIME, DEFAULT_END_TIME),
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


def load_runner_config():
    config = {
        "modo": "once",
        "dias": "1,2,3,4,5",
        "hora_global_inicio": DEFAULT_START_TIME,
        "hora_global_fin": DEFAULT_END_TIME,
        "ignorar_horarios": "False",
        "generar_tickers": "si",
        "espera_segundos": str(DEFAULT_LOOP_SLEEP_SECONDS),
    }
    if CONFIG_FILE.exists() and CONFIG_FILE.is_file():
        for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            if key:
                config[normalize_config_key(key)] = value
    config["hora_global_inicio"] = normalize_time_value(config.get("hora_global_inicio"), DEFAULT_START_TIME)
    config["hora_global_fin"] = normalize_time_value(config.get("hora_global_fin"), DEFAULT_END_TIME)
    return config


def normalize_config_key(value):
    return str(value).strip().lower()


def config_bool(config, key, default=True):
    value = str(config.get(key, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "si", "sí", "s", "true", "yes", "on"}


def config_int(config, key, default):
    return parse_int(config.get(key), default)


def normalize_time_value(value, default):
    raw = str(value or "").strip()
    if not raw:
        raw = default
    try:
        hour, minute = [int(part.strip()) for part in raw.split(":", 1)]
    except (TypeError, ValueError):
        hour, minute = [int(part) for part in default.split(":", 1)]
    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return f"{hour:02d}:{minute:02d}"


def config_days(config):
    days = set()
    for part in str(config.get("dias", "1,2,3,4,5")).split(","):
        try:
            day = int(part.strip())
        except ValueError:
            continue
        if 1 <= day <= 7:
            days.add(day)
    return days or {1, 2, 3, 4, 5}


def normalize_key(value):
    return "".join(char.lower() for char in str(value) if char.isalnum())


def run_selected_once(strategies=None, prepare_tickers=True, config=None):
    started_at = datetime.now(UTC)
    print(f"Inicio ejecucion: {started_at.isoformat()}")

    config = config or load_runner_config()
    if prepare_tickers and config_bool(config, "generar_tickers", True):
        run_ticker_generation()

    print("Generador revisado. Leyendo estrategias seleccionadas...", flush=True)
    strategies = strategies or selected_strategies()
    print(f"Estrategias seleccionadas: {len(strategies)}")
    for strategy in strategies:
        print(f"- {strategy['name']}")

    results = []
    for strategy in strategies:
        write_running_status_file(strategy, started_at)
        sync_to_database()
        result = run_strategy_safely(strategy)
        results.append(result)
        write_status_file(results, started_at, datetime.now(UTC))
        sync_to_database()

    finished_at = datetime.now(UTC)
    write_status_file(results, started_at, finished_at)
    run_simulated_operations()
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


def run_loop(config=None):
    config = config or load_runner_config()
    schedules = selected_strategy_schedules()
    if not schedules:
        print("No hay estrategias configuradas para ejecutar.")
        return 1

    start_time = config.get("hora_global_inicio", DEFAULT_START_TIME)
    end_time = config.get("hora_global_fin", DEFAULT_END_TIME)
    ignore_hours = config_bool(config, "ignorar_horarios", False)
    allowed_days = config_days(config)
    loop_sleep_seconds = config_int(config, "espera_segundos", DEFAULT_LOOP_SLEEP_SECONDS)

    print("Modo automatico local iniciado.")
    print(f"Configuracion: {CONFIG_FILE}")
    print(f"Ventana global: dias {sorted(allowed_days)}, {start_time} - {end_time} hora Madrid.")
    print(f"Ignorar horarios: {ignore_hours}")
    for strategy in schedules:
        print(f"- {strategy['name']} | {strategy['start']} - {strategy['end']} | cada {strategy['interval']} min")

    if not ignore_hours:
        wait_code = wait_until_global_market_window(start_time, end_time, allowed_days, loop_sleep_seconds)
        if wait_code is not None:
            return wait_code

    last_strategy_runs = {}
    last_idle_message_at = None
    if config_bool(config, "generar_tickers", True):
        run_ticker_generation()

    print("Generador revisado. Entrando en bucle de estrategias...", flush=True)
    while True:
        now = datetime.now(MADRID_TZ)
        if not ignore_hours and not is_allowed_day(now, allowed_days):
            print("Fin de ejecucion: ya no es dia laborable. Saliendo con codigo 0.")
            return 0
        if not ignore_hours and now > time_for_today(now, end_time):
            print(f"Fin de ejecucion: pasada la hora limite {end_time}. Saliendo con codigo 0.")
            return 0

        due = due_strategies(schedules, now, last_strategy_runs, ignore_hours=ignore_hours)
        if not due and should_print_idle_message(now, last_idle_message_at):
            next_due = next_strategy_due_text(schedules, now, last_strategy_runs, ignore_hours=ignore_hours)
            print(f"Sin estrategias pendientes ahora. {next_due}")
            last_idle_message_at = now

        for strategy in due:
            result_code = run_selected_once([strategy], prepare_tickers=False, config=config)
            last_strategy_runs[strategy["name"]] = datetime.now(MADRID_TZ)
            if result_code != 0:
                print(f"{strategy['name']} termino con aviso/error, se continua con la siguiente.")

        if due:
            next_due = next_strategy_due_text(schedules, datetime.now(MADRID_TZ), last_strategy_runs, ignore_hours=ignore_hours)
            print(f"Pasada terminada. {next_due}", flush=True)

        time.sleep(loop_sleep_seconds)


def wait_until_global_market_window(start_value, end_value, allowed_days, loop_sleep_seconds):
    now = datetime.now(MADRID_TZ)
    if not is_allowed_day(now, allowed_days):
        print("Hoy no es dia de mercado para el runner local. Saliendo con codigo 0.")
        return 0

    start = time_for_today(now, start_value)
    end = time_for_today(now, end_value)
    if now > end:
        print(f"Hora actual posterior a {end_value}. Saliendo con codigo 0.")
        return 0

    while now < start:
        seconds = max(1, int((start - now).total_seconds()))
        sleep_for = min(seconds, loop_sleep_seconds)
        print(f"Esperando a la apertura global {start_value}. Faltan {seconds} segundos.")
        time.sleep(sleep_for)
        now = datetime.now(MADRID_TZ)

    return None


def is_allowed_day(value, allowed_days):
    return value.isoweekday() in allowed_days


def due_strategies(schedules, now, last_strategy_runs, ignore_hours=False):
    due = []
    for strategy in schedules:
        if not ignore_hours and not within_time_window(now, strategy["start"], strategy["end"]):
            continue
        last_run = last_strategy_runs.get(strategy["name"])
        if last_run is None or now - last_run >= timedelta(minutes=strategy["interval"]):
            due.append(strategy)
    return due


def should_print_idle_message(now, last_idle_message_at):
    if last_idle_message_at is None:
        return True
    return now - last_idle_message_at >= timedelta(minutes=5)


def next_strategy_due_text(schedules, now, last_strategy_runs, ignore_hours=False):
    next_times = []
    for strategy in schedules:
        next_time = next_due_time_for_strategy(strategy, now, last_strategy_runs, ignore_hours=ignore_hours)
        if next_time is not None:
            next_times.append((next_time, strategy["name"]))
    if not next_times:
        return "No hay proxima ejecucion dentro de la ventana configurada."

    next_time, strategy_name = min(next_times, key=lambda item: item[0])
    return f"Proxima: {strategy_name} a las {next_time.strftime('%H:%M:%S')}."


def next_due_time_for_strategy(strategy, now, last_strategy_runs, ignore_hours=False):
    if ignore_hours:
        last_run = last_strategy_runs.get(strategy["name"])
        if last_run is None:
            return now
        return max(now, last_run + timedelta(minutes=strategy["interval"]))

    start = time_for_today(now, strategy["start"])
    end = time_for_today(now, strategy["end"])
    if end < start:
        end += timedelta(days=1)
    if now < start:
        return start
    if now > end:
        return None

    last_run = last_strategy_runs.get(strategy["name"])
    if last_run is None:
        return now
    next_time = last_run + timedelta(minutes=strategy["interval"])
    if next_time <= end:
        return max(now, next_time)
    return None


def within_time_window(now, start_value, end_value):
    start = time_for_today(now, start_value)
    end = time_for_today(now, end_value)
    if end < start:
        end += timedelta(days=1)
    return start <= now <= end


def time_for_today(now, value):
    normalized = normalize_time_value(value, DEFAULT_START_TIME)
    hour, minute = [int(part) for part in normalized.split(":", 1)]
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_ticker_generation():
    if os.environ.get("LOCAL_SKIP_TICKER_GENERATION") == "1":
        print("Generador de tickers omitido por LOCAL_SKIP_TICKER_GENERATION=1.")
        return
    if not TICKER_GENERATOR_SCRIPT.exists():
        print("Generador de tickers omitido: no existe generate_tickers.py")
        return

    print("\n=== Generando tickers filtrados desde Alpaca ===")
    command = [sys.executable, str(TICKER_GENERATOR_SCRIPT)]
    extra_args = os.environ.get("LOCAL_TICKER_GENERATOR_ARGS", "").strip()
    if extra_args:
        command.extend(extra_args.split())

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    completed = run_command_with_tee(
        command,
        cwd=BASE_DIR,
        log_path=daily_log_path(),
        title="Generador de tickers filtrados",
    )
    if completed.returncode != 0:
        print(f"Generador de tickers con aviso/error. Codigo {completed.returncode}. Se continua con el tickers.txt existente.")
    else:
        print("Generador de tickers terminado correctamente.")


def sync_to_database():
    if os.environ.get("TRADING_SKIP_DB_SYNC") == "1":
        print("Sincronizacion PostgreSQL omitida por TRADING_SKIP_DB_SYNC=1.")
        return

    sync_script = BASE_DIR.parent / "sync_signals_to_db.py"
    if not sync_script.exists():
        print("Sincronizacion PostgreSQL omitida: no existe sync_signals_to_db.py")
        return

    print("\n=== Sincronizando datos con PostgreSQL ===")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    completed = run_command_with_tee(
        [sys.executable, str(sync_script)],
        cwd=BASE_DIR.parent,
        log_path=daily_log_path(),
        title="Sincronizacion PostgreSQL",
    )
    if completed.returncode != 0:
        print(f"Sincronizacion PostgreSQL con avisos: codigo {completed.returncode}", file=sys.stderr)
    else:
        print("Sincronizacion PostgreSQL terminada correctamente.")


def run_simulated_operations():
    if os.environ.get("TRADING_SKIP_SIMULATION") == "1":
        print("Revision de operaciones omitida por TRADING_SKIP_SIMULATION=1.")
        return
    if not SIMULATE_OPERATIONS_SCRIPT.exists():
        print("Revision de operaciones omitida: no existe simulate_operations.py")
        return

    print("\n=== Revisando operaciones ===")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    completed = run_command_with_tee(
        [sys.executable, str(SIMULATE_OPERATIONS_SCRIPT)],
        cwd=BASE_DIR,
        log_path=daily_log_path(),
        title="Revision de operaciones",
    )
    if completed.returncode != 0:
        print(f"Revision de operaciones con avisos: codigo {completed.returncode}", file=sys.stderr)
    else:
        print("Revision de operaciones terminada correctamente.")


def main():
    parser = argparse.ArgumentParser(description="Ejecuta estrategias locales y sincroniza PostgreSQL.")
    parser.add_argument("--loop", action="store_true", help="Fuerza modo automatico.")
    parser.add_argument("--once", action="store_true", help="Fuerza una sola ejecucion.")
    args = parser.parse_args()
    config = load_runner_config()
    mode = str(config.get("modo", "once")).strip().lower()
    if args.once:
        return run_selected_once(config=config)
    if args.loop or mode == "loop":
        return run_loop(config=config)
    return run_selected_once(config=config)


if __name__ == "__main__":
    raise SystemExit(main())
