"""
Simulador local de operaciones a partir de los TXT de avisos.

Lee Estrategias/salidas_txt/*.txt, abre operaciones simuladas si no existen,
actualiza precio actual, beneficio/perdida y cierra por objetivo o stop.

Archivos generados:
- operaciones_simuladas/operaciones_estado.json
- operaciones_simuladas/operaciones_abiertas.txt
- operaciones_simuladas/operaciones_cerradas.txt
- operaciones_simuladas/operaciones_todas.txt

Si DATABASE_URL existe, sincroniza tambien la tabla simulated_operations.
"""

from __future__ import annotations

import json
import os
import re
import sys
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from env_loader import load_env


load_env()

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:
    from db import engine
    from sqlalchemy import text
except Exception as error:
    engine = None
    text = None
    print(f"Base de datos no disponible al iniciar simulador: {error}")

try:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
except Exception:
    DataFeed = StockHistoricalDataClient = StockLatestTradeRequest = None


MADRID_TZ = ZoneInfo("Europe/Madrid")
SIGNALS_DIR = BASE_DIR / "salidas_txt"
OPERATIONS_DIR = BASE_DIR / "operaciones_simuladas"
STATE_FILE = OPERATIONS_DIR / "operaciones_estado.json"
OPEN_TXT = OPERATIONS_DIR / "operaciones_abiertas.txt"
CLOSED_TXT = OPERATIONS_DIR / "operaciones_cerradas.txt"
ALL_TXT = OPERATIONS_DIR / "operaciones_todas.txt"
PERFORMANCE_TXT = OPERATIONS_DIR / "rentabilidad_estrategias.txt"

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,14}$")
SIDE_WORDS = {"LONG", "SHORT", "BUY", "SELL", "COMPRA", "VENTA"}

STRATEGIES = [
    {"name": "Momentum", "txt": "Momentum.txt"},
    {"name": "Swing Trading", "txt": "SwingTrading.txt"},
    {"name": "BreaKout", "txt": "BreaKout.txt"},
    {"name": "Mean Reversion", "txt": "Mean_Reversion.txt"},
    {"name": "Value Trading", "txt": "ValueTrading.txt"},
    {"name": "Dividend Growth", "txt": "DividenGrowth.txt"},
    {"name": "Trend Following", "txt": "TrendFollowing.txt"},
    {"name": "Pairs Trading", "txt": "PairsTrading.txt"},
    {"name": "Sector Rotation", "txt": "SectorRotation.txt"},
    {"name": "Quality Investing", "txt": "QualityInvesting.txt"},
    {"name": "Opening Range BreaKout", "txt": "OpeningRangeBreaKout.txt"},
    {"name": "VWAP Reversion", "txt": "VWAP_Reversion.txt"},
    {"name": "Momentum Intradia", "txt": "MomentumIntradia.txt"},
    {"name": "Scalping The PullBacks", "txt": "ScalpingThePullBacKs.txt"},
    {"name": "Gap and Go", "txt": "Gap_and_Go.txt"},
]


def main():
    OPERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MADRID_TZ)
    capital_per_trade = float(os.environ.get("TRADING_SIM_TRADE_USD", "1000"))
    strategy_capital = float(os.environ.get("TRADING_STRATEGY_CAPITAL_USD", "50000"))

    operations = load_operations()
    indexed = build_operation_index(operations)
    signals_by_strategy = read_all_signals()
    print_signal_summary(signals_by_strategy, indexed)

    pending_signals = []
    for strategy in STRATEGIES:
        txt_name = strategy["txt"]
        for signal in signals_by_strategy.get(txt_name, []):
            operation_key = build_operation_key(txt_name, signal)
            legacy_key = build_legacy_operation_key(txt_name, signal["symbol"], signal["side"], signal["signal_date"])
            existing = indexed.get(operation_key) or indexed.get(legacy_key)
            if existing:
                continue
            pending_signals.append((strategy, signal, operation_key))

    symbols_to_price = sorted(
        {
            operation["symbol"]
            for operation in operations
            if operation["status"] == "OPEN"
        }
        | {
            signal["symbol"]
            for _strategy, signal, _operation_key in pending_signals
        }
    )
    latest_prices = fetch_latest_prices(symbols_to_price)

    new_operations = 0
    for strategy, signal, operation_key in pending_signals:
        current_price = latest_prices.get(signal["symbol"])
        operation = create_operation(strategy, signal, operation_key, capital_per_trade, now, current_price)
        if not operation:
            continue
        operations.append(operation)
        indexed[operation_key] = operation
        new_operations += 1
        print(f"OPERACION ABIERTA | {operation['strategy_name']} | {operation['symbol']} | {operation['entry_price']:.4f}")

    closed_now = []
    for operation in operations:
        if operation["status"] != "OPEN":
            continue
        price = latest_prices.get(operation["symbol"]) or operation.get("current_price") or operation["entry_price"]
        update_operation(operation, float(price), now)
        close_reason = close_reason_for_operation(operation)
        if close_reason:
            close_operation(operation, close_reason, now)
            closed_now.append(operation)
            print(f"OPERACION CERRADA | {operation['strategy_name']} | {operation['symbol']} | {close_reason} | P/L {operation['profit_pct']:.2f}%")

    if closed_now:
        remove_closed_signals_from_txt(closed_now)
        remove_closed_signals_from_database(closed_now)

    save_operations(operations)
    write_operation_txts(operations)
    performance_rows = calculate_strategy_performance(operations, strategy_capital)
    write_strategy_performance_txt(performance_rows)
    sync_operations_to_database(operations, performance_rows)

    print(f"Operaciones nuevas: {new_operations}")
    print(f"Operaciones abiertas: {sum(1 for op in operations if op['status'] == 'OPEN')}")
    print(f"Operaciones cerradas total: {sum(1 for op in operations if op['status'] == 'CLOSED')}")
    print(f"Rentabilidad por estrategia guardada en: {PERFORMANCE_TXT}")
    print(f"Estado guardado en: {STATE_FILE}")
    return 0


