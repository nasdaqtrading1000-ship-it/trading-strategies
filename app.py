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
DEFAULT_STRATEGY_LOG_DIR = (BASE_DIR / "Estrategias" / "logs").resolve()
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
DEFAULT_STRATEGY_SCHEDULES = {
    "Momentum": {"start": "22:10", "end": "22:25", "interval": 1440},
    "Swing Trading": {"start": "22:15", "end": "22:30", "interval": 1440},
    "BreaKout": {"start": "15:45", "end": "21:45", "interval": 30},
    "Mean Reversion": {"start": "21:30", "end": "21:55", "interval": 30},
    "Value Trading": {"start": "22:35", "end": "22:50", "interval": 1440},
    "Dividend Growth": {"start": "22:40", "end": "22:55", "interval": 1440},
    "Trend Following": {"start": "22:20", "end": "22:35", "interval": 1440},
    "Pairs Trading": {"start": "16:00", "end": "21:45", "interval": 60},
    "Sector Rotation": {"start": "22:30", "end": "22:45", "interval": 1440},
    "Quality Investing": {"start": "22:45", "end": "23:00", "interval": 1440},
    "Opening Range BreaKout": {"start": "15:35", "end": "17:00", "interval": 10},
    "VWAP Reversion": {"start": "16:00", "end": "21:45", "interval": 20},
    "Momentum Intradia": {"start": "15:40", "end": "21:45", "interval": 15},
    "Scalping The PullBacks": {"start": "15:40", "end": "21:45", "interval": 10},
    "Gap and Go": {"start": "15:35", "end": "17:30", "interval": 10},
}
SIGNAL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,14}$")
SIGNAL_SIDE_WORDS = {"LONG", "SHORT", "BUY", "SELL", "COMPRA", "VENTA"}
TECHNICAL_TERM_HELP = [
    ("precio actual", "Ultimo precio usado por la estrategia cuando genero el aviso."),
    ("precio", "Precio de referencia del activo en el momento del calculo."),
    ("direccion", "Sentido de la operacion: LONG busca subida, SHORT busca caida."),
    ("apertura", "Precio de referencia para abrir o vigilar la operacion."),
    ("entrada", "Precio de referencia para entrar en la operacion."),
    ("cierre", "Precio objetivo o condicion de salida de la operacion."),
    ("salida", "Zona o condicion donde la estrategia podria cerrar la operacion."),
    ("stop loss", "Nivel defensivo para limitar la perdida si la operacion va en contra."),
    ("stop", "Nivel defensivo para limitar la perdida si la operacion va en contra."),
    ("tp", "Take profit: objetivo de beneficio propuesto por la estrategia."),
    ("score", "Puntuacion interna para ordenar candidatos. Cuanto mayor, mejor segun la estrategia."),
    ("rs vs sector", "Fuerza relativa de la accion comparada con su sector."),
    ("rs ", "Fuerza relativa frente a un indice, sector o referencia."),
    ("rsi", "Indicador de fuerza relativa. Valores bajos suelen indicar sobreventa y altos sobrecompra."),
    ("vwap", "Precio medio ponderado por volumen durante la sesion."),
    ("sma", "Media movil simple. Resume el precio medio de las ultimas velas o dias."),
    ("ema", "Media movil exponencial. Da mas peso a los precios recientes."),
    ("atr", "Rango medio real. Mide volatilidad y ayuda a colocar stops."),
    ("vol xmedia", "Volumen actual comparado con su volumen medio."),
    ("vol$", "Volumen monetario negociado: precio multiplicado por volumen."),
    ("vol", "Volumen negociado. Ayuda a medir liquidez e interes del mercado."),
    ("momentum", "Fuerza del movimiento reciente del precio."),
    ("ruptura", "El precio supera una zona importante, como resistencia o rango previo."),
    ("breakout", "Ruptura de una resistencia o rango relevante."),
    ("resistencia", "Zona donde antes el precio tuvo dificultad para seguir subiendo."),
    ("soporte", "Zona donde antes el precio tuvo dificultad para seguir bajando."),
    ("gap", "Diferencia entre el precio de apertura y el cierre anterior."),
    ("zscore", "Distancia estadistica frente a la media. En pares mide si el spread esta extremo."),
    ("corr", "Correlacion entre activos. Cerca de 1 significa que suelen moverse parecido."),
    ("hedge", "Relacion aproximada entre dos activos para construir una posicion de pares."),
    ("per", "Precio dividido entre beneficio por accion. Mide valoracion relativa."),
    ("p/b", "Precio dividido entre valor contable. Mide cuanto paga el mercado por el patrimonio."),
    ("p/s", "Precio dividido entre ventas. Mide valoracion frente a ingresos."),
    ("roe", "Rentabilidad sobre fondos propios. Mide eficiencia del capital de la empresa."),
    ("roic", "Rentabilidad sobre capital invertido. Mide calidad del negocio."),
    ("yield", "Rentabilidad por dividendo aproximada."),
    ("payout", "Porcentaje del beneficio que se destina a dividendos."),
    ("deuda", "Nivel de endeudamiento de la empresa."),
    ("margen", "Porcentaje de ventas que queda como beneficio o resultado operativo."),
    ("crec", "Crecimiento historico de ingresos, beneficios o dividendos."),
]
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
        active_strategy_names, total_active = strategy_names_batch_for_runner()
        if not active_strategy_names:
            return {"ok": False, "message": "No hay estrategias activas para ejecutar."}
        mark_strategies_as_running_file(active_strategy_names)
        timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
        env = os.environ.copy()
        env["TRADING_ACTIVE_STRATEGIES"] = json.dumps(active_strategy_names)
        DEFAULT_STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        runner_log_path = DEFAULT_STRATEGY_LOG_DIR / "run_all_strategies.log"
        try:
            with runner_log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                completed = subprocess.run(
                    [sys.executable, str(STRATEGIES_RUNNER)],
                    cwd=str(STRATEGIES_RUNNER.parent),
                    text=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            mark_running_strategies_error(
                f"Estrategias canceladas por superar {timeout_seconds} segundos."
            )
            return {
                "ok": False,
                "message": f"Estrategias canceladas por superar {timeout_seconds} segundos.",
            }
        persisted_results = persist_strategy_status_file_results()
        summary = strategy_runner_summary(completed.returncode)
        summary = f"{summary} Lote ejecutado: {len(active_strategy_names)}/{total_active} activas."
        if completed.returncode != 0 and not persisted_results:
            log_tail = read_text_tail(runner_log_path)
            mark_running_strategies_error(f"{summary}\n{log_tail}".strip())
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


def parse_status_datetime_value(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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

    txt_path = single_strategy_txt_path(strategy)
    previous_mtime = txt_path.stat().st_mtime if txt_path and txt_path.exists() else None
    mark_single_strategy_status(strategy, running=True)
    timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
    DEFAULT_STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEFAULT_STRATEGY_LOG_DIR / f"{safe_log_filename(strategy['name'])}.log"
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            completed = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(path.parent),
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
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

    output = read_text_tail(log_path)
    txt_updated = output_txt_updated(txt_path, previous_mtime)
    result = {
        "ok": completed.returncode == 0,
        "message": (
            f"{strategy['name']} ejecutada correctamente. TXT {'actualizado' if txt_updated else 'sin cambios'}."
            if completed.returncode == 0
            else f"{strategy['name']} fallo. Codigo {completed.returncode}. {output[-700:]}"
        ),
        "returncode": completed.returncode,
        "txt_updated": txt_updated,
        "stdout": output,
        "stderr": "",
    }
    if result["ok"] and txt_path:
        sync_signal_file_to_database(strategy.get("signals_txt_name", ""), txt_path)
    mark_single_strategy_status(strategy, running=False, result=result)
    return result


def single_strategy_txt_path(strategy):
    txt_name = (strategy.get("signals_txt_name") or "").strip()
    if not txt_name or not valid_txt_name(txt_name):
        return None
    return (BASE_DIR / "Estrategias" / "salidas_txt" / txt_name).resolve()


def output_txt_updated(path, previous_mtime):
    if path is None or not path.exists() or not path.is_file():
        return False
    current_mtime = path.stat().st_mtime
    if previous_mtime is None:
        return True
    return current_mtime > previous_mtime


def safe_log_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._-")
    return cleaned or "strategy"


def read_text_tail(path, max_chars=1200):
    try:
        text_value = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text_value.strip()[-max_chars:]


def signal_date_from_line(line):
    expected = "fecha:"
    for part in str(line).split("|"):
        part = part.strip()
        if part.lower().startswith(expected):
            value = part.split(":", 1)[1].strip()
            return value[:10]
    return ""


def sync_signal_file_to_database(txt_name, path=None):
    if not txt_name or not valid_txt_name_global(txt_name):
        return 0

    path = path or (DEFAULT_SIGNALS_DIR / txt_name)
    try:
        lines = [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return 0

    saved = 0
    with engine.begin() as connection:
        for line in lines:
            signal_date = signal_date_from_line(line)
            if not signal_date:
                continue
            exists = connection.execute(
                text(
                    """
                    SELECT 1
                    FROM strategy_signals
                    WHERE txt_name = :txt_name
                      AND signal_date = :signal_date
                      AND line = :line
                    LIMIT 1
                    """
                ),
                {"txt_name": txt_name, "signal_date": signal_date, "line": line},
            ).fetchone()
            if exists:
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO strategy_signals (txt_name, signal_date, line)
                    VALUES (:txt_name, :signal_date, :line)
                    """
                ),
                {"txt_name": txt_name, "signal_date": signal_date, "line": line},
            )
            saved += 1
    return saved


def valid_txt_name_global(txt_name):
    path = Path(txt_name)
    return (
        path.name == txt_name
        and txt_name.lower().endswith(".txt")
        and "/" not in txt_name
        and "\\" not in txt_name
    )


def mark_single_strategy_status(strategy, running=False, result=None):
    now = datetime.now(UTC).isoformat()
    persist_single_strategy_status(strategy, running=running, result=result, now=datetime.now(MADRID_TZ))
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
        item["txt_updated"] = bool(result.get("txt_updated"))
        item["returncode"] = result.get("returncode")
        item["error"] = "" if result.get("ok") else result.get("message", "")
        item["stdout"] = result.get("stdout", "")
        item["stderr"] = result.get("stderr", "")
        data["finished_at"] = now

    data["strategies"][strategy["name"]] = item
    DEFAULT_STRATEGY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STRATEGY_STATUS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def persist_single_strategy_status(strategy, running=False, result=None, now=None):
    strategy_id = strategy.get("id")
    if not strategy_id:
        return

    now = now or datetime.now(MADRID_TZ)
    if running:
        values = {
            "id": strategy_id,
            "run_status": "RUNNING",
            "run_message": "En ejecucion",
            "run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
            "run_txt_updated": 0,
            "run_returncode": None,
        }
    else:
        result = result or {"ok": False, "message": "Sin resultado.", "returncode": None}
        values = {
            "id": strategy_id,
            "run_status": "OK" if result.get("ok") else "ERROR",
            "run_message": result.get("message", "")[:1000],
            "run_at": now.astimezone(MADRID_TZ).replace(tzinfo=None),
            "run_txt_updated": 1 if result.get("txt_updated") else 0,
            "run_returncode": result.get("returncode"),
        }

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET run_status = :run_status,
                    run_message = :run_message,
                    run_at = :run_at,
                    run_txt_updated = :run_txt_updated,
                    run_returncode = :run_returncode
                WHERE id = :id
                """
            ),
            values,
        )


def active_strategy_names_for_runner():
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE is_active = 1 ORDER BY name")
        ).mappings().fetchall()
    return [row["name"] for row in rows]


