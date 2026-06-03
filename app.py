import os
import sqlite3
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
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "strategies.db")


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
    app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
        "ADMIN_PASSWORD_HASH", generate_password_hash("admin123")
    )

    @app.before_request
    def before_request():
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row

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
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, is_active
            FROM strategies
            WHERE is_active = 1
            ORDER BY created_at DESC
            """
        ).fetchall()
        return render_template("index.html", strategies=strategies)

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
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, is_active, created_at
            FROM strategies
            ORDER BY is_active DESC, created_at DESC
            """
        ).fetchall()
        return render_template("admin/dashboard.html", strategies=strategies)

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
            "UPDATE strategies SET is_active = ? WHERE id = ?",
            (next_state, strategy_id),
        )
        g.db.commit()
        flash("Estado actualizado.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/delete", methods=["POST"])
    @login_required
    def strategy_delete(strategy_id):
        get_strategy_or_404(strategy_id)
        g.db.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
        g.db.commit()
        flash("Estrategia eliminada.", "info")
        return redirect(url_for("admin_dashboard"))

    def get_strategy_or_404(strategy_id):
        strategy = g.db.execute(
            "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
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
                """
                UPDATE strategies
                SET name = ?, description = ?, risk_level = ?, signal_frequency = ?,
                    historical_return = ?, telegram_url = ?, is_active = ?
                WHERE id = ?
                """,
                (
                    name,
                    description,
                    risk_level,
                    signal_frequency,
                    historical_return,
                    telegram_url,
                    is_active,
                    strategy_id,
                ),
            )
            flash("Estrategia actualizada.", "success")
        else:
            g.db.execute(
                """
                INSERT INTO strategies
                (name, description, risk_level, signal_frequency,
                 historical_return, telegram_url, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    description,
                    risk_level,
                    signal_frequency,
                    historical_return,
                    telegram_url,
                    is_active,
                ),
            )
            flash("Estrategia creada.", "success")

        g.db.commit()
        return redirect(url_for("admin_dashboard"))

    return app


def init_db():
    connection = sqlite3.connect(DATABASE)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    count = connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    if count == 0:
        connection.executemany(
            """
            INSERT INTO strategies
            (name, description, risk_level, signal_frequency,
             historical_return, telegram_url, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "Momentum Intradia",
                    "Entrada en activos con ruptura de volumen y confirmacion de tendencia a corto plazo.",
                    "Medio",
                    "3-6 senales por semana",
                    "+18.4% anualizado",
                    "https://t.me/tu_canal_momentum",
                    1,
                ),
                (
                    "Swing Conservador",
                    "Operativa de varios dias con gestion estricta del riesgo y objetivos parciales.",
                    "Bajo",
                    "1-3 senales por semana",
                    "+11.2% anualizado",
                    "https://t.me/tu_canal_swing",
                    1,
                ),
                (
                    "Crypto Breakout",
                    "Seguimiento de rupturas en criptomonedas liquidas con stops dinamicos.",
                    "Alto",
                    "5-10 senales por semana",
                    "+34.7% anualizado",
                    "https://t.me/tu_canal_crypto",
                    1,
                ),
            ],
        )

    connection.commit()
    connection.close()


init_db()
app = create_app()


if __name__ == "__main__":
    debug_enabled = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