def read_all_signals():
    output = {}
    for strategy in STRATEGIES:
        path = SIGNALS_DIR / strategy["txt"]
        output[strategy["txt"]] = read_signals(path)
    return output


def read_signals(path):
    if not path.exists() or not path.is_file():
        return []
    signals = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        signal = parse_signal_line(line)
        if signal["symbol"]:
            signals.append(signal)
    return signals


def print_signal_summary(signals_by_strategy, indexed):
    print("\n=== Resumen avisos para simulacion ===")
    for strategy in STRATEGIES:
        txt_name = strategy["txt"]
        signals = signals_by_strategy.get(txt_name, [])
        unique_signals = {
            build_operation_key(txt_name, signal)
            for signal in signals
        }
        known_unique = {
            build_operation_key(txt_name, signal)
            for signal in signals
            if build_operation_key(txt_name, signal) in indexed
            or build_legacy_operation_key(txt_name, signal["symbol"], signal["side"], signal["signal_date"]) in indexed
        }
        already_known = len(known_unique)
        pending = max(0, len(unique_signals) - already_known)
        without_date = sum(1 for signal in signals if not signal["signal_date"])
        print(
            f"{strategy['name']} | TXT {txt_name} | "
            f"lineas={len(signals)} | unicas={len(unique_signals)} | "
            f"ya_existian={already_known} | pendientes={pending} | sin_fecha={without_date}"
        )
    print("=== Fin resumen avisos ===\n")


def build_operation_index(operations):
    indexed = {}
    for operation in operations:
        if operation.get("operation_key"):
            indexed[operation["operation_key"]] = operation
        legacy_key = build_legacy_operation_key(
            operation.get("txt_name", ""),
            operation.get("symbol", ""),
            operation.get("direction", ""),
            operation.get("signal_date", ""),
        )
        indexed.setdefault(legacy_key, operation)
    return indexed


def parse_signal_line(line):
    parts = [part.strip() for part in line.split("|") if part.strip()]
    side = ""
    symbol = ""
    field_parts = parts
    if parts:
        first_clean = parts[0].strip().lstrip("-").strip()
        first = first_clean.upper()
        if first in SIDE_WORDS and len(parts) > 1:
            side = normalize_side(first)
            symbol = parts[1].strip().upper()
            field_parts = parts[2:]
        elif SYMBOL_RE.match(first_clean):
            symbol = first_clean.upper()
            field_parts = parts[1:]

    fields = {}
    for part in field_parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[key.strip().lower()] = value.strip()

    if not side:
        side = normalize_side(first_existing(fields, ["direccion", "side", "tipo"]) or "LONG")

    return {
        "line": line,
        "symbol": symbol,
        "side": side,
        "fields": fields,
        "signal_date": first_existing(fields, ["fecha"])[:10] or "",
    }