def strategy_names_batch_for_runner():
    batch_size = strategy_runner_batch_size()
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE is_active = 1 ORDER BY name")
        ).mappings().fetchall()
        names = [row["name"] for row in rows]
        total = len(names)
        if total == 0:
            return [], 0

        cursor_row = connection.execute(
            text(
                """
                SELECT batch_cursor
                FROM automation_schedules
                WHERE task_name = 'strategies'
                """
            )
        ).mappings().fetchone()
        cursor = int(cursor_row["batch_cursor"] or 0) if cursor_row else 0
        start = cursor % total
        selected = [
            names[(start + index) % total]
            for index in range(min(batch_size, total))
        ]
        next_cursor = (start + len(selected)) % total
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET batch_cursor = :batch_cursor
                WHERE task_name = 'strategies'
                """
            ),
            {"batch_cursor": next_cursor},
        )
    return selected, total


def strategy_runner_batch_size():
    try:
        value = int(os.environ.get("TRADING_STRATEGY_BATCH_SIZE", "3"))
    except ValueError:
        value = 3
    return max(1, min(value, 15))


def mark_strategies_as_running_file(strategy_names=None):
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    names = list(strategy_names or [])
    with engine.begin() as connection:
        if not names:
            rows = connection.execute(
                text("SELECT name FROM strategies WHERE is_active = 1 ORDER BY name")
            ).mappings().fetchall()
            names = [row["name"] for row in rows]
        for name in names:
            connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = 'RUNNING',
                        run_message = 'En ejecucion',
                        run_at = :run_at,
                        run_txt_updated = 0,
                        run_returncode = NULL
                    WHERE is_active = 1
                      AND name = :name
                    """
                ),
                {
                    "run_at": datetime.now(MADRID_TZ).replace(tzinfo=None),
                    "name": name,
                },
            )

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


