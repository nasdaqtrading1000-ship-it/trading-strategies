"""
Construye la curva diaria de capital por estrategia.

Lee operaciones ya simuladas/backtest desde simulated_operations y precios diarios
desde EstrategiasV2/historical_data/daily_txt. Si la curva ya existe, solo anade
las fechas posteriores hasta hoy, salvo que se use --rebuild.
"""

from __future__ import annotations

import argparse
import csv
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config_env import load_local_env
from db import engine


BASE_DIR = Path(__file__).resolve().parent
BACKTEST_JSON_FILE = BASE_DIR / "EstrategiasV2" / "outputs" / "historical_backtest_5y.json"
BACKTEST_INCLUDED_TXT = BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_backtest_incluidas.txt"
HISTORICAL_PRICE_DIR = BASE_DIR / "EstrategiasV2" / "historical_data" / "daily_txt"
CAPITAL_MAX_FILE = BASE_DIR / "Estrategias" / "operaciones_simuladas" / "capital_maximos_estrategias.txt"
DEFAULT_TRADE_USD = 1000.0
DEFAULT_STRATEGY_CAPITAL_USD = 50_000.0


@dataclass
class Operation:
    strategy_name: str
    txt_name: str
    symbol: str
    direction: str
    entry_date: date
    close_date: date | None
    entry_price: float
    exit_price: float
    current_price: float
    shares: float
    profit_usd: float
    status: str
    operation_key: str = ""

    @property
    def entry_value(self) -> float:
        return self.entry_price * self.shares

    @property
    def final_value(self) -> float:
        if self.profit_usd:
            return self.entry_value + self.profit_usd
        if self.exit_price:
            return marked_value(self, self.exit_price)
        return marked_value(self, self.current_price or self.entry_price)


def main() -> int:
    load_local_env()
    args = parse_args()
    today = date.today()
    intraday_point_at = datetime.now().replace(second=0, microsecond=0)
    with engine.begin() as connection:
        ensure_equity_curve_table(connection)
        ensure_equity_curve_intraday_table(connection)
        cleanup_intraday_curve_rows(connection, today)
        strategies = load_strategies(connection)
        backtest_txt_operations = load_operations_from_backtest_txt()
        operations = load_operations_from_database(connection, include_backtest=not bool(backtest_txt_operations))
        if backtest_txt_operations:
            operations.extend(backtest_txt_operations)
        if not operations:
            operations = load_operations_from_backtest_json(strategies)
        if not operations:
            print("Curva capital | sin operaciones simuladas/backtest para procesar.")
            return 0
        if args.rebuild:
            connection.execute(text("DELETE FROM strategy_equity_curve"))
            print("Curva capital | rebuild activo | tabla limpiada.")

        grouped = group_operations(operations)
        capital_bases = load_strategy_capital_bases()
        required_symbols = sorted({operation.symbol for operation in operations})
        prices = load_price_series(required_symbols)
        curve_dates = build_curve_dates(prices, operations, today)
        total_rows = 0
        for txt_name, strategy_ops in sorted(grouped.items()):
            strategy_name = strategy_ops[0].strategy_name
            dates_to_save = curve_dates_to_save(connection, txt_name, curve_dates, args.rebuild, args.refresh_days, today)
            latest_price_date = latest_price_date_for_operations(prices, strategy_ops)
            dates_to_save = reliable_curve_dates_to_save(dates_to_save, latest_price_date, today)
            if not dates_to_save:
                print(f"Curva capital | {txt_name} | ya actualizada.")
                continue
            all_rows = build_strategy_curve(
                strategy_name,
                txt_name,
                strategy_ops,
                prices,
                curve_dates,
                capital_bases.get(txt_name, 0.0),
            )
            rows = [
                row
                for row in all_rows
                if parse_operation_date(row["curve_date"]) in dates_to_save
            ]
            if rows:
                upsert_curve_rows(connection, rows)
                intraday_row = next((row for row in reversed(rows) if row["curve_date"] == today.isoformat()), None)
                if intraday_row:
                    upsert_intraday_curve_row(connection, intraday_row, intraday_point_at)
                total_rows += len(rows)
                print(
                    f"Curva capital | {txt_name} | {len(rows)} puntos | "
                    f"{rows[0]['curve_date']} -> {rows[-1]['curve_date']}"
                )
        print(f"Curva capital | terminado | puntos nuevos/actualizados={total_rows}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Construye strategy_equity_curve.")
    parser.add_argument("--rebuild", action="store_true", help="Borra y reconstruye toda la curva.")
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=45,
        help="Recalcula y actualiza los ultimos N dias para reparar/actualizar P/L reciente.",
    )
    return parser.parse_args()


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