def create_operation(strategy, signal, operation_key, capital_per_trade, now, live_price=None):
    signal_entry = parse_number(first_existing(signal["fields"], ["apertura", "entrada", "precio entrada", "precio actual", "precio"]))
    entry = float(live_price) if live_price else signal_entry
    target = parse_number(first_existing(signal["fields"], ["cierre", "salida", "tp1", "objetivo", "take profit", "target"]))
    stop = parse_number(first_existing(signal["fields"], ["stop loss", "stop", "sl"]))
    current = float(live_price) if live_price else parse_number(first_existing(signal["fields"], ["precio actual", "precio"])) or entry
    if not entry or entry <= 0:
        print(f"Operacion omitida sin apertura valida | {strategy['name']} | {signal['symbol']}")
        return None

    shares = capital_per_trade / entry
    opened_at = now.isoformat()
    return {
        "operation_key": operation_key,
        "strategy_name": strategy["name"],
        "txt_name": strategy["txt"],
        "symbol": signal["symbol"],
        "direction": signal["side"],
        "status": "OPEN",
        "signal_date": signal["signal_date"],
        "signal_line": signal["line"],
        "opened_at": opened_at,
        "closed_at": "",
        "entry_price": float(entry),
        "target_price": float(target or 0),
        "stop_loss": float(stop or 0),
        "shares": float(shares),
        "current_price": float(current or entry),
        "investment_value": float(capital_per_trade),
        "profit_usd": 0.0,
        "profit_pct": 0.0,
        "close_reason": "",
        "updated_at": opened_at,
    }


def update_operation(operation, current_price, now):
    entry = float(operation["entry_price"])
    shares = float(operation["shares"])
    direction = normalize_side(operation.get("direction", "LONG"))
    if direction == "SHORT":
        profit_usd = (entry - current_price) * shares
    else:
        profit_usd = (current_price - entry) * shares
    invested = entry * shares
    operation["current_price"] = current_price
    operation["investment_value"] = current_price * shares
    operation["profit_usd"] = profit_usd
    operation["profit_pct"] = (profit_usd / invested * 100) if invested else 0.0
    operation["updated_at"] = now.isoformat()


def close_reason_for_operation(operation):
    if is_pair_operation(operation):
        return ""

    price = float(operation["current_price"])
    target = float(operation.get("target_price") or 0)
    stop = float(operation.get("stop_loss") or 0)
    direction = normalize_side(operation.get("direction", "LONG"))
    if direction == "SHORT":
        if target and price <= target:
            return "OBJETIVO"
        if stop and price >= stop:
            return "STOP LOSS"
    else:
        if target and price >= target:
            return "OBJETIVO"
        if stop and price <= stop:
            return "STOP LOSS"
    return ""


def close_operation(operation, reason, now):
    operation["status"] = "CLOSED"
    operation["close_reason"] = reason
    operation["closed_at"] = now.isoformat()
    operation["updated_at"] = now.isoformat()


def fetch_latest_prices(symbols):
    tradeable_symbols = [
        symbol
        for symbol in symbols
        if is_single_market_symbol(symbol)
    ]
    skipped_symbols = sorted(set(symbols) - set(tradeable_symbols))
    for symbol in skipped_symbols:
        print(f"PRECIO ACTUAL OMITIDO | {symbol} | instrumento compuesto/no compatible con Alpaca")

    if not tradeable_symbols:
        return {}
    if not all([StockHistoricalDataClient, StockLatestTradeRequest]):
        print("Alpaca no disponible. Se usan precios del aviso.")
        return {}
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("Claves Alpaca no configuradas. Se usan precios del aviso.")
        return {}

    client = StockHistoricalDataClient(api_key, secret_key)
    request = StockLatestTradeRequest(
        symbol_or_symbols=tradeable_symbols,
        feed=DataFeed.IEX,
    )
    try:
        trades = client.get_stock_latest_trade(request)
    except Exception as error:
        print(f"No se pudieron actualizar precios actuales: {error}")
        return {}

    prices = {}
    for symbol, trade in trades.items():
        price = getattr(trade, "price", None)
        if price is not None:
            prices[symbol] = float(price)
            print(f"PRECIO ACTUAL | {symbol} | {prices[symbol]:.4f}")
    return prices


def is_pair_operation(operation):
    symbol = str(operation.get("symbol", ""))
    direction = normalize_side(operation.get("direction", ""))
    return "/" in symbol or direction == "PAIR" or operation.get("strategy_name") == "Pairs Trading"


def is_single_market_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    return bool(SYMBOL_RE.match(symbol)) and "/" not in symbol


def remove_closed_signals_from_txt(closed_operations):
    closed_by_txt = {}
    for operation in closed_operations:
        closed_by_txt.setdefault(operation["txt_name"], set()).add(operation["signal_line"])

    for txt_name, closed_lines in closed_by_txt.items():
        path = SIGNALS_DIR / txt_name
        if not path.exists():
            continue
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        kept = [line for line in lines if line not in closed_lines]
        if kept != lines:
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            print(f"TXT avisos limpiado | {txt_name} | cerradas quitadas: {len(lines) - len(kept)}")


