import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from hmac import compare_digest
from functools import wraps
from uuid import uuid4
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    abort,
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from db import SQLITE_DATABASE, engine
from market_scanner import (
    available_markets,
    available_sectors,
    csv_updated_at,
    filter_assets,
    load_assets,
    load_universe_assets,
    save_universe_assets,
    snapshot_count,
    universe_count,
    ensure_universe_table,
)
from update_market_data import update_market_data
from update_assets import build_assets_from_alpaca, write_assets


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SIGNALS_DIR = (BASE_DIR / "Estrategias" / "salidas_txt").resolve()
DEFAULT_STRATEGY_STATUS_FILE = (BASE_DIR / "Estrategias" / "strategy_run_status.json").resolve()
STRATEGIES_RUNNER = BASE_DIR / "Estrategias" / "run_all_strategies.py"
MADRID_TZ = ZoneInfo("Europe/Madrid")
SCHEDULER_THREAD_STARTED = False
SCHEDULER_LOCK = threading.Lock()
SCHEDULER_TASKS = {
    "assets_csv": "Actualizar CSV de activos",
    "market_batch": "Actualizar mercado por tanda",
    "market_full": "Actualizar mercado completo",
    "strategies": "Ejecutar estrategias",
}
WEEKDAYS = [
    (1, "Lun"),
    (2, "Mar"),
    (3, "Mie"),
    (4, "Jue"),
    (5, "Vie"),
    (6, "Sab"),
    (7, "Dom"),
]
DEFAULT_WEEKDAYS = "1,2,3,4,5"
DEFAULT_STRATEGY_FILES = {
    "Momentum": "Momentum.py",
    "Swing Trading": "SwingTrading.py",
    "BreaKout": "BreaKout.py",
    "Mean Reversion": "Mean Reversion.py",
    "Value Trading": "ValueTrading.py",
    "Dividend Growth": "DividenGrowth.py",
    "Trend Following": "TrendFollowing.py",
    "Pairs Trading": "PairsTrading.py",
    "Sector Rotation": "SectorRotation.py",
    "Quality Investing": "QualityInvesting.py",
    "Opening Range BreaKout": "OpeningRangeBreaKout.py",
    "VWAP Reversion": "VWAP Reversion.py",
    "Momentum Intradia": "MomentumIntradia.py",
    "Scalping The PullBacks": "ScalpingThePullBacKs.py",
    "Gap and Go": "Gap and Go.py",
}
SIGNAL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,14}$")
SIGNAL_SIDE_WORDS = {"LONG", "SHORT", "BUY", "SELL", "COMPRA", "VENTA"}
DEFAULT_REAL_STRATEGIES = [
    {
        "name": "Momentum",
        "description": "Compra activos con fuerza relativa alta, tendencia alcista y buen comportamiento frente al mercado.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / swing",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_momentum",
        "signals_txt_name": "Momentum.txt",
    },
    {
        "name": "Swing Trading",
        "description": "Busca entradas de varios dias en activos con tendencia sana, retrocesos controlados y confirmacion tecnica.",
        "risk_level": "Medio",
        "signal_frequency": "Varias senales por semana",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_swing_trading",
        "signals_txt_name": "SwingTrading.txt",
    },
    {
        "name": "BreaKout",
        "description": "Detecta rupturas de resistencia con aumento de volumen, expansion de rango y precio cerca de maximos relevantes.",
        "risk_level": "Alto",
        "signal_frequency": "Segun rupturas de mercado",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_breakout",
        "signals_txt_name": "BreaKout.txt",
    },
    {
        "name": "Mean Reversion",
        "description": "Busca activos sobrevendidos o alejados de su media que puedan volver a niveles normales.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_mean_reversion",
        "signals_txt_name": "Mean_Reversion.txt",
    },
    {
        "name": "Value Trading",
        "description": "Filtra companias con valoracion atractiva, fundamentales razonables y descuento relativo.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_value_trading",
        "signals_txt_name": "ValueTrading.txt",
    },
    {
        "name": "Dividend Growth",
        "description": "Selecciona companias con crecimiento de dividendos, estabilidad financiera y perfil defensivo.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_dividend_growth",
        "signals_txt_name": "DividenGrowth.txt",
    },
    {
        "name": "Trend Following",
        "description": "Sigue tendencias establecidas mediante medias, momentum y confirmacion de precio.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_trend_following",
        "signals_txt_name": "TrendFollowing.txt",
    },
    {
        "name": "Pairs Trading",
        "description": "Analiza pares correlacionados y busca desviaciones estadisticas para operar convergencia.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_pairs_trading",
        "signals_txt_name": "PairsTrading.txt",
    },
    {
        "name": "Sector Rotation",
        "description": "Compara fuerza relativa por sectores y propone activos lideres dentro de los sectores fuertes.",
        "risk_level": "Medio",
        "signal_frequency": "Semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_sector_rotation",
        "signals_txt_name": "SectorRotation.txt",
    },
    {
        "name": "Quality Investing",
        "description": "Busca empresas de calidad con buenos margenes, crecimiento y estabilidad financiera.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_quality_investing",
        "signals_txt_name": "QualityInvesting.txt",
    },
    {
        "name": "Opening Range BreaKout",
        "description": "Estrategia intradia que espera la ruptura del rango inicial de la sesion con volumen.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_opening_range_breakout",
        "signals_txt_name": "OpeningRangeBreaKout.txt",
    },
    {
        "name": "VWAP Reversion",
        "description": "Busca reversiones intradia hacia VWAP cuando el precio se aleja demasiado.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_vwap_reversion",
        "signals_txt_name": "VWAP_Reversion.txt",
    },
    {
        "name": "Momentum Intradia",
        "description": "Detecta movimientos fuertes dentro de la sesion usando momentum reciente, VWAP y volumen relativo.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_momentum_intradia",
        "signals_txt_name": "MomentumIntradia.txt",
    },
    {
        "name": "Scalping The PullBacks",
        "description": "Busca pequenos retrocesos dentro de una tendencia intradia para entrar a favor del movimiento principal.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / frecuente",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_scalping_pullbacks",
        "signals_txt_name": "ScalpingThePullBacKs.txt",
    },
    {
        "name": "Gap and Go",
        "description": "Detecta activos que abren con gap relevante y continuan en la direccion del impulso.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / apertura",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_gap_and_go",
        "signals_txt_name": "Gap_and_Go.txt",
    },
]


def database_status():
    url = engine.url
    return {
        "dialect": engine.dialect.name,
        "database": url.database or "",
        "host": url.host or "local file",
        "is_persistent": engine.dialect.name == "postgresql",
    }


