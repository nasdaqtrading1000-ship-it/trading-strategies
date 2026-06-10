import json
import os
import subprocess
import sys
import threading
import time
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
        mark_strategies_as_running_file()
        subprocess.Popen(
            [sys.executable, str(STRATEGIES_RUNNER)],
            cwd=str(STRATEGIES_RUNNER.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "message": "Estrategias lanzadas en segundo plano."}

    return {"ok": False, "message": "Tarea no reconocida."}


def mark_strategies_as_running_file():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def process_due_schedules():
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
        result = run_scheduler_task(schedule["task_name"])
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
                    "last_run_key": due_key,
                    "last_run_at": now.replace(tzinfo=None),
                    "last_status": "OK" if result["ok"] else "ERROR",
                    "last_message": result["message"][:1000],
                    "task_name": schedule["task_name"],
                },
            )


def due_schedule_key(schedule, now):
    try:
        hour, minute = [int(part) for part in str(schedule["start_time"]).split(":", 1)]
        runs_per_day = max(1, int(schedule["runs_per_day"]))
        interval_minutes = max(1, int(schedule["interval_minutes"]))
    except (TypeError, ValueError):
        return ""

    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    grace_minutes = max(5, min(interval_minutes, 60))
    for index in range(runs_per_day):
        planned = start + timedelta(minutes=interval_minutes * index)
        if planned.date() == now.date() and planned <= now < planned + timedelta(minutes=grace_minutes):
            return f"{now.date().isoformat()}|{schedule['task_name']}|{index}"
    return ""


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
                   historical_return, telegram_url, signals_txt_name, is_active
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
                   historical_return, telegram_url, signals_txt_name, is_active, created_at
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

    @app.route("/admin/strategies/run-all", methods=["POST"])
    @login_required
    def admin_run_all_strategies():
        if not STRATEGIES_RUNNER.exists():
            flash("No se encontro Estrategias/run_all_strategies.py.", "danger")
            return redirect(url_for("admin_dashboard"))

        mark_strategies_as_running()
        try:
            subprocess.Popen(
                [sys.executable, str(STRATEGIES_RUNNER)],
                cwd=str(STRATEGIES_RUNNER.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            flash(f"No se pudo lanzar el ejecutor de estrategias: {error}", "danger")
            return redirect(url_for("admin_dashboard"))

        flash("Ejecucion de estrategias lanzada. La web ira actualizando el estado cuando termine.", "info")
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
                },
            )
        g.db.commit()
        flash("Programacion automatica guardada.", "success")
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
        signals_txt_name = request.form.get("signals_txt_name", "").strip()
        is_active = 1 if request.form.get("is_active") == "on" else 0

        errors = []
        if not name:
            errors.append("El nombre es obligatorio.")
        if risk_level not in {"Bajo", "Medio", "Alto"}:
            errors.append("El nivel de riesgo no es valido.")
        if not telegram_url.startswith(
            ("https://t.me/", "http://t.me/", "https://telegram.me/")
        ):
            errors.append("Usa un enlace valido de Telegram.")
        if signals_txt_name and not valid_txt_name(signals_txt_name):
            errors.append("El nombre del TXT debe ser un archivo .txt sin carpetas.")

        form_strategy = {
            "id": strategy_id,
            "name": name,
            "description": description,
            "risk_level": risk_level,
            "signal_frequency": signal_frequency,
            "historical_return": historical_return,
            "telegram_url": telegram_url,
            "signals_txt_name": signals_txt_name,
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
                    signals_txt_name = :signals_txt_name,
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
                    "signals_txt_name": signals_txt_name,
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
                 historical_return, telegram_url, signals_txt_name, is_active)
                VALUES (:name, :description, :risk_level, :signal_frequency,
                        :historical_return, :telegram_url, :signals_txt_name, :is_active)
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "signals_txt_name": signals_txt_name,
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
        strategy["run_status"] = strategy_run_status(strategy.get("name", ""))
        return strategy

    def strategy_run_status(strategy_name):
        data = load_strategy_status_data()
        item = data.get("strategies", {}).get(strategy_name)
        if not item:
            return {
                "ok": False,
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
                "label": "OK",
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

    def format_status_datetime(value):
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return value

    def mark_strategies_as_running():
        mark_strategies_as_running_file()

    def load_automation_schedules():
        rows = g.db.execute(
            text("SELECT * FROM automation_schedules ORDER BY task_name")
        ).mappings().fetchall()
        return {row["task_name"]: row for row in rows}

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

    def read_strategy_signals(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None:
            return []

        try:
            return [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ][:50]
        except OSError:
            return []

    def strategy_signals_updated_at(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None:
            return ""

        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return ""
        return updated_at.strftime("%d/%m/%Y %H:%M")

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
                    signals_txt_name TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        ensure_universe_table(connection)
        add_strategy_column(connection, "signals_txt_name")
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
                    last_run_key TEXT NOT NULL DEFAULT '',
                    last_run_at TIMESTAMP,
                    last_status TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
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
                    (task_name, is_enabled, start_time, runs_per_day, interval_minutes)
                    VALUES (:task_name, 0, '15:30', 1, 60)
                    """
                ),
                {"task_name": task_name},
            )


def ensure_default_real_strategies(connection):
    existing = {
        row["name"]: row
        for row in connection.execute(
            text("SELECT name, telegram_url FROM strategies")
        ).mappings().fetchall()
    }
    for strategy in DEFAULT_REAL_STRATEGIES:
        if strategy["name"] not in existing:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, signals_txt_name, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :signals_txt_name, 1)
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
                    is_active = 1
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


def add_strategy_column(connection, column_name):
    if strategy_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE strategies ADD COLUMN {column_name} TEXT NOT NULL DEFAULT ''"
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
