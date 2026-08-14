"""
Sincroniza la curva de capital local hacia PostgreSQL.

Lee strategy_equity_curve desde strategies.db y envia solo los puntos que faltan
en PostgreSQL, identificados por (txt_name, curve_date). Por defecto no pisa
puntos existentes.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from config_env import load_local_env


BASE_DIR = Path(__file__).resolve().parent
SQLITE_DATABASE = BASE_DIR / "strategies.db"
BATCH_SIZE = 1000
COLUMNS = [
    "txt_name",
    "strategy_name",
    "curve_date",
    "capital_actual",
    "capital_aportado",
    "capital_invertido",
    "profit_usd",
    "return_pct",
    "open_operations",
    "closed_operations",
    "source",
    "updated_at",
]
INTRADAY_COLUMNS = [
    "txt_name",
    "strategy_name",
    "point_date",
    "point_at",
    "capital_actual",
    "capital_aportado",
    "capital_invertido",
    "profit_usd",
    "return_pct",
    "open_operations",
    "closed_operations",
    "source",
    "updated_at",
]


def main() -> int:
    load_local_env()
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Curva capital sync | omitido: no hay DATABASE_URL PostgreSQL.")
        return 0
    if not database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        print("Curva capital sync | omitido: DATABASE_URL no apunta a PostgreSQL.")
        return 0
    if not SQLITE_DATABASE.exists():
        print(f"Curva capital sync | omitido: no existe SQLite local {SQLITE_DATABASE}.")
        return 0

    postgres_engine = create_engine(normalized_database_url(database_url), future=True)
    local_rows = load_local_curve_rows()
    if not local_rows:
        print("Curva capital sync | no hay puntos locales para enviar.")
        return 0

    with postgres_engine.begin() as connection:
        ensure_equity_curve_table(connection)
        ensure_equity_curve_intraday_table(connection)
        cleanup_postgres_intraday_curve_rows(connection)
        rows_to_send = rows_missing_in_postgres(connection, local_rows, upsert_existing=args.upsert_existing)
        sent = insert_postgres_rows(connection, rows_to_send, upsert_existing=args.upsert_existing)
        local_intraday_rows = load_local_intraday_curve_rows()
        intraday_rows_to_send = intraday_rows_missing_in_postgres(
            connection,
            local_intraday_rows,
            upsert_existing=args.upsert_existing,
        )
        intraday_sent = insert_postgres_intraday_rows(
            connection,
            intraday_rows_to_send,
            upsert_existing=args.upsert_existing,
        )

    skipped = len(local_rows) - len(rows_to_send)
    mode = "upsert" if args.upsert_existing else "solo faltantes"
    print(
        "Curva capital sync | terminado | "
        f"modo={mode} | locales={len(local_rows)} | enviados={sent} | existentes={skipped} | "
        f"intradia_locales={len(local_intraday_rows)} | intradia_enviados={intraday_sent}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sincroniza strategy_equity_curve local hacia PostgreSQL.")
    parser.add_argument(
        "--upsert-existing",
        action="store_true",
        help="Actualiza tambien puntos ya existentes. Por defecto solo inserta faltantes.",
    )
    return parser.parse_args()


def normalized_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_equity_curve_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS strategy_equity_curve (
                txt_name TEXT NOT NULL,
                strategy_name TEXT NOT NULL DEFAULT '',
                curve_date TEXT NOT NULL,
                capital_actual FLOAT NOT NULL DEFAULT 0,
                capital_aportado FLOAT NOT NULL DEFAULT 0,
                capital_invertido FLOAT NOT NULL DEFAULT 0,
                profit_usd FLOAT NOT NULL DEFAULT 0,
                return_pct FLOAT NOT NULL DEFAULT 0,
                open_operations INTEGER NOT NULL DEFAULT 0,
                closed_operations INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'simulated_operations',
                updated_at TIMESTAMP,
                PRIMARY KEY (txt_name, curve_date)
            )
            """
        )
    )
    ensure_equity_curve_column(connection, "capital_invertido", "FLOAT NOT NULL DEFAULT 0")
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_strategy_equity_curve_txt_date
            ON strategy_equity_curve(txt_name, curve_date)
            """
        )
    )


def ensure_equity_curve_intraday_table(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS strategy_equity_curve_intraday (
                txt_name TEXT NOT NULL,
                strategy_name TEXT NOT NULL DEFAULT '',
                point_date TEXT NOT NULL,
                point_at TEXT NOT NULL,
                capital_actual FLOAT NOT NULL DEFAULT 0,
                capital_aportado FLOAT NOT NULL DEFAULT 0,
                capital_invertido FLOAT NOT NULL DEFAULT 0,
                profit_usd FLOAT NOT NULL DEFAULT 0,
                return_pct FLOAT NOT NULL DEFAULT 0,
                open_operations INTEGER NOT NULL DEFAULT 0,
                closed_operations INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'simulated_operations_intraday',
                updated_at TIMESTAMP,
                PRIMARY KEY (txt_name, point_at)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_strategy_equity_curve_intraday_txt_at
            ON strategy_equity_curve_intraday(txt_name, point_at)
            """
        )
    )


def cleanup_postgres_intraday_curve_rows(connection) -> None:
    connection.execute(
        text("DELETE FROM strategy_equity_curve_intraday WHERE point_date < :today"),
        {"today": date.today().isoformat()},
    )


def ensure_equity_curve_column(connection, column_name: str, definition: str) -> None:
    if table_column_exists(connection, "strategy_equity_curve", column_name):
        return
    connection.execute(text(f"ALTER TABLE strategy_equity_curve ADD COLUMN {column_name} {definition}"))