def run_scheduler_task(task_name):
    if task_name == "assets_csv":
        rows, source = build_assets_from_alpaca()
        write_assets(rows)
        save_universe_assets(rows)
        return {
            "ok": True,
            "message": f"CSV actualizado: {len(rows)} activos. Fuente: {source}.",
        }

    if task_name == "market_batch":
        result = update_market_data(full=False)
        return {
            "ok": bool(result.get("ok")),
            "message": f"Tanda mercado. Guardados: {result.get('saved_rows', 0)}. Error: {result.get('last_error', '') or 'Sin error'}.",
        }

    if task_name == "market_full":
        result = update_market_data(full=True)
        return {
            "ok": bool(result.get("ok")),
            "message": f"Mercado completo. Guardados: {result.get('saved_rows', 0)}. Error: {result.get('last_error', '') or 'Sin error'}.",
        }

    if task_name == "strategies":
        if not STRATEGIES_RUNNER.exists():
            return {"ok": False, "message": "No se encontro run_all_strategies.py."}
        active_strategy_names = active_strategy_names_for_runner()
        mark_strategies_as_running_file()
        timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
        env = os.environ.copy()
        env["TRADING_ACTIVE_STRATEGIES"] = json.dumps(active_strategy_names)
        try:
            completed = subprocess.run(
                [sys.executable, str(STRATEGIES_RUNNER)],
                cwd=str(STRATEGIES_RUNNER.parent),
                text=True,
                capture_output=True,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "message": f"Estrategias canceladas por superar {timeout_seconds} segundos.",
            }
        summary = strategy_runner_summary(completed.returncode)
        if completed.returncode == 0:
            return {
                "ok": True,
                "message": summary,
            }
        return {
            "ok": False,
            "message": summary,
        }

    return {"ok": False, "message": "Tarea no reconocida."}


def strategy_runner_summary(returncode):
    try:
        data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if returncode == 0:
            return "Estrategias finalizadas correctamente."
        return f"Estrategias con error. Codigo {returncode}. Revisa Fallos de estrategias."

    results = data.get("strategies", {})
    ok_count = sum(1 for item in results.values() if item.get("ok"))
    fail_count = sum(1 for item in results.values() if not item.get("ok"))
    if fail_count:
        return (
            f"Estrategias finalizadas con {fail_count} fallos. "
            f"Correctas: {ok_count}. Revisa Fallos de estrategias."
        )
    return f"Estrategias finalizadas correctamente. Correctas: {ok_count}."


def run_single_strategy(strategy):
    py_file = (strategy.get("python_file") or "").strip()
    if not py_file:
        return {"ok": False, "message": "La estrategia no tiene archivo Python asociado."}
    if not valid_python_filename(py_file):
        return {"ok": False, "message": "Archivo Python no valido."}

    strategies_dir = (BASE_DIR / "Estrategias").resolve()
    path = (strategies_dir / py_file).resolve()
    if strategies_dir not in path.parents:
        return {"ok": False, "message": "Ruta de estrategia no permitida."}
    if not path.exists() or not path.is_file():
        return {"ok": False, "message": f"No existe {py_file}."}

    mark_single_strategy_status(strategy, running=True)
    timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(path.parent),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        result = {
            "ok": False,
            "message": f"{strategy['name']} cancelada por superar {timeout_seconds} segundos.",
            "returncode": None,
        }
        mark_single_strategy_status(strategy, running=False, result=result)
        return result

    output = "\n".join(
        part.strip()
        for part in [completed.stdout, completed.stderr]
        if part and part.strip()
    )
    result = {
        "ok": completed.returncode == 0,
        "message": (
            f"{strategy['name']} finalizada correctamente."
            if completed.returncode == 0
            else f"{strategy['name']} fallo. Codigo {completed.returncode}. {output[-700:]}"
        ),
        "returncode": completed.returncode,
    }
    mark_single_strategy_status(strategy, running=False, result=result)
    return result


def mark_single_strategy_status(strategy, running=False, result=None):
    now = datetime.now(UTC).isoformat()
    data = {}
    try:
        if DEFAULT_STRATEGY_STATUS_FILE.exists():
            data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}

    data.setdefault("strategies", {})
    item = {
        "file": strategy.get("python_file", ""),
        "txt": strategy.get("signals_txt_name", ""),
        "ok": False,
        "txt_updated": False,
        "returncode": None,
        "error": "",
        "ran_at": now,
    }
    if running:
        item["running"] = True
        data["started_at"] = now
        data["finished_at"] = ""
    else:
        result = result or {"ok": False, "message": "Sin resultado.", "returncode": None}
        item["running"] = False
        item["ok"] = bool(result.get("ok"))
        item["returncode"] = result.get("returncode")
        item["error"] = "" if result.get("ok") else result.get("message", "")
        data["finished_at"] = now

    data["strategies"][strategy["name"]] = item
    DEFAULT_STRATEGY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STRATEGY_STATUS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def active_strategy_names_for_runner():
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE is_active = 1 ORDER BY name")
        ).mappings().fetchall()
    return [row["name"] for row in rows]


def mark_strategies_as_running_file():
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    names = []
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE is_active = 1 ORDER BY name")
        ).mappings().fetchall()
        names = [row["name"] for row in rows]

    payload = {
        "started_at": now,
        "finished_at": "",
        "running": True,
        "strategies": {
            name: {
                "file": "",
                "ok": False,
                "running": True,
                "returncode": None,
                "error": "",
                "ran_at": now,
            }
            for name in names
        },
    }
    DEFAULT_STRATEGY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STRATEGY_STATUS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def start_scheduler_thread():
    global SCHEDULER_THREAD_STARTED
    if os.environ.get("DISABLE_INTERNAL_SCHEDULER") == "1":
        return
    with SCHEDULER_LOCK:
        if SCHEDULER_THREAD_STARTED:
            return
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
        SCHEDULER_THREAD_STARTED = True


def scheduler_loop():
    while True:
        try:
            process_due_schedules()
        except Exception as error:
            print(f"[scheduler] Error: {error}", flush=True)
        time.sleep(60)


def process_due_schedules(background=True):
    now = datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        schedules = connection.execute(
            text(
                """
                SELECT *
                FROM automation_schedules
                WHERE is_enabled = 1
                ORDER BY task_name
                """
            )
        ).mappings().fetchall()

    for schedule in schedules:
        due_key = due_schedule_key(schedule, now)
        if not due_key or due_key == schedule["last_run_key"]:
            continue
        record_schedule_running(schedule["task_name"], due_key, now)
        if background:
            launch_scheduler_task_in_background(schedule["task_name"], due_key)
        else:
            try:
                result = run_scheduler_task(schedule["task_name"])
            except Exception as error:
                result = {"ok": False, "message": f"Error ejecutando tarea: {error}"}
            record_schedule_result(schedule["task_name"], due_key, result, now)

    process_due_strategy_schedules(now, background=background)


def process_due_strategy_schedules(now, background=True):
    with engine.begin() as connection:
        strategies = connection.execute(
            text(
                """
                SELECT *
                FROM strategies
                WHERE is_active = 1
                  AND auto_execute = 1
                ORDER BY name
                """
            )
        ).mappings().fetchall()

    for strategy in strategies:
        due_key = due_strategy_schedule_key(strategy, now)
        if not due_key or due_key == strategy["schedule_last_run_key"]:
            continue
        record_strategy_schedule_running(strategy["id"], due_key, now)
        if background:
            launch_strategy_task_in_background(dict(strategy), due_key)
        else:
            try:
                result = run_single_strategy(strategy)
            except Exception as error:
                result = {"ok": False, "message": f"Error ejecutando estrategia: {error}"}
            record_strategy_schedule_result(strategy["id"], due_key, result, now)


def record_schedule_result(task_name, run_key, result, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_run_key = :last_run_key,
                    last_run_at = :last_run_at,
                    last_status = :last_status,
                    last_message = :last_message
                WHERE task_name = :task_name
                """
            ),
            {
                "last_run_key": run_key,
                "last_run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
                "last_status": "OK" if result["ok"] else "ERROR",
                "last_message": result["message"][:1000],
                "task_name": task_name,
            },
        )


def record_schedule_running(task_name, run_key, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_run_key = :last_run_key,
                    last_run_at = :last_run_at,
                    last_status = 'RUNNING',
                    last_message = 'En ejecucion'
                WHERE task_name = :task_name
                """
            ),
            {
                "last_run_key": run_key,
                "last_run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
                "task_name": task_name,
            },
        )


