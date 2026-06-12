"""
Sincroniza los avisos generados en Estrategias/salidas_txt con la base de datos.

Uso local recomendado:
    python sync_signals_to_db.py

Necesita DATABASE_URL en .env apuntando a la base PostgreSQL de Render.
No sube archivos por Git y no reinicia la web.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re

from sqlalchemy import text

from config_env import load_local_env
from db import engine


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_DIR = BASE_DIR / "Estrategias" / "salidas_txt"
STATUS_FILE = BASE_DIR / "Estrategias" / "strategy_run_status.json"
SELECTION_FILE = BASE_DIR / "Estrategias" / "estrategias_a_ejecutar.txt"
TXT_RE = re.compile(r"^[^\\/]+\.txt$", re.IGNORECASE)

STRATEGIES = [
    {"name": "Momentum", "file": "Momentum.py", "txt": "Momentum.txt"},
    {"name": "Swing Trading", "file": "SwingTrading.py", "txt": "SwingTrading.txt"},
    {"name": "BreaKout", "file": "BreaKout.py", "txt": "BreaKout.txt"},
    {"name": "Mean Reversion", "file": "Mean Reversion.py", "txt": "Mean_Reversion.txt"},
    {"name": "Value Trading", "file": "ValueTrading.py", "txt": "ValueTrading.txt"},
    {"name": "Dividend Growth", "file": "DividenGrowth.py", "txt": "DividenGrowth.txt"},
    {"name": "Trend Following", "file": "TrendFollowing.py", "txt": "TrendFollowing.txt"},
    {"name": "Pairs Trading", "file": "PairsTrading.py", "txt": "PairsTrading.txt"},
    {"name": "Sector Rotation", "file": "SectorRotation.py", "txt": "SectorRotation.txt"},
    {"name": "Quality Investing", "file": "QualityInvesting.py", "txt": "QualityInvesting.txt"},
    {"name": "Opening Range BreaKout", "file": "OpeningRangeBreaKout.py", "txt": "OpeningRangeBreaKout.txt"},
    {"name": "VWAP Reversion", "file": "VWAP Reversion.py", "txt": "VWAP_Reversion.txt"},
    {"name": "Momentum Intradia", "file": "MomentumIntradia.py", "txt": "MomentumIntradia.txt"},
    {"name": "Scalping The PullBacks", "file": "ScalpingThePullBacKs.py", "txt": "ScalpingThePullBacKs.txt"},
    {"name": "Gap and Go", "file": "Gap and Go.py", "txt": "Gap_and_Go.txt"},
]


def main():
    load_local_env()
    if not os.environ.get("DATABASE_URL"):
        print("AVISO: no hay DATABASE_URL. Se sincronizara en SQLite local, Render no vera estos avisos.")
    else:
        print("DATABASE_URL detectada. Sincronizando con la base configurada.")

    ensure_strategy_signals_table()
    ensure_strategy_status_columns()
    active_count = sync_active_strategies_from_selection()
    if active_count is not None:
        print(f"Estrategias activas sincronizadas en PostgreSQL: {active_count}")

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
    status_count = sync_strategy_status()
    print(f"Estados de estrategias actualizados en PostgreSQL: {status_count}")
    return 0


def sync_active_strategies_from_selection():
    selected = selected_strategy_names_from_file()
    if selected is None:
        return None

    catalog_names = [strategy["name"] for strategy in STRATEGIES]
    with engine.begin() as connection:
        for strategy in STRATEGIES:
            is_active = 1 if strategy["name"] in selected else 0
            connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET is_active = :is_active
                    WHERE name = :name
                       OR python_file = :python_file
                       OR signals_txt_name = :txt_name
                    """
                ),
                {
                    "is_active": is_active,
                    "name": strategy["name"],
                    "python_file": strategy["file"],
                    "txt_name": strategy["txt"],
                },
            )
    return sum(1 for name in catalog_names if name in selected)


def selected_strategy_names_from_file():
    if not SELECTION_FILE.exists() or not SELECTION_FILE.is_file():
        return None

    requested = []
    for raw_line in SELECTION_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            requested.append(normalize_key(line))

    if not requested:
        return None

    selected = []
    for strategy in STRATEGIES:
        keys = {
            normalize_key(strategy["name"]),
            normalize_key(strategy["file"]),
            normalize_key(Path(strategy["file"]).stem),
            normalize_key(strategy["txt"]),
        }
        if any(value in keys for value in requested):
            selected.append(strategy["name"])
    return set(selected)


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


def ensure_strategy_status_columns():
    columns = {
        "run_status": "TEXT NOT NULL DEFAULT ''",
        "run_message": "TEXT NOT NULL DEFAULT ''",
        "run_at": "TIMESTAMP",
        "run_txt_updated": "INTEGER NOT NULL DEFAULT 0",
        "run_returncode": "INTEGER",
    }
    with engine.begin() as connection:
        for column_name, definition in columns.items():
            if strategy_column_exists(connection, column_name):
                continue
            connection.execute(
                text(f"ALTER TABLE strategies ADD COLUMN {column_name} {definition}")
            )


def strategy_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        return connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'strategies'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        ).scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(strategies)")).fetchall()
    return any(row[1] == column_name for row in rows)


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


def sync_strategy_status():
    if not STATUS_FILE.exists():
        return 0

    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    strategies = data.get("strategies", {})
    if not strategies:
        return 0

    updated = 0
    with engine.begin() as connection:
        for name, item in strategies.items():
            status = "OK" if item.get("ok") else "ERROR"
            error = item.get("error", "")
            message = "" if item.get("ok") else (error or "La estrategia termino con error.")
            result = connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = :run_status,
                        run_message = :run_message,
                        run_at = :run_at,
                        run_txt_updated = :run_txt_updated,
                        run_returncode = :run_returncode
                    WHERE name = :name
                       OR python_file = :python_file
                       OR signals_txt_name = :txt_name
                    """
                ),
                {
                    "name": name,
                    "python_file": item.get("file", ""),
                    "txt_name": item.get("txt", ""),
                    "run_status": status,
                    "run_message": message[:1000],
                    "run_at": parse_status_datetime(item.get("ran_at", "")),
                    "run_txt_updated": 1 if item.get("txt_updated") else 0,
                    "run_returncode": item.get("returncode"),
                },
            )
            updated += result.rowcount or 0
    return updated


def parse_status_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now()
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def signal_date_from_line(line):
    for part in str(line).split("|"):
        part = part.strip()
        if part.lower().startswith("fecha:"):
            value = part.split(":", 1)[1].strip()
            return value[:10]
    return datetime.now().date().isoformat()


def valid_txt_name(txt_name):
    return bool(TXT_RE.match(txt_name))


def normalize_key(value):
    return "".join(char.lower() for char in str(value) if char.isalnum())


if __name__ == "__main__":
    raise SystemExit(main())
