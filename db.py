import os

from sqlalchemy import create_engine


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_DATABASE = os.path.join(BASE_DIR, "strategies.db")


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return f"sqlite:///{SQLITE_DATABASE}"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


engine = create_engine(database_url(), future=True)