def launch_scheduler_task_in_background(task_name, run_key):
    def worker():
        try:
            result = run_scheduler_task(task_name)
        except Exception as error:
            result = {"ok": False, "message": f"Error ejecutando tarea: {error}"}
        record_schedule_result(task_name, run_key, result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def launch_strategy_task_in_background(strategy, run_key):
    def worker():
        try:
            result = run_single_strategy(strategy)
        except Exception as error:
            result = {"ok": False, "message": f"Error ejecutando estrategia: {error}"}
        record_strategy_schedule_result(strategy["id"], run_key, result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def record_strategy_schedule_running(strategy_id, run_key, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_run_key = :run_key,
                    schedule_last_run_at = :last_run_at,
                    schedule_last_status = 'RUNNING',
                    schedule_last_message = 'En ejecucion'
                WHERE id = :id
                """
            ),
            {
                "run_key": run_key,
                "last_run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
                "id": strategy_id,
            },
        )


def record_strategy_schedule_result(strategy_id, run_key, result, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_run_key = :run_key,
                    schedule_last_run_at = :last_run_at,
                    schedule_last_status = :status,
                    schedule_last_message = :message
                WHERE id = :id
                """
            ),
            {
                "run_key": run_key,
                "last_run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
                "status": "OK" if result.get("ok") else "ERROR",
                "message": result.get("message", "")[:1000],
                "id": strategy_id,
            },
        )


def due_schedule_key(schedule, now):
    try:
        hour, minute = [int(part) for part in str(schedule["start_time"]).split(":", 1)]
        runs_per_day = max(1, int(schedule["runs_per_day"]))
        interval_minutes = max(1, int(schedule["interval_minutes"]))
    except (TypeError, ValueError):
        return ""

    allowed_days = parse_weekdays(schedule.get("weekdays", DEFAULT_WEEKDAYS))
    if now.isoweekday() not in allowed_days:
        return ""

    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    grace_minutes = max(5, min(interval_minutes, 60))
    latest_due_key = ""
    for index in range(runs_per_day):
        planned = start + timedelta(minutes=interval_minutes * index)
        if planned.date() == now.date() and planned <= now < planned + timedelta(minutes=grace_minutes):
            return f"{now.date().isoformat()}|{schedule['task_name']}|{index}"
        if planned.date() == now.date() and planned <= now:
            latest_due_key = f"{now.date().isoformat()}|{schedule['task_name']}|{index}"
    if latest_due_key and schedule["last_run_key"] != latest_due_key:
        return latest_due_key
    return ""


def due_strategy_schedule_key(strategy, now):
    if now.isoweekday() not in parse_weekdays(DEFAULT_WEEKDAYS):
        return ""
    try:
        start_hour, start_minute = [int(part) for part in str(strategy["schedule_start_time"]).split(":", 1)]
        end_hour, end_minute = [int(part) for part in str(strategy["schedule_end_time"]).split(":", 1)]
        interval_minutes = max(1, int(strategy["schedule_interval_minutes"]))
    except (TypeError, ValueError):
        return ""

    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end < start:
        end = end + timedelta(days=1)
    if not start <= now <= end:
        return ""

    elapsed_minutes = int((now - start).total_seconds() // 60)
    slot = elapsed_minutes // interval_minutes
    planned = start + timedelta(minutes=slot * interval_minutes)
    grace_minutes = max(5, min(interval_minutes, 60))
    if planned <= now < planned + timedelta(minutes=grace_minutes):
        return f"{now.date().isoformat()}|strategy|{strategy['id']}|{slot}"
    return ""


def parse_weekdays(value):
    days = set()
    for part in str(value or "").split(","):
        try:
            day = int(part)
        except ValueError:
            continue
        if 1 <= day <= 7:
            days.add(day)
    return days or {1, 2, 3, 4, 5}


def valid_python_filename(filename):
    path = Path(filename)
    return (
        path.name == filename
        and filename.lower().endswith(".py")
        and "/" not in filename
        and "\\" not in filename
    )


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
    app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
        "ADMIN_PASSWORD_HASH", generate_password_hash("admin123")
    )

    @app.before_request
    def before_request():
        g.db = engine.connect()
        if public_site_locked():
            return require_site_password()
        track_visitor()

    @app.teardown_request
    def teardown_request(_exception):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not session.get("admin_logged_in"):
                flash("Inicia sesion para acceder al panel.", "warning")
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped_view

    def public_site_locked():
        site_password = os.environ.get("SITE_PASSWORD", "").strip()
        if not site_password:
            return False
        if session.get("site_unlocked"):
            return False
        endpoint = request.endpoint or ""
        path = request.path or ""
        allowed_endpoints = {"static", "site_login", "login", "logout"}
        if endpoint in allowed_endpoints:
            return False
        if path.startswith("/admin"):
            return False
        return True

    def require_site_password():
        if request.method == "GET":
            session["site_next_url"] = request.full_path.rstrip("?") or url_for("index")
        return redirect(url_for("site_login"))

    @app.route("/acceso", methods=["GET", "POST"])
    def site_login():
        site_password = os.environ.get("SITE_PASSWORD", "").strip()
        if not site_password:
            return redirect(url_for("index"))

        if request.method == "POST":
            password = request.form.get("password", "")
            if compare_digest(password, site_password):
                session["site_unlocked"] = True
                next_url = session.pop("site_next_url", url_for("index"))
                flash("Acceso concedido.", "success")
                return redirect(next_url)
            flash("Contrasena incorrecta.", "danger")

        return render_template("site_login.html")

    @app.route("/")
    def index():
        rows = g.db.execute(
            text(
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, has_telegram, signals_txt_name,
                   python_file, auto_execute, schedule_start_time, schedule_end_time,
                   schedule_interval_minutes, is_active
            FROM strategies
            WHERE is_active = 1
            ORDER BY created_at DESC
            """
            )
        ).mappings().fetchall()
        strategies = [
            strategy_with_signals(row)
            for row in rows
        ]
        community_url = os.environ.get("COMMUNITY_URL")
        if not community_url and strategies:
            community_url = strategies[0]["telegram_url"]
        donation_url = os.environ.get("DONATION_URL", "").strip()
        return render_template(
            "index.html",
            strategies=strategies,
            community_url=community_url,
            donation_url=donation_url,
        )

    @app.route("/estrategia/<int:strategy_id>/diagnostico/<path:symbol>")
    def strategy_diagnostic(strategy_id, symbol):
        strategy = get_strategy_or_404(strategy_id)
        signals = read_strategy_signals(strategy["signals_txt_name"])
        normalized_symbol = normalize_signal_symbol(symbol)
        signal = next(
            (
                item
                for item in signals
                if normalize_signal_symbol(item.get("symbol", "")) == normalized_symbol
            ),
            None,
        )
        if signal is None:
            abort(404)

        diagnostic = build_signal_diagnostic(strategy, signal)
        return render_template(
            "strategy_diagnostic.html",
            strategy=strategy,
            signal=signal,
            diagnostic=diagnostic,
        )

    @app.route("/filtrado-activos")
    def asset_filter():
        filters = {
            "month_window": int(request.args.get("month_window", 1)),
            "min_money_volume": int(request.args.get("min_money_volume", 0)),
            "day_volume_window": int(request.args.get("day_volume_window", 1)),
            "week_volume_window": int(request.args.get("week_volume_window", 1)),
            "limit": int(request.args.get("limit", 10)),
            "sector": request.args.get("sector", "Todos"),
            "market": request.args.get("market", "Todos"),
            "data_source": request.args.get("data_source", "database"),
            "sort_by": request.args.get("sort_by", "money_volume_selected"),
        }
        assets = load_universe_assets()
        csv_total = len(assets)
        database_universe_total = universe_count()
        snapshots_total = snapshot_count()
        sectors = available_sectors(assets)
        markets = available_markets(assets)
        results, data_source, universe_total = filter_assets(filters, assets)
        filter_labels = {
            "money_volume": (
                f"Media volumen monetario {filters['month_window']} "
                f"mes{'es' if filters['month_window'] > 1 else ''}"
            ),
            "day_volume": (
                f"Volumen ultimos {filters['day_volume_window']} "
                f"dia{'s' if filters['day_volume_window'] > 1 else ''}"
            ),
            "week_volume": (
                f"Volumen ultimas {filters['week_volume_window']} "
                f"semana{'s' if filters['week_volume_window'] > 1 else ''}"
            ),
            "ratio": (
                f"Ratio volumen {filters['day_volume_window']}d / "
                f"media {filters['month_window']}m"
            ),
        }
        sort_options = [
            ("money_volume_selected", filter_labels["money_volume"]),
            ("day_money_volume_selected", filter_labels["day_volume"]),
            ("week_money_volume_selected", filter_labels["week_volume"]),
            ("day_to_month_volume_ratio", filter_labels["ratio"]),
            ("price", "Precio"),
        ]
        return render_template(
            "asset_filter.html",
            filters=filters,
            filter_labels=filter_labels,
            sort_options=sort_options,
            results=results,
            sectors=sectors,
            markets=markets,
            data_source=data_source,
            universe_total=universe_total,
            csv_total=csv_total,
            database_universe_total=database_universe_total,
            snapshots_total=snapshots_total,
            csv_updated_at=csv_updated_at(),
        )

    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if check_password_hash(app.config["ADMIN_PASSWORD_HASH"], password):
                session["admin_logged_in"] = True
                flash("Sesion iniciada correctamente.", "success")
                return redirect(url_for("admin_dashboard"))
            flash("Contrasena incorrecta.", "danger")

        return render_template("login.html")

    @app.route("/admin/logout", methods=["POST"])
    @login_required
    def logout():
        session.clear()
        flash("Sesion cerrada.", "info")
        return redirect(url_for("index"))

    @app.route("/admin")
    @login_required
    def admin_dashboard():
        strategies = g.db.execute(
            text(
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, has_telegram, signals_txt_name,
                   python_file, auto_execute, schedule_start_time, schedule_end_time,
                   schedule_interval_minutes, schedule_last_status, schedule_last_message,
                   schedule_last_run_at, is_active, created_at
            FROM strategies
            ORDER BY is_active DESC, created_at DESC
            """
            )
        ).mappings().fetchall()
        return render_template(
            "admin/dashboard.html",
            strategies=strategies,
            active_visitors=active_visitor_count(),
            schedules=load_automation_schedules(),
            scheduler_tasks=SCHEDULER_TASKS,
            weekdays=WEEKDAYS,
            strategy_failures=load_strategy_failures(),
        )

    @app.route("/admin/system")
    @login_required
    def admin_system():
        return render_template("admin/system.html", database=database_status())

    @app.route("/admin/market-data/update", methods=["POST"])
    @login_required
    def admin_market_data_update():
        full_update = request.form.get("full_update") == "1"
        result = update_market_data(full=full_update)
        session["last_market_update"] = result
        if result["ok"]:
            flash(
                f"Datos de mercado actualizados correctamente. {result.get('saved_rows', 0)} activos guardados.",
                "success",
            )
        else:
            flash(
                f"No se pudieron actualizar los datos. {result.get('last_error', '')}",
                "danger",
            )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/assets/update-csv", methods=["POST"])
    @login_required
    def admin_assets_update_csv():
        rows, source = build_assets_from_alpaca()
        write_assets(rows)
        save_universe_assets(rows)
        flash(
            f"CSV y universo de activos actualizados correctamente: {len(rows)} activos. Fuente: {source}.",
            "success",
        )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/update", methods=["POST"])
    @login_required
    def admin_schedules_update():
        for task_name in SCHEDULER_TASKS:
            enabled = 1 if request.form.get(f"{task_name}_enabled") == "1" else 0
            start_time = request.form.get(f"{task_name}_start_time", "15:30").strip()
            runs_per_day = parse_schedule_int(
                request.form.get(f"{task_name}_runs_per_day"),
                default=1,
                minimum=1,
                maximum=24,
            )
            interval_minutes = parse_schedule_int(
                request.form.get(f"{task_name}_interval_minutes"),
                default=60,
                minimum=1,
                maximum=1440,
            )
            weekdays = normalize_weekdays(
                request.form.getlist(f"{task_name}_weekdays")
            )
            if not valid_schedule_time(start_time):
                flash(f"Hora no valida para {SCHEDULER_TASKS[task_name]}. Usa formato HH:MM.", "danger")
                return redirect(url_for("admin_dashboard"))

            g.db.execute(
                text(
                    """
                    UPDATE automation_schedules
                    SET is_enabled = :is_enabled,
                        start_time = :start_time,
                        runs_per_day = :runs_per_day,
                        interval_minutes = :interval_minutes,
                        weekdays = :weekdays,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_name = :task_name
                    """
                ),
                {
                    "task_name": task_name,
                    "is_enabled": enabled,
                    "start_time": start_time,
                    "runs_per_day": runs_per_day,
                    "interval_minutes": interval_minutes,
                    "weekdays": weekdays,
                },
            )
        g.db.commit()
        flash("Programacion automatica guardada.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/<task_name>/run-now", methods=["POST"])
    @login_required
    def admin_schedule_run_now(task_name):
        if task_name not in SCHEDULER_TASKS:
            abort(404)
        run_key = f"manual|{task_name}|{datetime.now(MADRID_TZ).isoformat()}"
        record_schedule_running(task_name, run_key)
        launch_scheduler_task_in_background(task_name, run_key)
        flash(f"{SCHEDULER_TASKS[task_name]} iniciado. Refresca el panel para ver si termina en OK o ERROR.", "info")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/<task_name>/clear-running", methods=["POST"])
    @login_required
    def admin_schedule_clear_running(task_name):
        if task_name not in SCHEDULER_TASKS:
            abort(404)
        g.db.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_status = 'ERROR',
                    last_message = :message
                WHERE task_name = :task_name
                  AND last_status = 'RUNNING'
                """
            ),
            {
                "task_name": task_name,
                "message": "Ejecucion marcada como bloqueada y limpiada manualmente desde admin.",
            },
        )
        g.db.commit()
        flash(f"Estado RUNNING limpiado para {SCHEDULER_TASKS[task_name]}.", "warning")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/new", methods=["GET", "POST"])
    @login_required
    def strategy_new():
        if request.method == "POST":
            return save_strategy()
        return render_template(
            "admin/form.html",
            strategy=None,
            title="Crear estrategia",
            action=url_for("strategy_new"),
        )

    @app.route("/admin/strategies/<int:strategy_id>/edit", methods=["GET", "POST"])
    @login_required
    def strategy_edit(strategy_id):
        strategy = get_strategy_or_404(strategy_id)
        if request.method == "POST":
            return save_strategy(strategy_id)

        return render_template(
            "admin/form.html",
            strategy=strategy,
            title="Modificar estrategia",
            action=url_for("strategy_edit", strategy_id=strategy_id),
        )

    @app.route("/admin/strategies/<int:strategy_id>/toggle", methods=["POST"])
    @login_required
    def strategy_toggle(strategy_id):
        strategy = get_strategy_or_404(strategy_id)
        next_state = 0 if strategy["is_active"] else 1
        g.db.execute(
            text("UPDATE strategies SET is_active = :is_active WHERE id = :id"),
            {"is_active": next_state, "id": strategy_id},
        )
        g.db.commit()
        flash("Estado actualizado.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/deactivate-all", methods=["POST"])
    @login_required
    def strategies_deactivate_all():
        g.db.execute(text("UPDATE strategies SET is_active = 0 WHERE is_active = 1"))
        g.db.commit()
        flash("Todas las estrategias han sido desactivadas.", "warning")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/run", methods=["POST"])
    @login_required
    def strategy_run_now(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not strategy["is_active"]:
            flash("Activa la estrategia antes de ejecutarla.", "warning")
            return redirect(url_for("admin_dashboard"))
        run_key = f"manual|strategy|{strategy_id}|{datetime.now(MADRID_TZ).isoformat()}"
        record_strategy_schedule_running(strategy_id, run_key)
        launch_strategy_task_in_background(strategy, run_key)
        flash(f"{strategy['name']} iniciada. Refresca el panel para ver el resultado.", "info")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/delete", methods=["POST"])
    @login_required
    def strategy_delete(strategy_id):
        get_strategy_or_404(strategy_id)
        g.db.execute(text("DELETE FROM strategies WHERE id = :id"), {"id": strategy_id})
        g.db.commit()
        flash("Estrategia eliminada.", "info")
        return redirect(url_for("admin_dashboard"))

    def get_strategy_or_404(strategy_id):
        strategy = g.db.execute(
            text("SELECT * FROM strategies WHERE id = :id"), {"id": strategy_id}
        ).mappings().fetchone()
        if strategy is None:
            abort(404)
        return strategy

    def save_strategy(strategy_id=None):
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        risk_level = request.form.get("risk_level", "Medio")
        signal_frequency = request.form.get("signal_frequency", "").strip()
        historical_return = request.form.get("historical_return", "").strip()
        telegram_url = request.form.get("telegram_url", "").strip()
        has_telegram = 1 if request.form.get("has_telegram") == "on" else 0
        signals_txt_name = request.form.get("signals_txt_name", "").strip()
        python_file = request.form.get("python_file", "").strip()
        auto_execute = 1 if request.form.get("auto_execute") == "on" else 0
        schedule_start_time = request.form.get("schedule_start_time", "15:30").strip()
        schedule_end_time = request.form.get("schedule_end_time", "21:30").strip()
        schedule_interval_minutes = parse_schedule_int(
            request.form.get("schedule_interval_minutes"),
            default=30,
            minimum=1,
            maximum=1440,
        )
        is_active = 1 if request.form.get("is_active") == "on" else 0

        errors = []
        if not name:
            errors.append("El nombre es obligatorio.")
        if risk_level not in {"Bajo", "Medio", "Alto"}:
            errors.append("El nivel de riesgo no es valido.")
        if has_telegram and not telegram_url.startswith(
            ("https://t.me/", "http://t.me/", "https://telegram.me/")
        ):
            errors.append("Usa un enlace valido de Telegram o desmarca Tiene canal de Telegram.")
        if signals_txt_name and not valid_txt_name(signals_txt_name):
            errors.append("El nombre del TXT debe ser un archivo .txt sin carpetas.")
        if python_file and not valid_python_filename(python_file):
            errors.append("El archivo Python debe ser un .py sin carpetas.")
        if auto_execute and not python_file:
            errors.append("Para ejecutar automaticamente debes indicar el archivo Python.")
        if not valid_schedule_time(schedule_start_time):
            errors.append("La hora inicial de la estrategia no es valida.")
        if not valid_schedule_time(schedule_end_time):
            errors.append("La hora final de la estrategia no es valida.")

        form_strategy = {
            "id": strategy_id,
            "name": name,
            "description": description,
            "risk_level": risk_level,
            "signal_frequency": signal_frequency,
            "historical_return": historical_return,
            "telegram_url": telegram_url,
            "has_telegram": has_telegram,
            "signals_txt_name": signals_txt_name,
            "python_file": python_file,
            "auto_execute": auto_execute,
            "schedule_start_time": schedule_start_time,
            "schedule_end_time": schedule_end_time,
            "schedule_interval_minutes": schedule_interval_minutes,
            "is_active": is_active,
        }

        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template(
                "admin/form.html",
                strategy=form_strategy,
                title="Modificar estrategia" if strategy_id else "Crear estrategia",
                action=(
                    url_for("strategy_edit", strategy_id=strategy_id)
                    if strategy_id
                    else url_for("strategy_new")
                ),
            )

        if strategy_id:
            g.db.execute(
                text(
                """
                UPDATE strategies
                SET name = :name, description = :description, risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = :historical_return,
                    telegram_url = :telegram_url,
                    has_telegram = :has_telegram,
                    signals_txt_name = :signals_txt_name,
                    python_file = :python_file,
                    auto_execute = :auto_execute,
                    schedule_start_time = :schedule_start_time,
                    schedule_end_time = :schedule_end_time,
                    schedule_interval_minutes = :schedule_interval_minutes,
                    is_active = :is_active
                WHERE id = :id
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "has_telegram": has_telegram,
                    "signals_txt_name": signals_txt_name,
                    "python_file": python_file,
                    "auto_execute": auto_execute,
                    "schedule_start_time": schedule_start_time,
                    "schedule_end_time": schedule_end_time,
                    "schedule_interval_minutes": schedule_interval_minutes,
                    "is_active": is_active,
                    "id": strategy_id,
                },
            )
            flash("Estrategia actualizada.", "success")
        else:
            g.db.execute(
                text(
                """
                INSERT INTO strategies
                (name, description, risk_level, signal_frequency,
                 historical_return, telegram_url, has_telegram, signals_txt_name,
                 python_file, auto_execute, schedule_start_time, schedule_end_time,
                 schedule_interval_minutes, is_active)
                VALUES (:name, :description, :risk_level, :signal_frequency,
                        :historical_return, :telegram_url, :has_telegram, :signals_txt_name,
                        :python_file, :auto_execute, :schedule_start_time, :schedule_end_time,
                        :schedule_interval_minutes, :is_active)
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "has_telegram": has_telegram,
                    "signals_txt_name": signals_txt_name,
                    "python_file": python_file,
                    "auto_execute": auto_execute,
                    "schedule_start_time": schedule_start_time,
                    "schedule_end_time": schedule_end_time,
                    "schedule_interval_minutes": schedule_interval_minutes,
                    "is_active": is_active,
                },
            )
            flash("Estrategia creada.", "success")

        g.db.commit()
        return redirect(url_for("admin_dashboard"))

    def track_visitor():
        if request.endpoint == "static":
            return
        visitor_id = session.get("visitor_id")
        if not visitor_id:
            visitor_id = uuid4().hex
            session["visitor_id"] = visitor_id

        now = datetime.now(UTC)
        if engine.dialect.name == "postgresql":
            g.db.execute(
                text(
                    """
                    INSERT INTO active_visitors (visitor_id, last_seen)
                    VALUES (:visitor_id, :last_seen)
                    ON CONFLICT (visitor_id) DO UPDATE SET
                      last_seen = EXCLUDED.last_seen
                    """
                ),
                {"visitor_id": visitor_id, "last_seen": now},
            )
        else:
            g.db.execute(
                text(
                    """
                    INSERT OR REPLACE INTO active_visitors (visitor_id, last_seen)
                    VALUES (:visitor_id, :last_seen)
                    """
                ),
                {"visitor_id": visitor_id, "last_seen": now},
            )
        g.db.commit()

    def active_visitor_count(minutes=5):
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        return g.db.execute(
            text("SELECT COUNT(*) FROM active_visitors WHERE last_seen >= :cutoff"),
            {"cutoff": cutoff},
        ).scalar_one()

    def strategy_with_signals(row):
        strategy = dict(row)
        txt_name = strategy.get("signals_txt_name", "")
        signals = read_strategy_signals(txt_name)
        strategy["signals"] = signals
        strategy["signals_count"] = len(signals)
        strategy["signals_updated_at"] = strategy_signals_updated_at(txt_name)
        strategy["run_status"] = strategy_run_status(strategy.get("name", ""), txt_name)
        return strategy

    def strategy_run_status(strategy_name, txt_name):
        data = load_strategy_status_data()
        item = data.get("strategies", {}).get(strategy_name)
        if not item:
            return {
                "ok": False,
                "running": False,
                "label": "No ejecutado",
                "ran_at": "",
                "error": "Todavia no hay registro de ejecucion para esta estrategia.",
            }

        ran_at = format_status_datetime(item.get("ran_at", ""))

        if item.get("running"):
            return {
                "ok": False,
                "running": True,
                "label": "En ejecucion",
                "ran_at": ran_at,
                "error": "",
            }
        if item.get("ok"):
            return {
                "ok": True,
                "running": False,
                "label": "Correcto",
                "ran_at": ran_at,
                "error": "",
            }
        return {
            "ok": False,
            "running": False,
            "label": "Fallo",
            "ran_at": ran_at,
            "error": item.get("error", "") or "La estrategia termino con error.",
        }

    def load_strategy_status_data():
        status_path = Path(
            os.environ.get("STRATEGY_STATUS_FILE", DEFAULT_STRATEGY_STATUS_FILE)
        ).resolve()
        try:
            if status_path != DEFAULT_STRATEGY_STATUS_FILE and BASE_DIR not in status_path.parents:
                return {}
            if not status_path.exists() or not status_path.is_file():
                return {}
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def load_strategy_failures():
        data = load_strategy_status_data()
        failures = []
        for name, item in data.get("strategies", {}).items():
            is_failure = (
                not item.get("ok")
                or item.get("returncode") not in (None, 0)
            )
            if item.get("running") or not is_failure:
                continue
            failures.append(
                {
                    "name": name,
                    "file": item.get("file", ""),
                    "txt": item.get("txt", ""),
                    "ran_at": format_status_datetime(item.get("ran_at", "")),
                    "returncode": item.get("returncode"),
                    "error": build_strategy_failure_error(item),
                }
            )
        if not failures:
            failures = load_strategy_failures_from_schedule_message()
        failures.sort(key=lambda item: item["name"])
        return failures

    def build_strategy_failure_error(item):
        details = []
        if item.get("returncode") not in (None, 0):
            details.append(f"Codigo de salida: {item.get('returncode')}.")
        if item.get("error"):
            details.append(str(item.get("error")))
        if not details:
            details.append("La estrategia termino marcada como ERROR, pero no devolvio detalle adicional.")
        return "\n".join(details)

    def load_strategy_failures_from_schedule_message():
        row = g.db.execute(
            text(
                """
                SELECT last_run_at, last_message
                FROM automation_schedules
                WHERE task_name = 'strategies'
                  AND last_status = 'ERROR'
                """
            )
        ).mappings().fetchone()
        if not row or not row["last_message"]:
            return []

        failures = []
        pattern = re.compile(r"ERROR - (?P<name>.+?) \((?P<file>.+?)\) \| (?P<txt_status>TXT [^|]+)")
        for match in pattern.finditer(row["last_message"]):
            failures.append(
                {
                    "name": match.group("name").strip(),
                    "file": match.group("file").strip(),
                    "txt": "",
                    "ran_at": format_status_datetime(row["last_run_at"]),
                    "returncode": 1,
                    "error": match.group("txt_status").strip(),
                }
            )
        return failures

    def format_status_datetime(value):
        if not value:
            return ""
        parsed = parse_status_datetime(value)
        if parsed is None:
            return value
        return parsed.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def parse_status_datetime(value):
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def mark_strategies_as_running():
        mark_strategies_as_running_file()

    def load_automation_schedules():
        expire_stale_running_schedules()
        rows = g.db.execute(
            text("SELECT * FROM automation_schedules ORDER BY task_name")
        ).mappings().fetchall()
        return {row["task_name"]: row for row in rows}

    def expire_stale_running_schedules():
        rows = g.db.execute(
            text(
                """
                SELECT task_name, last_run_at
                FROM automation_schedules
                WHERE last_status = 'RUNNING'
                """
            )
        ).mappings().fetchall()

        for row in rows:
            if row["task_name"] != "strategies":
                continue
            result = completed_strategy_runner_result(row["last_run_at"])
            if not result:
                continue
            g.db.execute(
                text(
                    """
                    UPDATE automation_schedules
                    SET last_status = :last_status,
                        last_message = :last_message
                    WHERE task_name = 'strategies'
                      AND last_status = 'RUNNING'
                    """
                ),
                {
                    "last_status": "OK" if result["ok"] else "ERROR",
                    "last_message": result["message"],
                },
            )
        g.db.commit()

    def completed_strategy_runner_result(last_run_at):
        data = load_strategy_status_data()
        finished_at = parse_status_datetime(data.get("finished_at", ""))
        if finished_at is None:
            return None

        started_at = parse_status_datetime(data.get("started_at", ""))
        schedule_started_at = parse_database_datetime(last_run_at)
        if schedule_started_at and started_at and started_at < schedule_started_at:
            return None

        results = data.get("strategies", {})
        if not results:
            return {
                "ok": False,
                "message": "Estrategias finalizadas sin resultados guardados.",
            }

        failures = [
            name
            for name, item in results.items()
            if not item.get("ok")
        ]
        if failures:
            return {
                "ok": False,
                "message": f"Estrategias finalizadas con {len(failures)} fallos: {', '.join(failures[:6])}.",
            }
        return {
            "ok": True,
            "message": "Estrategias finalizadas correctamente.",
        }

    def parse_database_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_status_datetime(value)
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MADRID_TZ)
        return parsed.astimezone(UTC)

    def parse_schedule_int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def valid_schedule_time(value):
        try:
            hour, minute = [int(part) for part in value.split(":", 1)]
        except (AttributeError, ValueError):
            return False
        return 0 <= hour <= 23 and 0 <= minute <= 59

    def normalize_weekdays(values):
        days = []
        for value in values:
            try:
                day = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7 and day not in days:
                days.append(day)
        if not days:
            days = [1, 2, 3, 4, 5]
        return ",".join(str(day) for day in sorted(days))

    def read_strategy_signals(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None:
            return []

        try:
            return [
                parse_signal_line(line.strip())
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ][:50]
        except OSError:
            return []

    def parse_signal_line(line):
        parts = [part.strip() for part in line.split("|") if part.strip()]
        side = ""
        symbol = ""
        field_parts = parts

        if parts:
            first = parts[0].upper()
            if first in SIGNAL_SIDE_WORDS and len(parts) > 1:
                side = parts[0]
                symbol = parts[1]
                field_parts = parts[2:]
            elif SIGNAL_SYMBOL_RE.match(parts[0]):
                symbol = parts[0]
                field_parts = parts[1:]

        fields = {}
        for part in field_parts:
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            fields[key.strip()] = value.strip()

        return {
            "line": line,
            "symbol": symbol,
            "side": side,
            "fields": fields,
            "common": common_signal_fields(side, fields),
        }

    def common_signal_fields(side, fields):
        return {
            "direccion": side or first_existing(fields, ["Direccion", "Dirección", "Side", "Tipo"]),
            "apertura": first_existing(fields, ["Entrada", "Apertura", "Precio entrada", "Precio", "Entry"]),
            "cierre": first_existing(fields, ["Salida", "Cierre", "TP1", "Objetivo", "Take Profit", "Target"]),
            "stop": first_existing(fields, ["Stop", "Stop Loss", "SL"]),
        }

    def first_existing(fields, keys):
        lower_fields = {str(key).lower(): value for key, value in fields.items()}
        for key in keys:
            value = lower_fields.get(key.lower())
            if value:
                return value
        return ""

    def normalize_signal_symbol(symbol):
        return str(symbol).strip().upper().replace(" ", "")

    def build_signal_diagnostic(strategy, signal):
        fields = signal.get("fields", {})
        strategy_name = strategy["name"].lower()
        points = []

        if signal.get("side"):
            points.append(f"Direccion detectada: {signal['side']}.")
        if "Precio" in fields:
            points.append(f"Precio de referencia del aviso: {fields['Precio']}.")
        if "Score" in fields:
            points.append(f"Score de la estrategia: {fields['Score']}. Cuanto mayor sea, mas arriba quedo en el ranking interno.")
        if "Vol$" in fields:
            points.append(f"Volumen monetario observado: {fields['Vol$']}.")
        if "Vol xMedia" in fields:
            points.append(f"Volumen relativo frente a la media: {fields['Vol xMedia']}x.")
        if "Stop" in fields:
            points.append(f"Nivel tecnico de stop sugerido por el modelo: {fields['Stop']}.")
        if "TP1" in fields:
            points.append(f"Primer objetivo tecnico: {fields['TP1']}.")
        if "TP2" in fields:
            points.append(f"Segundo objetivo tecnico: {fields['TP2']}.")
        if "TP1 VWAP" in fields:
            points.append(f"Primer objetivo hacia VWAP: {fields['TP1 VWAP']}.")

        if "momentum" in strategy_name:
            focus = "El diagnostico se centra en fuerza relativa, impulso del precio y volumen."
        elif "breakout" in strategy_name or "gap" in strategy_name:
            focus = "El diagnostico se centra en ruptura, rango inicial, gap y continuidad del movimiento."
        elif "reversion" in strategy_name:
            focus = "El diagnostico se centra en sobreextension, vuelta a medias/VWAP y agotamiento."
        elif "value" in strategy_name or "quality" in strategy_name or "dividend" in strategy_name:
            focus = "El diagnostico se centra en filtros de calidad/fundamentales y confirmacion tecnica."
        elif "pairs" in strategy_name:
            focus = "El diagnostico se centra en relacion estadistica entre activos, z-score y convergencia."
        elif "sector" in strategy_name:
            focus = "El diagnostico se centra en fuerza relativa sectorial y liderazgo dentro del sector."
        else:
            focus = "El diagnostico resume los datos clave generados por la estrategia."

        if not points:
            points.append("El aviso no trae campos estructurados suficientes; se muestra la linea original para revision manual.")

        return {
            "focus": focus,
            "points": points,
            "warning": "Lectura automatica informativa. No es asesoramiento financiero ni recomendacion de compra o venta.",
        }

    def generate_ai_signal_analysis(strategy, signal, diagnostic):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return ""

        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip()
        prompt = build_ai_analysis_prompt(strategy, signal, diagnostic)
        payload = {
            "model": model,
            "input": prompt,
            "max_output_tokens": 260,
        }
        request_data = json.dumps(payload).encode("utf-8")
        request_obj = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=request_data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            return f"No se pudo generar analisis IA ahora mismo: {error}"

        text_output = data.get("output_text", "").strip()
        if text_output:
            return text_output

        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks).strip()

    def build_ai_analysis_prompt(strategy, signal, diagnostic):
        fields = "\n".join(
            f"- {key}: {value}"
            for key, value in signal.get("fields", {}).items()
        ) or "- Sin campos estructurados."
        points = "\n".join(f"- {point}" for point in diagnostic["points"])
        return f"""
Eres un analista tecnico prudente. Redacta un analisis breve en espanol para una web de senales de trading.

No des asesoramiento financiero. No digas que hay que comprar o vender. No prometas resultados.
Usa solo los datos recibidos. Si falta informacion, dilo.

Estrategia: {strategy['name']}
Descripcion estrategia: {strategy['description']}
Riesgo estrategia: {strategy['risk_level']}
Ticker: {signal['symbol']}
Direccion detectada: {signal.get('side') or 'No indicada'}
Aviso original: {signal['line']}

Campos:
{fields}

Diagnostico automatico:
{points}

Devuelve 4 bloques cortos:
1. Lectura rapida
2. Puntos a favor
3. Riesgos o dudas
4. Niveles/datos a vigilar
""".strip()

    def strategy_signals_updated_at(txt_name):
        updated_at = strategy_signals_updated_at_datetime(txt_name)
        if updated_at is None:
            return ""
        return updated_at.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def strategy_signals_updated_at_datetime(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None:
            return None

        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return None

    def strategy_signals_path(txt_name):
        if not txt_name or not valid_txt_name(txt_name):
            return None

        signals_dir = Path(os.environ.get("STRATEGY_SIGNALS_DIR", DEFAULT_SIGNALS_DIR)).resolve()
        path = (signals_dir / txt_name).resolve()

        try:
            if signals_dir not in path.parents and path != signals_dir:
                return None
            if not path.exists() or not path.is_file():
                return None
        except OSError:
            return None
        return path

    def valid_txt_name(txt_name):
        path = Path(txt_name)
        return (
            path.name == txt_name
            and txt_name.lower().endswith(".txt")
            and "/" not in txt_name
            and "\\" not in txt_name
        )

    return app


def init_db():
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS strategies (
                    id {id_column},
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    risk_level TEXT NOT NULL CHECK (risk_level IN ('Bajo', 'Medio', 'Alto')),
                    signal_frequency TEXT NOT NULL DEFAULT '',
                    historical_return TEXT NOT NULL DEFAULT '',
                    telegram_url TEXT NOT NULL DEFAULT '',
                    has_telegram INTEGER NOT NULL DEFAULT 1,
                    signals_txt_name TEXT NOT NULL DEFAULT '',
                    python_file TEXT NOT NULL DEFAULT '',
                    auto_execute INTEGER NOT NULL DEFAULT 0,
                    schedule_start_time TEXT NOT NULL DEFAULT '15:30',
                    schedule_end_time TEXT NOT NULL DEFAULT '21:30',
                    schedule_interval_minutes INTEGER NOT NULL DEFAULT 30,
                    schedule_last_run_key TEXT NOT NULL DEFAULT '',
                    schedule_last_run_at TIMESTAMP,
                    schedule_last_status TEXT NOT NULL DEFAULT '',
                    schedule_last_message TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        ensure_universe_table(connection)
        add_strategy_column(connection, "signals_txt_name")
        add_strategy_column(connection, "has_telegram", "INTEGER NOT NULL DEFAULT 1")
        add_strategy_column(connection, "python_file")
        add_strategy_column(connection, "auto_execute", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "schedule_start_time", "TEXT NOT NULL DEFAULT '15:30'")
        add_strategy_column(connection, "schedule_end_time", "TEXT NOT NULL DEFAULT '21:30'")
        add_strategy_column(connection, "schedule_interval_minutes", "INTEGER NOT NULL DEFAULT 30")
        add_strategy_column(connection, "schedule_last_run_key")
        add_strategy_column(connection, "schedule_last_run_at", "TIMESTAMP")
        add_strategy_column(connection, "schedule_last_status")
        add_strategy_column(connection, "schedule_last_message")
        ensure_default_real_strategies(connection)

        count = connection.execute(text("SELECT COUNT(*) FROM strategies")).scalar_one()
        if count == 0:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :is_active)
                    """
                ),
                [
                    {
                        "name": "Momentum Intradia",
                        "description": "Entrada en activos con ruptura de volumen y confirmacion de tendencia a corto plazo.",
                        "risk_level": "Medio",
                        "signal_frequency": "3-6 senales por semana",
                        "historical_return": "+18.4% anualizado",
                        "telegram_url": "https://t.me/tu_canal_momentum",
                        "is_active": 1,
                    },
                    {
                        "name": "Swing Conservador",
                        "description": "Operativa de varios dias con gestion estricta del riesgo y objetivos parciales.",
                        "risk_level": "Bajo",
                        "signal_frequency": "1-3 senales por semana",
                        "historical_return": "+11.2% anualizado",
                        "telegram_url": "https://t.me/tu_canal_swing",
                        "is_active": 1,
                    },
                    {
                        "name": "Crypto Breakout",
                        "description": "Seguimiento de rupturas en criptomonedas liquidas con stops dinamicos.",
                        "risk_level": "Alto",
                        "signal_frequency": "5-10 senales por semana",
                        "historical_return": "+34.7% anualizado",
                        "telegram_url": "https://t.me/tu_canal_crypto",
                        "is_active": 1,
                    },
                ],
            )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS asset_snapshots (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    market TEXT NOT NULL,
                    price FLOAT NOT NULL,
                    money_volume FLOAT NOT NULL,
                    money_volume_1m FLOAT NOT NULL DEFAULT 0,
                    money_volume_2m FLOAT NOT NULL DEFAULT 0,
                    money_volume_3m FLOAT NOT NULL DEFAULT 0,
                    day_money_volume FLOAT NOT NULL DEFAULT 0,
                    week_money_volume FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_1d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_2d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_3d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_4d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_5d FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_1w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_2w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_3w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_4w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_5w FLOAT NOT NULL DEFAULT 0,
                    day_volume_score FLOAT NOT NULL,
                    week_volume_score FLOAT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        for column_name in [
            "money_volume_1m",
            "money_volume_2m",
            "money_volume_3m",
            "day_money_volume",
            "week_money_volume",
            "day_money_volume_1d",
            "day_money_volume_2d",
            "day_money_volume_3d",
            "day_money_volume_4d",
            "day_money_volume_5d",
            "week_money_volume_1w",
            "week_money_volume_2w",
            "week_money_volume_3w",
            "week_money_volume_4w",
            "week_money_volume_5w",
        ]:
            add_asset_snapshot_column(connection, column_name)
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS active_visitors (
                    visitor_id TEXT PRIMARY KEY,
                    last_seen TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS automation_schedules (
                    id {id_column},
                    task_name TEXT NOT NULL UNIQUE,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    start_time TEXT NOT NULL DEFAULT '15:30',
                    runs_per_day INTEGER NOT NULL DEFAULT 1,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5',
                    last_run_key TEXT NOT NULL DEFAULT '',
                    last_run_at TIMESTAMP,
                    last_status TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        add_automation_schedule_column(connection, "weekdays", f"TEXT NOT NULL DEFAULT '{DEFAULT_WEEKDAYS}'")
        existing_schedules = {
            row[0]
            for row in connection.execute(
                text("SELECT task_name FROM automation_schedules")
            ).fetchall()
        }
        for task_name in SCHEDULER_TASKS:
            if task_name in existing_schedules:
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO automation_schedules
                    (task_name, is_enabled, start_time, runs_per_day, interval_minutes, weekdays)
                    VALUES (:task_name, 0, '15:30', 1, 60, :weekdays)
                    """
                ),
                {"task_name": task_name, "weekdays": DEFAULT_WEEKDAYS},
            )


def ensure_default_real_strategies(connection):
    existing = {
        row["name"]: row
        for row in connection.execute(
            text("SELECT name, telegram_url FROM strategies")
        ).mappings().fetchall()
    }
    for strategy in DEFAULT_REAL_STRATEGIES:
        strategy = {
            **strategy,
            "python_file": DEFAULT_STRATEGY_FILES.get(strategy["name"], ""),
        }
        if strategy["name"] not in existing:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, signals_txt_name, python_file, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :signals_txt_name, :python_file, 1)
                    """
                ),
                strategy,
            )
            continue

        connection.execute(
            text(
                """
                UPDATE strategies
                SET description = :description,
                    risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = CASE
                        WHEN historical_return = '' THEN :historical_return
                        ELSE historical_return
                    END,
                    signals_txt_name = :signals_txt_name,
                    python_file = CASE
                        WHEN python_file = '' THEN :python_file
                        ELSE python_file
                    END
                WHERE name = :name
                """
            ),
            strategy,
        )


def add_asset_snapshot_column(connection, column_name):
    if asset_snapshot_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE asset_snapshots ADD COLUMN {column_name} FLOAT NOT NULL DEFAULT 0"
        )
    )


def add_automation_schedule_column(connection, column_name, definition):
    if automation_schedule_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE automation_schedules ADD COLUMN {column_name} {definition}"
        )
    )


def automation_schedule_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'automation_schedules'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(automation_schedules)")).fetchall()
    return any(row[1] == column_name for row in rows)


def add_strategy_column(connection, column_name, definition="TEXT NOT NULL DEFAULT ''"):
    if strategy_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE strategies ADD COLUMN {column_name} {definition}"
        )
    )


def strategy_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'strategies'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(strategies)")).fetchall()
    return any(row[1] == column_name for row in rows)


def asset_snapshot_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'asset_snapshots'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(asset_snapshots)")).fetchall()
    return any(row[1] == column_name for row in rows)


init_db()
app = create_app()
start_scheduler_thread()


if __name__ == "__main__":
    debug_enabled = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