def cleanup_intraday_curve_rows(connection, today: date) -> None:
    connection.execute(
        text("DELETE FROM strategy_equity_curve_intraday WHERE point_date < :today"),
        {"today": today.isoformat()},
    )


def ensure_equity_curve_column(connection, column_name: str, definition: str) -> None:
    if table_column_exists(connection, "strategy_equity_curve", column_name):
        return
    connection.execute(text(f"ALTER TABLE strategy_equity_curve ADD COLUMN {column_name} {definition}"))


def table_column_exists(connection, table_name: str, column_name: str) -> bool:
    if engine is not None and getattr(engine.dialect, "name", "") == "postgresql":
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
    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(row[1] == column_name for row in rows)


def load_strategies(connection) -> dict[str, dict[str, str]]:
    rows = connection.execute(
        text("SELECT name, signals_txt_name FROM strategies WHERE COALESCE(signals_txt_name, '') <> ''")
    ).mappings().fetchall()
    return {
        normalize_name(row["name"]): {
            "name": row["name"],
            "txt_name": row["signals_txt_name"],
        }
        for row in rows
    }


def load_operations_from_database(connection, include_backtest: bool = True) -> list[Operation]:
    backtest_filter = "" if include_backtest else "AND operation_key NOT LIKE 'BACKTEST|%'"
    rows = connection.execute(
        text(
            f"""
            SELECT operation_key, strategy_name, txt_name, symbol, direction, status,
                   signal_date, opened_at, closed_at, entry_price,
                   shares, current_price, profit_usd
            FROM simulated_operations
            WHERE COALESCE(txt_name, '') <> ''
              AND COALESCE(symbol, '') <> ''
              AND COALESCE(entry_price, 0) > 0
              AND COALESCE(shares, 0) > 0
              {backtest_filter}
            """
        )
    ).mappings().fetchall()
    operations = []
    for row in rows:
        operation = operation_from_db_row(row)
        if operation is not None:
            operations.append(operation)
    return operations


def operation_from_db_row(row) -> Operation | None:
    entry_date = parse_operation_date(row.get("signal_date")) or parse_operation_date(row.get("opened_at"))
    if entry_date is None:
        return None
    closed_at = parse_operation_date(row.get("closed_at"), shift_late_utc=True)
    status = str(row.get("status") or "").upper()
    if status != "CLOSED":
        closed_at = None
    entry_price = safe_float(row.get("entry_price"))
    shares = safe_float(row.get("shares"))
    current_price = safe_float(row.get("current_price")) or entry_price
    profit_usd = safe_float(row.get("profit_usd"))
    exit_price = current_price
    return Operation(
        strategy_name=str(row.get("strategy_name") or row.get("txt_name") or ""),
        txt_name=str(row.get("txt_name") or ""),
        symbol=str(row.get("symbol") or "").upper(),
        direction=str(row.get("direction") or "LONG").upper(),
        entry_date=entry_date,
        close_date=closed_at,
        entry_price=entry_price,
        exit_price=exit_price,
        current_price=current_price,
        shares=shares,
        profit_usd=profit_usd,
        status=status or "OPEN",
        operation_key=str(row.get("operation_key") or ""),
    )