def remove_closed_signals_from_database(closed_operations):
    try:
        with engine.begin() as connection:
            deleted_total = 0
            for operation in closed_operations:
                result = connection.execute(
                    text(
                        """
                        DELETE FROM strategy_signals
                        WHERE txt_name = :txt_name
                          AND line = :line
                        """
                    ),
                    {
                        "txt_name": operation["txt_name"],
                        "line": operation["signal_line"],
                    },
                )
                deleted_total += result.rowcount or 0
        print(f"Avisos cerrados eliminados de PostgreSQL/DB: {deleted_total}")
    except Exception as error:
        print(f"No se pudieron eliminar avisos cerrados de DB: {error}")


def write_operation_txts(operations):
    open_lines = [format_operation_line(op) for op in operations if op["status"] == "OPEN"]
    closed_lines = [format_operation_line(op) for op in operations if op["status"] == "CLOSED"]
    all_lines = [format_operation_line(op) for op in operations]
    OPEN_TXT.write_text("\n".join(open_lines) + ("\n" if open_lines else ""), encoding="utf-8")
    CLOSED_TXT.write_text("\n".join(closed_lines) + ("\n" if closed_lines else ""), encoding="utf-8")
    ALL_TXT.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")


def calculate_strategy_performance(operations, strategy_capital):
    rows = []
    now = datetime.now(MADRID_TZ).isoformat()
    for strategy in STRATEGIES:
        strategy_operations = [
            operation
            for operation in operations
            if operation.get("strategy_name") == strategy["name"]
        ]
        total_ops = len(strategy_operations)
        open_ops = sum(1 for operation in strategy_operations if operation.get("status") == "OPEN")
        closed_ops = sum(1 for operation in strategy_operations if operation.get("status") == "CLOSED")
        wins = sum(1 for operation in strategy_operations if float(operation.get("profit_usd") or 0) > 0)
        losses = sum(1 for operation in strategy_operations if float(operation.get("profit_usd") or 0) < 0)
        profit_usd = sum(float(operation.get("profit_usd") or 0) for operation in strategy_operations)
        current_capital = strategy_capital + profit_usd
        return_pct = (profit_usd / strategy_capital * 100) if strategy_capital else 0.0
        if total_ops:
            label = (
                f"{profit_usd:+.2f} USD "
                f"({return_pct:+.2f}%, capital inicial {strategy_capital:.0f} USD, "
                f"capital actual {current_capital:.2f} USD, "
                f"{total_ops} ops, {open_ops} abiertas, {closed_ops} cerradas)"
            )
        else:
            label = "Sin operaciones"
        rows.append(
            {
                "strategy_name": strategy["name"],
                "txt_name": strategy["txt"],
                "historical_return": label,
                "return_pct": return_pct,
                "profit_usd": profit_usd,
                "invested": strategy_capital,
                "total_ops": total_ops,
                "open_ops": open_ops,
                "closed_ops": closed_ops,
                "wins": wins,
                "losses": losses,
                "updated_at": now,
            }
        )
    return rows


def write_strategy_performance_txt(rows):
    lines = [
        "# strategy | txt | historical_return | return_pct | profit_usd | invested | total_ops | open_ops | closed_ops | wins | losses | updated_at"
    ]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    row["strategy_name"],
                    row["txt_name"],
                    row["historical_return"],
                    f"{row['return_pct']:.4f}",
                    f"{row['profit_usd']:.2f}",
                    f"{row['invested']:.2f}",
                    str(row["total_ops"]),
                    str(row["open_ops"]),
                    str(row["closed_ops"]),
                    str(row["wins"]),
                    str(row["losses"]),
                    row["updated_at"],
                ]
            )
        )
    PERFORMANCE_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_operation_line(operation):
    return (
        f"{operation['strategy_name']} | {operation['txt_name']} | {operation['symbol']} | "
        f"Estado: {operation['status']} | Direccion: {operation['direction']} | "
        f"Fecha aviso: {operation['signal_date']} | Ejecutada: {operation['opened_at']} | "
        f"Entrada: {operation['entry_price']:.4f} | Acciones: {operation['shares']:.4f} | "
        f"Precio actual: {operation['current_price']:.4f} | Valor inversion: {operation['investment_value']:.2f} | "
        f"Beneficio USD: {operation['profit_usd']:.2f} | Beneficio %: {operation['profit_pct']:.2f}% | "
        f"Cierre: {operation['target_price']:.4f} | Stop Loss: {operation['stop_loss']:.4f} | "
        f"Cerrada: {operation['closed_at']} | Motivo: {operation['close_reason']}"
    )