def persist_strategy_status_file_results():
    try:
        data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    results = data.get("strategies", {})
    if not results:
        return 0

    saved = 0
    with engine.begin() as connection:
        for name, item in results.items():
            txt_name = item.get("txt", "")
            if txt_name:
                sync_signal_file_to_database(txt_name)
            status = "OK" if item.get("ok") else "ERROR"
            message = "" if item.get("ok") else (item.get("error", "") or "La estrategia termino con error.")
            connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = :run_status,
                        run_message = :run_message,
                        run_at = :run_at,
                        run_txt_updated = :run_txt_updated,
                        run_returncode = :run_returncode
                    WHERE name = :name
                    """
                ),
                {
                    "name": name,
                    "run_status": status,
                    "run_message": message[:1000],
                    "run_at": parse_status_datetime_value(item.get("ran_at", "")) or datetime.now(UTC),
                    "run_txt_updated": 1 if item.get("txt_updated") else 0,
                    "run_returncode": item.get("returncode"),
                },
            )
            saved += 1
    return saved


def mark_running_strategies_error(message):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET run_status = 'ERROR',
                    run_message = :message,
                    run_returncode = 1
                WHERE run_status = 'RUNNING'
                """
            ),
            {"message": message[:1000]},
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


def valid_txt_name(txt_name):
    path = Path(txt_name)
    return (
        path.name == txt_name
        and txt_name.lower().endswith(".txt")
        and "/" not in txt_name
        and "\\" not in txt_name
    )