def load_operations_from_backtest_txt() -> list[Operation]:
    if not BACKTEST_INCLUDED_TXT.exists():
        return []
    operations = []
    try:
        with BACKTEST_INCLUDED_TXT.open(encoding="utf-8", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                operation = operation_from_text_line(raw_line, line_number)
                if operation is not None:
                    operations.append(operation)
    except OSError:
        return []
    return operations


def operation_from_text_line(raw_line: str, line_number: int) -> Operation | None:
    line = raw_line.strip()
    if not line or " | " not in line:
        return None
    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 3:
        return None
    strategy_name = parts[0]
    txt_name = parts[1]
    symbol = parts[2].upper()
    fields = {}
    for part in parts[3:]:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[normalize_field_name(key)] = value.strip()
    status = str(fields.get("estado") or "CLOSED").upper()
    entry_date = parse_operation_date(fields.get("fecha_aviso") or fields.get("ejecutada"))
    if entry_date is None:
        return None
    close_date = parse_operation_date(fields.get("cerrada")) if status == "CLOSED" else None
    entry_price = safe_float(fields.get("entrada"))
    shares = safe_float(fields.get("acciones"))
    if not entry_price or not shares:
        return None
    current_price = safe_float(fields.get("precio_actual")) or entry_price
    return Operation(
        strategy_name=strategy_name,
        txt_name=txt_name,
        symbol=symbol,
        direction=str(fields.get("direccion") or "LONG").upper(),
        entry_date=entry_date,
        close_date=close_date,
        entry_price=entry_price,
        exit_price=current_price,
        current_price=current_price,
        shares=shares,
        profit_usd=safe_float(fields.get("beneficio_usd")),
        status=status,
        operation_key=f"BACKTEST_TXT|{txt_name}|{symbol}|{entry_date.isoformat()}|{line_number}",
    )


def load_operations_from_backtest_json(strategies: dict[str, dict[str, str]]) -> list[Operation]:
    if not BACKTEST_JSON_FILE.exists():
        return []
    try:
        payload = json.loads(BACKTEST_JSON_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_operations = list(payload.get("closed_operations") or []) + list(payload.get("open_operations") or [])
    operations = []
    for raw in raw_operations:
        strategy_name = str(raw.get("strategy") or raw.get("strategy_name") or "")
        strategy = strategies.get(normalize_name(strategy_name), {})
        txt_name = strategy.get("txt_name") or f"{strategy_name.replace(' ', '')}.txt"
        entry_date = parse_operation_date(raw.get("signal_date") or raw.get("entry_date"))
        if entry_date is None:
            continue
        status = str(raw.get("status") or "CLOSED").upper()
        close_date = parse_operation_date(raw.get("exit_date")) if status == "CLOSED" else None
        entry_price = safe_float(raw.get("entry_price"))
        shares = safe_float(raw.get("shares"))
        if not entry_price or not shares:
            continue
        exit_price = safe_float(raw.get("exit_price")) or entry_price
        operations.append(
            Operation(
                strategy_name=strategy.get("name") or strategy_name,
                txt_name=txt_name,
                symbol=str(raw.get("symbol") or "").upper(),
                direction=str(raw.get("direction") or "LONG").upper(),
                entry_date=entry_date,
                close_date=close_date,
                entry_price=entry_price,
                exit_price=exit_price,
                current_price=exit_price,
                shares=shares,
                profit_usd=safe_float(raw.get("profit_usd")),
                status=status,
            )
        )
    return operations


def group_operations(operations: list[Operation]) -> dict[str, list[Operation]]:
    grouped: dict[str, list[Operation]] = {}
    for operation in operations:
        grouped.setdefault(operation.txt_name, []).append(operation)
    for rows in grouped.values():
        rows.sort(key=lambda item: item.entry_date)
    return grouped


def load_strategy_capital_bases() -> dict[str, float]:
    if not CAPITAL_MAX_FILE.exists():
        return {}
    bases: dict[str, float] = {}
    try:
        with CAPITAL_MAX_FILE.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [part.strip() for part in line.split("|")]
                if len(parts) < 4:
                    continue
                txt_name = parts[1]
                capital_base = safe_float(parts[3])
                if txt_name and capital_base:
                    bases[txt_name] = capital_base
    except OSError:
        return {}
    return bases


def load_price_series(symbols: list[str]) -> dict[str, dict[str, Any]]:
    prices = {}
    for symbol in symbols:
        path = HISTORICAL_PRICE_DIR / f"{symbol}.txt"
        if not path.exists():
            continue
        dated_prices = {}
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    row_date = parse_operation_date(row.get("timestamp"))
                    close = safe_float(row.get("close"))
                    if row_date and close:
                        dated_prices[row_date] = close
        except OSError:
            continue
        if dated_prices:
            dates = sorted(dated_prices)
            prices[symbol] = {"dates": dates, "values": dated_prices}
    return prices


def build_curve_dates(prices: dict[str, dict[str, Any]], operations: list[Operation], today: date) -> list[date]:
    date_values = {item for series in prices.values() for item in series["dates"]}
    if operations:
        first = min(operation.entry_date for operation in operations)
        date_values.update(first + timedelta(days=offset) for offset in range((today - first).days + 1))
    dates = sorted(date_values)
    if today not in dates:
        dates.append(today)
    return sorted(date_item for date_item in dates if date_item <= today)


def next_curve_date(connection, txt_name: str, rebuild: bool) -> date:
    if rebuild:
        return date.min
    max_date = connection.execute(
        text("SELECT MAX(curve_date) FROM strategy_equity_curve WHERE txt_name = :txt_name"),
        {"txt_name": txt_name},
    ).scalar()
    parsed = parse_operation_date(max_date)
    if parsed is None:
        return date.min
    return parsed + timedelta(days=1)


def missing_curve_dates(connection, txt_name: str, curve_dates: list[date], rebuild: bool) -> list[date]:
    if rebuild:
        return list(curve_dates)
    existing_dates = load_existing_curve_dates(connection, txt_name)
    return [item for item in curve_dates if item not in existing_dates]


def curve_dates_to_save(
    connection,
    txt_name: str,
    curve_dates: list[date],
    rebuild: bool,
    refresh_days: int,
    today: date,
) -> set[date]:
    if rebuild:
        return set(curve_dates)
    existing_dates = load_existing_curve_dates(connection, txt_name)
    missing_dates = {item for item in curve_dates if item not in existing_dates}
    refresh_start = today - timedelta(days=max(0, int(refresh_days or 0) - 1))
    recent_dates = {item for item in curve_dates if item >= refresh_start}
    return missing_dates | recent_dates


def latest_price_date_for_operations(prices: dict[str, dict[str, Any]], operations: list[Operation]) -> date | None:
    latest_dates = [
        series["dates"][-1]
        for operation in operations
        for series in [prices.get(operation.symbol)]
        if series and series.get("dates")
    ]
    return max(latest_dates) if latest_dates else None


def reliable_curve_dates_to_save(dates_to_save: set[date], latest_price_date: date | None, today: date) -> set[date]:
    if latest_price_date is None:
        return {item for item in dates_to_save if item >= today}
    return {
        item
        for item in dates_to_save
        if item <= latest_price_date or item >= today
    }


def load_existing_curve_dates(connection, txt_name: str) -> set[date]:
    rows = connection.execute(
        text("SELECT curve_date FROM strategy_equity_curve WHERE txt_name = :txt_name"),
        {"txt_name": txt_name},
    ).fetchall()
    return {
        parsed
        for row in rows
        for parsed in [parse_operation_date(row[0])]
        if parsed is not None
    }


def build_strategy_curve(
    strategy_name: str,
    txt_name: str,
    operations: list[Operation],
    prices: dict[str, dict[str, Any]],
    curve_dates: list[date],
    fixed_capital_base: float = 0.0,
) -> list[dict[str, Any]]:
    rows = []
    sorted_operations = sorted(operations, key=lambda item: item.entry_date)
    next_operation = 0
    active_operations: list[Operation] = []
    realized_profit = 0.0
    closed_count = 0
    max_account_capital = 0.0
    first_operation_date = sorted_operations[0].entry_date if sorted_operations else None
    for curve_date in curve_dates:
        if first_operation_date and curve_date < first_operation_date:
            continue
        while next_operation < len(sorted_operations) and sorted_operations[next_operation].entry_date <= curve_date:
            operation = sorted_operations[next_operation]
            active_operations.append(operation)
            next_operation += 1
        max_account_capital = max(max_account_capital, account_capital_needed(active_operations))
        if fixed_capital_base and curve_date >= date.today():
            max_account_capital = max(max_account_capital, fixed_capital_base)
        capital_base = max(DEFAULT_STRATEGY_CAPITAL_USD, max_account_capital)
        if not capital_base:
            continue
        unrealized_profit = 0.0
        remaining_active = []
        for operation in active_operations:
            if operation.close_date and operation.close_date <= curve_date:
                realized_profit += operation.profit_usd or operation_profit(
                    operation,
                    operation.exit_price or operation.current_price or operation.entry_price,
                )
                closed_count += 1
                continue
            remaining_active.append(operation)
            if curve_date >= date.today():
                price = operation.current_price
            else:
                price = price_on_or_before(prices, operation.symbol, curve_date)
            unrealized_profit += operation_profit(operation, price or operation.entry_price)
        active_operations = remaining_active
        profit = realized_profit + unrealized_profit
        invested_capital = account_capital_needed(active_operations)
        current_capital = capital_base + profit
        return_pct = (profit / capital_base) * 100 if capital_base else 0.0
        rows.append(
            {
                "txt_name": txt_name,
                "strategy_name": strategy_name,
                "curve_date": curve_date.isoformat(),
                "capital_actual": round(current_capital, 4),
                "capital_aportado": round(capital_base, 4),
                "capital_invertido": round(invested_capital, 4),
                "profit_usd": round(profit, 4),
                "return_pct": round(return_pct, 6),
                "open_operations": len(active_operations),
                "closed_operations": closed_count,
                "source": "simulated_operations",
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )
    return rows


def account_capital_needed(
    active_operations: list[Operation],
) -> float:
    return len(active_operations) * DEFAULT_TRADE_USD


def marked_value(operation: Operation, price: float) -> float:
    if operation.direction == "SHORT":
        return operation.entry_value + ((operation.entry_price - price) * operation.shares)
    return price * operation.shares


def operation_profit(operation: Operation, price: float) -> float:
    return marked_value(operation, price) - operation.entry_value


def price_on_or_before(prices: dict[str, dict[str, Any]], symbol: str, curve_date: date) -> float | None:
    series = prices.get(symbol)
    if not series:
        return None
    dates = series["dates"]
    index = bisect_right(dates, curve_date) - 1
    if index < 0:
        return None
    return series["values"].get(dates[index])


def upsert_curve_rows(connection, rows: list[dict[str, Any]]) -> None:
    connection.execute(
        text(
            """
            INSERT INTO strategy_equity_curve
            (txt_name, strategy_name, curve_date, capital_actual, capital_aportado, capital_invertido,
             profit_usd, return_pct, open_operations, closed_operations, source, updated_at)
            VALUES
            (:txt_name, :strategy_name, :curve_date, :capital_actual, :capital_aportado, :capital_invertido,
             :profit_usd, :return_pct, :open_operations, :closed_operations, :source, :updated_at)
            ON CONFLICT (txt_name, curve_date) DO UPDATE SET
                strategy_name = excluded.strategy_name,
                capital_actual = excluded.capital_actual,
                capital_aportado = excluded.capital_aportado,
                capital_invertido = excluded.capital_invertido,
                profit_usd = excluded.profit_usd,
                return_pct = excluded.return_pct,
                open_operations = excluded.open_operations,
                closed_operations = excluded.closed_operations,
                source = excluded.source,
                updated_at = excluded.updated_at
            """
        ),
        rows,
    )


def upsert_intraday_curve_row(connection, daily_row: dict[str, Any], point_at: datetime) -> None:
    row = {
        **daily_row,
        "point_date": daily_row["curve_date"],
        "point_at": point_at.isoformat(timespec="minutes"),
        "source": "simulated_operations_intraday",
        "updated_at": datetime.now(UTC).replace(tzinfo=None),
    }
    connection.execute(
        text(
            """
            INSERT INTO strategy_equity_curve_intraday
            (txt_name, strategy_name, point_date, point_at, capital_actual, capital_aportado, capital_invertido,
             profit_usd, return_pct, open_operations, closed_operations, source, updated_at)
            VALUES
            (:txt_name, :strategy_name, :point_date, :point_at, :capital_actual, :capital_aportado, :capital_invertido,
             :profit_usd, :return_pct, :open_operations, :closed_operations, :source, :updated_at)
            ON CONFLICT (txt_name, point_at) DO UPDATE SET
                strategy_name = excluded.strategy_name,
                point_date = excluded.point_date,
                capital_actual = excluded.capital_actual,
                capital_aportado = excluded.capital_aportado,
                capital_invertido = excluded.capital_invertido,
                profit_usd = excluded.profit_usd,
                return_pct = excluded.return_pct,
                open_operations = excluded.open_operations,
                closed_operations = excluded.closed_operations,
                source = excluded.source,
                updated_at = excluded.updated_at
            """
        ),
        row,
    )


def parse_operation_date(value: Any, shift_late_utc: bool = False) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return None
        try:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(text_value[:10])
            except ValueError:
                return None
    if shift_late_utc and parsed.time() >= time(21, 0):
        parsed = parsed + timedelta(days=1)
    return parsed.date()


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_name(value: str) -> str:
    return re_sub_non_alnum(str(value or "").lower())


def normalize_field_name(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.translate(str.maketrans("áéíóúüñ", "aeiouun"))
    normalized = normalized.replace("%", "pct")
    return "_".join(part for part in normalized.split() if part)


def re_sub_non_alnum(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as error:
        print(f"Curva capital | error de base de datos: {error}")
        raise SystemExit(1)
