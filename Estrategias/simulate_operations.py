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
import subprocess
import sys
import hashlib
from datetime import UTC, datetime, timedelta
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
CAPITAL_MAXIMA_TXT = OPERATIONS_DIR / "capital_maximos_estrategias.txt"
BACKTEST_INCLUDED_TXT = OPERATIONS_DIR / "operaciones_backtest_incluidas.txt"
BACKTEST_SUMMARY_CACHE = OPERATIONS_DIR / "backtest_resumen_estrategias.json"
CHIP_STATUS_TXT = OPERATIONS_DIR / "chip_status.txt"
WEB_SETTINGS_FILE = PROJECT_DIR / "local_panel_data" / "web_settings.json"
BACKTEST_JSON_FILE = PROJECT_DIR / "EstrategiasV2" / "outputs" / "historical_backtest_5y.json"
BASE_ACCOUNT_CAPITAL_USD = 50_000.0

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
    {"name": "Follow The Money", "txt": "Follow_The_Money.txt"},
    {"name": "Entrada Dinero Direccional", "txt": "Entrada_Dinero_Direccional.txt"},
    {"name": "Acumula Metales", "txt": "Acumula_Metales.txt"},
    {"name": "Acumulacion", "txt": "Acumulacion.txt"},
    {"name": "Reversion RSI 5", "txt": "Reversion_RSI_5.txt"},
]
TXT_BY_STRATEGY_NAME = {strategy["name"]: strategy["txt"] for strategy in STRATEGIES}

NO_AUTO_CLOSE_STRATEGIES = {
    "Acumula Metales",
    "Acumulacion",
}