def table_column_exists(connection, table_name: str, column_name: str) -> bool:
    result = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    return result.scalar_one() > 0


def load_local_curve_rows() -> list[dict[str, Any]]:
    connection = sqlite3.connect(SQLITE_DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT {", ".join(COLUMNS)}
            FROM strategy_equity_curve
            ORDER BY txt_name, curve_date
            """
        ).fetchall()
    finally:
        connection.close()
    return [normalize_row(dict(row)) for row in rows]


def load_local_intraday_curve_rows() -> list[dict[str, Any]]:
    connection = sqlite3.connect(SQLITE_DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        if not sqlite_table_exists(connection, "strategy_equity_curve_intraday"):
            return []
        rows = connection.execute(
            f"""
            SELECT {", ".join(INTRADAY_COLUMNS)}
            FROM strategy_equity_curve_intraday
            ORDER BY txt_name, point_at
            """
        ).fetchall()
    finally:
        connection.close()
    return [normalize_row(dict(row)) for row in rows]


def sqlite_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            value = float(value)
        if key == "updated_at" and isinstance(value, str):
            value = parse_datetime(value) or value
        normalized[key] = value
    return normalized


def parse_datetime(value: str) -> datetime | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        return None


def rows_missing_in_postgres(connection, local_rows: list[dict[str, Any]], upsert_existing: bool) -> list[dict[str, Any]]:
    if upsert_existing:
        return local_rows
    existing_keys = {
        (row["txt_name"], row["curve_date"])
        for row in connection.execute(text("SELECT txt_name, curve_date FROM strategy_equity_curve")).mappings()
    }
    return [
        row
        for row in local_rows
        if (row["txt_name"], row["curve_date"]) not in existing_keys
    ]


def intraday_rows_missing_in_postgres(connection, local_rows: list[dict[str, Any]], upsert_existing: bool) -> list[dict[str, Any]]:
    if upsert_existing:
        return local_rows
    existing_keys = {
        (row["txt_name"], row["point_at"])
        for row in connection.execute(text("SELECT txt_name, point_at FROM strategy_equity_curve_intraday")).mappings()
    }
    return [
        row
        for row in local_rows
        if (row["txt_name"], row["point_at"]) not in existing_keys
    ]


def insert_postgres_rows(connection, rows: list[dict[str, Any]], upsert_existing: bool = False) -> int:
    if not rows:
        return 0
    statement = postgres_upsert_statement() if upsert_existing else postgres_insert_missing_statement()
    total = 0
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset: offset + BATCH_SIZE]
        connection.execute(statement, batch)
        total += len(batch)
    return total


def insert_postgres_intraday_rows(connection, rows: list[dict[str, Any]], upsert_existing: bool = False) -> int:
    if not rows:
        return 0
    statement = postgres_intraday_upsert_statement() if upsert_existing else postgres_intraday_insert_missing_statement()
    total = 0
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset: offset + BATCH_SIZE]
        connection.execute(statement, batch)
        total += len(batch)
    return total


def postgres_insert_missing_statement():
    column_sql = ", ".join(COLUMNS)
    value_sql = ", ".join(f":{column}" for column in COLUMNS)
    return text(
        f"""
        INSERT INTO strategy_equity_curve ({column_sql})
        VALUES ({value_sql})
        ON CONFLICT (txt_name, curve_date) DO NOTHING
        """
    )


def postgres_intraday_insert_missing_statement():
    column_sql = ", ".join(INTRADAY_COLUMNS)
    value_sql = ", ".join(f":{column}" for column in INTRADAY_COLUMNS)
    return text(
        f"""
        INSERT INTO strategy_equity_curve_intraday ({column_sql})
        VALUES ({value_sql})
        ON CONFLICT (txt_name, point_at) DO NOTHING
        """
    )


def postgres_upsert_statement():
    column_sql = ", ".join(COLUMNS)
    value_sql = ", ".join(f":{column}" for column in COLUMNS)
    return text(
        f"""
        INSERT INTO strategy_equity_curve ({column_sql})
        VALUES ({value_sql})
        ON CONFLICT (txt_name, curve_date) DO UPDATE SET
            strategy_name = EXCLUDED.strategy_name,
            capital_actual = EXCLUDED.capital_actual,
            capital_aportado = EXCLUDED.capital_aportado,
            capital_invertido = EXCLUDED.capital_invertido,
            profit_usd = EXCLUDED.profit_usd,
            return_pct = EXCLUDED.return_pct,
            open_operations = EXCLUDED.open_operations,
            closed_operations = EXCLUDED.closed_operations,
            source = EXCLUDED.source,
            updated_at = EXCLUDED.updated_at
        """
    )


def postgres_intraday_upsert_statement():
    column_sql = ", ".join(INTRADAY_COLUMNS)
    value_sql = ", ".join(f":{column}" for column in INTRADAY_COLUMNS)
    return text(
        f"""
        INSERT INTO strategy_equity_curve_intraday ({column_sql})
        VALUES ({value_sql})
        ON CONFLICT (txt_name, point_at) DO UPDATE SET
            strategy_name = EXCLUDED.strategy_name,
            point_date = EXCLUDED.point_date,
            capital_actual = EXCLUDED.capital_actual,
            capital_aportado = EXCLUDED.capital_aportado,
            capital_invertido = EXCLUDED.capital_invertido,
            profit_usd = EXCLUDED.profit_usd,
            return_pct = EXCLUDED.return_pct,
            open_operations = EXCLUDED.open_operations,
            closed_operations = EXCLUDED.closed_operations,
            source = EXCLUDED.source,
            updated_at = EXCLUDED.updated_at
        """
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as error:
        print(f"Curva capital sync | error PostgreSQL: {error}")
        raise SystemExit(1)
