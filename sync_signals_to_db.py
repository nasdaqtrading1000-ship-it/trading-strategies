"""
Sincroniza los avisos generados en Estrategias/salidas_txt con la base de datos.

Uso local recomendado:
    python sync_signals_to_db.py

Necesita DATABASE_URL en .env apuntando a la base PostgreSQL de Render.
No sube archivos por Git y no reinicia la web.
"""

from datetime import datetime
from pathlib import Path
import re

from sqlalchemy import text

from config_env import load_local_env
from db import engine


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_DIR = BASE_DIR / "Estrategias" / "salidas_txt"
TXT_RE = re.compile(r"^[^\\/]+\.txt$", re.IGNORECASE)


def main():
    load_local_env()
    ensure_strategy_signals_table()

    if not SIGNALS_DIR.exists():
        print(f"No existe la carpeta: {SIGNALS_DIR}")
        return 1

    total_files = 0
    total_lines = 0
    inserted = 0

    for path in sorted(SIGNALS_DIR.glob("*.txt")):
        if not valid_txt_name(path.name):
            continue
        file_lines, file_inserted = sync_file(path)
        total_files += 1
        total_lines += file_lines
        inserted += file_inserted
        print(f"{path.name}: {file_lines} lineas, {file_inserted} nuevas")

    print("")
    print(f"Sincronizacion terminada: {total_files} TXT, {total_lines} lineas, {inserted} nuevas.")
    return 0


def ensure_strategy_signals_table():
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    with engine.begin() as connection:
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


def sync_file(path):
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        return 0, 0

    inserted = 0
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
                {"txt_name": path.name, "signal_date": signal_date, "line": line},
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
                {"txt_name": path.name, "signal_date": signal_date, "line": line},
            )
            inserted += 1
    return len(lines), inserted


def signal_date_from_line(line):
    for part in str(line).split("|"):
        part = part.strip()
        if part.lower().startswith("fecha:"):
            value = part.split(":", 1)[1].strip()
            return value[:10]
    return datetime.now().date().isoformat()


def valid_txt_name(txt_name):
    return bool(TXT_RE.match(txt_name))


if __name__ == "__main__":
    raise SystemExit(main())
