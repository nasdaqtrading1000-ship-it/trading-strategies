import os
from functools import wraps

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
    snapshot_count,
)
from update_market_data import update_market_data
from update_assets import build_assets_from_alpaca, write_assets


def database_status():
    url = engine.url
    return {
        "dialect": engine.dialect.name,
        "database": url.database or "",
        "host": url.host or "local file",
        "is_persistent": engine.dialect.name == "postgresql",
    }


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
    app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
        "ADMIN_PASSWORD_HASH", generate_password_hash("admin123")
    )

    @app.before_request
    def before_request():
        g.db = engine.connect()

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

    @app.route("/")
    def index():
        strategies = g.db.execute(
            text(
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, is_active
            FROM strategies
            WHERE is_active = 1
            ORDER BY created_at DESC
            """
            )
        ).mappings().fetchall()
        return render_template("index.html", strategies=strategies)

    @app.route("/filtrado-activos")
    def asset_filter():
        filters = {
            "month_window": int(request.args.get("month_window", 1)),
            "min_money_volume": int(request.args.get("min_money_volume", 500)),
            "day_volume_window": int(request.args.get("day_volume_window", 1)),
            "week_volume_window": int(request.args.get("week_volume_window", 1)),
            "limit": int(request.args.get("limit", 10)),
            "sector": request.args.get("sector", "Todos"),
            "market": request.args.get("market", "Todos"),
            "data_source": request.args.get("data_source", "csv"),
        }
        assets = load_assets()
        csv_total = len(assets)
        snapshots_total = snapshot_count()
        sectors = available_sectors(assets)
        markets = available_markets(assets)
        results, data_source, universe_total = filter_assets(filters, assets)
        return render_template(
            "asset_filter.html",
            filters=filters,
            results=results,
            sectors=sectors,
            markets=markets,
            data_source=data_source,
            universe_total=universe_total,
            csv_total=csv_total,
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
                   historical_return, telegram_url, is_active, created_at
            FROM strategies
            ORDER BY is_active DESC, created_at DESC
            """
            )
        ).mappings().fetchall()
        return render_template("admin/dashboard.html", strategies=strategies)

    @app.route("/admin/system")
    @login_required
    def admin_system():
        return render_template("admin/system.html", database=database_status())

    @app.route("/admin/market-data/update", methods=["POST"])
    @login_required
    def admin_market_data_update():
        result = update_market_data()
        if result == 0:
            flash("Datos de mercado actualizados correctamente.", "success")
        else:
            flash(
                "No se pudieron actualizar los datos. Revisa las claves de Alpaca y las variables del servicio.",
                "danger",
            )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/assets/update-csv", methods=["POST"])
    @login_required
    def admin_assets_update_csv():
        rows, source = build_assets_from_alpaca()
        write_assets(rows)
        flash(
            f"CSV de activos actualizado correctamente: {len(rows)} activos. Fuente: {source}.",
            "success",
        )
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

        form_strategy = {
            "id": strategy_id,
            "name": name,
            "description": description,
            "risk_level": risk_level,
            "signal_frequency": signal_frequency,
            "historical_return": historical_return,
            "telegram_url": telegram_url,
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
                    telegram_url = :telegram_url, is_active = :is_active
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
                 historical_return, telegram_url, is_active)
                VALUES (:name, :description, :risk_level, :signal_frequency,
                        :historical_return, :telegram_url, :is_active)
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "is_active": is_active,
                },
            )
            flash("Estrategia creada.", "success")

        g.db.commit()
        return redirect(url_for("admin_dashboard"))

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
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

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
                    day_volume_score FLOAT NOT NULL,
                    week_volume_score FLOAT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )


init_db()
app = create_app()


if __name__ == "__main__":
    debug_enabled = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