def sync_operations_to_database(operations, performance_rows):
    if engine is None or text is None:
        print("Sincronizacion DB omitida: modulo db/sqlalchemy no disponible.")
        return

    try:
        with engine.begin() as connection:
            ensure_operations_table(connection)
            connection.execute(text("DELETE FROM simulated_operations"))
            for operation in operations:
                connection.execute(
                    text(
                        """
                        INSERT INTO simulated_operations
                        (operation_key, strategy_name, txt_name, symbol, direction, status,
                         signal_date, signal_line, opened_at, closed_at, entry_price,
                         target_price, stop_loss, shares, current_price, investment_value,
                         profit_usd, profit_pct, close_reason, updated_at)
                        VALUES
                        (:operation_key, :strategy_name, :txt_name, :symbol, :direction, :status,
                         :signal_date, :signal_line, :opened_at, :closed_at, :entry_price,
                         :target_price, :stop_loss, :shares, :current_price, :investment_value,
                         :profit_usd, :profit_pct, :close_reason, :updated_at)
                        """
                    ),
                    serialize_for_db(operation),
                )
            sync_strategy_performance(connection, performance_rows)
        print(f"Operaciones sincronizadas con PostgreSQL/DB: {len(operations)}")
    except Exception as error:
        print(f"No se pudieron sincronizar operaciones con DB: {error}")


def sync_strategy_performance(connection, performance_rows):
    updated = 0
    for row in performance_rows:
        result = connection.execute(
            text(
                """
                UPDATE strategies
                SET historical_return = :historical_return
                WHERE name = :strategy_name
                   OR signals_txt_name = :txt_name
                """
            ),
            {
                "historical_return": row["historical_return"],
                "strategy_name": row["strategy_name"],
                "txt_name": row["txt_name"],
            },
        )
        updated += result.rowcount or 0
    print(f"Rentabilidad historica actualizada en PostgreSQL/DB: {updated} estrategias")


def ensure_operations_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS simulated_operations (
                operation_key TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                txt_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                signal_date TEXT NOT NULL DEFAULT '',
                signal_line TEXT NOT NULL DEFAULT '',
                opened_at TIMESTAMP,
                closed_at TIMESTAMP,
                entry_price FLOAT NOT NULL DEFAULT 0,
                target_price FLOAT NOT NULL DEFAULT 0,
                stop_loss FLOAT NOT NULL DEFAULT 0,
                shares FLOAT NOT NULL DEFAULT 0,
                current_price FLOAT NOT NULL DEFAULT 0,
                investment_value FLOAT NOT NULL DEFAULT 0,
                profit_usd FLOAT NOT NULL DEFAULT 0,
                profit_pct FLOAT NOT NULL DEFAULT 0,
                close_reason TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP
            )
            """
        )
    )


def serialize_for_db(operation):
    row = dict(operation)
    for key in ("opened_at", "closed_at", "updated_at"):
        row[key] = parse_datetime_for_db(row.get(key))
    return row


def parse_datetime_for_db(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None


def load_operations():
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    return []


def save_operations(operations):
    STATE_FILE.write_text(json.dumps(operations, indent=2, ensure_ascii=False), encoding="utf-8")


def build_operation_key(txt_name, signal):
    line_hash = hashlib.sha1(str(signal.get("line", "")).strip().encode("utf-8")).hexdigest()[:12]
    return "|".join(
        [
            txt_name,
            signal["symbol"],
            normalize_side(signal["side"]),
            signal["signal_date"] or "sin_fecha",
            line_hash,
        ]
    )


def build_legacy_operation_key(txt_name, symbol, side, signal_date):
    return "|".join(
        [
            txt_name,
            str(symbol or "").strip().upper(),
            normalize_side(side),
            signal_date or "sin_fecha",
        ]
    )


def first_existing(fields, keys):
    for key in keys:
        value = fields.get(key.lower())
        if value:
            return value
    for key in keys:
        lookup = key.lower()
        for field_key, field_value in fields.items():
            if field_key.startswith(f"{lookup} ") and field_value:
                return field_value
    return ""


def normalize_side(value):
    value = str(value or "").strip().upper()
    if value in {"BUY", "COMPRA"}:
        return "LONG"
    if value in {"SELL", "VENTA"}:
        return "SHORT"
    return value or "LONG"


def parse_number(value):
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