def technical_term_help(term):
    normalized = re.sub(r"\s+", " ", str(term).strip().lower())
    compact = normalized.replace(" ", "")
    for key, help_text in TECHNICAL_TERM_HELP:
        key_normalized = key.strip().lower()
        key_compact = key_normalized.replace(" ", "")
        if normalized == key_normalized or compact == key_compact:
            return help_text
    for key, help_text in TECHNICAL_TERM_HELP:
        key_normalized = key.strip().lower()
        key_compact = key_normalized.replace(" ", "")
        if (
            normalized.startswith(key_normalized)
            or compact.startswith(key_compact)
        ):
            return help_text
    return ""


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
    app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
        "ADMIN_PASSWORD_HASH", generate_password_hash("admin123")
    )
    app.jinja_env.globals["technical_term_help"] = technical_term_help

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

    @app.route("/admin/strategies/apply-recommended-schedules", methods=["POST"])
    @login_required
    def strategies_apply_recommended_schedules():
        apply_recommended_strategy_schedules(g.db)
        g.db.commit()
        flash("Horarios recomendados aplicados a las estrategias.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/clear-failures", methods=["POST"])
    @login_required
    def strategies_clear_failures():
        try:
            DEFAULT_STRATEGY_STATUS_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        g.db.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_message = '',
                    last_status = '',
                    last_run_key = ''
                WHERE task_name = 'strategies'
                """
            )
        )
        g.db.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_message = '',
                    schedule_last_status = '',
                    schedule_last_run_key = '',
                    run_status = '',
                    run_message = '',
                    run_txt_updated = 0,
                    run_returncode = NULL
                """
            )
        )
        g.db.commit()
        flash("Fallos de estrategias limpiados.", "success")
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
        strategy["run_status"] = strategy_run_status(strategy, txt_name)
        return strategy

    def strategy_run_status(strategy, txt_name):
        db_status = (strategy.get("run_status") or "").strip()
        if db_status:
            ran_at = format_database_datetime(strategy.get("run_at"))
            if db_status == "RUNNING":
                return {
                    "ok": False,
                    "running": True,
                    "label": "En ejecucion",
                    "ran_at": ran_at,
                    "error": "",
                }
            if db_status == "OK":
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
                "error": strategy.get("run_message") or "La estrategia termino con error.",
            }

        strategy_name = strategy.get("name", "")
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
        db_failures = load_strategy_failures_from_database()
        if db_failures:
            return db_failures

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

    def load_strategy_failures_from_database():
        rows = g.db.execute(
            text(
                """
                SELECT name, python_file, signals_txt_name, run_at,
                       run_returncode, run_message
                FROM strategies
                WHERE run_status = 'ERROR'
                ORDER BY name
                """
            )
        ).mappings().fetchall()
        return [
            {
                "name": row["name"],
                "file": row["python_file"] or "",
                "txt": row["signals_txt_name"] or "",
                "ran_at": format_database_datetime(row["run_at"]),
                "returncode": row["run_returncode"],
                "error": row["run_message"] or "La estrategia termino con error.",
            }
            for row in rows
        ]

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

    def format_database_datetime(value):
        parsed = parse_database_datetime(value)
        if parsed is None:
            return ""
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
        schedules = {}
        for row in rows:
            schedule = dict(row)
            schedule["last_message"] = clean_schedule_message(
                schedule["task_name"],
                schedule.get("last_message", ""),
            )
            schedules[schedule["task_name"]] = schedule
        return schedules

    def clean_schedule_message(task_name, message):
        if task_name != "strategies" or not message:
            return message
        technical_markers = ["===", "Traceback", "KeyError", "File \"<frozen os>\"", "Ejecutando "]
        if any(marker in message for marker in technical_markers):
            return "Estrategias con errores. Revisa Fallos de estrategias."
        return message

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
        if path is None and not valid_txt_name_global(txt_name):
            return []

        if path is not None:
            sync_signal_file_to_database(txt_name, path)

        today = datetime.now(MADRID_TZ).date().isoformat()
        rows = g.db.execute(
            text(
                """
                SELECT line
                FROM strategy_signals
                WHERE txt_name = :txt_name
                  AND signal_date = :signal_date
                ORDER BY created_at DESC, id DESC
                LIMIT 50
                """
            ),
            {"txt_name": txt_name, "signal_date": today},
        ).mappings().fetchall()
        if rows:
            return [
                parse_signal_line(row["line"])
                for row in rows
            ]
        if path is None:
            return []

        try:
            return [
                parse_signal_line(line.strip())
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and signal_line_is_today(line.strip())
            ][:50]
        except OSError:
            return []

    def signal_line_is_today(line):
        date_value = signal_line_field(line, "Fecha")
        if not date_value:
            return False
        return date_value == datetime.now(MADRID_TZ).date().isoformat()

    def signal_line_field(line, field_name):
        expected = f"{field_name.lower()}:"
        for part in line.split("|"):
            part = part.strip()
            if part.lower().startswith(expected):
                return part.split(":", 1)[1].strip()
        return ""

    def parse_signal_line(line):
        parts = [part.strip() for part in line.split("|") if part.strip()]
        side = ""
        symbol = ""
        field_parts = parts

        if parts:
            first_clean = parts[0].strip().lstrip("-").strip()
            first = first_clean.upper()
            if first in SIGNAL_SIDE_WORDS and len(parts) > 1:
                side = parts[0]
                symbol = parts[1]
                field_parts = parts[2:]
            elif SIGNAL_SYMBOL_RE.match(first_clean):
                symbol = first_clean
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
            "precio_actual": first_existing(fields, ["Precio actual", "Precio", "Price", "Current Price"]),
            "apertura": first_existing(fields, ["Apertura", "Entrada", "Precio entrada", "Precio actual", "Precio", "Entry"]),
            "cierre": first_existing(fields, ["Salida", "Cierre", "TP1", "Objetivo", "Take Profit", "Target"]),
            "stop": first_existing(fields, ["Stop", "Stop Loss", "SL"]),
        }

    def first_existing(fields, keys):
        lower_fields = {str(key).lower(): value for key, value in fields.items()}
        for key in keys:
            lookup_key = key.lower()
            value = lower_fields.get(lookup_key)
            if value:
                return value
            for field_key, field_value in lower_fields.items():
                if field_key.startswith(f"{lookup_key} ") and field_value:
                    return field_value
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
                    run_status TEXT NOT NULL DEFAULT '',
                    run_message TEXT NOT NULL DEFAULT '',
                    run_at TIMESTAMP,
                    run_txt_updated INTEGER NOT NULL DEFAULT 0,
                    run_returncode INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        ensure_universe_table(connection)
        ensure_strategy_signals_table(connection)
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
        add_strategy_column(connection, "run_status")
        add_strategy_column(connection, "run_message")
        add_strategy_column(connection, "run_at", "TIMESTAMP")
        add_strategy_column(connection, "run_txt_updated", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "run_returncode", "INTEGER")
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
                    batch_cursor INTEGER NOT NULL DEFAULT 0,
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
        add_automation_schedule_column(connection, "batch_cursor", "INTEGER NOT NULL DEFAULT 0")
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
        schedule = DEFAULT_STRATEGY_SCHEDULES.get(
            strategy["name"],
            {"start": "15:30", "end": "21:30", "interval": 30},
        )
        strategy = {
            **strategy,
            "python_file": DEFAULT_STRATEGY_FILES.get(strategy["name"], ""),
            "schedule_start_time": schedule["start"],
            "schedule_end_time": schedule["end"],
            "schedule_interval_minutes": schedule["interval"],
        }
        if strategy["name"] not in existing:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, signals_txt_name, python_file,
                     schedule_start_time, schedule_end_time, schedule_interval_minutes, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :signals_txt_name, :python_file,
                            :schedule_start_time, :schedule_end_time, :schedule_interval_minutes, 1)
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
                    END,
                    schedule_start_time = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_start_time
                        ELSE schedule_start_time
                    END,
                    schedule_end_time = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_end_time
                        ELSE schedule_end_time
                    END,
                    schedule_interval_minutes = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_interval_minutes
                        ELSE schedule_interval_minutes
                    END
                WHERE name = :name
                """
            ),
            strategy,
        )


def apply_recommended_strategy_schedules(connection):
    for name, schedule in DEFAULT_STRATEGY_SCHEDULES.items():
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_start_time = :start_time,
                    schedule_end_time = :end_time,
                    schedule_interval_minutes = :interval_minutes
                WHERE name = :name
                """
            ),
            {
                "name": name,
                "start_time": schedule["start"],
                "end_time": schedule["end"],
                "interval_minutes": schedule["interval"],
            },
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


def ensure_strategy_signals_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS strategy_signals (
                id {id_column},
                txt_name TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                line TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
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
