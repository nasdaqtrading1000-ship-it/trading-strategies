"""
Copia los datos principales de PostgreSQL a SQLite local.

Uso:
    python sync_postgres_to_sqlite.py

PostgreSQL sigue siendo la base principal para Render. SQLite se usa como copia
rapida para probar la web en 127.0.0.1 sin depender de la red.
"""

import os
from pathlib import Path
from decimal import Decimal

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from config_env import load_local_env


BASE_DIR = Path(__file__).resolve().parent
SQLITE_DATABASE = BASE_DIR / "strategies.db"
TABLES_TO_COPY = [
    "strategies",
    "users",
    "user_simulator_settings",
    "user_simulator_strategies",
    "automation_schedules",
    "strategy_signals",
    "simulated_operations",
    "asset_universe",
    "asset_snapshots",
    "top_money_volume_assets",
    "strategy_diagnostics",
    "market_news",
    "execution_status",
    "upload_file_status",
    "chip_status",
]


def normalized_database_url(url):
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def main():
    load_local_env()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Sincronizacion SQLite omitida: no hay DATABASE_URL PostgreSQL.")
        return 0
    if not database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        print("Sincronizacion SQLite omitida: DATABASE_URL no apunta a PostgreSQL.")
        return 0

    postgres_engine = create_engine(normalized_database_url(database_url), future=True)
    sqlite_engine = create_engine(f"sqlite:///{SQLITE_DATABASE}", future=True)
    ensure_sqlite_schema()

    total_rows = 0
    for table_name in TABLES_TO_COPY:
        copied = copy_table(postgres_engine, sqlite_engine, table_name)
        total_rows += copied
        print(f"{table_name}: {copied} filas copiadas a SQLite")

    print(f"SQLite local sincronizado: {total_rows} filas.")
    return 0


def ensure_sqlite_schema():
    previous_mode = os.environ.get("TRADING_DATABASE_MODE")
    os.environ["TRADING_DATABASE_MODE"] = "sqlite"
    try:
        import app  # noqa: F401
    finally:
        if previous_mode is None:
            os.environ.pop("TRADING_DATABASE_MODE", None)
        else:
            os.environ["TRADING_DATABASE_MODE"] = previous_mode


def copy_table(source_engine, target_engine, table_name):
    source_columns = table_columns(source_engine, table_name)
    target_columns = table_columns(target_engine, table_name)
    common_columns = [column for column in source_columns if column in target_columns]
    if not common_columns:
        return 0

    rows = read_rows(source_engine, table_name, common_columns)
    with target_engine.begin() as connection:
        connection.execute(text(f"DELETE FROM {table_name}"))
        if rows:
            connection.execute(insert_statement(table_name, common_columns), rows)
    return len(rows)


def table_columns(engine, table_name):
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return []
    return [column["name"] for column in inspector.get_columns(table_name)]


def read_rows(engine, table_name, columns):
    column_sql = ", ".join(columns)
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT {column_sql} FROM {table_name}")).mappings().fetchall()
    return [
        {
            key: sqlite_safe_value(value)
            for key, value in dict(row).items()
        }
        for row in rows
    ]


def sqlite_safe_value(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def insert_statement(table_name, columns):
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)
    return text(f"INSERT INTO {table_name} ({column_sql}) VALUES ({value_sql})")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as error:
        print(f"No se pudo sincronizar PostgreSQL con SQLite: {error}")
        raise SystemExit(1)
