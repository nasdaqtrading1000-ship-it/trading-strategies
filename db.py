import os
import sys

from sqlalchemy import create_engine

from config_env import load_local_env


load_local_env()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_DATABASE = os.path.join(BASE_DIR, "strategies.db")


def running_on_render():
    return any(
        os.environ.get(key)
        for key in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_INSTANCE_ID")
    )


def running_local_web_app():
    entrypoint = os.path.basename(sys.argv[0] or "").lower()
    return entrypoint in {"app.py", "flask"} or os.environ.get("FLASK_RUN_FROM_CLI") == "true"


def normalized_database_url(url):
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def database_url():
    local_url = f"sqlite:///{SQLITE_DATABASE}"
    mode = os.environ.get("TRADING_DATABASE_MODE", "auto").strip().lower()
    url = os.environ.get("DATABASE_URL")

    if mode in {"local", "sqlite"}:
        return local_url
    if mode in {"remote", "postgres", "postgresql"} and url:
        return normalized_database_url(url)
    if not url:
        return local_url
    if running_on_render():
        return normalized_database_url(url)
    if running_local_web_app():
        return local_url
    return normalized_database_url(url)


engine = create_engine(database_url(), future=True)