GROUP_PROFIT_CLOSE_STRATEGIES = {
    "Reversion RSI 5",
}
GROUP_PROFIT_TARGET_PCT = 5.0


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
    reserved_daily_keys = set(indexed)
    skipped_same_day = 0
    for strategy in STRATEGIES:
        txt_name = strategy["txt"]
        for signal in signals_by_strategy.get(txt_name, []):
            operation_key = build_operation_key(txt_name, signal)
            legacy_key = build_legacy_operation_key(txt_name, signal["symbol"], signal["side"], signal["signal_date"])
            daily_symbol_key = build_daily_symbol_key(txt_name, signal["symbol"], signal["signal_date"])
            existing = indexed.get(operation_key) or indexed.get(legacy_key) or indexed.get(daily_symbol_key)
            if existing:
                continue
            if daily_symbol_key in reserved_daily_keys:
                skipped_same_day += 1
                continue
            pending_signals.append((strategy, signal, operation_key))
            reserved_daily_keys.add(daily_symbol_key)

    if skipped_same_day:
        print(f"Operaciones omitidas por repeticion mismo activo/mismo dia: {skipped_same_day}")

    symbols_to_price = sorted(
        {
            price_symbol
            for operation in operations
            if operation["status"] == "OPEN"
            for price_symbol in price_symbols_for_market_data(operation["symbol"])
        }
        | {
            price_symbol
            for _strategy, signal, _operation_key in pending_signals
            for price_symbol in price_symbols_for_market_data(signal["symbol"])
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
        indexed[build_legacy_operation_key(strategy["txt"], signal["symbol"], signal["side"], signal["signal_date"])] = operation
        indexed[build_daily_symbol_key(strategy["txt"], signal["symbol"], signal["signal_date"])] = operation
        new_operations += 1
        print(f"OPERACION ABIERTA | {operation['strategy_name']} | {operation['symbol']} | {operation['entry_price']:.4f}")

    closed_now = []
    for operation in operations:
        if operation["status"] != "OPEN":
            continue
        if is_pair_operation(operation):
            update_pair_operation(operation, latest_prices, now)
        else:
            price = latest_prices.get(operation["symbol"]) or operation.get("current_price") or operation["entry_price"]
            update_operation(operation, float(price), now)
        close_reason = close_reason_for_operation(operation)
        if close_reason:
            close_operation(operation, close_reason, now)
            closed_now.append(operation)
            print(f"OPERACION CERRADA | {operation['strategy_name']} | {operation['symbol']} | {close_reason} | P/L {operation['profit_pct']:.2f}%")

    group_closed = close_profitable_groups(operations, now)
    if group_closed:
        closed_now.extend(group_closed)

    if closed_now:
        remove_closed_signals_from_txt(closed_now)
        remove_closed_signals_from_database(closed_now)

    save_operations(operations)
    write_operation_txts(operations)
    backtest_operations, backtest_summary = load_backtest_operations_for_simulation(capital_per_trade)
    write_backtest_included_txt(backtest_operations)
    performance_rows = calculate_strategy_performance(
        operations,
        strategy_capital,
        capital_per_trade,
        backtest_summary,
        backtest_operations,
    )
    write_strategy_performance_txt(performance_rows)
    write_capital_maxima_txt(performance_rows)
    chip_status_rows = build_chip_status_rows(now)
    write_chip_status_txt(chip_status_rows)
    sync_operations_to_database(operations, backtest_operations, performance_rows)
    mirror_postgres_to_sqlite()

    print(f"Operaciones nuevas: {new_operations}")
    print(f"Operaciones abiertas: {sum(1 for op in operations if op['status'] == 'OPEN')}")
    print(f"Operaciones cerradas total: {sum(1 for op in operations if op['status'] == 'CLOSED')}")
    print(f"Operaciones backtest incluidas: {len(backtest_operations)}")
    print(f"Rentabilidad por estrategia guardada en: {PERFORMANCE_TXT}")
    print(f"Semaforos guardados en: {CHIP_STATUS_TXT}")
    print(f"Estado guardado en: {STATE_FILE}")
    return 0


def mirror_postgres_to_sqlite():
    if engine is None or getattr(engine.dialect, "name", "") != "postgresql":
        return
    sync_script = PROJECT_DIR / "sync_postgres_to_sqlite.py"
    if not sync_script.exists():
        print("Copia SQLite omitida: no existe sync_postgres_to_sqlite.py")
        return
    result = subprocess.run([sys.executable, str(sync_script)], cwd=str(PROJECT_DIR), text=True)
    if result.returncode != 0:
        print(f"Copia SQLite termino con codigo {result.returncode}.")


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
            or build_daily_symbol_key(txt_name, signal["symbol"], signal["signal_date"]) in indexed
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
        daily_symbol_key = build_daily_symbol_key(
            operation.get("txt_name", ""),
            operation.get("symbol", ""),
            operation.get("signal_date", ""),
        )
        indexed.setdefault(daily_symbol_key, operation)
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


def update_pair_operation(operation, latest_prices, now):
    left, right = split_pair_symbol(operation.get("symbol"))
    if not left or not right:
        update_operation(operation, float(operation.get("current_price") or operation["entry_price"]), now)
        return
    price_left = latest_prices.get(left)
    price_right = latest_prices.get(right)
    if price_left is None or price_right is None:
        operation["updated_at"] = now.isoformat()
        return

    signal = parse_signal_line(operation.get("signal_line", ""))
    fields = signal.get("fields", {})
    entry_left, entry_right = parse_pair_prices(first_existing(fields, ["apertura", "entrada", "precio entrada", "precio actual", "precio"]))
    entry_left = entry_left or float(operation.get("entry_price") or 0)
    hedge = parse_number(first_existing(fields, ["hedge", "hedge ratio"])) or 1.0
    if not entry_left or not entry_right:
        update_operation(operation, float(price_left), now)
        return

    entry_spread = entry_left - (hedge * entry_right)
    current_spread = float(price_left) - (hedge * float(price_right))
    direction = normalize_side(operation.get("direction", "LONG"))
    if direction == "SHORT":
        profit_per_unit = entry_spread - current_spread
    else:
        profit_per_unit = current_spread - entry_spread

    shares = float(operation.get("shares") or 0)
    invested = entry_left * shares
    profit_usd = profit_per_unit * shares
    operation["current_price"] = float(price_left)
    operation["investment_value"] = float(price_left) * shares
    operation["profit_usd"] = profit_usd
    operation["profit_pct"] = (profit_usd / invested * 100) if invested else 0.0
    operation["updated_at"] = now.isoformat()


def close_reason_for_operation(operation):
    if operation.get("strategy_name") in NO_AUTO_CLOSE_STRATEGIES:
        return ""
    if operation.get("strategy_name") in GROUP_PROFIT_CLOSE_STRATEGIES:
        return ""
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


def close_profitable_groups(operations, now):
    groups = {}
    for operation in operations:
        if operation.get("status") != "OPEN":
            continue
        if operation.get("strategy_name") not in GROUP_PROFIT_CLOSE_STRATEGIES:
            continue
        key = (
            operation.get("strategy_name", ""),
            operation.get("symbol", ""),
            normalize_side(operation.get("direction", "LONG")),
        )
        groups.setdefault(key, []).append(operation)

    closed = []
    for (strategy_name, symbol, direction), group_operations in groups.items():
        invested = sum(
            float(operation.get("entry_price") or 0) * float(operation.get("shares") or 0)
            for operation in group_operations
        )
        profit = sum(float(operation.get("profit_usd") or 0) for operation in group_operations)
        profit_pct = (profit / invested * 100) if invested else 0.0
        if profit_pct < GROUP_PROFIT_TARGET_PCT:
            continue

        for operation in group_operations:
            close_operation(operation, f"BENEFICIO GRUPO {profit_pct:.2f}%", now)
            closed.append(operation)
        print(
            f"OPERACION CERRADA GRUPO | {strategy_name} | {symbol} | {direction} | "
            f"{len(group_operations)} entradas | P/L grupo {profit_pct:.2f}%"
        )
    return closed


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


def split_pair_symbol(symbol):
    parts = [part.strip().upper() for part in str(symbol or "").split("/", 1)]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def price_symbols_for_market_data(symbol):
    left, right = split_pair_symbol(symbol)
    if left and right:
        return [left, right]
    return [str(symbol or "").strip().upper()]


def parse_pair_prices(value):
    parts = [part.strip() for part in str(value or "").split("/", 1)]
    if len(parts) != 2:
        return 0.0, 0.0
    return parse_number(parts[0]), parse_number(parts[1])


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


def backtest_is_enabled():
    try:
        data = json.loads(WEB_SETTINGS_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("show_backtest_5y")) if isinstance(data, dict) else False


def load_backtest_operations_for_simulation(capital_per_trade):
    if not backtest_is_enabled():
        return [], {}
    try:
        data = json.loads(BACKTEST_JSON_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        print(f"Backtest activado, pero no se pudo leer: {BACKTEST_JSON_FILE}")
        return [], {}
    raw_operations = []
    if isinstance(data, dict):
        for key in ("closed_operations", "open_operations"):
            items = data.get(key, [])
            if isinstance(items, list):
                raw_operations.extend(items)
    if not isinstance(raw_operations, list):
        return [], {}
    fingerprint = backtest_fingerprint()
    summary = load_backtest_summary_cache(fingerprint)
    operations = []
    seen_keys = set()
    for raw in raw_operations:
        if not isinstance(raw, dict):
            continue
        operation = backtest_operation_to_simulated(raw, capital_per_trade)
        if not operation:
            continue
        if operation["operation_key"] in seen_keys:
            continue
        seen_keys.add(operation["operation_key"])
        operations.append(operation)
    now = datetime.now(MADRID_TZ)
    refreshed_open = refresh_backtest_open_operations_market_prices(operations, now)
    has_open_operations = any(operation.get("status") == "OPEN" for operation in operations)
    if has_open_operations:
        summary = summarize_operations_by_strategy(operations)
        print("Resumen de backtest recalculado con operaciones abiertas valoradas a mercado.")
    elif not summary:
        summary = summarize_operations_by_strategy(operations)
        save_backtest_summary_cache(fingerprint, summary, len(operations))
        print("Resumen de backtest calculado y guardado en cache.")
    else:
        print("Resumen de backtest reutilizado desde cache.")
    print(
        f"Backtest activado: {len(operations)} operaciones historicas cargadas desde JSON "
        f"| abiertas revaloradas={refreshed_open}."
    )
    return operations, summary


def backtest_fingerprint():
    if not BACKTEST_JSON_FILE.exists():
        return {}
    stat = BACKTEST_JSON_FILE.stat()
    return {
        "path": str(BACKTEST_JSON_FILE),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "sha256": file_sha256(BACKTEST_JSON_FILE),
    }


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_backtest_summary_cache(fingerprint):
    if not fingerprint:
        return {}
    try:
        data = json.loads(BACKTEST_SUMMARY_CACHE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return {}
    summary = data.get("summary", {})
    return summary if isinstance(summary, dict) else {}


def save_backtest_summary_cache(fingerprint, summary, operation_count):
    payload = {
        "generated_at": datetime.now(MADRID_TZ).isoformat(),
        "fingerprint": fingerprint,
        "operation_count": operation_count,
        "summary": summary,
    }
    BACKTEST_SUMMARY_CACHE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def refresh_backtest_open_operations_market_prices(operations, now):
    symbols_to_price = sorted(
        {
            price_symbol
            for operation in operations
            if operation.get("status") == "OPEN"
            for price_symbol in price_symbols_for_market_data(operation.get("symbol"))
        }
    )
    if not symbols_to_price:
        return 0
    latest_prices = fetch_latest_prices(symbols_to_price)
    if not latest_prices:
        return 0
    refreshed = 0
    for operation in operations:
        if operation.get("status") != "OPEN":
            continue
        if is_pair_operation(operation):
            previous_updated_at = operation.get("updated_at")
            update_pair_operation(operation, latest_prices, now)
            if operation.get("updated_at") != previous_updated_at:
                refreshed += 1
            continue
        price = latest_prices.get(operation.get("symbol"))
        if price is None:
            continue
        update_operation(operation, float(price), now)
        refreshed += 1
    return refreshed


def backtest_operation_to_simulated(raw, capital_per_trade):
    strategy_name = str(raw.get("strategy") or "").strip()
    txt_name = TXT_BY_STRATEGY_NAME.get(strategy_name)
    if not txt_name:
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    entry_date = str(raw.get("entry_date") or raw.get("signal_date") or "").strip()
    exit_date = str(raw.get("exit_date") or "").strip()
    entry_price = float_value(raw.get("entry_price"))
    shares = float_value(raw.get("shares"))
    if not shares and entry_price:
        shares = round(float(capital_per_trade) / entry_price, 6)
    exit_price = float_value(raw.get("exit_price")) or float_value(raw.get("current_price")) or entry_price
    investment = entry_price * shares if entry_price and shares else float_value(raw.get("investment_value"))
    profit_usd = float_value(raw.get("profit_usd"))
    profit_pct = float_value(raw.get("profit_pct"))
    raw_status = str(raw.get("status") or "").upper()
    status = "OPEN" if raw_status == "OPEN" else "CLOSED"
    closed_at = "" if status == "OPEN" else exit_date
    close_reason = "" if status == "OPEN" else f"BACKTEST {raw.get('close_reason') or ''}".strip()
    identifier = raw.get("id") or hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return {
        "operation_key": f"BACKTEST|{txt_name}|{symbol}|{entry_date}|{identifier}",
        "strategy_name": strategy_name,
        "txt_name": txt_name,
        "symbol": symbol,
        "direction": normalize_side(raw.get("direction") or "LONG"),
        "status": status,
        "signal_date": str(raw.get("signal_date") or entry_date),
        "signal_line": f"BACKTEST {strategy_name} {symbol} {entry_date}",
        "opened_at": entry_date,
        "closed_at": closed_at,
        "entry_price": entry_price,
        "target_price": float_value(raw.get("target_price")),
        "stop_loss": float_value(raw.get("stop_loss")),
        "shares": shares,
        "current_price": exit_price,
        "investment_value": investment,
        "profit_usd": profit_usd,
        "profit_pct": profit_pct,
        "close_reason": close_reason,
        "updated_at": exit_date or entry_date,
    }


def write_backtest_included_txt(operations):
    lines = [format_operation_line(op) for op in operations]
    BACKTEST_INCLUDED_TXT.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def float_value(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def calculate_strategy_performance(
    operations,
    strategy_capital,
    capital_per_trade,
    historical_summary=None,
    historical_operations=None,
):
    rows = []
    now_dt = datetime.now(MADRID_TZ)
    now = now_dt.isoformat()
    stored_maxima = load_capital_maxima()
    live_summary = summarize_operations_by_strategy(operations)
    historical_summary = historical_summary or {}
    combined_operations = list(operations or []) + list(historical_operations or [])
    combined_summary = summarize_operations_by_strategy(combined_operations)
    period_summary = summarize_period_returns_by_strategy(combined_operations, strategy_capital, capital_per_trade, now_dt)
    for strategy in STRATEGIES:
        live = live_summary.get(strategy["txt"], empty_performance_summary(strategy))
        historical = historical_summary.get(strategy["txt"], empty_performance_summary(strategy))
        combined = combined_summary.get(strategy["txt"], empty_performance_summary(strategy))
        periods = period_summary.get(strategy["txt"], {})
        total_ops = int(live.get("total_ops", 0)) + int(historical.get("total_ops", 0))
        open_ops = int(live.get("open_ops", 0)) + int(historical.get("open_ops", 0))
        closed_ops = int(live.get("closed_ops", 0)) + int(historical.get("closed_ops", 0))
        wins = int(live.get("wins", 0)) + int(historical.get("wins", 0))
        losses = int(live.get("losses", 0)) + int(historical.get("losses", 0))
        profit_usd = float(live.get("profit_usd", 0)) + float(historical.get("profit_usd", 0))
        closed_duration_seconds = float(combined.get("closed_duration_seconds", 0))
        first_operation_at = combined.get("first_operation_at", "")
        detected_max_open = max(
            int(live.get("max_open_operations", 0)),
            int(historical.get("max_open_operations", 0)),
        )
        detected_at = live.get("max_detected_at") or historical.get("max_detected_at") or ""
        previous = stored_maxima.get(strategy["txt"], {})
        stored_max_open = int(previous.get("max_open_operations") or 0)
        max_open = max(stored_max_open, detected_max_open)
        capital_base = max(strategy_capital, BASE_ACCOUNT_CAPITAL_USD, max_open * capital_per_trade)
        current_capital = capital_base + profit_usd
        return_pct = (profit_usd / capital_base * 100) if capital_base else 0.0
        if total_ops:
            label = (
                f"{profit_usd:+.2f} USD "
                f"({return_pct:+.2f}%, capital base {capital_base:.2f} USD, "
                f"capital actual {current_capital:.2f} USD, "
                f"max abiertas {max_open}, "
                f"{total_ops} ops, {open_ops} abiertas, {closed_ops} cerradas)"
            )
            period_labels = [
                format_period_return_label("Last 1M", periods.get("last_1m")),
                format_period_return_label("Last 3M", periods.get("last_3m")),
                format_period_return_label("Last 12M", periods.get("last_12m")),
                format_period_return_label(str(now_dt.year), periods.get("ytd")),
                format_period_return_label(str(now_dt.year - 1), periods.get("prev_year")),
            ]
            label = " | ".join([label, *period_labels])
        else:
            label = "Sin operaciones"
        max_detected_at = (
            datetime_to_text(detected_at)
            if detected_at and detected_max_open >= stored_max_open
            else previous.get("max_detected_at", "")
        )
        average_close_seconds = (closed_duration_seconds / closed_ops) if closed_ops else 0.0
        success_rate = (wins / closed_ops * 100) if closed_ops else 0.0
        rows.append(
            {
                "strategy_name": strategy["name"],
                "txt_name": strategy["txt"],
                "historical_return": label,
                "return_pct": return_pct,
                "profit_usd": profit_usd,
                "invested": capital_base,
                "capital_base": capital_base,
                "current_capital": current_capital,
                "max_open_operations": max_open,
                "max_detected_at": max_detected_at,
                "total_ops": total_ops,
                "open_ops": open_ops,
                "closed_ops": closed_ops,
                "wins": wins,
                "losses": losses,
                "average_close_duration": format_duration_seconds(average_close_seconds),
                "success_rate": f"{success_rate:.1f}%" if closed_ops else "Sin cierres todavia",
                "first_operation_display": format_short_date(first_operation_at),
                "updated_at": now,
            }
        )
    return rows


def summarize_period_returns_by_strategy(operations, strategy_capital, capital_per_trade, now_dt):
    ytd_start = datetime(now_dt.year, 1, 1, tzinfo=MADRID_TZ)
    prev_year_start = datetime(now_dt.year - 1, 1, 1, tzinfo=MADRID_TZ)
    prev_year_end = datetime(now_dt.year, 1, 1, tzinfo=MADRID_TZ)
    ranges = {
        "last_1m": (now_dt - timedelta(days=30), now_dt),
        "last_3m": (now_dt - timedelta(days=90), now_dt),
        "last_12m": (now_dt - timedelta(days=365), now_dt),
        "ytd": (ytd_start, now_dt),
        "prev_year": (prev_year_start, prev_year_end),
    }
    result = {}
    for strategy in STRATEGIES:
        strategy_operations = [
            operation
            for operation in operations
            if operation.get("txt_name") == strategy["txt"]
        ]
        result[strategy["txt"]] = {
            key: summarize_period_return(strategy_operations, start, end, strategy_capital, capital_per_trade)
            for key, (start, end) in ranges.items()
        }
    return result


def is_backtest_final_close(operation):
    close_reason = str(operation.get("close_reason") or "").upper()
    operation_key = str(operation.get("operation_key") or "")
    return operation_key.startswith("BACKTEST|") and "FIN_BACKTEST" in close_reason


def summarize_period_return(operations, start, end, strategy_capital, capital_per_trade):
    closed = []
    open_operations = []
    events = []
    for operation in operations:
        opened_at = parse_datetime_value(operation.get("opened_at") or operation.get("signal_date"))
        closed_at = parse_datetime_value(operation.get("closed_at"))
        period_closed_at = opened_at if is_backtest_final_close(operation) else closed_at
        include_open = operation.get("status") == "OPEN" and opened_at and start <= opened_at < end
        include_closed = operation.get("status") == "CLOSED" and period_closed_at and start <= period_closed_at < end
        if opened_at and (include_open or include_closed):
            effective_close = closed_at
            if operation.get("status") == "OPEN" or not effective_close or effective_close < opened_at:
                effective_close = end
            events.append((max(opened_at, start), 1))
            events.append((min(effective_close, end), -1))
        if include_open:
            open_operations.append(operation)
            continue
        if include_closed:
            closed.append(operation)

    max_open, _max_at = max_open_from_events(events)
    capital_base = max(strategy_capital, BASE_ACCOUNT_CAPITAL_USD, max_open * capital_per_trade)
    closed_profit_usd = sum(float_value(operation.get("profit_usd")) for operation in closed)
    open_profit_usd = sum(float_value(operation.get("profit_usd")) for operation in open_operations)
    profit_usd = closed_profit_usd + open_profit_usd
    return_pct = (profit_usd / capital_base * 100) if capital_base else 0.0
    wins = sum(1 for operation in closed if float_value(operation.get("profit_usd")) > 0)
    losses = sum(1 for operation in closed if float_value(operation.get("profit_usd")) < 0)
    return {
        "profit_usd": profit_usd,
        "closed_profit_usd": closed_profit_usd,
        "open_profit_usd": open_profit_usd,
        "return_pct": return_pct,
        "capital_base": capital_base,
        "closed_ops": len(closed),
        "open_ops": len(open_operations),
        "wins": wins,
        "losses": losses,
        "max_open_operations": max_open,
    }


def format_period_return_label(label, summary):
    summary = summary or {}
    profit_usd = float(summary.get("profit_usd", 0) or 0)
    return_pct = float(summary.get("return_pct", 0) or 0)
    capital_base = float(summary.get("capital_base", BASE_ACCOUNT_CAPITAL_USD) or BASE_ACCOUNT_CAPITAL_USD)
    closed_ops = int(summary.get("closed_ops", 0) or 0)
    open_ops = int(summary.get("open_ops", 0) or 0)
    max_open = int(summary.get("max_open_operations", 0) or 0)
    return (
        f"{label} {profit_usd:+.2f} USD "
        f"({return_pct:+.2f}%, capital base {capital_base:.2f} USD, "
        f"max abiertas {max_open}, {closed_ops} cerradas, {open_ops} abiertas)"
    )


def summarize_operations_by_strategy(operations):
    grouped = {
        strategy["txt"]: {
            **empty_performance_summary(strategy),
            "_events": [],
        }
        for strategy in STRATEGIES
    }
    for operation in operations:
        txt_name = operation.get("txt_name")
        summary = grouped.get(txt_name)
        if summary is None:
            continue
        summary["total_ops"] += 1
        profit = float_value(operation.get("profit_usd"))
        opened_at = parse_datetime_value(operation.get("opened_at") or operation.get("signal_date"))
        if operation.get("status") == "OPEN":
            summary["open_ops"] += 1
            summary["open_profit_usd"] += profit
            summary["profit_usd"] += profit
        if operation.get("status") == "CLOSED":
            summary["closed_ops"] += 1
            summary["closed_profit_usd"] += profit
            summary["profit_usd"] += profit
            closed_at_for_duration = parse_datetime_value(operation.get("closed_at"))
            if opened_at and closed_at_for_duration and closed_at_for_duration >= opened_at:
                summary["closed_duration_seconds"] += (closed_at_for_duration - opened_at).total_seconds()
            if profit > 0:
                summary["wins"] += 1
            elif profit < 0:
                summary["losses"] += 1
        if not opened_at:
            continue
        if not summary["first_operation_at"] or opened_at < summary["first_operation_at"]:
            summary["first_operation_at"] = opened_at
        closed_at = parse_datetime_value(operation.get("closed_at"))
        if operation.get("status") == "OPEN" or not closed_at or closed_at < opened_at:
            closed_at = datetime.now(MADRID_TZ)
        summary["_events"].append((opened_at, 1))
        summary["_events"].append((closed_at, -1))

    clean = {}
    for txt_name, summary in grouped.items():
        max_open, max_at = max_open_from_events(summary.pop("_events", []))
        summary["max_open_operations"] = max_open
        summary["max_detected_at"] = datetime_to_text(max_at)
        summary["first_operation_at"] = datetime_to_text(summary.get("first_operation_at"))
        clean[txt_name] = summary
    return clean


def empty_performance_summary(strategy):
    return {
        "strategy_name": strategy["name"],
        "txt_name": strategy["txt"],
        "total_ops": 0,
        "open_ops": 0,
        "closed_ops": 0,
        "wins": 0,
        "losses": 0,
        "profit_usd": 0.0,
        "closed_profit_usd": 0.0,
        "open_profit_usd": 0.0,
        "closed_duration_seconds": 0.0,
        "first_operation_at": "",
        "max_open_operations": 0,
        "max_detected_at": "",
    }


def earliest_text_datetime(*values):
    parsed_values = [parse_datetime_value(value) for value in values if value]
    parsed_values = [value for value in parsed_values if value]
    return datetime_to_text(min(parsed_values)) if parsed_values else ""


def format_short_date(value):
    parsed = parse_datetime_value(value)
    return parsed.strftime("%d/%m/%y") if parsed else ""


def format_duration_seconds(seconds):
    try:
        seconds = float(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "Sin cierres todavia"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f} h"
    days = seconds / 86400
    if days < 30:
        return f"{days:.1f} dias"
    return f"{days / 30:.1f} meses"


def max_open_from_events(events):
    events.sort(key=lambda item: (item[0], -item[1]))
    current = 0
    maximum = 0
    maximum_at = None
    for event_at, delta in events:
        current += delta
        if current > maximum:
            maximum = current
            maximum_at = event_at
    return maximum, maximum_at


def datetime_to_text(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def load_capital_maxima():
    if not CAPITAL_MAXIMA_TXT.exists():
        return {}
    maxima = {}
    for raw_line in CAPITAL_MAXIMA_TXT.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        try:
            maxima[parts[1]] = {
                "strategy_name": parts[0],
                "txt_name": parts[1],
                "max_open_operations": int(float(parts[2] or 0)),
                "capital_base": float(parts[3] or 0),
                "max_detected_at": parts[4],
            }
        except ValueError:
            continue
    return maxima


def max_open_operations(operations):
    events = []
    now = datetime.now(MADRID_TZ)
    for operation in operations:
        opened_at = parse_datetime_value(operation.get("opened_at") or operation.get("signal_date"))
        if not opened_at:
            continue
        closed_at = parse_datetime_value(operation.get("closed_at"))
        if operation.get("status") == "OPEN" or not closed_at or closed_at < opened_at:
            closed_at = now
        events.append((opened_at, 1))
        events.append((closed_at, -1))
    events.sort(key=lambda item: (item[0], -item[1]))
    current = 0
    maximum = 0
    maximum_at = None
    for event_at, delta in events:
        current += delta
        if current > maximum:
            maximum = current
            maximum_at = event_at
    return maximum, maximum_at


def parse_datetime_value(value):
    if not value:
        return None
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
                parsed = datetime.fromisoformat(f"{text_value[:10]}T00:00:00")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MADRID_TZ)
    return parsed.astimezone(MADRID_TZ)


def write_strategy_performance_txt(rows):
    lines = [
        "# strategy | txt | historical_return | return_pct | profit_usd | capital_base | current_capital | max_open | total_ops | open_ops | closed_ops | wins | losses | average_close_duration | success_rate | first_operation | updated_at"
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
                    f"{row['capital_base']:.2f}",
                    f"{row['current_capital']:.2f}",
                    str(row["max_open_operations"]),
                    str(row["total_ops"]),
                    str(row["open_ops"]),
                    str(row["closed_ops"]),
                    str(row["wins"]),
                    str(row["losses"]),
                    row["average_close_duration"],
                    row["success_rate"],
                    row["first_operation_display"],
                    row["updated_at"],
                ]
            )
        )
    PERFORMANCE_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_capital_maxima_txt(rows):
    lines = [
        "# strategy | txt | max_open_operations | capital_base_usd | max_detected_at | current_capital_usd | updated_at"
    ]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    row["strategy_name"],
                    row["txt_name"],
                    str(row["max_open_operations"]),
                    f"{row['capital_base']:.2f}",
                    row.get("max_detected_at", ""),
                    f"{row['current_capital']:.2f}",
                    row["updated_at"],
                ]
            )
        )
    CAPITAL_MAXIMA_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_chip_status_rows(now):
    market_open = time_in_madrid_window(now, "15:30", "22:00")
    watch_items = [
        ("strategies", "Strategies", [BASE_DIR / "strategy_run_status.json"]),
        ("strategies_v2", "Engine V2", [PROJECT_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.json"]),
        ("backtest_5y", "Backset", [BACKTEST_JSON_FILE]),
        ("universe", "Universe", [BASE_DIR / "tickers.txt", PROJECT_DIR / "data" / "assets.csv"]),
        ("market_full", "Market Full", [PROJECT_DIR / "data" / "market_data.csv"]),
        ("news", "Notices", [PROJECT_DIR / "data" / "market_news.json"]),
        ("sync_sqlite", "Post Sync", [PROJECT_DIR / "strategies.db"]),
        ("market-hours", "STATUS", []),
        ("Signals", "Signals", sorted(SIGNALS_DIR.glob("*.txt"))),
        ("Run", "Run", [BASE_DIR / "strategy_run_status.json"]),
        ("Selected", "Selected", [BASE_DIR / "estrategias_a_ejecutar.txt"]),
        ("Top Vol", "Top Vol", [BASE_DIR / "top_money_volume_assets.txt"]),
        ("V2 Diag", "V2 Diag", [PROJECT_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.json"]),
        ("Diag TXT", "Diag TXT", [PROJECT_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.txt"]),
        ("Ops State", "Ops State", [STATE_FILE]),
        ("Open Ops", "Open Ops", [OPEN_TXT]),
        ("Closed Ops", "Closed Ops", [CLOSED_TXT]),
        ("All Ops", "All Ops", [ALL_TXT]),
        ("Perf", "Perf", [PERFORMANCE_TXT]),
        ("Max Cap", "Max Cap", [CAPITAL_MAXIMA_TXT]),
        ("BT JSON", "BT JSON", [BACKTEST_JSON_FILE]),
        ("Assets", "Assets", [PROJECT_DIR / "data" / "assets.csv"]),
    ]
    rows = []
    today = now.date()
    for key, label, paths in watch_items:
        status = file_group_status(paths)
        updated_at = status["updated_at"]
        if key == "market-hours":
            ok = market_open
            updated_display = "15:30-22:00" if market_open else "22:00-15:30"
        else:
            ok = bool(market_open and updated_at and updated_at.date() == today)
            updated_display = updated_at.strftime("%H:%M") if updated_at else "no file"
        rows.append(
            {
                "key": key,
                "label": label,
                "ok": ok,
                "updated_display": updated_display,
                "file_count": status["count"],
                "total_bytes": status["bytes"],
                "latest_name": status["latest_name"],
                "updated_at": updated_at.isoformat() if updated_at else "",
                "synced_at": now.isoformat(),
            }
        )
    return rows


def file_group_status(paths):
    latest_path = None
    latest_mtime = None
    total_bytes = 0
    count = 0
    for raw_path in paths or []:
        try:
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            stats = path.stat()
        except OSError:
            continue
        count += 1
        total_bytes += int(stats.st_size or 0)
        if latest_mtime is None or stats.st_mtime > latest_mtime:
            latest_mtime = stats.st_mtime
            latest_path = path
    updated_at = datetime.fromtimestamp(latest_mtime, MADRID_TZ) if latest_mtime else None
    return {
        "count": count,
        "bytes": total_bytes,
        "latest_name": latest_path.name if latest_path else "",
        "updated_at": updated_at,
    }


def write_chip_status_txt(rows):
    lines = ["# key | label | ok | updated_display | updated_at | files | bytes | latest"]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    row["key"],
                    row["label"],
                    "green" if row["ok"] else "red",
                    row["updated_display"],
                    row["updated_at"],
                    str(row["file_count"]),
                    str(row["total_bytes"]),
                    row["latest_name"],
                ]
            )
        )
    CHIP_STATUS_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def time_in_madrid_window(now, start_text, end_text):
    start_hour, start_minute = [int(part) for part in start_text.split(":", 1)]
    end_hour, end_minute = [int(part) for part in end_text.split(":", 1)]
    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


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


def sync_operations_to_database(live_operations, backtest_operations, performance_rows):
    if engine is None or text is None:
        print("Sincronizacion DB omitida: modulo db/sqlalchemy no disponible.")
        return

    try:
        with engine.begin() as connection:
            ensure_operations_table(connection)
            ensure_sync_metadata_table(connection)
            ensure_chip_status_table(connection)
            print(f"Sincronizando operaciones live con DB: {len(live_operations)}")
            upsert_operations(connection, live_operations, label="live")
            sync_backtest_operations_to_database(connection, backtest_operations)
            sync_strategy_performance(connection, performance_rows)
            sync_chip_status_to_database(connection, read_chip_status_txt())
        print(
            "Operaciones sincronizadas con PostgreSQL/DB: "
            f"live={len(live_operations)} backtest={len(backtest_operations)}"
        )
    except Exception as error:
        print(f"No se pudieron sincronizar operaciones con DB: {error}")


def sync_backtest_operations_to_database(connection, backtest_operations):
    metadata_key = "backtest_operations_fingerprint"
    if not backtest_operations:
        deleted = connection.execute(
            text("DELETE FROM simulated_operations WHERE operation_key LIKE 'BACKTEST|%'")
        ).rowcount or 0
        if deleted:
            print(f"Backtest desactivado: filas backtest eliminadas de DB: {deleted}")
        set_sync_metadata(connection, metadata_key, "")
        return

    fingerprint = json.dumps(backtest_fingerprint(), sort_keys=True)
    existing_fingerprint = get_sync_metadata(connection, metadata_key)
    existing_count = connection.execute(
        text("SELECT COUNT(*) FROM simulated_operations WHERE operation_key LIKE 'BACKTEST|%'")
    ).scalar_one()
    if existing_fingerprint == fingerprint and int(existing_count or 0) == len(backtest_operations):
        print(f"Backtest ya sincronizado en DB: {existing_count} operaciones. No se reenvia.")
        return

    incremental_txt_names = backtest_incremental_txt_names()
    if incremental_txt_names and int(existing_count or 0) > 0:
        changed_operations = [
            operation
            for operation in backtest_operations
            if operation.get("txt_name") in incremental_txt_names
        ]
        deleted = delete_backtest_operations_for_txt_names(connection, incremental_txt_names)
        print(
            "Backtest incremental: "
            f"estrategias={', '.join(sorted(incremental_txt_names))} | "
            f"filas anteriores eliminadas={deleted} | filas nuevas={len(changed_operations)}"
        )
        upsert_operations(connection, changed_operations, label="backtest incremental")
        set_sync_metadata(connection, metadata_key, fingerprint)
        return

    deleted = connection.execute(
        text("DELETE FROM simulated_operations WHERE operation_key LIKE 'BACKTEST|%'")
    ).rowcount or 0
    if deleted:
        print(f"Backtest cambiado: filas anteriores eliminadas de DB: {deleted}")
    print(f"Sincronizando backtest con DB: {len(backtest_operations)}")
    upsert_operations(connection, backtest_operations, label="backtest")
    set_sync_metadata(connection, metadata_key, fingerprint)


def backtest_incremental_txt_names():
    try:
        data = json.loads(BACKTEST_JSON_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict) or data.get("merge_mode") != "incremental_strategy_update":
        return set()
    update = data.get("last_incremental_update") or {}
    names = update.get("strategy_names") or []
    txt_names = {
        TXT_BY_STRATEGY_NAME.get(str(name or "").strip())
        for name in names
        if str(name or "").strip()
    }
    return {txt for txt in txt_names if txt}


def delete_backtest_operations_for_txt_names(connection, txt_names):
    names = sorted({str(txt or "").strip() for txt in txt_names if str(txt or "").strip()})
    if not names:
        return 0
    placeholders = ", ".join(f":txt_{index}" for index, _name in enumerate(names))
    params = {f"txt_{index}": name for index, name in enumerate(names)}
    statement = text(
        "DELETE FROM simulated_operations "
        "WHERE operation_key LIKE 'BACKTEST|%' "
        f"AND txt_name IN ({placeholders})"
    )
    return connection.execute(statement, params).rowcount or 0


def upsert_operations(connection, operations, label="operaciones"):
    total_operations = len(operations)
    statement = operation_upsert_statement()
    for index, operation in enumerate(operations, start=1):
        connection.execute(statement, serialize_for_db(operation))
        if index % 1000 == 0 or index == total_operations:
            print(f"Operaciones {label} sincronizadas: {index}/{total_operations}")


def operation_upsert_statement():
    return text(
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
        ON CONFLICT(operation_key) DO UPDATE SET
            strategy_name = excluded.strategy_name,
            txt_name = excluded.txt_name,
            symbol = excluded.symbol,
            direction = excluded.direction,
            status = excluded.status,
            signal_date = excluded.signal_date,
            signal_line = excluded.signal_line,
            opened_at = excluded.opened_at,
            closed_at = excluded.closed_at,
            entry_price = excluded.entry_price,
            target_price = excluded.target_price,
            stop_loss = excluded.stop_loss,
            shares = excluded.shares,
            current_price = excluded.current_price,
            investment_value = excluded.investment_value,
            profit_usd = excluded.profit_usd,
            profit_pct = excluded.profit_pct,
            close_reason = excluded.close_reason,
            updated_at = excluded.updated_at
        """
    )


def sync_strategy_performance(connection, performance_rows):
    ensure_strategy_performance_columns(connection)
    updated = 0
    for row in performance_rows:
        result = connection.execute(
            text(
                """
                UPDATE strategies
                SET historical_return = :historical_return,
                    closed_operations_count = :closed_operations_count,
                    average_close_duration = :average_close_duration,
                    success_rate = :success_rate,
                    first_operation_display = :first_operation_display
                WHERE name = :strategy_name
                   OR signals_txt_name = :txt_name
                """
            ),
            {
                "historical_return": row["historical_return"],
                "closed_operations_count": int(row["closed_ops"]),
                "average_close_duration": row["average_close_duration"],
                "success_rate": row["success_rate"],
                "first_operation_display": row["first_operation_display"],
                "strategy_name": row["strategy_name"],
                "txt_name": row["txt_name"],
            },
        )
        updated += result.rowcount or 0
    print(f"Rentabilidad historica actualizada en PostgreSQL/DB: {updated} estrategias")


def ensure_strategy_performance_columns(connection):
    column_defs = [
        ("closed_operations_count", "INTEGER NOT NULL DEFAULT 0"),
        ("average_close_duration", "TEXT NOT NULL DEFAULT ''"),
        ("success_rate", "TEXT NOT NULL DEFAULT ''"),
        ("first_operation_display", "TEXT NOT NULL DEFAULT ''"),
    ]
    for column_name, definition in column_defs:
        if not strategy_column_exists(connection, column_name):
            connection.execute(text(f"ALTER TABLE strategies ADD COLUMN {column_name} {definition}"))


def strategy_column_exists(connection, column_name):
    if engine is not None and getattr(engine.dialect, "name", "") == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'strategies'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0
    rows = connection.execute(text("PRAGMA table_info(strategies)")).fetchall()
    return any(row[1] == column_name for row in rows)


def read_chip_status_txt():
    rows = []
    try:
        lines = CHIP_STATUS_TXT.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 8:
            continue
        rows.append(
            {
                "key": parts[0],
                "label": parts[1],
                "ok": 1 if parts[2].lower() == "green" else 0,
                "updated_display": parts[3],
                "updated_at": parse_datetime_for_db(parts[4]),
                "file_count": parse_int(parts[5]),
                "total_bytes": parse_int(parts[6]),
                "latest_name": parts[7],
                "synced_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )
    return rows


def sync_chip_status_to_database(connection, rows):
    if not rows:
        return
    statement = text(
        """
        INSERT INTO chip_status
        (key, label, ok, updated_display, updated_at, file_count, total_bytes, latest_name, synced_at)
        VALUES
        (:key, :label, :ok, :updated_display, :updated_at, :file_count, :total_bytes, :latest_name, :synced_at)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label,
            ok = excluded.ok,
            updated_display = excluded.updated_display,
            updated_at = excluded.updated_at,
            file_count = excluded.file_count,
            total_bytes = excluded.total_bytes,
            latest_name = excluded.latest_name,
            synced_at = excluded.synced_at
        """
    )
    connection.execute(statement, rows)
    print(f"Semaforos sincronizados con PostgreSQL/DB: {len(rows)}")


def ensure_sync_metadata_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sync_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP
            )
            """
        )
    )


def get_sync_metadata(connection, key):
    row = connection.execute(
        text("SELECT value FROM sync_metadata WHERE key = :key"),
        {"key": key},
    ).fetchone()
    return row[0] if row else ""


def set_sync_metadata(connection, key, value):
    connection.execute(
        text(
            """
            INSERT INTO sync_metadata (key, value, updated_at)
            VALUES (:key, :value, :updated_at)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """
        ),
        {
            "key": key,
            "value": value,
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        },
    )


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


def ensure_chip_status_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS chip_status (
                key TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                updated_display TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP,
                file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                latest_name TEXT NOT NULL DEFAULT '',
                synced_at TIMESTAMP
            )
            """
        )
    )


def serialize_for_db(operation):
    row = dict(operation)
    for key in ("opened_at", "closed_at", "updated_at"):
        row[key] = parse_datetime_for_db(row.get(key))
    return row


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def build_daily_symbol_key(txt_name, symbol, signal_date):
    return "|".join(
        [
            txt_name,
            str(symbol or "").strip().upper(),
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
