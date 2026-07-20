import json
import os
import re
import hmac
import hashlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from hmac import compare_digest
from functools import wraps
from uuid import uuid4
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    abort,
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import bindparam, text
from werkzeug.security import check_password_hash, generate_password_hash

from db import SQLITE_DATABASE, engine
from market_scanner import (
    available_markets,
    available_sectors,
    csv_updated_at,
    filter_assets,
    load_assets,
    load_universe_assets,
    save_universe_assets,
    snapshot_count,
    universe_count,
    ensure_universe_table,
)
from update_market_data import update_market_data
from update_assets import build_assets_from_alpaca, write_assets


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SIGNALS_DIR = (BASE_DIR / "Estrategias" / "salidas_txt").resolve()
DEFAULT_STRATEGY_STATUS_FILE = (BASE_DIR / "Estrategias" / "strategy_run_status.json").resolve()
DEFAULT_STRATEGY_LOG_DIR = (BASE_DIR / "Estrategias" / "logs").resolve()
DEFAULT_STRATEGY_TICKERS_FILE = (BASE_DIR / "Estrategias" / "tickers.txt").resolve()
DEFAULT_TOP_MONEY_VOLUME_FILE = (BASE_DIR / "Estrategias" / "top_money_volume_assets.txt").resolve()
DEFAULT_SIMULATED_OPERATIONS_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_estado.json").resolve()
DEFAULT_OPEN_OPERATIONS_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_abiertas.txt").resolve()
DEFAULT_CLOSED_OPERATIONS_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_cerradas.txt").resolve()
DEFAULT_STRATEGY_PERFORMANCE_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "rentabilidad_estrategias.txt").resolve()
DEFAULT_CAPITAL_MAX_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "capital_maximos_estrategias.txt").resolve()
DEFAULT_BACKTEST_SUMMARY_FILE = (BASE_DIR / "Estrategias" / "operaciones_simuladas" / "backtest_resumen_estrategias.json").resolve()
DEFAULT_BACKTEST_OUTPUT_FILE = (BASE_DIR / "EstrategiasV2" / "outputs" / "historical_backtest_5y.json").resolve()
DEFAULT_V2_SIGNALS_TXT_FILE = (BASE_DIR / "EstrategiasV2" / "outputs" / "signals_v2.txt").resolve()
DEFAULT_V2_DIAGNOSTICS_FILE = (BASE_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.json").resolve()
DEFAULT_V2_DIAGNOSTICS_TXT_FILE = (BASE_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.txt").resolve()
DEFAULT_HISTORICAL_MANIFEST_FILE = (BASE_DIR / "EstrategiasV2" / "historical_data" / "manifest.json").resolve()
LOCAL_SQLITE_FILE = Path(SQLITE_DATABASE).resolve()
STRATEGIES_RUNNER = BASE_DIR / "Estrategias" / "run_all_strategies.py"
MADRID_TZ = ZoneInfo("Europe/Madrid")


def rollback_request_db():
    try:
        connection = getattr(g, "db", None)
        if connection is not None:
            connection.rollback()
    except Exception:
        pass
SCHEDULER_THREAD_STARTED = False
SCHEDULER_LOCK = threading.Lock()
SCHEDULER_TASKS = {
    "assets_csv": "Actualizar CSV de activos",
    "market_batch": "Actualizar mercado por tanda",
    "market_full": "Actualizar mercado completo",
    "strategies": "Ejecutar estrategias",
}
WEEKDAYS = [
    (1, "Lun"),
    (2, "Mar"),
    (3, "Mie"),
    (4, "Jue"),
    (5, "Vie"),
    (6, "Sab"),
    (7, "Dom"),
]
DEFAULT_WEEKDAYS = "1,2,3,4,5"
DEFAULT_STRATEGY_FILES = {
    "Momentum": "Momentum.py",
    "Swing Trading": "SwingTrading.py",
    "BreaKout": "BreaKout.py",
    "Mean Reversion": "Mean Reversion.py",
    "Value Trading": "ValueTrading.py",
    "Dividend Growth": "DividenGrowth.py",
    "Trend Following": "TrendFollowing.py",
    "Pairs Trading": "PairsTrading.py",
    "Sector Rotation": "SectorRotation.py",
    "Quality Investing": "QualityInvesting.py",
    "Opening Range BreaKout": "OpeningRangeBreaKout.py",
    "VWAP Reversion": "VWAP Reversion.py",
    "Momentum Intradia": "MomentumIntradia.py",
    "Scalping The PullBacks": "ScalpingThePullBacKs.py",
    "Gap and Go": "Gap and Go.py",
    "Follow The Money": "FollowTheMoney.py",
    "Entrada Dinero Direccional": "EntradaDineroDireccional.py",
    "Acumula Metales": "AcumulaMetales.py",
    "Acumulacion": "Acumulacion.py",
    "Reversion RSI 5": "ReversionRSI5.py",
}
DEFAULT_STRATEGY_SCHEDULES = {
    "Momentum": {"start": "22:10", "end": "22:25", "interval": 1440},
    "Swing Trading": {"start": "22:15", "end": "22:30", "interval": 1440},
    "BreaKout": {"start": "15:45", "end": "21:45", "interval": 30},
    "Mean Reversion": {"start": "21:30", "end": "21:55", "interval": 30},
    "Value Trading": {"start": "22:35", "end": "22:50", "interval": 1440},
    "Dividend Growth": {"start": "22:40", "end": "22:55", "interval": 1440},
    "Trend Following": {"start": "22:20", "end": "22:35", "interval": 1440},
    "Pairs Trading": {"start": "16:00", "end": "21:45", "interval": 60},
    "Sector Rotation": {"start": "22:30", "end": "22:45", "interval": 1440},
    "Quality Investing": {"start": "22:45", "end": "23:00", "interval": 1440},
    "Opening Range BreaKout": {"start": "15:35", "end": "17:00", "interval": 10},
    "VWAP Reversion": {"start": "16:00", "end": "21:45", "interval": 20},
    "Momentum Intradia": {"start": "15:40", "end": "21:45", "interval": 15},
    "Scalping The PullBacks": {"start": "15:40", "end": "21:45", "interval": 10},
    "Gap and Go": {"start": "15:35", "end": "17:30", "interval": 10},
    "Follow The Money": {"start": "15:30", "end": "22:00", "interval": 60},
    "Entrada Dinero Direccional": {"start": "22:05", "end": "22:20", "interval": 1440},
    "Acumula Metales": {"start": "15:30", "end": "22:00", "interval": 240},
    "Acumulacion": {"start": "15:30", "end": "22:00", "interval": 240},
    "Reversion RSI 5": {"start": "15:30", "end": "22:00", "interval": 10},
}
SIGNAL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,14}$")
SIGNAL_SIDE_WORDS = {"LONG", "SHORT", "BUY", "SELL", "COMPRA", "VENTA"}
HISTORY_OPERATION_PAGE_SIZE = 100


def terminal_feed_payload(limit=None):
    include_local_files = True
    feed_path = terminal_feed_txt_path() if include_local_files else None
    updated_line = ""
    signal_count_line = ""
    non_empty_lines = 0
    detected_signals = 0
    stats = None
    if feed_path:
        stats = feed_path.stat()
        try:
            with feed_path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    non_empty_lines += 1
                    lower_line = line.lower()
                    if lower_line.startswith("actualizado:"):
                        updated_line = line
                    elif lower_line.startswith("senales:") or lower_line.startswith("señales:"):
                        signal_count_line = line
                    elif terminal_line_is_signal(line):
                        detected_signals += 1
                    if limit is not None and non_empty_lines >= limit:
                        break
        except OSError as error:
            return {
                "signature": "read-error",
                "lines": [
                    "ERROR signals_v2.txt read failed",
                    f"CategoryInfo : {error}",
                    "ActionRequired : check file permissions",
                ],
            }

    lines = terminal_database_event_lines()
    if include_local_files and feed_path and stats:
        lines.extend(
            [
                f"READ :: LOCAL TXT {short_terminal_path(feed_path)}",
                f"LOAD OK :: LOCAL TXT {updated_line or 'timestamp not declared'}",
                f"CHECK :: LOCAL TXT {signal_count_line or f'signals detected: {detected_signals}'}",
                f"ACCEPTED :: LOCAL TXT physical_lines={non_empty_lines} bytes={stats.st_size}",
            ]
        )
    if include_local_files:
        lines.extend(terminal_local_file_event_lines())
        lines.extend(terminal_upload_status_event_lines())
    lines.append("WAIT :: next database update")
    signature_parts = terminal_event_signature_parts(feed_path, stats)
    return {
        "signature": "industrial-events:" + ":".join(signature_parts),
        "lines": lines,
        "file_statuses": terminal_upload_status_payload(),
    }


def terminal_database_event_lines():
    lines = []
    try:
        dialect = terminal_database_dialect()
        channel = terminal_database_channel()
        lines.append(f"READ :: {channel} connection OK dialect={dialect}")

        strategies = terminal_scalar("SELECT COUNT(*) FROM strategies")
        active_strategies = terminal_scalar("SELECT COUNT(*) FROM strategies WHERE COALESCE(is_active, 0) = 1")
        visible_free = terminal_scalar("SELECT COUNT(*) FROM strategies WHERE COALESCE(public_visible, 0) = 1")
        strategy_signals = terminal_scalar("SELECT COUNT(*) FROM strategy_signals")
        simulated_operations = terminal_scalar("SELECT COUNT(*) FROM simulated_operations")
        open_operations = terminal_scalar("SELECT COUNT(*) FROM simulated_operations WHERE status = 'OPEN'")
        closed_operations = terminal_scalar("SELECT COUNT(*) FROM simulated_operations WHERE status = 'CLOSED'")
        asset_universe = terminal_scalar("SELECT COUNT(*) FROM asset_universe")
        asset_snapshots = terminal_scalar("SELECT COUNT(*) FROM asset_snapshots")
        top_volume = terminal_scalar("SELECT COUNT(*) FROM top_money_volume_assets")
        diagnostics = terminal_scalar("SELECT COUNT(*) FROM strategy_diagnostics")
        market_news = terminal_scalar("SELECT COUNT(*) FROM market_news")
        latest_signal = terminal_scalar("SELECT MAX(created_at) FROM strategy_signals")
        latest_operation = terminal_scalar("SELECT MAX(updated_at) FROM simulated_operations")
        latest_top_volume = terminal_scalar("SELECT MAX(updated_at) FROM top_money_volume_assets")
        latest_market = terminal_scalar("SELECT MAX(updated_at) FROM asset_snapshots")
        latest_diagnostics = terminal_scalar("SELECT MAX(updated_at) FROM strategy_diagnostics")
        latest_news = terminal_scalar("SELECT MAX(created_at) FROM market_news")

        lines.extend(
            [
                f"CHECK :: strategies total={strategies} active={active_strategies} public={visible_free}",
                f"LOAD OK :: strategy_signals rows={strategy_signals}",
                f"LOAD OK :: simulated_operations rows={simulated_operations} open={open_operations} closed={closed_operations}",
                f"LOAD OK :: asset_universe rows={asset_universe}",
                f"LOAD OK :: asset_snapshots rows={asset_snapshots}",
                f"LOAD OK :: top_volume rows={top_volume}",
                f"LOAD OK :: strategy_diagnostics rows={diagnostics}",
                f"LOAD OK :: market_news rows={market_news}",
            ]
        )
        if latest_signal:
            lines.append(f"UPDATE :: strategy_signals latest={latest_signal}")
        else:
            lines.append("WARNING :: strategy_signals empty :: waiting for local engine sync")
        if latest_operation:
            lines.append(f"UPDATE :: simulated_operations latest={latest_operation}")
        else:
            lines.append("WARNING :: simulated_operations empty :: no operations loaded yet")
        if latest_top_volume:
            lines.append(f"UPDATE :: top_volume latest={latest_top_volume}")
        if latest_market:
            lines.append(f"UPDATE :: market_snapshot latest={latest_market}")
        if latest_diagnostics:
            lines.append(f"UPDATE :: strategy_diagnostics latest={latest_diagnostics}")
        if latest_news:
            lines.append(f"UPDATE :: market_news latest={latest_news}")
        lines.extend(terminal_strategy_activity_lines())
        lines.extend(terminal_operation_activity_lines())
        lines.extend(terminal_signal_activity_lines())
        lines.extend(terminal_strategy_error_lines())
    except Exception as error:
        lines.append(f"ERROR :: {terminal_database_channel()} connection failed")
        lines.append(f"CategoryInfo : {type(error).__name__}: {str(error)[:180]}")
    return lines


def terminal_database_channel():
    return "POSTGRES" if engine.dialect.name == "postgresql" else "LOCAL DB"


def terminal_database_dialect():
    return engine.dialect.name.upper()


def terminal_scalar(sql):
    try:
        with engine.connect() as connection:
            value = connection.execute(text(sql)).scalar()
    except Exception:
        return 0
    return "" if value is None else value


def terminal_strategy_error_lines(limit=8):
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT name, run_status, run_message, run_at, run_returncode
                        FROM strategies
                        WHERE COALESCE(run_status, '') IN ('ERROR', 'RUNNING')
                           OR COALESCE(run_returncode, 0) <> 0
                        ORDER BY run_at DESC, name ASC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []

    lines = []
    for row in rows:
        status = (row.get("run_status") or "UNKNOWN").upper()
        name = row.get("name") or "strategy"
        message = (row.get("run_message") or "").replace("\n", " ").strip()
        if status == "ERROR":
            lines.append(f"ERROR STRATEGY UNIT :: {name} :: returncode={row.get('run_returncode')}")
        elif status == "RUNNING":
            lines.append(f"WARNING STRATEGY UNIT :: {name} :: status=RUNNING")
        if message:
            lines.append(f"DETAIL :: {name} :: {message[:160]}")
    if not lines:
        lines.append("ACCEPTED :: strategy status clean")
    return lines


def terminal_strategy_activity_lines(limit=6):
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT name, run_status, run_message, run_at, run_txt_updated
                        FROM strategies
                        WHERE run_at IS NOT NULL
                        ORDER BY run_at DESC, name ASC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []

    lines = []
    for row in rows:
        status = (row.get("run_status") or "PENDING").upper()
        txt_state = "txt_updated=1" if row.get("run_txt_updated") else "txt_updated=0"
        lines.append(f"STRATEGY UPDATE :: {row.get('name') or 'strategy'} status={status} {txt_state} run_at={row.get('run_at')}")
        message = (row.get("run_message") or "").replace("\n", " ").strip()
        if message:
            lines.append(f"STRATEGY DETAIL :: {row.get('name') or 'strategy'} :: {message[:140]}")
    return lines


def terminal_operation_activity_lines(limit=8):
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT strategy_name, symbol, status, profit_loss_pct, close_reason, updated_at
                        FROM simulated_operations
                        ORDER BY updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []

    lines = []
    for row in rows:
        status = (row.get("status") or "UNKNOWN").upper()
        prefix = "CLOSED OP" if status == "CLOSED" else "OPEN OP"
        reason = f" reason={row.get('close_reason')}" if row.get("close_reason") else ""
        pnl = row.get("profit_loss_pct")
        try:
            pnl_text = f"{float(pnl):+.2f}%"
        except (TypeError, ValueError):
            pnl_text = str(pnl or "0.00%")
        lines.append(
            f"{prefix} :: {row.get('strategy_name') or 'strategy'} {row.get('symbol') or '-'} pnl={pnl_text}{reason} updated={row.get('updated_at')}"
        )
    return lines


def terminal_signal_activity_lines(limit=6):
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT txt_name, COUNT(*) AS signals, MAX(created_at) AS latest
                        FROM strategy_signals
                        GROUP BY txt_name
                        ORDER BY latest DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []

    lines = []
    for row in rows:
        lines.append(f"SIGNAL UPDATE :: {row.get('txt_name') or 'signals'} rows={row.get('signals')} latest={row.get('latest')}")
    return lines


def terminal_watched_files():
    return [
        ("LOCAL TXT", terminal_feed_txt_path()),
        ("RUN STATUS", DEFAULT_STRATEGY_STATUS_FILE),
        ("TOP VOLUME", DEFAULT_TOP_MONEY_VOLUME_FILE),
        ("OPERATIONS STATE", DEFAULT_SIMULATED_OPERATIONS_FILE),
        ("OPEN OPERATIONS", DEFAULT_OPEN_OPERATIONS_FILE),
        ("CLOSED OPERATIONS", DEFAULT_CLOSED_OPERATIONS_FILE),
        ("STRATEGY PERF", DEFAULT_STRATEGY_PERFORMANCE_FILE),
        ("CAPITAL MAX", DEFAULT_CAPITAL_MAX_FILE),
        ("BACKTEST SUMMARY", DEFAULT_BACKTEST_SUMMARY_FILE),
        ("BACKTEST JSON", DEFAULT_BACKTEST_OUTPUT_FILE),
        ("V2 SIGNALS", DEFAULT_V2_SIGNALS_TXT_FILE),
        ("V2 DIAGNOSTICS", DEFAULT_V2_DIAGNOSTICS_FILE),
        ("ASSETS CSV", BASE_DIR / "data" / "assets.csv"),
        ("MARKET CSV", BASE_DIR / "data" / "market_data.csv"),
    ]


def terminal_upload_watch_groups():
    signal_files = sorted(DEFAULT_SIGNALS_DIR.glob("*.txt")) if DEFAULT_SIGNALS_DIR.exists() else []
    return [
        ("Signals", signal_files),
        ("Run", [DEFAULT_STRATEGY_STATUS_FILE]),
        ("Selected", [BASE_DIR / "Estrategias" / "estrategias_a_ejecutar.txt"]),
        ("Top Vol", [DEFAULT_TOP_MONEY_VOLUME_FILE]),
        ("V2 Diag", [DEFAULT_V2_DIAGNOSTICS_FILE]),
        ("Diag TXT", [DEFAULT_V2_DIAGNOSTICS_TXT_FILE]),
        ("Ops State", [DEFAULT_SIMULATED_OPERATIONS_FILE]),
        ("Open Ops", [DEFAULT_OPEN_OPERATIONS_FILE]),
        ("Closed Ops", [DEFAULT_CLOSED_OPERATIONS_FILE]),
        ("All Ops", [BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_todas.txt"]),
        ("Perf", [DEFAULT_STRATEGY_PERFORMANCE_FILE]),
        ("Max Cap", [DEFAULT_CAPITAL_MAX_FILE]),
        ("BT JSON", [DEFAULT_BACKTEST_OUTPUT_FILE]),
        ("Assets", [BASE_DIR / "data" / "assets.csv"]),
    ]


def upload_status_description(label):
    descriptions = {
        "Signals": "TXT de señales",
        "Run": "estado de ejecucion",
        "Selected": "archivo de estrategias",
        "Top Vol": "top activos",
        "V2 Diag": "diagnostico generado por motor V2",
        "Diag TXT": "diagnostico V2 en TXT",
        "Ops State": "estado general de operaciones",
        "Open Ops": "operaciones abiertas",
        "Closed Ops": "operaciones cerradas",
        "All Ops": "todas las operaciones",
        "Perf": "rentabilidad/rendimiento por estrategia",
        "Max Cap": "capital maximo/base calculado",
        "BT JSON": "JSON del back",
        "Assets": "universo",
    }
    return descriptions.get(label, label)


def operation_pilot_description(key, label=None):
    descriptions = {
        "strategies": "ejecucion del motor antiguo / clasico",
        "strategies_v2": "ejecucion del motor V2",
        "backtest_5y": "generacion/carga del historico",
        "universe": "actualizacion del universo",
        "market_full": "actualizacion completa de mercado/snapshots",
        "news": "actualizacion de noticias relevantes",
        "sync_sqlite": "sincronizacion PostgreSQL/SQLite",
        "market-hours": "ventana",
    }
    return descriptions.get(key, label or key)


def terminal_upload_status_event_lines():
    lines = ["CHECK :: upload file status panel"]
    upload_labels = [label for label, _paths in terminal_upload_watch_groups()]
    db_items = database_chip_status_payload(upload_labels) or database_upload_status_payload()
    if db_items:
        for item in db_items:
            prefix = "OK FILE" if item["ok"] else "WARNING FILE"
            color = "green" if item["ok"] else "red"
            detail = item["updated_display"] if item["exists"] else "missing"
            lines.append(f"{prefix} :: {item['label']} {color} updated={detail} files={item['count']}")
        return lines
    today = datetime.now(MADRID_TZ).date()
    for label, paths in terminal_upload_watch_groups():
        status = terminal_file_group_status(paths)
        if not status["exists"]:
            lines.append(f"WARNING FILE :: {label} red missing")
            continue
        is_fresh = status["updated_at"].date() == today
        prefix = "OK FILE" if is_fresh else "WARNING FILE"
        color = "green" if is_fresh else "red"
        lines.append(
            f"{prefix} :: {label} {color} updated={status['updated_at'].strftime('%H:%M:%S')} "
            f"files={status['count']} bytes={status['bytes']} latest={status['latest_name']}"
        )
    return lines


def terminal_upload_status_payload():
    upload_labels = [label for label, _paths in terminal_upload_watch_groups()]
    db_items = database_chip_status_payload(upload_labels) or database_upload_status_payload()
    if db_items:
        return db_items
    today = datetime.now(MADRID_TZ).date()
    items = []
    for label, paths in terminal_upload_watch_groups():
        status = terminal_file_group_status(paths)
        updated_at = status["updated_at"]
        ok = bool(updated_at and updated_at.date() == today)
        items.append(
            {
                "label": label,
                "ok": ok,
                "exists": status["exists"],
                "count": status["count"],
                "updated_display": updated_at.strftime("%H:%M") if updated_at else "no file",
                "description": upload_status_description(label),
            }
        )
    return items


def database_chip_status_payload(keys=None):
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT key, label, ok, updated_display, updated_at, file_count, synced_at
                        FROM chip_status
                        """
                    )
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []
    if not rows:
        return []
    keys = list(keys or [])
    rows_by_key = {row.get("key"): dict(row) for row in rows}
    requested = keys or list(rows_by_key.keys())
    now = datetime.now(MADRID_TZ)
    today = now.date()
    market_open = time_in_madrid_window_global(now, "15:30", "22:00")
    items = []
    for key in requested:
        row = rows_by_key.get(key)
        if not row:
            continue
        updated_at = parse_database_datetime_global(row.get("updated_at") or row.get("synced_at"))
        updated_today = bool(updated_at and updated_at.astimezone(MADRID_TZ).date() == today)
        ok = bool(row.get("ok")) and updated_today
        if key == "market-hours":
            ok = market_open
        items.append(
            {
                "key": key,
                "label": row.get("label") or key,
                "ok": ok,
                "exists": True,
                "count": int(row.get("file_count") or 0),
                "updated_display": row.get("updated_display") or (updated_at.strftime("%H:%M") if updated_at else "sin fecha"),
                "description": upload_status_description(row.get("label") or key),
                "title": f"{upload_status_description(row.get('label') or key)} | {row.get('updated_display') or ''}",
            }
        )
    return items


def database_upload_status_payload():
    labels = [label for label, _paths in terminal_upload_watch_groups()]
    if not labels:
        return []
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT label, exists_flag, file_count, latest_updated_at
                        FROM upload_file_status
                        """
                    )
                )
                .mappings()
                .fetchall()
            )
    except Exception:
        return []
    rows_by_label = {row.get("label"): dict(row) for row in rows}
    today = datetime.now(MADRID_TZ).date()
    items = []
    for label in labels:
        row = rows_by_label.get(label)
        updated_at = parse_database_datetime_global(row.get("latest_updated_at")) if row else None
        ok = bool(updated_at and updated_at.date() == today)
        items.append(
            {
                "label": label,
                "ok": ok,
                "exists": bool(row and row.get("exists_flag")),
                "count": int(row.get("file_count") or 0) if row else 0,
                "updated_display": updated_at.strftime("%H:%M") if updated_at else "no file",
                "description": upload_status_description(label),
            }
        )
    return items


def parse_database_datetime_global(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(MADRID_TZ)


def time_in_madrid_window_global(now, start_text, end_text):
    start_hour, start_minute = [int(part) for part in start_text.split(":", 1)]
    end_hour, end_minute = [int(part) for part in end_text.split(":", 1)]
    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def terminal_file_group_status(paths):
    existing = []
    latest_path = None
    latest_mtime = None
    total_bytes = 0
    for raw_path in paths or []:
        if not raw_path:
            continue
        try:
            path = Path(raw_path).resolve()
            if not path.exists() or not path.is_file():
                continue
            stats = path.stat()
        except OSError:
            continue
        existing.append(path)
        total_bytes += stats.st_size
        if latest_mtime is None or stats.st_mtime > latest_mtime:
            latest_mtime = stats.st_mtime
            latest_path = path
    updated_at = datetime.fromtimestamp(latest_mtime, MADRID_TZ) if latest_mtime else None
    return {
        "exists": bool(existing),
        "count": len(existing),
        "bytes": total_bytes,
        "updated_at": updated_at,
        "latest_name": latest_path.name if latest_path else "-",
    }


def terminal_local_file_event_lines():
    lines = []
    today = datetime.now(MADRID_TZ).date()
    for label, path in terminal_watched_files():
        if not path:
            continue
        try:
            resolved = Path(path).resolve()
            stats = resolved.stat()
        except OSError:
            continue
        updated_datetime = datetime.fromtimestamp(stats.st_mtime, MADRID_TZ)
        updated_at = updated_datetime.strftime("%d/%m/%Y %H:%M:%S")
        lines.append(f"UPDATE :: LOCAL FILE {label} bytes={stats.st_size} mtime={updated_at}")
        if updated_datetime.date() == today:
            lines.append(f"LOCAL LOAD OK :: {label}")
        else:
            lines.append(f"WARNING FILE :: {label} red stale updated={updated_at}")
    if not lines:
        lines.append("CHECK :: LOCAL FILES optional watch empty")
    return lines


def terminal_event_signature_parts(feed_path, stats):
    parts = terminal_database_signature_parts()
    if feed_path and stats:
        parts.append(f"{feed_path.name}-{int(stats.st_mtime)}-{stats.st_size}")
    for _label, path in terminal_watched_files():
        if not path:
            continue
        try:
            resolved = Path(path).resolve()
            file_stats = resolved.stat()
        except OSError:
            continue
        parts.append(f"{resolved.name}-{int(file_stats.st_mtime)}-{file_stats.st_size}")
    for label, paths in terminal_upload_watch_groups():
        status = terminal_file_group_status(paths)
        if not status["exists"]:
            parts.append(f"{label}-missing")
            continue
        parts.append(
            f"{label}-{int(status['updated_at'].timestamp())}-{status['count']}-{status['bytes']}-{status['latest_name']}"
        )
    return parts or ["no-files"]


def terminal_database_signature_parts():
    queries = [
        ("strategy_signals", "SELECT COUNT(*), MAX(created_at) FROM strategy_signals"),
        ("simulated_operations", "SELECT COUNT(*), MAX(updated_at) FROM simulated_operations"),
        ("top_money_volume_assets", "SELECT COUNT(*), MAX(updated_at) FROM top_money_volume_assets"),
        ("asset_snapshots", "SELECT COUNT(*), MAX(updated_at) FROM asset_snapshots"),
        ("strategy_diagnostics", "SELECT COUNT(*), MAX(updated_at) FROM strategy_diagnostics"),
        ("market_news", "SELECT COUNT(*), MAX(created_at) FROM market_news"),
        ("strategies", "SELECT COUNT(*), MAX(run_at) FROM strategies"),
    ]
    parts = [terminal_database_channel()]
    try:
        with engine.connect() as connection:
            for label, sql in queries:
                try:
                    row = connection.execute(text(sql)).first()
                except Exception:
                    continue
                if row:
                    parts.append(f"{label}-{row[0]}-{row[1] or ''}")
    except Exception:
        parts.append("database-unavailable")
    return parts


def terminal_feed_txt_path():
    candidates = [
        os.environ.get("TRADING_V2_SIGNALS_TXT_FILE", "").strip(),
        DEFAULT_V2_SIGNALS_TXT_FILE,
        BASE_DIR / "EstrategiasV2" / "outputs" / "signals_v2.txt",
        os.environ.get("TRADING_V2_DIAGNOSTICS_TXT_FILE", "").strip(),
        DEFAULT_V2_DIAGNOSTICS_TXT_FILE,
        BASE_DIR / "EstrategiasV2" / "outputs" / "diagnostics_v2.txt",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = Path(candidate).expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def short_terminal_path(path):
    return "\\".join(path.parts[-5:])


def terminal_line_is_signal(line):
    parts = [part.strip() for part in line.split("|")]
    return len(parts) >= 7 and ":" not in parts[0]


TECHNICAL_TERM_HELP = [
    ("ticker", "Simbolo del activo cotizado."),
    ("precio actual", "Ultimo precio usado por la estrategia cuando genero el aviso."),
    ("precio", "Precio de referencia del activo en el momento del calculo."),
    ("direccion", "Sentido de la operacion: LONG busca subida, SHORT busca caida."),
    ("apertura", "Precio de referencia para abrir o vigilar la operacion."),
    ("entrada", "Precio de referencia para entrar en la operacion."),
    ("cierre", "Precio objetivo o condicion de salida de la operacion."),
    ("salida", "Zona o condicion donde la estrategia podria cerrar la operacion."),
    ("stop loss", "Nivel defensivo para limitar la perdida si la operacion va en contra."),
    ("stop", "Nivel defensivo para limitar la perdida si la operacion va en contra."),
    ("tp", "Take profit: objetivo de beneficio propuesto por la estrategia."),
    ("score", "Puntuacion interna para ordenar candidatos. Cuanto mayor, mejor segun la estrategia."),
    ("rsi14 d", "RSI de 14 velas diarias. Mide sobrecompra/sobreventa en grafico diario."),
    ("rsi5 1m", "RSI de 5 velas de 1 minuto. Se usa para detectar extremos intradia rapidos."),
    ("rs vs sector", "Fuerza relativa de la accion comparada con su sector."),
    ("rs20d%", "Fuerza relativa de 20 dias frente al benchmark configurado."),
    ("rs ", "Fuerza relativa frente a un indice, sector o referencia."),
    ("rsi", "Indicador de fuerza relativa. Valores bajos suelen indicar sobreventa y altos sobrecompra."),
    ("vwap", "Precio medio ponderado por volumen durante la sesion."),
    ("vwap 1m", "Precio medio ponderado por volumen de la ultima sesion intradia cargada."),
    ("sma20 d", "Media movil simple de 20 sesiones diarias."),
    ("sma50 d", "Media movil simple de 50 sesiones diarias."),
    ("sma120 d", "Media movil simple de 120 sesiones diarias."),
    ("sma180 d", "Media movil simple de 180 sesiones diarias."),
    ("sma200 d", "Media movil simple de 200 sesiones diarias."),
    ("sma120 w", "Media movil simple de 120 semanas."),
    ("sma120 1m", "Media movil simple de las ultimas 120 velas de 1 minuto disponibles."),
    ("sma", "Media movil simple. Resume el precio medio de las ultimas velas o dias."),
    ("media 2h", "Media del precio de las ultimas dos horas. Sirve para medir cuanto se ha alejado el precio en intradia."),
    ("ema", "Media movil exponencial. Da mas peso a los precios recientes."),
    ("atr", "Rango medio real. Mide volatilidad y ayuda a colocar stops."),
    ("vol$20d", "Volumen monetario medio de las ultimas 20 sesiones."),
    ("vol$x20d", "Ratio entre el volumen monetario actual y el volumen monetario medio de 20 sesiones."),
    ("vol xmedia", "Volumen actual comparado con su volumen medio."),
    ("vol$", "Volumen monetario negociado: precio multiplicado por volumen."),
    ("vol", "Volumen negociado. Ayuda a medir liquidez e interes del mercado."),
    ("mom20d%", "Rentabilidad del activo en las ultimas 20 sesiones."),
    ("momentum", "Fuerza del movimiento reciente del precio."),
    ("res20d", "Resistencia previa de 20 sesiones, sin contar la vela actual."),
    ("break%", "Distancia porcentual del precio respecto a la resistencia previa."),
    ("ruptura", "El precio supera una zona importante, como resistencia o rango previo."),
    ("breakout", "Ruptura de una resistencia o rango relevante."),
    ("resistencia", "Zona donde antes el precio tuvo dificultad para seguir subiendo."),
    ("soporte", "Zona donde antes el precio tuvo dificultad para seguir bajando."),
    ("gap%", "Diferencia porcentual entre apertura diaria y cierre anterior."),
    ("gap", "Diferencia entre el precio de apertura y el cierre anterior."),
    ("zscore", "Distancia estadistica frente a la media. En pares mide si el spread esta extremo."),
    ("corr", "Correlacion entre activos. Cerca de 1 significa que suelen moverse parecido."),
    ("hedge", "Relacion aproximada entre dos activos para construir una posicion de pares."),
    ("sector", "Sector al que pertenece la empresa segun la fuente de fundamentales."),
    ("per", "Precio dividido entre beneficio por accion. Mide valoracion relativa."),
    ("p/b", "Precio dividido entre valor contable. Mide cuanto paga el mercado por el patrimonio."),
    ("p/s", "Precio dividido entre ventas. Mide valoracion frente a ingresos."),
    ("roe", "Rentabilidad sobre fondos propios. Mide eficiencia del capital de la empresa."),
    ("roe%", "Rentabilidad sobre recursos propios expresada en porcentaje."),
    ("roic", "Rentabilidad sobre capital invertido. Mide calidad del negocio."),
    ("d/e", "Deuda sobre patrimonio. Mide apalancamiento financiero de la empresa."),
    ("div%", "Rentabilidad por dividendo aproximada."),
    ("yield", "Rentabilidad por dividendo aproximada."),
    ("payout", "Porcentaje del beneficio que se destina a dividendos."),
    ("deuda", "Nivel de endeudamiento de la empresa."),
    ("margen", "Porcentaje de ventas que queda como beneficio o resultado operativo."),
    ("crec", "Crecimiento historico de ingresos, beneficios o dividendos."),
    ("barrasd", "Numero de velas diarias cargadas para calcular indicadores."),
    ("fecha1m", "Fecha de la sesion intradia usada para calcular indicadores de 1 minuto."),
    ("barras1m", "Numero de velas intradia de 1 minuto cargadas."),
]
DEFAULT_REAL_STRATEGIES = [
    {
        "name": "Momentum",
        "description": "Compra activos con fuerza relativa alta, tendencia alcista y buen comportamiento frente al mercado.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / swing",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_momentum",
        "signals_txt_name": "Momentum.txt",
    },
    {
        "name": "Follow The Money",
        "description": "Busca donde entra el dinero detectando activos con volumen monetario diario muy superior a sus medias de 1, 2 y 3 meses.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / cada hora",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_follow_the_money",
        "signals_txt_name": "Follow_The_Money.txt",
    },
    {
        "name": "Entrada Dinero Direccional",
        "description": "Busca activos liquidos con entrada fuerte de dinero frente a 120 dias y direccion alcista: precio sobre SMA20, SMA20 sobre SMA50 y rentabilidad 5D positiva.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / rotacion",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_entrada_dinero_direccional",
        "signals_txt_name": "Entrada_Dinero_Direccional.txt",
    },
    {
        "name": "Acumula Metales",
        "description": "Compra metales y activos ligados a metales cuando estan castigados: bajo SMA180 diaria, bajo SMA120 semanal y con RSI14 menor que 30.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / acumulacion",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_acumula_metales",
        "signals_txt_name": "Acumula_Metales.txt",
    },
    {
        "name": "Acumulacion",
        "description": "Busca compras de acumulacion en todo el universo filtrado cuando el activo esta bajo SMA180 diaria, bajo SMA120 semanal y con RSI14 menor que 30.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / acumulacion",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_acumulacion",
        "signals_txt_name": "Acumulacion.txt",
    },
    {
        "name": "Reversion RSI 5",
        "description": "Opera extremos intradia: cortos con RSI14 mayor de 80 y precio mas de 5% sobre la media de 2 horas; largos al reves. Cierra por beneficio conjunto del 5%.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / cada 10 minutos",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_reversion_rsi_5",
        "signals_txt_name": "Reversion_RSI_5.txt",
    },
    {
        "name": "Swing Trading",
        "description": "Busca entradas de varios dias en activos con tendencia sana, retrocesos controlados y confirmacion tecnica.",
        "risk_level": "Medio",
        "signal_frequency": "Varias senales por semana",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_swing_trading",
        "signals_txt_name": "SwingTrading.txt",
    },
    {
        "name": "BreaKout",
        "description": "Detecta rupturas de resistencia con aumento de volumen, expansion de rango y precio cerca de maximos relevantes.",
        "risk_level": "Alto",
        "signal_frequency": "Segun rupturas de mercado",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_breakout",
        "signals_txt_name": "BreaKout.txt",
    },
    {
        "name": "Mean Reversion",
        "description": "Busca activos sobrevendidos o alejados de su media que puedan volver a niveles normales.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_mean_reversion",
        "signals_txt_name": "Mean_Reversion.txt",
    },
    {
        "name": "Value Trading",
        "description": "Filtra companias con valoracion atractiva, fundamentales razonables y descuento relativo.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_value_trading",
        "signals_txt_name": "ValueTrading.txt",
    },
    {
        "name": "Dividend Growth",
        "description": "Selecciona companias con crecimiento de dividendos, estabilidad financiera y perfil defensivo.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_dividend_growth",
        "signals_txt_name": "DividenGrowth.txt",
    },
    {
        "name": "Trend Following",
        "description": "Sigue tendencias establecidas mediante medias, momentum y confirmacion de precio.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / semanal",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_trend_following",
        "signals_txt_name": "TrendFollowing.txt",
    },
    {
        "name": "Pairs Trading",
        "description": "Analiza pares correlacionados y busca desviaciones estadisticas para operar convergencia.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_pairs_trading",
        "signals_txt_name": "PairsTrading.txt",
    },
    {
        "name": "Sector Rotation",
        "description": "Compara fuerza relativa por sectores y propone activos lideres dentro de los sectores fuertes.",
        "risk_level": "Medio",
        "signal_frequency": "Semanal",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_sector_rotation",
        "signals_txt_name": "SectorRotation.txt",
    },
    {
        "name": "Quality Investing",
        "description": "Busca empresas de calidad con buenos margenes, crecimiento y estabilidad financiera.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_quality_investing",
        "signals_txt_name": "QualityInvesting.txt",
    },
    {
        "name": "Opening Range BreaKout",
        "description": "Estrategia intradia que espera la ruptura del rango inicial de la sesion con volumen.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_opening_range_breakout",
        "signals_txt_name": "OpeningRangeBreaKout.txt",
    },
    {
        "name": "VWAP Reversion",
        "description": "Busca reversiones intradia hacia VWAP cuando el precio se aleja demasiado.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_vwap_reversion",
        "signals_txt_name": "VWAP_Reversion.txt",
    },
    {
        "name": "Momentum Intradia",
        "description": "Detecta movimientos fuertes dentro de la sesion usando momentum reciente, VWAP y volumen relativo.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_momentum_intradia",
        "signals_txt_name": "MomentumIntradia.txt",
    },
    {
        "name": "Scalping The PullBacks",
        "description": "Busca pequenos retrocesos dentro de una tendencia intradia para entrar a favor del movimiento principal.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / frecuente",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_scalping_pullbacks",
        "signals_txt_name": "ScalpingThePullBacKs.txt",
    },
    {
        "name": "Gap and Go",
        "description": "Detecta activos que abren con gap relevante y continuan en la direccion del impulso.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / apertura",
        "historical_return": "Pendiente de datos",
        "telegram_url": "https://t.me/tu_canal_gap_and_go",
        "signals_txt_name": "Gap_and_Go.txt",
    },
]


def database_status():
    url = engine.url
    return {
        "dialect": engine.dialect.name,
        "database": url.database or "",
        "host": url.host or "local file",
        "is_persistent": engine.dialect.name == "postgresql",
    }


def safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def run_scheduler_task(task_name):
    if task_name == "assets_csv":
        rows, source = build_assets_from_alpaca()
        write_assets(rows)
        save_universe_assets(rows)
        return {
            "ok": True,
            "message": f"CSV actualizado: {len(rows)} activos. Fuente: {source}.",
        }

    if task_name == "market_batch":
        result = update_market_data(full=False)
        return {
            "ok": bool(result.get("ok")),
            "message": f"Tanda mercado. Guardados: {result.get('saved_rows', 0)}. Error: {result.get('last_error', '') or 'Sin error'}.",
        }

    if task_name == "market_full":
        result = update_market_data(full=True)
        return {
            "ok": bool(result.get("ok")),
            "message": f"Mercado completo. Guardados: {result.get('saved_rows', 0)}. Error: {result.get('last_error', '') or 'Sin error'}.",
        }

    if task_name == "strategies":
        if not STRATEGIES_RUNNER.exists():
            return {"ok": False, "message": "No se encontro run_all_strategies.py."}
        active_strategy_names, total_active, next_batch_cursor = strategy_names_batch_for_runner()
        if not active_strategy_names:
            return {"ok": False, "message": "No hay estrategias activas para ejecutar."}
        mark_strategies_as_running_file(active_strategy_names)
        timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
        env = os.environ.copy()
        env["TRADING_ACTIVE_STRATEGIES"] = json.dumps(active_strategy_names)
        DEFAULT_STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        runner_log_path = DEFAULT_STRATEGY_LOG_DIR / "run_all_strategies.log"
        try:
            with runner_log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                completed = subprocess.run(
                    [sys.executable, str(STRATEGIES_RUNNER)],
                    cwd=str(STRATEGIES_RUNNER.parent),
                    text=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            mark_running_strategies_error(
                f"Estrategias canceladas por superar {timeout_seconds} segundos."
            )
            return {
                "ok": False,
                "message": f"Estrategias canceladas por superar {timeout_seconds} segundos.",
            }
        persisted_results = persist_strategy_status_file_results()
        advance_strategy_batch_cursor(next_batch_cursor)
        summary = strategy_runner_summary(completed.returncode)
        summary = f"{summary} Lote ejecutado: {len(active_strategy_names)}/{total_active} activas."
        if completed.returncode != 0 and not persisted_results:
            log_tail = read_text_tail(runner_log_path)
            mark_running_strategies_error(f"{summary}\n{log_tail}".strip())
        if completed.returncode == 0:
            return {
                "ok": True,
                "message": summary,
            }
        return {
            "ok": False,
            "message": summary,
        }

    return {"ok": False, "message": "Tarea no reconocida."}


def strategy_runner_summary(returncode):
    try:
        data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if returncode == 0:
            return "Estrategias finalizadas correctamente."
        return f"Estrategias con error. Codigo {returncode}. Revisa Fallos de estrategias."

    results = data.get("strategies", {})
    ok_count = sum(1 for item in results.values() if item.get("ok"))
    fail_count = sum(1 for item in results.values() if not item.get("ok"))
    if fail_count:
        return (
            f"Estrategias finalizadas con {fail_count} fallos. "
            f"Correctas: {ok_count}. Revisa Fallos de estrategias."
        )
    return f"Estrategias finalizadas correctamente. Correctas: {ok_count}."


def parse_status_datetime_value(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def run_single_strategy(strategy):
    py_file = (strategy.get("python_file") or "").strip()
    if not py_file:
        return {"ok": False, "message": "La estrategia no tiene archivo Python asociado."}
    if not valid_python_filename(py_file):
        return {"ok": False, "message": "Archivo Python no valido."}

    strategies_dir = (BASE_DIR / "Estrategias").resolve()
    path = (strategies_dir / py_file).resolve()
    if strategies_dir not in path.parents:
        return {"ok": False, "message": "Ruta de estrategia no permitida."}
    if not path.exists() or not path.is_file():
        return {"ok": False, "message": f"No existe {py_file}."}

    txt_path = single_strategy_txt_path(strategy)
    previous_mtime = txt_path.stat().st_mtime if txt_path and txt_path.exists() else None
    mark_single_strategy_status(strategy, running=True)
    timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
    DEFAULT_STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEFAULT_STRATEGY_LOG_DIR / f"{safe_log_filename(strategy['name'])}.log"
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            completed = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(path.parent),
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                timeout=timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        result = {
            "ok": False,
            "message": f"{strategy['name']} cancelada por superar {timeout_seconds} segundos.",
            "returncode": None,
        }
        mark_single_strategy_status(strategy, running=False, result=result)
        return result

    output = read_text_tail(log_path)
    txt_updated = output_txt_updated(txt_path, previous_mtime)
    result = {
        "ok": completed.returncode == 0,
        "message": (
            f"{strategy['name']} ejecutada correctamente. TXT {'actualizado' if txt_updated else 'sin cambios'}."
            if completed.returncode == 0
            else f"{strategy['name']} fallo. Codigo {completed.returncode}. {output[-700:]}"
        ),
        "returncode": completed.returncode,
        "txt_updated": txt_updated,
        "stdout": output,
        "stderr": "",
    }
    if result["ok"] and txt_path:
        sync_signal_file_to_database(strategy.get("signals_txt_name", ""), txt_path)
    mark_single_strategy_status(strategy, running=False, result=result)
    return result


def single_strategy_txt_path(strategy):
    txt_name = (strategy.get("signals_txt_name") or "").strip()
    if not txt_name or not valid_txt_name(txt_name):
        return None
    return (BASE_DIR / "Estrategias" / "salidas_txt" / txt_name).resolve()


def output_txt_updated(path, previous_mtime):
    if path is None or not path.exists() or not path.is_file():
        return False
    current_mtime = path.stat().st_mtime
    if previous_mtime is None:
        return True
    return current_mtime > previous_mtime


def safe_log_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._-")
    return cleaned or "strategy"


def read_text_tail(path, max_chars=1200):
    try:
        text_value = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text_value.strip()[-max_chars:]


def signal_date_from_line(line):
    expected = "fecha:"
    for part in str(line).split("|"):
        part = part.strip()
        if part.lower().startswith(expected):
            value = part.split(":", 1)[1].strip()
            return value[:10]
    return ""


def sync_signal_file_to_database(txt_name, path=None):
    if not txt_name or not valid_txt_name_global(txt_name):
        return 0

    path = path or (DEFAULT_SIGNALS_DIR / txt_name)
    try:
        lines = list(dict.fromkeys(
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ))
    except OSError:
        return 0

    saved = 0
    lines_by_date = {}
    for line in lines:
        signal_date = signal_date_from_line(line)
        if signal_date:
            lines_by_date.setdefault(signal_date, set()).add(line)

    with engine.begin() as connection:
        existing_lines_by_date = {}
        for signal_date, current_lines in lines_by_date.items():
            existing_rows = connection.execute(
                text(
                    """
                    SELECT id, line
                    FROM strategy_signals
                    WHERE txt_name = :txt_name
                      AND signal_date = :signal_date
                    """
                ),
                {"txt_name": txt_name, "signal_date": signal_date},
            ).mappings().fetchall()
            existing_lines = {row["line"] for row in existing_rows}
            existing_lines_by_date[signal_date] = existing_lines
            for row in existing_rows:
                if row["line"] not in current_lines:
                    connection.execute(
                        text("DELETE FROM strategy_signals WHERE id = :id"),
                        {"id": row["id"]},
                    )

        for line in lines:
            signal_date = signal_date_from_line(line)
            if not signal_date:
                continue
            if line in existing_lines_by_date.get(signal_date, set()):
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO strategy_signals (txt_name, signal_date, line)
                    VALUES (:txt_name, :signal_date, :line)
                    """
                ),
                {"txt_name": txt_name, "signal_date": signal_date, "line": line},
            )
            saved += 1
    return saved


def valid_txt_name_global(txt_name):
    path = Path(txt_name)
    return (
        path.name == txt_name
        and txt_name.lower().endswith(".txt")
        and "/" not in txt_name
        and "\\" not in txt_name
    )


def mark_single_strategy_status(strategy, running=False, result=None):
    now = datetime.now(UTC).isoformat()
    persist_single_strategy_status(strategy, running=running, result=result, now=datetime.now(MADRID_TZ))
    data = {}
    try:
        if DEFAULT_STRATEGY_STATUS_FILE.exists():
            data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}

    data.setdefault("strategies", {})
    item = {
        "file": strategy.get("python_file", ""),
        "txt": strategy.get("signals_txt_name", ""),
        "ok": False,
        "txt_updated": False,
        "returncode": None,
        "error": "",
        "ran_at": now,
    }
    if running:
        item["running"] = True
        data["started_at"] = now
        data["finished_at"] = ""
    else:
        result = result or {"ok": False, "message": "Sin resultado.", "returncode": None}
        item["running"] = False
        item["ok"] = bool(result.get("ok"))
        item["txt_updated"] = bool(result.get("txt_updated"))
        item["returncode"] = result.get("returncode")
        item["error"] = "" if result.get("ok") else result.get("message", "")
        item["stdout"] = result.get("stdout", "")
        item["stderr"] = result.get("stderr", "")
        data["finished_at"] = now

    data["strategies"][strategy["name"]] = item
    DEFAULT_STRATEGY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STRATEGY_STATUS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def persist_single_strategy_status(strategy, running=False, result=None, now=None):
    strategy_id = strategy.get("id")
    if not strategy_id:
        return

    now = now or datetime.now(MADRID_TZ)
    if running:
        values = {
            "id": strategy_id,
            "run_status": "RUNNING",
            "run_message": "En ejecucion",
            "run_at": now.astimezone(UTC).replace(tzinfo=None),
            "run_txt_updated": 0,
            "run_returncode": None,
        }
    else:
        result = result or {"ok": False, "message": "Sin resultado.", "returncode": None}
        values = {
            "id": strategy_id,
            "run_status": "OK" if result.get("ok") else "ERROR",
            "run_message": result.get("message", "")[:1000],
            "run_at": now.astimezone(UTC).replace(tzinfo=None),
            "run_txt_updated": 1 if result.get("txt_updated") else 0,
            "run_returncode": result.get("returncode"),
        }

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET run_status = :run_status,
                    run_message = :run_message,
                    run_at = :run_at,
                    run_txt_updated = :run_txt_updated,
                    run_returncode = :run_returncode
                WHERE id = :id
                """
            ),
            values,
        )


def active_strategy_names_for_runner():
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE run_locally = 1 ORDER BY name")
        ).mappings().fetchall()
    return [row["name"] for row in rows]


def strategy_names_batch_for_runner():
    batch_size = strategy_runner_batch_size()
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM strategies WHERE run_locally = 1 ORDER BY name")
        ).mappings().fetchall()
        names = [row["name"] for row in rows]
        total = len(names)
        if total == 0:
            return [], 0

        cursor_row = connection.execute(
            text(
                """
                SELECT batch_cursor
                FROM automation_schedules
                WHERE task_name = 'strategies'
                """
            )
        ).mappings().fetchone()
        cursor = int(cursor_row["batch_cursor"] or 0) if cursor_row else 0
        start = cursor % total
        selected = [
            names[(start + index) % total]
            for index in range(min(batch_size, total))
        ]
        next_cursor = (start + len(selected)) % total
    return selected, total, next_cursor


def advance_strategy_batch_cursor(next_cursor):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET batch_cursor = :batch_cursor
                WHERE task_name = 'strategies'
                """
            ),
            {"batch_cursor": next_cursor},
        )


def strategy_runner_batch_size():
    try:
        value = int(os.environ.get("TRADING_STRATEGY_BATCH_SIZE", "3"))
    except ValueError:
        value = 3
    return max(1, min(value, 15))


def mark_strategies_as_running_file(strategy_names=None):
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    names = list(strategy_names or [])
    with engine.begin() as connection:
        if not names:
            rows = connection.execute(
                text("SELECT name FROM strategies WHERE run_locally = 1 ORDER BY name")
            ).mappings().fetchall()
            names = [row["name"] for row in rows]
        for name in names:
            connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = 'RUNNING',
                        run_message = 'En ejecucion',
                        run_at = :run_at,
                        run_txt_updated = 0,
                        run_returncode = NULL
                    WHERE run_locally = 1
                      AND name = :name
                    """
                ),
                {
                    "run_at": datetime.now(UTC).replace(tzinfo=None),
                    "name": name,
                },
            )

    payload = {
        "started_at": now,
        "finished_at": "",
        "running": True,
        "strategies": {
            name: {
                "file": "",
                "ok": False,
                "running": True,
                "returncode": None,
                "error": "",
                "ran_at": now,
            }
            for name in names
        },
    }
    DEFAULT_STRATEGY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_STRATEGY_STATUS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def persist_strategy_status_file_results():
    try:
        data = json.loads(DEFAULT_STRATEGY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    results = data.get("strategies", {})
    if not results:
        return 0

    saved = 0
    with engine.begin() as connection:
        for name, item in results.items():
            txt_name = item.get("txt", "")
            if txt_name:
                sync_signal_file_to_database(txt_name)
            status = "OK" if item.get("ok") else "ERROR"
            message = "" if item.get("ok") else (item.get("error", "") or "La estrategia termino con error.")
            connection.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = :run_status,
                        run_message = :run_message,
                        run_at = :run_at,
                        run_txt_updated = :run_txt_updated,
                        run_returncode = :run_returncode
                    WHERE name = :name
                    """
                ),
                {
                    "name": name,
                    "run_status": status,
                    "run_message": message[:1000],
                    "run_at": parse_status_datetime_value(item.get("ran_at", "")) or datetime.now(UTC),
                    "run_txt_updated": 1 if item.get("txt_updated") else 0,
                    "run_returncode": item.get("returncode"),
                },
            )
            saved += 1
    return saved


def mark_running_strategies_error(message):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET run_status = 'ERROR',
                    run_message = :message,
                    run_returncode = 1
                WHERE run_status = 'RUNNING'
                """
            ),
            {"message": message[:1000]},
        )


def start_scheduler_thread():
    global SCHEDULER_THREAD_STARTED
    if os.environ.get("DISABLE_INTERNAL_SCHEDULER") == "1":
        return
    with SCHEDULER_LOCK:
        if SCHEDULER_THREAD_STARTED:
            return
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
        SCHEDULER_THREAD_STARTED = True


def scheduler_loop():
    while True:
        try:
            process_due_schedules()
        except Exception as error:
            print(f"[scheduler] Error: {error}", flush=True)
        time.sleep(60)


def process_due_schedules(background=True):
    now = datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        schedules = connection.execute(
            text(
                """
                SELECT *
                FROM automation_schedules
                WHERE is_enabled = 1
                ORDER BY task_name
                """
            )
        ).mappings().fetchall()

    for schedule in schedules:
        due_key = due_schedule_key(schedule, now)
        if not due_key or due_key == schedule["last_run_key"]:
            continue
        record_schedule_running(schedule["task_name"], due_key, now)
        if background:
            launch_scheduler_task_in_background(schedule["task_name"], due_key)
        else:
            try:
                result = run_scheduler_task(schedule["task_name"])
            except Exception as error:
                result = {"ok": False, "message": f"Error ejecutando tarea: {error}"}
            record_schedule_result(schedule["task_name"], due_key, result, now)

    process_due_strategy_schedules(now, background=background)


def process_due_strategy_schedules(now, background=True):
    with engine.begin() as connection:
        strategies = connection.execute(
            text(
                """
                SELECT *
                FROM strategies
                WHERE is_active = 1
                  AND auto_execute = 1
                ORDER BY name
                """
            )
        ).mappings().fetchall()

    for strategy in strategies:
        due_key = due_strategy_schedule_key(strategy, now)
        if not due_key or due_key == strategy["schedule_last_run_key"]:
            continue
        record_strategy_schedule_running(strategy["id"], due_key, now)
        if background:
            launch_strategy_task_in_background(dict(strategy), due_key)
        else:
            try:
                result = run_single_strategy(strategy)
            except Exception as error:
                result = {"ok": False, "message": f"Error ejecutando estrategia: {error}"}
            record_strategy_schedule_result(strategy["id"], due_key, result, now)


def record_schedule_result(task_name, run_key, result, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_run_key = :last_run_key,
                    last_run_at = :last_run_at,
                    last_status = :last_status,
                    last_message = :last_message
                WHERE task_name = :task_name
                """
            ),
            {
                "last_run_key": run_key,
                "last_run_at": now.astimezone(UTC).replace(tzinfo=None),
                "last_status": "OK" if result["ok"] else "ERROR",
                "last_message": result["message"][:1000],
                "task_name": task_name,
            },
        )


def record_schedule_running(task_name, run_key, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_run_key = :last_run_key,
                    last_run_at = :last_run_at,
                    last_status = 'RUNNING',
                    last_message = 'En ejecucion'
                WHERE task_name = :task_name
                """
            ),
            {
                "last_run_key": run_key,
                "last_run_at": now.astimezone(UTC).replace(tzinfo=None),
                "task_name": task_name,
            },
        )


def launch_scheduler_task_in_background(task_name, run_key):
    def worker():
        try:
            result = run_scheduler_task(task_name)
        except Exception as error:
            result = {"ok": False, "message": f"Error ejecutando tarea: {error}"}
        record_schedule_result(task_name, run_key, result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def launch_strategy_task_in_background(strategy, run_key):
    def worker():
        try:
            result = run_single_strategy(strategy)
        except Exception as error:
            result = {"ok": False, "message": f"Error ejecutando estrategia: {error}"}
        record_strategy_schedule_result(strategy["id"], run_key, result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def record_strategy_schedule_running(strategy_id, run_key, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_run_key = :run_key,
                    schedule_last_run_at = :last_run_at,
                    schedule_last_status = 'RUNNING',
                    schedule_last_message = 'En ejecucion'
                WHERE id = :id
                """
            ),
            {
                "run_key": run_key,
                "last_run_at": now.astimezone(UTC).replace(tzinfo=None),
                "id": strategy_id,
            },
        )


def record_strategy_schedule_result(strategy_id, run_key, result, now=None):
    now = now or datetime.now(MADRID_TZ)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_run_key = :run_key,
                    schedule_last_run_at = :last_run_at,
                    schedule_last_status = :status,
                    schedule_last_message = :message
                WHERE id = :id
                """
            ),
            {
                "run_key": run_key,
                "last_run_at": now.astimezone(UTC).replace(tzinfo=None),
                "status": "OK" if result.get("ok") else "ERROR",
                "message": result.get("message", "")[:1000],
                "id": strategy_id,
            },
        )


def due_schedule_key(schedule, now):
    try:
        hour, minute = [int(part) for part in str(schedule["start_time"]).split(":", 1)]
        runs_per_day = max(1, int(schedule["runs_per_day"]))
        interval_minutes = max(1, int(schedule["interval_minutes"]))
    except (TypeError, ValueError):
        return ""

    allowed_days = parse_weekdays(schedule.get("weekdays", DEFAULT_WEEKDAYS))
    if now.isoweekday() not in allowed_days:
        return ""

    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    grace_minutes = max(5, min(interval_minutes, 60))
    latest_due_key = ""
    for index in range(runs_per_day):
        planned = start + timedelta(minutes=interval_minutes * index)
        if planned.date() == now.date() and planned <= now < planned + timedelta(minutes=grace_minutes):
            return f"{now.date().isoformat()}|{schedule['task_name']}|{index}"
        if planned.date() == now.date() and planned <= now:
            latest_due_key = f"{now.date().isoformat()}|{schedule['task_name']}|{index}"
    if latest_due_key and schedule["last_run_key"] != latest_due_key:
        return latest_due_key
    return ""


def due_strategy_schedule_key(strategy, now):
    if now.isoweekday() not in parse_weekdays(DEFAULT_WEEKDAYS):
        return ""
    try:
        start_hour, start_minute = [int(part) for part in str(strategy["schedule_start_time"]).split(":", 1)]
        end_hour, end_minute = [int(part) for part in str(strategy["schedule_end_time"]).split(":", 1)]
        interval_minutes = max(1, int(strategy["schedule_interval_minutes"]))
    except (TypeError, ValueError):
        return ""

    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end < start:
        end = end + timedelta(days=1)
    if not start <= now <= end:
        return ""

    elapsed_minutes = int((now - start).total_seconds() // 60)
    slot = elapsed_minutes // interval_minutes
    planned = start + timedelta(minutes=slot * interval_minutes)
    grace_minutes = max(5, min(interval_minutes, 60))
    if planned <= now < planned + timedelta(minutes=grace_minutes):
        return f"{now.date().isoformat()}|strategy|{strategy['id']}|{slot}"
    return ""


def parse_weekdays(value):
    days = set()
    for part in str(value or "").split(","):
        try:
            day = int(part)
        except ValueError:
            continue
        if 1 <= day <= 7:
            days.add(day)
    return days or {1, 2, 3, 4, 5}


def valid_python_filename(filename):
    path = Path(filename)
    return (
        path.name == filename
        and filename.lower().endswith(".py")
        and "/" not in filename
        and "\\" not in filename
    )


def valid_txt_name(txt_name):
    path = Path(txt_name)
    return (
        path.name == txt_name
        and txt_name.lower().endswith(".txt")
        and "/" not in txt_name
        and "\\" not in txt_name
    )


def technical_term_help(term):
    raw = str(term).strip().lower()
    normalized = normalize_help_key(raw)
    compact = normalized.replace(" ", "")
    for key, help_text in TECHNICAL_TERM_HELP:
        key_normalized = normalize_help_key(key)
        key_compact = key_normalized.replace(" ", "")
        if normalized == key_normalized or compact == key_compact:
            return help_text
    for key, help_text in TECHNICAL_TERM_HELP:
        key_normalized = normalize_help_key(key)
        key_compact = key_normalized.replace(" ", "")
        if (
            normalized.startswith(key_normalized)
            or compact.startswith(key_compact)
        ):
            return help_text
    return inferred_technical_term_help(raw, normalized)


def normalize_help_key(value):
    text_value = str(value).strip().lower()
    text_value = text_value.replace("_", " ")
    text_value = text_value.replace("-", " ")
    text_value = re.sub(r"\s+", " ", text_value)
    return text_value


def inferred_technical_term_help(raw, normalized):
    compact = normalized.replace(" ", "")
    if raw in {"symbol", "ticker"} or normalized in {"symbol", "simbolo"}:
        return "Simbolo de cotizacion del activo."
    if raw in {"name", "company", "company_name"} or normalized in {"nombre", "empresa"}:
        return "Nombre de la empresa o activo analizado."
    if raw in {"market", "exchange"} or normalized in {"mercado", "bolsa"}:
        return "Mercado o bolsa donde cotiza el activo."
    if raw in {"sector", "fmp_sector"} or normalized == "sector":
        return "Sector economico al que pertenece la empresa."
    if raw in {"price", "current_price"} or normalized in {"precio", "precio actual"}:
        return "Precio usado como referencia en el calculo del aviso."
    if raw in {"open", "daily_open", "intraday_1m_open"} or normalized == "apertura":
        return "Precio de apertura del periodo analizado."
    if raw in {"high", "daily_high"}:
        return "Precio maximo alcanzado en el periodo analizado."
    if raw in {"low", "daily_low"}:
        return "Precio minimo alcanzado en el periodo analizado."
    if raw in {"close", "daily_close"}:
        return "Precio de cierre del periodo analizado."
    if raw in {"previous_close", "prev_close", "intraday_1m_previous_close"}:
        return "Cierre anterior usado para comparar gaps, retornos o cambios de sesion."
    if raw in {"previous_low", "prev_low"}:
        return "Minimo anterior usado como referencia tecnica de soporte o riesgo."
    if raw == "volume" or normalized == "volumen":
        return "Numero de acciones o contratos negociados en el periodo analizado."
    if raw.startswith("avg_volume"):
        return "Volumen medio del periodo indicado. Sirve para comparar si el volumen actual es alto o bajo."
    if raw.endswith("_rank") or normalized.endswith(" rank"):
        return "Posicion del activo dentro del universo ordenado por ese parametro. Rank 1 es el mejor valor."
    if raw.endswith("_percentile") or normalized.endswith(" percentile"):
        return "Percentil del activo dentro del universo para ese parametro. Valores altos indican que esta por encima de la mayoria."
    if "dollar_volume_ratio_vs_prev" in raw:
        return "Ratio entre el volumen monetario actual y el volumen monetario de un periodo anterior equivalente."
    if "dollar_volume_ratio_vs" in raw:
        return "Ratio entre el volumen monetario actual y su media del periodo indicado."
    if "avg_dollar_volume" in raw:
        return "Volumen monetario medio del periodo indicado: precio multiplicado por volumen."
    if "current_dollar_volume" in raw or "intraday_day_dollar_volume" in raw:
        return "Volumen monetario actual: precio multiplicado por volumen negociado."
    if "dollar_volume" in raw:
        return "Volumen monetario: precio multiplicado por volumen negociado."
    if "volume_ratio" in raw:
        return "Volumen actual comparado con el volumen medio del periodo indicado."
    if raw.startswith("daily_sma") or raw.startswith("weekly_sma") or raw.startswith("intraday_1m_sma") or compact.startswith("sma"):
        return "Media movil simple del periodo indicado. Sirve para medir tendencia y distancia del precio a su media."
    if "distance_daily_sma" in raw or "distance_weekly_sma" in raw or "distance_sma" in raw or "distance" in raw and "sma" in raw:
        return "Distancia porcentual entre el precio actual y esa media movil."
    if raw.startswith("daily_rsi") or raw.startswith("weekly_rsi") or raw.startswith("intraday_1m_rsi") or compact.startswith("rsi"):
        return "RSI del periodo indicado. Mide sobrecompra/sobreventa: bajo suele indicar debilidad extrema; alto, fuerza o sobrecompra."
    if raw.startswith("daily_atr"):
        return "ATR del periodo indicado. Mide volatilidad media y ayuda a calcular stops."
    if raw.startswith("daily_macd"):
        return "MACD diario. Mide impulso y cruce entre medias exponenciales."
    if "bollinger" in raw:
        return "Banda de Bollinger. Compara el precio con una banda estadistica alrededor de la media."
    if raw.startswith("momentum_"):
        return "Rentabilidad del activo durante el periodo indicado."
    if raw.startswith("benchmark_return"):
        return "Rentabilidad del benchmark durante el periodo indicado."
    if raw.startswith("relative_strength"):
        return "Fuerza relativa frente al benchmark. Positivo significa que el activo lo hizo mejor que la referencia."
    if raw.startswith("resistance"):
        return "Resistencia previa del periodo indicado: zona donde el precio tuvo dificultad para superar maximos."
    if raw.startswith("support"):
        return "Soporte previo del periodo indicado: zona donde el precio tuvo dificultad para seguir cayendo."
    if raw.startswith("breakout") or "breakout_high" in raw:
        return "Distancia o ruptura por encima de una resistencia o maximo reciente."
    if "breakdown" in raw:
        return "Distancia o ruptura por debajo de un soporte o minimo reciente."
    if raw.startswith("daily_gap"):
        return "Diferencia porcentual entre la apertura diaria y el cierre anterior."
    if raw.startswith("daily_return"):
        return "Rentabilidad diaria acumulada en el periodo indicado."
    if raw.startswith("return") or normalized.startswith("rentabilidad"):
        return "Rentabilidad calculada para el periodo indicado."
    if raw.startswith("daily_change_from_open"):
        return "Movimiento porcentual desde la apertura diaria hasta el precio actual."
    if "change" in raw:
        return "Cambio porcentual o absoluto frente a una referencia previa."
    if raw.startswith("recent_high") or raw.startswith("high_"):
        return "Maximo reciente usado como referencia tecnica."
    if raw.startswith("recent_low") or raw.startswith("low") or raw.startswith("previous_low"):
        return "Minimo reciente usado como referencia tecnica."
    if raw.startswith("intraday_1m_vwap"):
        return "VWAP intradia: precio medio ponderado por volumen de la sesion cargada."
    if raw.startswith("intraday_1m_ema"):
        return "Media movil exponencial intradia. Da mas peso a las velas recientes."
    if raw.startswith("intraday_1m_momentum"):
        return "Momentum intradia del periodo indicado, calculado con velas de 1 minuto."
    if raw.startswith("intraday_1m_recent_high"):
        return "Maximo reciente calculado con velas intradia de 1 minuto."
    if raw.startswith("intraday_1m_recent_low"):
        return "Minimo reciente calculado con velas intradia de 1 minuto."
    if raw.startswith("opening_range_15m"):
        return "Dato del rango inicial de 15 minutos: zona usada para rupturas de apertura."
    if raw.startswith("fmp_pe"):
        return "PER: precio dividido entre beneficio por accion. Ayuda a valorar si una empresa parece cara o barata."
    if raw.startswith("fmp_pb"):
        return "Precio sobre valor contable. Compara precio de mercado con patrimonio contable."
    if raw.startswith("fmp_ps"):
        return "Precio sobre ventas. Compara valoracion de mercado con ingresos."
    if raw.startswith("fmp_roe"):
        return "ROE: rentabilidad sobre fondos propios. Mide eficiencia del capital de la empresa."
    if raw.startswith("fmp_roic"):
        return "ROIC: rentabilidad sobre capital invertido. Mide calidad y eficiencia del negocio."
    if raw.startswith("fmp_operating_margin"):
        return "Margen operativo. Porcentaje de ingresos que queda como resultado operativo."
    if raw.startswith("fmp_net_margin"):
        return "Margen neto. Porcentaje de ingresos que queda como beneficio neto."
    if raw.startswith("fmp_debt_to_equity"):
        return "Deuda sobre patrimonio. Mide apalancamiento financiero."
    if raw.startswith("fmp_revenue_growth"):
        return "Crecimiento de ingresos. Mide expansion o contraccion del negocio."
    if raw.startswith("fmp_eps_growth"):
        return "Crecimiento del beneficio por accion."
    if raw.startswith("fmp_dividend_yield"):
        return "Rentabilidad por dividendo estimada."
    if raw.startswith("fmp_payout"):
        return "Porcentaje del beneficio destinado a dividendos."
    if raw.startswith("fmp_dividend_growth"):
        return "Crecimiento historico del dividendo."
    if raw.startswith("fmp_"):
        return "Dato fundamental de la empresa obtenido de la fuente de fundamentales."
    if raw.endswith("_bars_loaded") or "bars_loaded" in raw:
        return "Numero de velas cargadas para calcular indicadores."
    if raw.endswith("_data_date") or raw.endswith("_last_timestamp"):
        return "Fecha u hora del ultimo dato usado para calcular el indicador."
    if "score" in raw:
        return "Puntuacion interna usada para ordenar o priorizar candidatos."
    if "target" in raw or "objetivo" in normalized:
        return "Precio objetivo o zona teorica de salida con beneficio."
    if "stop" in raw:
        return "Nivel defensivo usado para controlar el riesgo de la operacion."
    if "signal" in raw or "aviso" in normalized:
        return "Dato relacionado con la senal generada por la estrategia."
    if "date" in raw or "fecha" in normalized or "time" in raw or "hora" in normalized:
        return "Fecha u hora asociada al dato mostrado."
    return f"Parametro calculado por el motor de analisis: {str(raw or normalized)}."


def format_money_usd(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0

    absolute = abs(amount)
    if absolute >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f} T USD"
    if absolute >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f} B USD"
    if absolute >= 1_000_000:
        return f"{amount / 1_000_000:.1f} M USD"
    if absolute >= 1_000:
        return f"{amount / 1_000:.1f} K USD"
    return f"{amount:.0f} USD"


def format_signed_money_usd(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{format_money_usd(abs(amount))}"


def format_one_decimal(value):
    text_value = str(value or "").strip()
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", text_value)
    if not match:
        return text_value
    try:
        return f"{float(match.group(0).replace(',', '.')):.1f}"
    except ValueError:
        return text_value


def format_compact_count(value):
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= 1000:
        return "+1K"
    return str(count)


def profit_color_class(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return "text-success" if amount >= 0 else "text-danger"


def parse_return_percent(value):
    match = re.search(r"([-+]?\d+(?:[.,]\d+)?)\s*%", str(value or ""))
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return 0.0


def parse_profit_usd(value):
    text_value = str(value or "")
    match = re.search(r"([-+]?\d+(?:[.,]\d+)?)\s*USD", text_value, re.IGNORECASE)
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return 0.0


def has_profit_usd(value):
    return bool(re.search(r"[-+]?\d+(?:[.,]\d+)?\s*USD", str(value or ""), re.IGNORECASE))


def parse_strategy_capital_usd(value):
    text_value = str(value or "")
    match = re.search(
        r"capital(?:\s+(?:base|cuenta|movido|invertido|inicial|actual))?\s+([-+]?\d+(?:[.,]\d+)?)\s*USD",
        text_value,
        re.IGNORECASE,
    )
    if not match:
        return 50_000.0
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return 50_000.0


def parse_current_capital_usd(value):
    text_value = str(value or "")
    match = re.search(r"capital\s+actual\s+([-+]?\d+(?:[.,]\d+)?)\s*USD", text_value, re.IGNORECASE)
    if not match:
        capital = parse_strategy_capital_usd(value)
        return capital + parse_profit_usd(value)
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        capital = parse_strategy_capital_usd(value)
        return capital + parse_profit_usd(value)


def parse_operations_summary(value):
    text_value = str(value or "")
    match = re.search(r"(\d+)\s+ops,\s+(\d+)\s+abiertas,\s+(\d+)\s+cerradas", text_value, re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1)} ops · {match.group(2)} abiertas · {match.group(3)} cerradas"


def parse_operations_parts(value):
    text_value = str(value or "")
    match = re.search(r"(\d+)\s+ops,\s+(\d+)\s+abiertas,\s+(\d+)\s+cerradas", text_value, re.IGNORECASE)
    if not match:
        return {
            "total": "",
            "open": "",
            "closed": "",
            "max_open": "",
        }
    return {
        "total": f"{match.group(1)} ops",
        "open": f"{match.group(2)} abiertas",
        "closed": f"{match.group(3)} cerradas",
        "max_open": "",
    }


def parse_max_open_operations(value):
    match = re.search(r"max\s+abiertas\s+(\d+)", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def format_duration_seconds(seconds):
    try:
        seconds = int(seconds)
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


def build_return_metrics(value):
    text_value = str(value or "").strip()
    normalized_text = text_value.lower()
    if (
        not text_value
        or "sin operaciones" in normalized_text
        or "pendiente de backtest" in normalized_text
        or "pendiente de datos" in normalized_text
    ):
        return {
            "has_data": False,
            "result": "SOS",
            "percent": "Sin datos",
            "capital": "Sin datos",
            "current_capital": "",
            "operations": "Sin operaciones",
            "operations_parts": {
                "total": "Sin operaciones",
                "open": "",
                "closed": "",
                "max_open": "",
            },
            "period_lines": [],
            "result_class": "return-sos",
            "percent_class": "return-sos",
            "raw": clean_public_return_text(text_value),
        }
    profit_usd = parse_profit_usd(text_value)
    return_pct = parse_return_percent(text_value)
    account_capital = parse_strategy_capital_usd(text_value)
    current_capital = parse_current_capital_usd(text_value)
    return {
        "has_data": True,
        "result": format_signed_money_usd(profit_usd),
        "percent": f"{return_pct:+.2f}%",
        "capital": format_money_usd(account_capital),
        "current_capital": format_money_usd(current_capital),
        "operations": parse_operations_summary(text_value) or "Operaciones registradas",
        "operations_parts": parse_operations_parts(text_value),
        "period_lines": parse_period_return_lines(text_value),
        "result_class": profit_color_class(profit_usd),
        "percent_class": profit_color_class(return_pct),
        "raw": clean_public_return_text(text_value),
    }


def parse_period_return_lines(value):
    parts = [part.strip() for part in str(value or "").split("|")]
    lines = []
    for part in parts:
        if not re.match(r"^(Last 1M|Last 3M|Last 12M|YTD|Prev Year|\d{4})", part, re.IGNORECASE):
            continue
        label_match = re.match(r"^(Last 1M|Last 3M|Last 12M|YTD|Prev Year(?:\s+\d{4})?|\d{4})\s+", part, re.IGNORECASE)
        raw_label = label_match.group(1) if label_match else "Period"
        label = display_period_return_label(raw_label)
        text_after_label = part[label_match.end():] if label_match else part
        profit = parse_profit_usd(text_after_label)
        percent = parse_return_percent(text_after_label)
        closed_match = re.search(r"(\d+)\s+cerradas", part, re.IGNORECASE)
        closed_text = f"{closed_match.group(1)} cerradas" if closed_match else ""
        lines.append(
            {
                "label": label,
                "profit": format_signed_money_usd(profit),
                "percent": f"{percent:+.2f}%",
                "class": profit_color_class(profit),
                "closed": closed_text,
            }
        )
    return lines


def display_period_return_label(label):
    text_value = str(label or "").strip()
    if re.fullmatch(r"\d{4}", text_value):
        return text_value
    if re.fullmatch(r"YTD", text_value, re.IGNORECASE):
        return str(datetime.now(MADRID_TZ).year)
    prev_year_match = re.fullmatch(r"Prev Year(?:\s+(\d{4}))?", text_value, re.IGNORECASE)
    if prev_year_match:
        return prev_year_match.group(1) or str(datetime.now(MADRID_TZ).year - 1)
    return text_value


STRATEGY_SHORT_NAMES = {
    "momentum": "MOM",
    "swingtrading": "SWING",
    "breakout": "BRK",
    "meanreversion": "REV",
    "valuetrading": "VALUE",
    "dividendgrowth": "DIV",
    "trendfollowing": "TREND",
    "pairstrading": "PAIRS",
    "sectorrotation": "SECTOR",
    "qualityinvesting": "QUALITY",
    "openingrangebreakout": "ORB",
    "vwapreversion": "VWAP",
    "momentumintradia": "MOM-I",
    "scalpingthepullbacks": "SCALP",
    "gapandgo": "GAP",
}


def strategy_short_name(name):
    key = strategy_info_key(name)
    if key in STRATEGY_SHORT_NAMES:
        return STRATEGY_SHORT_NAMES[key]
    words = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if not words:
        return "EST"
    if len(words) == 1:
        return words[0][:8].upper()
    return "".join(word[0] for word in words[:4]).upper()


def strategy_return_badge(value):
    text_value = str(value or "").strip()
    if not text_value:
        return "SOS"
    ops_match = re.search(r"\((\d+)\s+ops", text_value, re.IGNORECASE)
    if "sin operaciones" in text_value.lower():
        return "SOS"
    if ops_match and int(ops_match.group(1)) <= 0:
        return "SOS"
    if has_profit_usd(text_value):
        profit_usd = parse_profit_usd(text_value)
        return f"{profit_usd:+.0f} USD"
    percent_match = re.search(r"[-+]?\d+(?:[.,]\d+)?\s*%", text_value)
    if percent_match:
        return percent_match.group(0).replace(",", ".").replace(" ", "")
    return "SOS"


def strategy_return_badge_class(value):
    text_value = str(value or "").strip()
    if not text_value or "sin operaciones" in text_value.lower():
        return "return-sos"
    ops_match = re.search(r"\((\d+)\s+ops", text_value, re.IGNORECASE)
    if ops_match and int(ops_match.group(1)) <= 0:
        return "return-sos"
    if has_profit_usd(text_value):
        return profit_color_class(parse_profit_usd(text_value))
    if re.search(r"[-+]?\d+(?:[.,]\d+)?\s*%", text_value):
        return profit_color_class(parse_return_percent(text_value))
    return "return-sos"


def clean_public_return_text(value):
    text_value = str(value or "").strip()
    if not text_value:
        return "SOS"
    text_value = re.sub(r"pendiente\s+de\s+backtest", "Pendiente de datos", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"\s+simulad[oa]s?", "", text_value, flags=re.IGNORECASE)
    return text_value


def build_totalizer(strategies):
    selected = [
        strategy
        for strategy in strategies
        if int(strategy.get("selected_for_totalizer") if "selected_for_totalizer" in strategy else strategy.get("include_in_totalizer") or 0) == 1
    ]
    total_usd = sum(parse_profit_usd(strategy.get("historical_return")) for strategy in selected)
    total_capital = sum(parse_strategy_capital_usd(strategy.get("historical_return")) for strategy in selected)
    current_capital = total_capital + total_usd
    total_pct = (total_usd / total_capital * 100) if total_capital else 0.0
    return {
        "strategies": selected,
        "count": len(selected),
        "total": total_usd,
        "capital": total_capital,
        "capital_display": format_money_usd(total_capital),
        "current_capital": current_capital,
        "current_capital_display": format_money_usd(current_capital),
        "total_pct": total_pct,
        "display": format_signed_money_usd(total_usd),
        "pct_display": f"{total_pct:+.2f}%",
        "result_class": profit_color_class(total_usd),
        "pct_class": profit_color_class(total_pct),
    }


def strategy_info_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


STRATEGY_EXPLANATIONS = {
    "momentum": {
        "summary": "Busca acciones que suben mas fuerte que el mercado y mantienen tendencia alcista.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario. Anade QQQ como benchmark para comparar fuerza relativa.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "Benchmark QQQ para comparar fuerza relativa.",
            "Momentum de 20 dias.",
            "SMA20 y SMA50.",
            "Maximo/minimo reciente de 20 dias para soporte.",
            "Volumen monetario medio de 20 dias.",
        ],
        "filters": [
            "Momentum de 20 dias positivo.",
            "Fuerza relativa positiva: rentabilidad del activo menos rentabilidad de QQQ.",
            "Precio por encima de SMA50.",
            "SMA20 por encima de SMA50.",
            "Volumen monetario medio 20 dias >= 20 M USD.",
        ],
        "score": "Ordena por fuerza relativa, momentum y distancia positiva frente a SMA50.",
        "risk": "Stop entre soporte reciente de 20 dias y SMA50. Objetivos TP1 y TP2 por multiplos de riesgo.",
    },
    "followthemoney": {
        "summary": "Busca activos donde el dinero esta entrando con fuerza hoy frente a su comportamiento normal.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "Volumen monetario del ultimo dia: cierre multiplicado por volumen.",
            "Media de volumen monetario de 1 mes, excluyendo el ultimo dia.",
            "Media de volumen monetario de 2 meses, excluyendo el ultimo dia.",
            "Media de volumen monetario de 3 meses, excluyendo el ultimo dia.",
            "Ratio del volumen monetario actual frente a cada media historica.",
        ],
        "filters": [
            "Volumen monetario del ultimo dia >= 10 M USD.",
            "Ratio frente a media de 1 mes >= 1.25x.",
            "Debe tener suficientes datos para comparar contra 3 meses.",
            "Devuelve solo los 10 activos con mayor score.",
        ],
        "score": "Ordena por expansion de volumen monetario frente a 1, 2 y 3 meses. Da mas peso al ratio de 1 mes y anade un pequeno ajuste por liquidez total.",
        "risk": "Operacion LONG. Apertura en precio actual, cierre objetivo al +10% y stop loss al -10%.",
    },
    "acumulametales": {
        "summary": "Busca oportunidades de acumulacion en metales cuando estan castigados y sobrevendidos.",
        "universe": "Analiza una lista fija de ETFs y activos liquidos vinculados a oro, plata, cobre, platino, paladio, mineras y metales industriales. Puede personalizarse con TRADING_METALS_SYMBOLS.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA180 diaria.",
            "SMA120 semanal calculada con cierres semanales.",
            "RSI14 diario.",
            "Distancia porcentual del precio a SMA180 diaria.",
            "Distancia porcentual del precio a SMA120 semanal.",
        ],
        "filters": [
            "Precio actual por debajo de SMA180 diaria.",
            "Precio actual por debajo de SMA120 semanal.",
            "RSI14 diario menor que 30.",
            "Debe tener suficiente historico para calcular SMA180 diaria y SMA120 semanal.",
        ],
        "score": "Ordena por descuento frente a SMA180, descuento frente a SMA120 semanal y nivel de sobreventa RSI. Cuanto mas castigado y sobrevendido, mayor puntuacion.",
        "risk": "Operacion LONG de acumulacion. Apertura en precio actual. De momento no tiene cierre automatico; cierre y stop se muestran solo como referencias informativas.",
    },
    "acumulacion": {
        "summary": "Busca oportunidades de acumulacion en cualquier activo del universo filtrado cuando esta castigado y sobrevendido.",
        "universe": "Analiza todos los tickets filtrados por liquidez y volumen monetario en tickers.txt, el mismo universo que usa el resto de estrategias generales.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA180 diaria.",
            "SMA120 semanal calculada con cierres semanales.",
            "RSI14 diario.",
            "Distancia porcentual del precio a SMA180 diaria.",
            "Distancia porcentual del precio a SMA120 semanal.",
        ],
        "filters": [
            "Precio actual por debajo de SMA180 diaria.",
            "Precio actual por debajo de SMA120 semanal.",
            "RSI14 diario menor que 30.",
            "Debe tener suficiente historico para calcular SMA180 diaria y SMA120 semanal.",
        ],
        "score": "Ordena por descuento frente a SMA180, descuento frente a SMA120 semanal y nivel de sobreventa RSI.",
        "risk": "Operacion LONG de acumulacion. Apertura en precio actual. De momento no tiene cierre automatico; cierre y stop se muestran solo como referencias informativas.",
    },
    "swingtrading": {
        "summary": "Busca entradas de varios dias en acciones alcistas que han corregido de forma controlada y empiezan a recuperar fuerza.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA20 y SMA50.",
            "RSI14.",
            "Rentabilidad de 50 dias.",
            "Maximo de 20 dias para medir pullback.",
            "Maximos recientes de 3 dias para confirmar recuperacion.",
            "Volumen monetario medio de 20 dias.",
        ],
        "filters": [
            "Tendencia positiva: precio > SMA50, SMA20 > SMA50 y rentabilidad 50 dias positiva.",
            "Pullback desde maximos de 20 dias entre 3% y 12%.",
            "RSI14 entre 40 y 60.",
            "Cierre actual supera maximos recientes de 3 dias y cierra mejor que ayer.",
            "Volumen monetario medio >= 20 M USD.",
        ],
        "score": "Prioriza tendencia de 50 dias, pullback sano, RSI equilibrado y distancia positiva sobre SMA50.",
        "risk": "Stop bajo minimo de 5 dias o SMA50. Objetivos TP1 y TP2 por multiplos de riesgo.",
    },
    "breakout": {
        "summary": "Detecta rupturas de resistencia reciente con tendencia y volumen fuerte.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA20 y SMA50.",
            "Resistencia: maximo de 20 dias excluyendo la vela actual.",
            "Volumen medio de 20 dias.",
            "Ratio de volumen actual frente a volumen medio.",
            "Volumen monetario medio de 20 dias.",
            "Minimo reciente de 5 dias para stop.",
        ],
        "filters": [
            "Resistencia = maximo de los ultimos 20 dias excluyendo la vela actual.",
            "Ruptura minima: cierre al menos 0.2% por encima de resistencia.",
            "Tendencia: precio > SMA50 y SMA20 > SMA50.",
            "Volumen actual >= 1.5 veces el volumen medio de 20 dias.",
            "Volumen monetario medio >= 20 M USD.",
        ],
        "score": "Combina porcentaje de ruptura, ratio de volumen y distancia sobre SMA50.",
        "risk": "Stop bajo resistencia rota o minimo reciente. Objetivos TP1 y TP2 por multiplos de riesgo.",
    },
    "meanreversion": {
        "summary": "Busca activos sobrevendidos que se alejaron demasiado de su media y podrian rebotar.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA20 como media de reversion.",
            "SMA100 como filtro de tendencia/daño.",
            "RSI14.",
            "Bandas de Bollinger de 20 periodos y 2 desviaciones.",
            "ATR14 para medir volatilidad y colocar stop.",
            "Volumen monetario medio de 20 dias.",
        ],
        "filters": [
            "Precio al menos 4% por debajo de SMA20.",
            "Precio en o bajo banda inferior de Bollinger 20 periodos y 2 desviaciones.",
            "RSI14 <= 35.",
            "No demasiado roto: distancia frente a SMA100 no inferior a -12%.",
            "Volumen monetario medio >= 20 M USD.",
            "Primer signo de estabilizacion: no cierra peor que ayer o deja minimo superior.",
        ],
        "score": "Prioriza mayor desviacion respecto a SMA20, RSI bajo, margen hasta SMA100 y potencial de vuelta a media.",
        "risk": "Stop bajo minimo reciente menos ATR14. Objetivos: vuelta a SMA20 y media de Bollinger.",
    },
    "valuetrading": {
        "summary": "Filtra empresas con valoracion barata y fundamentales aceptables.",
        "universe": "Analiza tickets filtrados por liquidez y volumen monetario. Para fundamentales usa solo los primeros activos mas liquidos para limitar llamadas externas.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA20 y SMA50 como filtro tecnico.",
            "Volumen monetario medio de 20 dias.",
            "PER.",
            "Precio/valor contable.",
            "Precio/ventas.",
            "ROE.",
            "Deuda/capital.",
            "Crecimiento de ingresos.",
        ],
        "filters": [
            "PER <= 18.",
            "Precio/valor contable <= 3.",
            "Precio/ventas <= 4.",
            "ROE >= 8.",
            "Deuda/capital <= 150.",
            "Crecimiento de ingresos >= -5%.",
            "Volumen monetario medio >= 10 M USD.",
            "Filtro tecnico con SMA20 y SMA50.",
        ],
        "score": "Prioriza descuento de valoracion, rentabilidad del negocio, menor deuda y tendencia suficiente.",
        "risk": "Stop aproximado por porcentaje y SMA50. Objetivos por multiplos de riesgo.",
    },
    "dividendgrowth": {
        "summary": "Busca empresas con dividendo razonable, crecimiento de dividendos y estabilidad financiera.",
        "universe": "Analiza tickets filtrados por liquidez y volumen monetario. Para dividendos usa solo los primeros activos mas liquidos para no gastar demasiadas llamadas externas.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA50 y SMA100 como filtro tecnico.",
            "Volumen monetario medio de 20 dias.",
            "Dividend yield.",
            "Crecimiento de dividendo a 3 años.",
            "Payout ratio.",
            "ROE.",
            "Deuda/capital.",
            "Crecimiento de ingresos.",
        ],
        "filters": [
            "Dividend yield entre 1% y 6%.",
            "Crecimiento de dividendo 3 anos >= 3%.",
            "Payout ratio <= 75%.",
            "ROE >= 8.",
            "Deuda/capital <= 180.",
            "Crecimiento de ingresos >= -5%.",
            "Volumen monetario medio >= 10 M USD.",
            "Filtro tecnico con SMA50 y SMA100.",
        ],
        "score": "Ordena por crecimiento de dividendo, rentabilidad, seguridad del payout, deuda y tendencia.",
        "risk": "Stop por porcentaje y SMA100. Objetivos por multiplos de riesgo.",
    },
    "trendfollowing": {
        "summary": "Sigue tendencias largas ya confirmadas y evita anticipar suelos.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA50 y SMA200.",
            "Pendiente de SMA200 comparada con 20 sesiones atras.",
            "Maximo de 55 dias para ruptura.",
            "ATR14.",
            "Rentabilidad de 3 meses.",
            "Volumen monetario medio de 20 dias.",
        ],
        "filters": [
            "Precio por encima de SMA200.",
            "SMA50 por encima de SMA200.",
            "Pendiente positiva de SMA200 frente a 20 sesiones atras.",
            "Ruptura de maximos de 55 dias.",
            "Volumen monetario medio >= 20 M USD.",
        ],
        "score": "Prioriza ruptura, rentabilidad de 3 meses y pendiente positiva de SMA200.",
        "risk": "Stop dinamico: maximo entre precio - 3 ATR14 y SMA50. Objetivos TP1 y TP2.",
    },
    "pairstrading": {
        "summary": "Analiza pares correlacionados y busca desviaciones estadisticas para operar convergencia.",
        "universe": "Analiza pares de activos predefinidos y correlacionados. No usa el universo general de tickets filtrados.",
        "data": [
            "Velas diarias de los dos activos del par: apertura, maximo, minimo, cierre y volumen.",
            "Correlacion entre ambos activos.",
            "Spread estadistico del par.",
            "Media y desviacion del spread en ventana de 60 sesiones.",
            "ZScore de spread.",
            "Ratio hedge aproximado.",
            "Volumen monetario medio.",
        ],
        "filters": [
            "Correlacion minima entre activos: 0.70.",
            "ZScore de entrada absoluto >= 2.0.",
            "Salida teorica cuando ZScore vuelve cerca de +/-0.3.",
            "Volumen monetario medio >= 10 M USD.",
        ],
        "score": "Prioriza desviacion estadistica, correlacion y calidad del par.",
        "risk": "La salida principal es convergencia del spread; stop por ZScore extremo queda pendiente de regla fina.",
    },
    "sectorrotation": {
        "summary": "Busca sectores fuertes frente a SPY y acciones lideres dentro de esos sectores.",
        "universe": "Compara ETFs sectoriales fijos y despues revisa acciones representativas de los sectores lideres.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "Benchmark SPY.",
            "ETFs sectoriales.",
            "Fuerza relativa en ventanas de 20, 60 y 120 dias.",
            "SMA20 y SMA50 para acciones lideres.",
            "Volumen monetario medio de 20 dias.",
        ],
        "filters": [
            "Compara fuerza relativa de sectores en ventanas 20, 60 y 120 dias.",
            "Selecciona TOP_SECTORS = 3.",
            "Dentro de cada sector busca acciones con precio > SMA50 y SMA20 > SMA50.",
            "Volumen monetario medio >= 20 M USD.",
            "Devuelve hasta TOP_STOCKS_PER_SECTOR = 5.",
        ],
        "score": "Prioriza sectores con mejor fuerza relativa y acciones lideres dentro de ellos.",
        "risk": "Stop orientativo por porcentaje sobre precio o por estructura tecnica de la accion.",
    },
    "qualityinvesting": {
        "summary": "Busca empresas de calidad con buenos margenes, crecimiento y deuda controlada.",
        "universe": "Analiza tickets filtrados por liquidez y volumen monetario. Para fundamentales usa solo los primeros activos mas liquidos para limitar llamadas externas.",
        "data": [
            "Velas diarias: apertura, maximo, minimo, cierre y volumen.",
            "SMA50 y SMA100 como filtro tecnico.",
            "Volumen monetario medio de 20 dias.",
            "ROE y ROIC.",
            "Margen operativo y margen neto.",
            "Deuda/capital.",
            "Crecimiento de ingresos a 3 años.",
            "Crecimiento de EPS a 3 años.",
            "PER y precio/ventas.",
        ],
        "filters": [
            "ROE >= 12 y ROIC >= 8.",
            "Margen operativo >= 12 y margen neto >= 8.",
            "Deuda/capital <= 150.",
            "Crecimiento de ingresos y EPS 3 anos >= 0.",
            "PER <= 45 y precio/ventas <= 15.",
            "Volumen monetario medio >= 10 M USD.",
            "Filtro tecnico con SMA50 y SMA100.",
        ],
        "score": "Prioriza calidad de negocio, crecimiento, deuda razonable, valoracion y tendencia.",
        "risk": "Stop por porcentaje y SMA100. Objetivos por multiplos de riesgo.",
    },
    "openingrangebreakout": {
        "summary": "Estrategia intradia que opera la ruptura del rango inicial de la sesion.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas intradia: apertura, maximo, minimo, cierre y volumen.",
            "Rango inicial de 15 minutos.",
            "Maximo y minimo del rango inicial.",
            "Ruptura porcentual del rango.",
            "Volumen medio intradia.",
            "Ratio de volumen frente a la media.",
            "Volumen monetario del rango inicial.",
        ],
        "filters": [
            "Solo funciona durante mercado regular USA.",
            "Calcula maximo/minimo del rango inicial.",
            "Ruptura minima: 0.05% por encima/debajo del rango.",
            "Volumen >= 1.5 veces volumen medio.",
            "Volumen monetario del rango inicial >= 1 M USD.",
        ],
        "score": "Ordena por fuerza de ruptura, volumen y claridad del movimiento.",
        "risk": "Stop al otro lado del rango inicial. Objetivos TP1 y TP2 por multiplos de riesgo.",
    },
    "vwapreversion": {
        "summary": "Busca reversion intradia cuando el precio se aleja demasiado de VWAP.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas intradia: apertura, maximo, minimo, cierre y volumen.",
            "VWAP de la sesion.",
            "Distancia porcentual del precio respecto a VWAP.",
            "RSI14.",
            "Volumen medio de 20 velas.",
            "Ratio de volumen frente a la media.",
            "Volumen monetario diario.",
            "Maximos/minimos recientes para stop.",
        ],
        "filters": [
            "Distancia minima a VWAP: 1%.",
            "Para LONG: RSI14 <= 35 y precio por debajo de VWAP.",
            "Para SHORT: RSI14 >= 65 y precio por encima de VWAP.",
            "Volumen relativo >= 0.8.",
            "Volumen monetario diario >= 2 M USD.",
        ],
        "score": "Prioriza distancia a VWAP, extremo de RSI, volumen y potencial de vuelta.",
        "risk": "Stop sobre maximo/minimo reciente. Objetivo principal: retorno hacia VWAP.",
    },
    "momentumintradia": {
        "summary": "Detecta movimientos fuertes dentro de la sesion con momentum reciente y volumen relativo.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas intradia: apertura, maximo, minimo, cierre y volumen.",
            "Momentum de 15 minutos.",
            "Maximos/minimos de 20 minutos para ruptura.",
            "VWAP de la sesion.",
            "Volumen medio de 20 velas.",
            "Ratio de volumen frente a la media.",
            "Volumen monetario diario.",
        ],
        "filters": [
            "Solo tras al menos 10 minutos desde apertura.",
            "Momentum minimo: 1% en 15 minutos.",
            "Ruptura de maximos/minimos de 20 minutos.",
            "Volumen >= 2 veces la media de 20 velas.",
            "Volumen monetario diario >= 3 M USD.",
            "Usa VWAP como confirmacion de direccion.",
        ],
        "score": "Ordena por momentum, volumen relativo y distancia/confirmacion frente a VWAP.",
        "risk": "Stop bajo/encima de zona reciente. Objetivos por multiplos de riesgo.",
    },
    "scalpingthepullbacks": {
        "summary": "Busca pequenos retrocesos intradia dentro de una tendencia para entrar a favor del movimiento.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas intradia: apertura, maximo, minimo, cierre y volumen.",
            "EMA9 y EMA21.",
            "RSI14.",
            "VWAP de la sesion.",
            "Volumen medio de 20 velas.",
            "Ratio de volumen frente a la media.",
            "Pullback de 20 velas.",
            "Distancia del precio a EMA9.",
            "Volumen monetario diario.",
        ],
        "filters": [
            "Solo tras al menos 10 minutos desde apertura.",
            "Tendencia con EMA9 y EMA21.",
            "Pullback minimo de 0.3%.",
            "Precio cerca de EMA9: maximo 0.4% de distancia.",
            "Volumen >= 1.2 veces la media.",
            "Volumen monetario diario >= 2 M USD.",
        ],
        "score": "Prioriza pullback limpio, recuperacion, volumen y alineacion con EMAs/VWAP.",
        "risk": "Stop cercano bajo/encima del retroceso. Objetivos cortos por multiplos de riesgo.",
    },
    "gapandgo": {
        "summary": "Detecta activos que abren con gap relevante y continuan en la direccion del impulso inicial.",
        "universe": "Analiza tickets filtrados previamente por liquidez y volumen monetario.",
        "data": [
            "Velas intradia: apertura, maximo, minimo, cierre y volumen.",
            "Cierre previo.",
            "Precio de apertura.",
            "Gap porcentual frente al cierre previo.",
            "Rango inicial de 15 minutos.",
            "Maximo/minimo del rango inicial.",
            "Volumen medio de 20 velas.",
            "Ratio de volumen frente a la media.",
            "Volumen monetario inicial.",
        ],
        "filters": [
            "Gap minimo 2% y maximo 20% frente al cierre previo.",
            "Espera confirmacion de los primeros 15 minutos.",
            "Ruptura del rango inicial con buffer de 0.05%.",
            "Volumen monetario inicial >= 2 M USD.",
            "Volumen >= 1.5 veces la media.",
        ],
        "score": "Prioriza tamano del gap, volumen y ruptura del rango inicial.",
        "risk": "Stop al otro lado del rango inicial. Objetivos TP1 y TP2 por multiplos de riesgo.",
    },
}


STRATEGY_MANUAL_GUIDES = {
    "momentum": [
        "Para usar Momentum manualmente, abre una grafica diaria del activo y comparalo con QQQ. La estrategia busca acciones que suben con mas fuerza que el mercado, no acciones simplemente baratas. Debes ver que el precio mantiene estructura alcista y que no esta perdiendo sus medias principales.",
        "Anade SMA20 y SMA50. La lectura buena es: precio por encima de SMA50, SMA20 por encima de SMA50, momentum de 20 dias positivo y fuerza relativa superior a QQQ. Si el activo sube mas que QQQ en las ultimas semanas y respeta soportes, la senal tiene mas sentido.",
        "La entrada manual se puede plantear en continuidad o tras un pequeno retroceso sano. El stop suele ir bajo un soporte reciente o bajo SMA50. Los objetivos se calculan por multiplos del riesgo asumido. Evita perseguir velas muy verticales o activos con volumen pobre.",
    ],
    "followthemoney": [
        "Follow The Money parte de una idea sencilla: cuando un activo mueve hoy mucho mas dinero de lo normal, puede estar entrando interes institucional, noticias, rotacion sectorial o acumulacion. No mira solo volumen de acciones; usa volumen monetario, que es precio multiplicado por volumen.",
        "Para revisarla manualmente, compara el volumen monetario del dia actual con su media de 1, 2 y 3 meses. Una senal interesante deberia mostrar un ratio claramente superior a 1.25x frente al ultimo mes y mantener ratios fuertes tambien frente a 2 y 3 meses. Cuanto mas alto sea el ratio y mas liquido sea el activo, mayor calidad tendra el aviso.",
        "La estrategia devuelve los 10 activos con mayor expansion de volumen monetario. La operativa propuesta es LONG con entrada en el precio actual, objetivo de salida al +10% y stop loss al -10%. Es una estrategia de riesgo alto porque el volumen fuerte puede aparecer tanto por acumulacion como por noticias negativas; por eso conviene revisar grafica, catalizador y direccion del precio antes de operar.",
    ],
    "acumulametales": [
        "Acumula Metales es una estrategia de compra progresiva en debilidad. No busca comprar metales cuando ya estan disparados, sino cuando estan por debajo de medias importantes y el RSI indica sobreventa. La idea es detectar zonas donde el activo podria estar castigado en exceso.",
        "Para revisarla manualmente, abre una grafica diaria y anade SMA180 y RSI14. Despues mira tambien la grafica semanal con SMA120. La senal aparece solo si el precio esta por debajo de SMA180 diaria, por debajo de SMA120 semanal y el RSI14 diario esta por debajo de 30.",
        "El aviso no significa que el suelo este hecho. Es una estrategia de acumulacion: puede requerir paciencia y puede seguir cayendo. De momento no se cierra automaticamente; cierre y stop quedan como referencias para seguimiento manual. Conviene revisar contexto macro, dolar, tipos de interes y fuerza del metal antes de aumentar exposicion.",
    ],
    "acumulacion": [
        "Acumulacion aplica la misma logica que Acumula Metales, pero sobre todo el universo filtrado de acciones/ETFs. Busca activos castigados, no activos fuertes. Por eso puede encontrar oportunidades en sectores muy distintos cuando el precio se aleja de sus medias y entra en sobreventa.",
        "Para revisarla manualmente, abre una grafica diaria con SMA180 y RSI14, y una grafica semanal con SMA120. La senal solo aparece si el precio esta por debajo de SMA180 diaria, por debajo de SMA120 semanal y RSI14 diario esta por debajo de 30.",
        "Es una estrategia de acumulacion y paciencia. El activo puede seguir bajando aunque este sobrevendido. De momento no se cierra automaticamente; cierre y stop quedan como referencias para seguimiento manual. Antes de operar conviene revisar noticias, resultados y si la caida viene de un deterioro real.",
    ],
    "swingtrading": [
        "Swing Trading busca operaciones de varios dias. Manualmente debes localizar una accion alcista que haya corregido sin romper su tendencia. No se trata de comprar cualquier caida, sino un retroceso ordenado dentro de una estructura fuerte.",
        "En la grafica diaria usa SMA20, SMA50 y RSI14. Lo ideal es precio por encima de SMA50, SMA20 por encima de SMA50, RSI entre 40 y 60 y recuperacion de maximos recientes de corto plazo. Eso indica que la correccion podria estar terminando.",
        "La entrada suele hacerse cuando el precio confirma recuperacion tras el pullback. El stop puede ir bajo el minimo reciente o bajo SMA50. El objetivo puede estar en antiguos maximos o en multiplos del riesgo. Evita acciones que pierden SMA50 con fuerza o que siguen haciendo minimos decrecientes.",
    ],
    "breakout": [
        "BreaKout se usa buscando rupturas de resistencia. Manualmente dibuja en la grafica diaria una zona donde el precio haya frenado varias veces. La senal tiene sentido cuando el precio supera esa zona con claridad.",
        "Comprueba que el precio este por encima de SMA50, que SMA20 este por encima de SMA50 y que la vela de ruptura venga con volumen superior a su media. Una ruptura sin volumen suele tener mas riesgo de fallo.",
        "La entrada puede hacerse al romper o en un retroceso hacia la resistencia rota. El stop va bajo la resistencia rota o bajo un minimo reciente. Si el precio vuelve rapidamente por debajo del nivel roto, la ruptura pierde calidad y conviene salir o no entrar.",
    ],
    "meanreversion": [
        "Mean Reversion busca rebotes cuando el precio se aleja demasiado de su media. Manualmente abre una grafica diaria y mira si el precio esta muy por debajo de SMA20, cerca de la banda inferior de Bollinger y con RSI14 bajo.",
        "La idea es que un movimiento bajista de corto plazo puede estar estirado y volver hacia la media. Antes de entrar, busca una primera senal de freno: rechazo en minimos, vela con mecha inferior, cierre que deja de caer o minimo superior.",
        "El objetivo suele ser la vuelta hacia SMA20 o hacia la media de Bollinger. El stop se coloca bajo el minimo reciente, ajustado con ATR si hay mucha volatilidad. Evita activos que caen por noticias graves o que rompen soportes mayores sin reaccion.",
    ],
    "valuetrading": [
        "Value Trading mezcla valoracion fundamental y filtro tecnico. Manualmente revisa si la empresa parece barata por PER, precio/ventas o precio/valor contable, pero tambien si mantiene calidad minima: ROE razonable, deuda controlada y ventas estables.",
        "Despues abre la grafica diaria con SMA20 y SMA50. Aunque una accion sea barata, conviene que el precio este estabilizando o intentando recuperar medias. Una empresa barata en caida libre puede seguir cayendo durante mucho tiempo.",
        "La entrada manual tiene mas sentido cerca de soporte, tras recuperacion de media o con mejora clara de estructura. El stop puede ir bajo soporte o bajo SMA50. Evita empresas baratas por deterioro real: deuda excesiva, margenes cayendo o ingresos desplomandose.",
    ],
    "dividendgrowth": [
        "Dividend Growth busca empresas que pagan dividendo y pueden aumentarlo. Manualmente revisa dividend yield, crecimiento del dividendo, payout ratio, ROE, deuda/capital y crecimiento de ingresos. El dividendo debe ser sostenible, no solo alto.",
        "En la grafica diaria usa SMA50 y SMA100 para no comprar en deterioro tecnico fuerte. Lo ideal es una accion estable, con correcciones controladas y sin perdida clara de tendencia.",
        "La entrada puede plantearse en retrocesos hacia soporte o medias. El stop suele ir bajo SMA100 o bajo soporte relevante. Evita yields demasiado altos si vienen acompanados de deuda, caida de beneficios o riesgo de recorte de dividendo.",
    ],
    "trendfollowing": [
        "Trend Following sigue tendencias largas ya confirmadas. Manualmente no intenta encontrar suelos; busca activos que ya estan por encima de sus medias principales y renovando maximos.",
        "En la grafica diaria usa SMA50 y SMA200. La lectura buena es precio por encima de SMA200, SMA50 por encima de SMA200 y SMA200 con pendiente positiva. La senal mejora si el precio rompe maximos de 55 dias.",
        "El stop puede ser dinamico, por debajo de SMA50 o a varias veces ATR14. El objetivo no siempre es fijo: muchas veces se deja correr la tendencia y se sale cuando pierde estructura. Evita mercados laterales con medias cruzandose sin direccion.",
    ],
    "pairstrading": [
        "Pairs Trading compara dos activos relacionados. Manualmente no se analiza solo si una accion sube o baja, sino si la relacion entre ambas se ha alejado demasiado de su comportamiento normal.",
        "La estrategia calcula correlacion, hedge ratio, spread y ZScore. Si el ZScore es alto, el primer activo esta caro frente al segundo: la idea es SHORT primer activo y LONG segundo. Si el ZScore es bajo, la idea es LONG primer activo y SHORT segundo.",
        "La salida teorica llega cuando el ZScore vuelve cerca de la media. La web muestra un precio objetivo del primer activo para poder simularlo, pero la logica real es la convergencia del par. Evita pares con baja correlacion, poca liquidez o noticias que rompan la relacion historica.",
    ],
    "sectorrotation": [
        "Sector Rotation busca invertir donde esta entrando el dinero. Manualmente compara ETFs sectoriales frente a SPY en ventanas de 20, 60 y 120 dias. Un sector interesante deberia hacerlo mejor que el mercado de forma consistente.",
        "Despues busca acciones lideres dentro de esos sectores. En la grafica diaria usa SMA20 y SMA50; interesan acciones que esten fuertes, con precio por encima de medias y volumen monetario suficiente.",
        "La entrada puede hacerse en pullbacks ordenados o rupturas dentro de sectores lideres. El stop va bajo soporte o bajo una media relevante. Evita acciones debiles solo porque pertenecen a un sector fuerte.",
    ],
    "qualityinvesting": [
        "Quality Investing busca empresas de calidad: buenos margenes, ROE/ROIC altos, deuda razonable y crecimiento estable. Manualmente revisa primero el negocio, no solo la grafica.",
        "En la parte tecnica usa SMA50 y SMA100. Una empresa excelente puede ser mala compra si esta excesivamente extendida o si el precio ha roto su estructura. Busca calidad con entrada razonable.",
        "La entrada suele tener sentido en correcciones hacia medias, recuperacion de soporte o continuidad alcista confirmada. El stop puede ir bajo SMA100 o bajo soporte estructural. Evita pagar cualquier precio por calidad si el crecimiento no lo justifica.",
    ],
    "openingrangebreakout": [
        "Opening Range BreaKout es intradia. Manualmente marca el maximo y minimo de los primeros 15 minutos de sesion. Ese rango inicial sera la referencia principal.",
        "La senal aparece si el precio rompe por encima o por debajo de ese rango con volumen fuerte. Si rompe arriba, la operacion es larga; si rompe abajo, corta. No basta con tocar el nivel: debe haber ruptura clara.",
        "El stop suele ir al otro lado del rango inicial. Los objetivos se calculan por multiplos del riesgo. Evita rangos iniciales enormes, volumen bajo o rupturas que vuelven inmediatamente dentro del rango.",
    ],
    "vwapreversion": [
        "VWAP Reversion es intradia y busca vuelta hacia VWAP cuando el precio se aleja demasiado. Manualmente activa VWAP y RSI14 en una grafica corta.",
        "Para largos, interesa precio por debajo de VWAP y RSI bajo; para cortos, precio por encima de VWAP y RSI alto. La entrada mejora si aparece rechazo del extremo o perdida de fuerza del movimiento.",
        "El objetivo suele ser VWAP o una zona intermedia hacia VWAP. El stop va mas alla del maximo o minimo reciente que invalida la reversion. Evita operar contra tendencias intradia muy fuertes con volumen creciente.",
    ],
    "momentumintradia": [
        "Momentum Intradia busca aceleraciones dentro de la sesion. Manualmente usa grafica de 1 a 5 minutos y revisa si el precio rompe maximos o minimos recientes con volumen relativo alto.",
        "La confirmacion mejora si el movimiento esta alineado con VWAP y si el volumen supera claramente su media de corto plazo. Para largos, el precio deberia mantenerse fuerte por encima de zonas recientes; para cortos, lo contrario.",
        "La entrada suele hacerse en ruptura o tras pequeno retroceso que no rompe estructura. El stop va bajo el ultimo retroceso o bajo VWAP. Evita entrar tras una sola vela aislada sin continuidad.",
    ],
    "scalpingthepullbacks": [
        "Scalping The PullBacks busca retrocesos pequenos dentro de una tendencia intradia. Manualmente usa EMA9, EMA21, VWAP y RSI14.",
        "Primero identifica direccion: para largos, EMA9 sobre EMA21 y precio por encima o cerca de VWAP; para cortos, lo contrario. Despues espera que el precio retroceda hacia EMA9 o EMA21 y vuelva a reaccionar.",
        "La entrada debe ser rapida y disciplinada. El stop va bajo el retroceso o al otro lado de EMA21/VWAP. Los objetivos son cortos, por multiplos del riesgo. Evita laterales donde las medias se cruzan constantemente.",
    ],
    "gapandgo": [
        "Gap and Go busca activos que abren con gap y continuan en la direccion del gap. Manualmente compara la apertura con el cierre anterior y confirma que el gap es relevante.",
        "Despues marca el rango de los primeros 15 minutos. Si el gap es alcista, busca ruptura por encima del rango con volumen. Si es bajista, ruptura por debajo. El gap por si solo no basta: necesitas continuidad.",
        "El stop suele ir al otro lado del rango inicial. Los objetivos se calculan por multiplos del riesgo o zonas intradia. Evita gaps demasiado grandes sin liquidez, o gaps que se giran inmediatamente tras la apertura.",
    ],
}


def strategy_explanation_for(strategy):
    key = strategy_info_key(strategy.get("name"))
    explanation = STRATEGY_EXPLANATIONS.get(key)
    if explanation:
        return {**explanation, "manual": STRATEGY_MANUAL_GUIDES.get(key, [])}
    return {
        "summary": strategy.get("description") or "Estrategia personalizada.",
        "universe": "Usa la configuracion indicada en el panel de administracion.",
        "data": ["Revisa el archivo Python vinculado para conocer la fuente exacta de datos."],
        "filters": ["No hay ficha tecnica detallada para esta estrategia personalizada."],
        "score": "La ordenacion depende del codigo asociado a la estrategia.",
        "risk": "El stop, objetivo y gestion de riesgo dependen del aviso generado por el codigo.",
        "manual": [],
    }


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
    app.config["ADMIN_PASSWORD_HASH"] = os.environ.get(
        "ADMIN_PASSWORD_HASH", generate_password_hash("admin123")
    )
    app.jinja_env.globals["technical_term_help"] = technical_term_help
    app.jinja_env.filters["money_usd"] = format_money_usd
    app.jinja_env.filters["one_decimal"] = format_one_decimal
    app.jinja_env.filters["compact_count"] = format_compact_count

    @app.before_request
    def before_request():
        g.db = engine.connect()
        if public_site_locked():
            return require_site_password()
        track_visitor()

    @app.teardown_request
    def teardown_request(_exception):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    @app.after_request
    def prevent_dynamic_page_cache(response):
        if response.content_type and response.content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not session.get("admin_logged_in"):
                flash("Inicia sesion para acceder al panel.", "warning")
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped_view

    def public_site_locked():
        site_password = os.environ.get("SITE_PASSWORD", "").strip()
        if not site_password:
            return False
        if session.get("site_unlocked"):
            return False
        endpoint = request.endpoint or ""
        path = request.path or ""
        allowed_endpoints = {
            "static",
            "favicon",
            "site_login",
            "login",
            "logout",
            "user_login",
            "user_register",
            "user_logout",
            "account",
            "membership",
            "payment_page",
            "payment_start",
            "payment_success",
            "payment_cancelled",
            "subscription_portal",
            "stripe_webhook",
        }
        if endpoint in allowed_endpoints:
            return False
        if path.startswith("/admin"):
            return False
        return True

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            app.static_folder,
            "favicon.svg",
            mimetype="image/svg+xml",
        )

    def require_site_password():
        if request.method == "GET":
            session["site_next_url"] = request.full_path.rstrip("?") or url_for("index")
        return redirect(url_for("site_login"))

    @app.route("/acceso", methods=["GET", "POST"])
    def site_login():
        site_password = os.environ.get("SITE_PASSWORD", "").strip()
        if not site_password:
            return redirect(url_for("index"))

        if request.method == "POST":
            password = request.form.get("password", "")
            if compare_digest(password, site_password):
                session["site_unlocked"] = True
                next_url = session.pop("site_next_url", url_for("index"))
                flash("Acceso concedido.", "success")
                return redirect(next_url)
            flash("Contrasena incorrecta.", "danger")

        return render_template("site_login.html")

    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return g.db.execute(
            text(
                """
                SELECT *
                FROM users
                WHERE id = :id
                """
            ),
            {"id": user_id},
        ).mappings().fetchone()

    def member_has_full_access(user=None):
        user = user if user is not None else current_user()
        return bool(user and int(user.get("has_access") or 0))

    def can_view_strategy(strategy):
        if session.get("admin_logged_in"):
            return True
        if member_has_full_access():
            return True
        return bool(int(strategy.get("public_visible") or 0))

    def load_user_totalizer_selection(user_id):
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT strategy_id
                    FROM user_strategy_selections
                    WHERE user_id = :user_id
                      AND selected = 1
                    """
                ),
                {"user_id": user_id},
            ).fetchall()
        except Exception:
            return set()
        return {int(row[0]) for row in rows}

    def save_user_strategy_selection(user_id, strategy_id, selected):
        if engine.dialect.name == "postgresql":
            statement = text(
                """
                INSERT INTO user_strategy_selections (user_id, strategy_id, selected, updated_at)
                VALUES (:user_id, :strategy_id, :selected, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, strategy_id)
                DO UPDATE SET selected = EXCLUDED.selected, updated_at = CURRENT_TIMESTAMP
                """
            )
        else:
            statement = text(
                """
                INSERT INTO user_strategy_selections (user_id, strategy_id, selected, updated_at)
                VALUES (:user_id, :strategy_id, :selected, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, strategy_id)
                DO UPDATE SET selected = excluded.selected, updated_at = CURRENT_TIMESTAMP
                """
            )
        g.db.execute(
            statement,
            {"user_id": user_id, "strategy_id": strategy_id, "selected": selected},
        )
        g.db.commit()

    def load_user_simulator(user_id, strategies):
        if not user_id:
            return default_simulator_state(strategies)
        settings = load_user_simulator_settings(user_id)
        selected_ids = load_user_simulator_strategy_ids(user_id)
        if not selected_ids:
            selected_ids = {
                int(strategy["id"])
                for strategy in strategies
                if int(strategy.get("selected_for_totalizer") or 0) == 1
            }
        rows = build_simulator_strategy_rows(strategies, selected_ids)
        summary = simulator_summary(settings, rows)
        return {
            "settings": settings,
            "strategies": rows,
            "summary": summary,
            "operations": [],
            "show_operations": False,
        }

    def default_simulator_state(strategies):
        settings = {
            "initial_capital": 10000.0,
            "monthly_contribution": 300.0,
            "start_date": datetime.now(MADRID_TZ).date().isoformat(),
        }
        rows = build_simulator_strategy_rows(strategies, set())
        return {
            "settings": settings,
            "strategies": rows,
            "summary": empty_simulator_summary(settings),
            "operations": [],
            "show_operations": False,
        }

    def load_user_simulator_settings(user_id):
        try:
            row = g.db.execute(
                text(
                    """
                    SELECT initial_capital, monthly_contribution, start_date
                    FROM user_simulator_settings
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            row = None
        if not row:
            return {
                "initial_capital": 10000.0,
                "monthly_contribution": 300.0,
                "start_date": datetime.now(MADRID_TZ).date().isoformat(),
            }
        return {
            "initial_capital": float(row.get("initial_capital") or 0),
            "monthly_contribution": float(row.get("monthly_contribution") or 0),
            "start_date": str(row.get("start_date") or datetime.now(MADRID_TZ).date().isoformat())[:10],
        }

    def load_user_simulator_strategy_ids(user_id):
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT strategy_id
                    FROM user_simulator_strategies
                    WHERE user_id = :user_id
                      AND selected = 1
                    """
                ),
                {"user_id": user_id},
            ).fetchall()
        except Exception:
            rollback_request_db()
            return set()
        return {int(row[0]) for row in rows}

    def build_simulator_strategy_rows(strategies, selected_ids):
        rows = []
        for strategy in strategies:
            rows.append(
                {
                    "id": int(strategy["id"]),
                    "name": strategy["name"],
                    "short_name": strategy.get("short_name") or strategy_short_name(strategy.get("name")),
                    "txt_name": strategy.get("signals_txt_name", ""),
                    "historical_return": strategy.get("historical_return", ""),
                    "selected": int(strategy["id"]) in selected_ids,
                    "locked": bool(strategy.get("is_locked")),
                }
            )
        return rows

    def simulator_strategy_context_for_user(user):
        has_full_access = member_has_full_access(user)
        rows = g.db.execute(
            text(
                """
                SELECT id, name, signals_txt_name, historical_return, public_visible
                FROM strategies
                WHERE is_active = 1
                ORDER BY created_at DESC
                """
            )
        ).mappings().fetchall()
        selected_ids = load_user_simulator_strategy_ids(user["id"])
        if not selected_ids:
            selected_ids = load_user_totalizer_selection(user["id"])
        strategies = []
        for row in rows:
            strategy = dict(row)
            strategy["short_name"] = strategy_short_name(strategy.get("name"))
            strategy["is_locked"] = not has_full_access and not int(strategy.get("public_visible") or 0)
            strategies.append(strategy)
        return build_simulator_strategy_rows(strategies, selected_ids)

    def simulator_summary(settings, strategy_rows):
        selected_txt = [row["txt_name"] for row in strategy_rows if row["selected"] and row["txt_name"]]
        contributed = simulator_contributed_capital(settings)
        if not selected_txt:
            summary = empty_simulator_summary(settings)
            summary["contributed"] = contributed
            return summary
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS total_ops,
                        SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_ops,
                        SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_ops,
                        SUM(COALESCE(profit_usd, 0)) AS profit_usd
                    FROM simulated_operations
                    WHERE txt_name IN :txt_names
                      AND (
                          operation_key LIKE 'BACKTEST|%'
                          OR COALESCE(opened_at, closed_at, updated_at) >= :start_date
                      )
                    """
                ).bindparams(bindparam("txt_names", expanding=True)),
                {"txt_names": selected_txt, "start_date": settings["start_date"]},
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            rows = None
        total_ops = int(rows.get("total_ops") or 0) if rows else 0
        open_ops = int(rows.get("open_ops") or 0) if rows else 0
        closed_ops = int(rows.get("closed_ops") or 0) if rows else 0
        profit = float(rows.get("profit_usd") or 0) if rows else 0.0
        return_pct = (profit / contributed * 100) if contributed else 0.0
        current_capital = contributed + profit
        return {
            "selected_count": len(selected_txt),
            "contributed": contributed,
            "contributed_display": format_money_usd(contributed),
            "monthly_display": format_money_usd(settings["monthly_contribution"]),
            "profit": profit,
            "profit_display": format_signed_money_usd(profit),
            "profit_class": profit_color_class(profit),
            "current_capital": current_capital,
            "current_capital_display": format_money_usd(current_capital),
            "return_pct": return_pct,
            "return_pct_display": f"{return_pct:+.2f}%",
            "return_pct_class": profit_color_class(return_pct),
            "total_ops": total_ops,
            "open_ops": open_ops,
            "closed_ops": closed_ops,
        }

    def simulator_selected_return_pct(strategy_rows):
        selected_returns = [
            parse_return_percent(row.get("historical_return"))
            for row in strategy_rows
            if row.get("selected") and row.get("txt_name")
        ]
        if not selected_returns:
            return 0.0
        return sum(selected_returns) / len(selected_returns)

    def empty_simulator_summary(settings):
        contributed = simulator_contributed_capital(settings)
        return {
            "selected_count": 0,
            "contributed": contributed,
            "contributed_display": format_money_usd(contributed),
            "monthly_display": format_money_usd(settings["monthly_contribution"]),
            "profit": 0.0,
            "profit_display": format_signed_money_usd(0),
            "profit_class": profit_color_class(0),
            "current_capital": contributed,
            "current_capital_display": format_money_usd(contributed),
            "return_pct": 0.0,
            "return_pct_display": "+0.00%",
            "return_pct_class": profit_color_class(0),
            "total_ops": 0,
            "open_ops": 0,
            "closed_ops": 0,
        }

    def simulator_contributed_capital(settings):
        initial = float(settings.get("initial_capital") or 0)
        monthly = float(settings.get("monthly_contribution") or 0)
        months = months_since_start(settings.get("start_date"))
        return initial + (monthly * months)

    def months_since_start(start_date):
        try:
            start = datetime.fromisoformat(str(start_date)[:10]).date()
        except ValueError:
            return 0
        today = datetime.now(MADRID_TZ).date()
        if start > today:
            return 0
        months = (today.year - start.year) * 12 + (today.month - start.month)
        if today.day >= start.day:
            months += 1
        return max(0, months)

    def simulator_operations(settings, strategy_rows, limit=100, offset=0):
        selected_txt = [row["txt_name"] for row in strategy_rows if row["selected"] and row["txt_name"]]
        if not selected_txt:
            return []
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT strategy_name, txt_name, symbol, direction, status,
                           signal_date, opened_at, closed_at, entry_price,
                           target_price, stop_loss, shares, current_price,
                           investment_value, profit_usd, profit_pct,
                           close_reason, updated_at
                    FROM simulated_operations
                    WHERE txt_name IN :txt_names
                      AND (
                          operation_key LIKE 'BACKTEST|%'
                          OR COALESCE(opened_at, closed_at, updated_at) >= :start_date
                      )
                    ORDER BY CASE WHEN opened_at IS NULL THEN 1 ELSE 0 END ASC,
                             opened_at DESC,
                             closed_at DESC,
                             updated_at DESC,
                             symbol ASC
                    LIMIT :limit
                    OFFSET :offset
                    """
                ).bindparams(bindparam("txt_names", expanding=True)),
                {
                    "txt_names": selected_txt,
                    "start_date": settings["start_date"],
                    "limit": int(limit),
                    "offset": int(offset),
                },
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            return []
        return [format_simulated_operation(dict(row)) for row in rows]

    def save_user_simulator(user_id, initial_capital, monthly_contribution, start_date, selected_ids):
        if engine.dialect.name == "postgresql":
            settings_statement = text(
                """
                INSERT INTO user_simulator_settings
                (user_id, initial_capital, monthly_contribution, start_date, updated_at)
                VALUES (:user_id, :initial_capital, :monthly_contribution, :start_date, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    initial_capital = EXCLUDED.initial_capital,
                    monthly_contribution = EXCLUDED.monthly_contribution,
                    start_date = EXCLUDED.start_date,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
        else:
            settings_statement = text(
                """
                INSERT INTO user_simulator_settings
                (user_id, initial_capital, monthly_contribution, start_date, updated_at)
                VALUES (:user_id, :initial_capital, :monthly_contribution, :start_date, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    initial_capital = excluded.initial_capital,
                    monthly_contribution = excluded.monthly_contribution,
                    start_date = excluded.start_date,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
        g.db.execute(
            settings_statement,
            {
                "user_id": user_id,
                "initial_capital": initial_capital,
                "monthly_contribution": monthly_contribution,
                "start_date": start_date,
            },
        )
        g.db.execute(text("DELETE FROM user_simulator_strategies WHERE user_id = :user_id"), {"user_id": user_id})
        if selected_ids:
            g.db.execute(
                text(
                    """
                    INSERT INTO user_simulator_strategies
                    (user_id, strategy_id, selected, updated_at)
                    VALUES (:user_id, :strategy_id, 1, CURRENT_TIMESTAMP)
                    """
                ),
                [{"user_id": user_id, "strategy_id": strategy_id} for strategy_id in selected_ids],
            )
        g.db.commit()

    def parse_money_form_value(value, default=0.0):
        try:
            return max(0.0, float(str(value or "").replace(",", ".")))
        except ValueError:
            return default

    def parse_date_form_value(value):
        try:
            return datetime.fromisoformat(str(value)[:10]).date().isoformat()
        except ValueError:
            return datetime.now(MADRID_TZ).date().isoformat()

    @app.context_processor
    def inject_user_context():
        user = current_user()
        return {
            "current_user": user,
            "member_has_access": member_has_full_access(user),
            "membership_price_text": membership_price_text(),
        }

    def free_trial_enabled():
        return os.environ.get("FREE_TRIAL_ACCESS", "0").strip().lower() in {"1", "true", "yes", "on"}

    def membership_price_text():
        return os.environ.get("MEMBERSHIP_PRICE_TEXT", "30 USD/mes").strip() or "30 USD/mes"

    def membership_payment_url():
        return os.environ.get("MEMBERSHIP_PAYMENT_URL", "").strip()

    def payment_product_catalog():
        return {
            "trading_premium": {
                "name": "Code Markets Premium",
                "description": "Acceso completo a estrategias, avisos, historiales y panel de cuenta.",
                "subject_type": "membership",
                "plans": {
                    "monthly": {
                        "label": "Mensual",
                        "price_text": os.environ.get("MEMBERSHIP_PRICE_TEXT", "30 USD/mes").strip() or "30 USD/mes",
                        "stripe_price_id": os.environ.get("STRIPE_PRICE_TRADING_MONTHLY", "").strip(),
                        "mode": "subscription",
                    },
                    "yearly": {
                        "label": "Anual",
                        "price_text": os.environ.get("MEMBERSHIP_PRICE_YEARLY_TEXT", "300 USD/ano").strip() or "300 USD/ano",
                        "stripe_price_id": os.environ.get("STRIPE_PRICE_TRADING_YEARLY", "").strip(),
                        "mode": "subscription",
                    },
                },
            },
        }

    def payment_product(product_key):
        return payment_product_catalog().get(product_key or "trading_premium")

    def stripe_secret_key():
        return os.environ.get("STRIPE_SECRET_KEY", "").strip()

    def stripe_publishable_key():
        return os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()

    def stripe_webhook_secret():
        return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    def stripe_configured():
        return bool(stripe_secret_key() and stripe_publishable_key())

    def create_payment_record(user, product_key, plan_key, product, plan):
        now = datetime.now(UTC).replace(tzinfo=None)
        result = g.db.execute(
            text(
                """
                INSERT INTO payments
                (user_id, product_key, plan_key, subject_type, subject_id, provider,
                 provider_session_id, provider_customer_id, provider_subscription_id,
                 status, mode, amount_text, currency, metadata_json, created_at, updated_at)
                VALUES (:user_id, :product_key, :plan_key, :subject_type, :subject_id, 'stripe',
                        '', '', '', 'created', :mode, :amount_text, 'USD',
                        :metadata_json, :created_at, :updated_at)
                RETURNING id
                """
            )
            if engine.dialect.name == "postgresql"
            else text(
                """
                INSERT INTO payments
                (user_id, product_key, plan_key, subject_type, subject_id, provider,
                 provider_session_id, provider_customer_id, provider_subscription_id,
                 status, mode, amount_text, currency, metadata_json, created_at, updated_at)
                VALUES (:user_id, :product_key, :plan_key, :subject_type, :subject_id, 'stripe',
                        '', '', '', 'created', :mode, :amount_text, 'USD',
                        :metadata_json, :created_at, :updated_at)
                """
            ),
            {
                "user_id": user["id"],
                "product_key": product_key,
                "plan_key": plan_key,
                "subject_type": product.get("subject_type", product_key),
                "subject_id": str(user["id"]),
                "mode": plan.get("mode", "payment"),
                "amount_text": plan.get("price_text", ""),
                "metadata_json": json.dumps({"product_key": product_key, "plan_key": plan_key}),
                "created_at": now,
                "updated_at": now,
            },
        )
        if engine.dialect.name == "postgresql":
            return result.scalar_one()
        return g.db.execute(text("SELECT last_insert_rowid()")).scalar_one()

    def update_payment_record(payment_id, status, **fields):
        allowed = {
            "provider_session_id",
            "provider_customer_id",
            "provider_subscription_id",
            "metadata_json",
        }
        assignments = ["status = :status", "updated_at = :updated_at"]
        params = {
            "id": payment_id,
            "status": status,
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        }
        for key, value in fields.items():
            if key in allowed:
                assignments.append(f"{key} = :{key}")
                params[key] = value or ""
        g.db.execute(
            text(f"UPDATE payments SET {', '.join(assignments)} WHERE id = :id"),
            params,
        )

    def mark_user_membership_paid(user_id, amount_text="", customer_id="", subscription_id=""):
        now = datetime.now(UTC).replace(tzinfo=None)
        g.db.execute(
            text(
                """
                UPDATE users
                SET has_access = 1,
                    payment_status = 'active',
                    membership_plan = 'Code Markets Premium',
                    membership_amount = COALESCE(NULLIF(:amount_text, ''), membership_amount),
                    membership_started_at = COALESCE(membership_started_at, :now),
                    stripe_customer_id = COALESCE(NULLIF(:customer_id, ''), stripe_customer_id),
                    stripe_subscription_id = COALESCE(NULLIF(:subscription_id, ''), stripe_subscription_id)
                WHERE id = :id
                """
            ),
            {
                "id": user_id,
                "amount_text": amount_text,
                "customer_id": customer_id,
                "subscription_id": subscription_id,
                "now": now,
            },
        )

    def mark_user_membership_cancelled(customer_id="", subscription_id=""):
        conditions = []
        params = {}
        if customer_id:
            conditions.append("stripe_customer_id = :customer_id")
            params["customer_id"] = customer_id
        if subscription_id:
            conditions.append("stripe_subscription_id = :subscription_id")
            params["subscription_id"] = subscription_id
        if not conditions:
            return
        g.db.execute(
            text(
                f"""
                UPDATE users
                SET has_access = 0,
                    payment_status = 'cancelled',
                    membership_expires_at = COALESCE(membership_expires_at, :now)
                WHERE {' OR '.join(conditions)}
                """
            ),
            {**params, "now": datetime.now(UTC).replace(tzinfo=None)},
        )

    def user_stripe_customer_id(user):
        if not user:
            return ""
        customer_id = (user.get("stripe_customer_id") or "").strip()
        if customer_id:
            return customer_id
        row = g.db.execute(
            text(
                """
                SELECT provider_customer_id
                FROM payments
                WHERE user_id = :user_id
                  AND provider = 'stripe'
                  AND provider_customer_id <> ''
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"user_id": user["id"]},
        ).mappings().fetchone()
        return (row["provider_customer_id"] if row else "").strip()

    def user_stripe_subscription_id(user):
        if not user:
            return ""
        subscription_id = (user.get("stripe_subscription_id") or "").strip()
        if subscription_id:
            return subscription_id
        row = g.db.execute(
            text(
                """
                SELECT provider_subscription_id
                FROM payments
                WHERE user_id = :user_id
                  AND provider = 'stripe'
                  AND provider_subscription_id <> ''
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"user_id": user["id"]},
        ).mappings().fetchone()
        return (row["provider_subscription_id"] if row else "").strip()

    def stripe_cancel_subscription(subscription_id):
        subscription_id = (subscription_id or "").strip()
        if not subscription_id:
            return True, {}
        if not stripe_secret_key():
            return False, {"error": "missing_stripe_secret"}
        stripe_request = urllib.request.Request(
            f"https://api.stripe.com/v1/subscriptions/{urllib.parse.quote(subscription_id)}",
            headers={"Authorization": f"Bearer {stripe_secret_key()}"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(stripe_request, timeout=20) as response:
                return True, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            return False, {"error": detail}
        except Exception as exc:
            return False, {"error": str(exc)}

    def stripe_customer_portal_session(user):
        customer_id = user_stripe_customer_id(user)
        if not stripe_secret_key() or not customer_id:
            return None, "missing_customer"
        return_url = os.environ.get("STRIPE_CUSTOMER_PORTAL_RETURN_URL", "").strip() or url_for("account", _external=True)
        payload = {
            "customer": customer_id,
            "return_url": return_url,
            "locale": "es",
        }
        request_data = urllib.parse.urlencode(payload).encode("utf-8")
        stripe_request = urllib.request.Request(
            "https://api.stripe.com/v1/billing_portal/sessions",
            data=request_data,
            headers={
                "Authorization": f"Bearer {stripe_secret_key()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(stripe_request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8")), ""
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            return None, detail
        except Exception as exc:
            return None, str(exc)

    def stripe_checkout_session(product_key, plan_key, plan, payment_id, user):
        price_id = plan.get("stripe_price_id", "").strip()
        if not stripe_configured() or not price_id:
            return None, "missing_config"
        success_url = url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
        cancel_url = url_for("payment_cancelled", product=product_key, plan=plan_key, _external=True)
        payload = {
            "mode": plan.get("mode", "subscription"),
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(payment_id),
            "customer_email": user.get("email") or "",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "metadata[payment_id]": str(payment_id),
            "metadata[user_id]": str(user["id"]),
            "metadata[product_key]": product_key,
            "metadata[plan_key]": plan_key,
            "allow_promotion_codes": "true",
        }
        request_data = urllib.parse.urlencode(payload).encode("utf-8")
        stripe_request = urllib.request.Request(
            "https://api.stripe.com/v1/checkout/sessions",
            data=request_data,
            headers={
                "Authorization": f"Bearer {stripe_secret_key()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(stripe_request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8")), ""
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            return None, detail
        except Exception as exc:
            return None, str(exc)

    def stripe_retrieve_session(session_id):
        if not stripe_secret_key() or not session_id:
            return None
        stripe_request = urllib.request.Request(
            f"https://api.stripe.com/v1/checkout/sessions/{urllib.parse.quote(session_id)}",
            headers={"Authorization": f"Bearer {stripe_secret_key()}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(stripe_request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

    def stripe_signature_valid(payload, signature_header):
        secret = stripe_webhook_secret()
        if not secret:
            return False
        parts = {}
        for item in (signature_header or "").split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                parts.setdefault(key, []).append(value)
        timestamp = (parts.get("t") or [""])[0]
        signatures = parts.get("v1") or []
        if not timestamp or not signatures:
            return False
        signed_payload = f"{timestamp}.".encode("utf-8") + payload
        expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        return any(compare_digest(expected, signature) for signature in signatures)

    def empty_to_none(value):
        value = (value or "").strip()
        return value or None

    @app.route("/registro", methods=["GET", "POST"])
    def user_register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            age_confirmed = 1 if request.form.get("age_confirmed") == "on" else 0
            risk_accepted = 1 if request.form.get("risk_accepted") == "on" else 0

            if not email or "@" not in email:
                flash("Introduce un email valido.", "danger")
                return render_template("user_register.html", name=name, email=email)
            if len(password) < 6:
                flash("La contrasena debe tener al menos 6 caracteres.", "danger")
                return render_template("user_register.html", name=name, email=email)
            if not age_confirmed or not risk_accepted:
                flash("Debes confirmar la mayoria de edad y aceptar el aviso de riesgo para crear la cuenta.", "danger")
                return render_template("user_register.html", name=name, email=email)

            existing = g.db.execute(
                text("SELECT id FROM users WHERE lower(email) = lower(:email)"),
                {"email": email},
            ).mappings().fetchone()
            if existing:
                flash("Ese email ya tiene cuenta. Entra con tu contrasena.", "warning")
                return redirect(url_for("user_login"))

            trial_access = 1 if free_trial_enabled() else 0
            payment_status = "trial" if trial_access else "registered"
            result = g.db.execute(
                text(
                    """
                    INSERT INTO users
                    (email, password_hash, name, has_access, payment_status, membership_plan,
                     membership_amount, age_confirmed, risk_accepted, accepted_terms_at)
                    VALUES (:email, :password_hash, :name, :has_access, :payment_status,
                            'Miembro', :membership_amount, :age_confirmed, :risk_accepted, :accepted_terms_at)
                    RETURNING id
                    """
                )
                if engine.dialect.name == "postgresql"
                else text(
                    """
                    INSERT INTO users
                    (email, password_hash, name, has_access, payment_status, membership_plan,
                     membership_amount, age_confirmed, risk_accepted, accepted_terms_at)
                    VALUES (:email, :password_hash, :name, :has_access, :payment_status,
                            'Miembro', :membership_amount, :age_confirmed, :risk_accepted, :accepted_terms_at)
                    """
                ),
                {
                    "email": email,
                    "password_hash": generate_password_hash(password),
                    "name": name,
                    "has_access": trial_access,
                    "payment_status": payment_status,
                    "membership_amount": membership_price_text(),
                    "age_confirmed": age_confirmed,
                    "risk_accepted": risk_accepted,
                    "accepted_terms_at": datetime.now(UTC).replace(tzinfo=None),
                },
            )
            if engine.dialect.name == "postgresql":
                user_id = result.scalar_one()
            else:
                user_id = g.db.execute(text("SELECT last_insert_rowid()")).scalar_one()
            g.db.commit()

            session["user_id"] = user_id
            session["user_email"] = email
            if trial_access:
                flash("Cuenta creada. Acceso completo activado.", "success")
                return redirect(url_for("index"))
            flash("Cuenta creada. Activa tu membresia para ver todas las estrategias.", "success")
            return redirect(url_for("membership"))

        return render_template("user_register.html")

    @app.route("/membresia", methods=["GET", "POST"])
    def membership():
        user = current_user()
        if request.method == "POST":
            if not user:
                flash("Crea una cuenta o entra antes de activar la membresia.", "warning")
                return redirect(url_for("user_login"))
            g.db.execute(
                text(
                    """
                    UPDATE users
                    SET payment_status = 'payment_pending',
                        membership_plan = 'Miembro',
                        membership_amount = :membership_amount
                    WHERE id = :id
                    """
                ),
                {"membership_amount": membership_price_text(), "id": user["id"]},
            )
            g.db.commit()
            payment_url = membership_payment_url()
            if payment_url:
                return redirect(payment_url)
            return redirect(url_for("payment_page", product="trading_premium", plan="monthly"))

        return render_template(
            "membership.html",
            user=user,
            payment_url=url_for("payment_page", product="trading_premium", plan="monthly"),
            price_text=membership_price_text(),
        )

    @app.route("/pago", methods=["GET"])
    def payment_page():
        user = current_user()
        if not user:
            flash("Entra o crea una cuenta antes de pagar.", "warning")
            return redirect(url_for("user_login"))
        product_key = request.args.get("product", "trading_premium").strip() or "trading_premium"
        product = payment_product(product_key)
        if product is None:
            abort(404)
        requested_plan = request.args.get("plan", "monthly").strip() or "monthly"
        plans = product.get("plans", {})
        selected_plan = requested_plan if requested_plan in plans else next(iter(plans), "")
        return render_template(
            "payment.html",
            user=user,
            product_key=product_key,
            product=product,
            plans=plans,
            selected_plan=selected_plan,
            stripe_configured=stripe_configured(),
        )

    @app.route("/pago/iniciar", methods=["POST"])
    def payment_start():
        user = current_user()
        if not user:
            flash("Entra o crea una cuenta antes de pagar.", "warning")
            return redirect(url_for("user_login"))
        product_key = request.form.get("product", "trading_premium").strip() or "trading_premium"
        plan_key = request.form.get("plan", "monthly").strip() or "monthly"
        product = payment_product(product_key)
        if product is None:
            abort(404)
        plan = product.get("plans", {}).get(plan_key)
        if plan is None:
            flash("Ese plan no esta disponible.", "warning")
            return redirect(url_for("payment_page", product=product_key))

        payment_id = create_payment_record(user, product_key, plan_key, product, plan)
        g.db.execute(
            text(
                """
                UPDATE users
                SET payment_status = 'payment_pending',
                    membership_plan = 'Code Markets Premium',
                    membership_amount = :membership_amount
                WHERE id = :id
                """
            ),
            {"membership_amount": plan.get("price_text", membership_price_text()), "id": user["id"]},
        )
        g.db.commit()

        session_payload, error = stripe_checkout_session(product_key, plan_key, plan, payment_id, user)
        if session_payload and session_payload.get("url"):
            update_payment_record(
                payment_id,
                "checkout_created",
                provider_session_id=session_payload.get("id", ""),
                provider_customer_id=session_payload.get("customer", ""),
                provider_subscription_id=session_payload.get("subscription", ""),
                metadata_json=json.dumps(session_payload.get("metadata") or {}),
            )
            g.db.commit()
            return redirect(session_payload["url"])

        update_payment_record(payment_id, "configuration_pending", metadata_json=json.dumps({"error": error[:500]}))
        g.db.commit()
        flash("Pago preparado, pero falta configurar el precio de Stripe o la conexion. Revisa los pasos de la pagina.", "warning")
        return redirect(url_for("payment_page", product=product_key, plan=plan_key))

    @app.route("/pago/exito")
    def payment_success():
        user = current_user()
        session_id = request.args.get("session_id", "").strip()
        session_payload = stripe_retrieve_session(session_id)
        payment_id = ""
        if session_payload:
            metadata = session_payload.get("metadata") or {}
            payment_id = metadata.get("payment_id") or session_payload.get("client_reference_id") or ""
            user_id = metadata.get("user_id") or (str(user["id"]) if user else "")
            if payment_id:
                update_payment_record(
                    int(payment_id),
                    "completed",
                    provider_session_id=session_payload.get("id", ""),
                    provider_customer_id=session_payload.get("customer", ""),
                    provider_subscription_id=session_payload.get("subscription", ""),
                    metadata_json=json.dumps(metadata),
                )
            if user_id:
                mark_user_membership_paid(
                    int(user_id),
                    membership_price_text(),
                    session_payload.get("customer", "") or "",
                    session_payload.get("subscription", "") or "",
                )
            g.db.commit()
        else:
            flash("Stripe ha devuelto el pago, pero no se pudo verificar automaticamente. El webhook o admin lo terminara de confirmar.", "warning")
        return render_template("payment_success.html", session_id=session_id)

    @app.route("/pago/cancelado")
    def payment_cancelled():
        flash("Pago cancelado. No se ha activado ningun cargo.", "info")
        return redirect(url_for("payment_page", product=request.args.get("product", "trading_premium"), plan=request.args.get("plan", "monthly")))

    @app.route("/stripe/webhook", methods=["POST"])
    def stripe_webhook():
        payload = request.get_data()
        if stripe_webhook_secret() and not stripe_signature_valid(payload, request.headers.get("Stripe-Signature", "")):
            return jsonify({"error": "invalid_signature"}), 400
        try:
            event = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return jsonify({"error": "invalid_payload"}), 400
        if event.get("type") == "checkout.session.completed":
            session_object = (event.get("data") or {}).get("object") or {}
            metadata = session_object.get("metadata") or {}
            payment_id = metadata.get("payment_id") or session_object.get("client_reference_id")
            user_id = metadata.get("user_id")
            if payment_id:
                update_payment_record(
                    int(payment_id),
                    "completed",
                    provider_session_id=session_object.get("id", ""),
                    provider_customer_id=session_object.get("customer", ""),
                    provider_subscription_id=session_object.get("subscription", ""),
                    metadata_json=json.dumps(metadata),
                )
            if user_id:
                mark_user_membership_paid(
                    int(user_id),
                    membership_price_text(),
                    session_object.get("customer", "") or "",
                    session_object.get("subscription", "") or "",
                )
            g.db.commit()
        elif event.get("type") == "customer.subscription.deleted":
            subscription_object = (event.get("data") or {}).get("object") or {}
            mark_user_membership_cancelled(
                subscription_object.get("customer", "") or "",
                subscription_object.get("id", "") or "",
            )
            g.db.commit()
        return jsonify({"received": True})

    @app.route("/entrar", methods=["GET", "POST"])
    def user_login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = g.db.execute(
                text(
                    """
                    SELECT id, email, password_hash
                    FROM users
                    WHERE lower(email) = lower(:email)
                    """
                ),
                {"email": email},
            ).mappings().fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                session["user_email"] = user["email"]
                flash("Has entrado correctamente.", "success")
                return redirect(url_for("index"))
            flash("Email o contrasena incorrectos.", "danger")

        return render_template("user_login.html")

    @app.route("/salir", methods=["POST"])
    def user_logout():
        session.pop("user_id", None)
        session.pop("user_email", None)
        flash("Sesion de usuario cerrada.", "info")
        return redirect(url_for("index"))

    @app.route("/mi-cuenta")
    def account():
        user = current_user()
        if not user:
            flash("Entra con tu cuenta para ver esta zona.", "warning")
            return redirect(url_for("user_login"))
        return render_template("account.html", user=user, stripe_customer_id=user_stripe_customer_id(user))

    @app.route("/cuenta/suscripcion", methods=["POST"])
    def subscription_portal():
        user = current_user()
        if not user:
            flash("Entra con tu cuenta para gestionar la suscripcion.", "warning")
            return redirect(url_for("user_login"))
        portal_session, error = stripe_customer_portal_session(user)
        if portal_session and portal_session.get("url"):
            return redirect(portal_session["url"])
        if error == "missing_customer":
            flash("Todavia no hay una suscripcion de Stripe asociada a esta cuenta.", "warning")
        else:
            flash("No se pudo abrir el portal de Stripe. Revisa la configuracion del portal en Stripe.", "warning")
        return redirect(url_for("account"))

    @app.route("/estrategias/<int:strategy_id>/seleccionar-totalizador", methods=["POST"])
    def user_select_strategy_totalizer(strategy_id):
        user = current_user()
        if not user:
            flash("Entra con tu cuenta para seleccionar estrategias.", "warning")
            return redirect(url_for("user_login"))
        exists = g.db.execute(
            text("SELECT id FROM strategies WHERE id = :id AND is_active = 1"),
            {"id": strategy_id},
        ).fetchone()
        if not exists:
            abort(404)
        selected = 1 if request.form.get("selected") == "on" else 0
        save_user_strategy_selection(user["id"], strategy_id, selected)
        return redirect(url_for("index", _anchor=f"strategy-{strategy_id}"))

    @app.route("/simulador-cuenta", methods=["POST"])
    def save_account_simulator():
        user = current_user()
        if not user:
            flash("Entra con tu cuenta para guardar tu configuracion.", "warning")
            return redirect(url_for("user_login"))
        strategy_ids = {
            int(value)
            for value in request.form.getlist("simulator_strategy")
            if str(value).isdigit()
        }
        save_user_simulator(
            user["id"],
            parse_money_form_value(request.form.get("initial_capital"), 10000.0),
            parse_money_form_value(request.form.get("monthly_contribution"), 300.0),
            parse_date_form_value(request.form.get("start_date")),
            strategy_ids,
        )
        flash("Configuracion guardada.", "success")
        return redirect(url_for("index", _anchor="account-simulator"))

    @app.route("/simulador-cuenta/operaciones")
    def account_simulator_operations():
        user = current_user()
        if not user:
            return jsonify({"error": "login_required"}), 403
        strategies = simulator_strategy_context_for_user(user)
        settings = load_user_simulator_settings(user["id"])
        summary = simulator_summary(settings, strategies)
        offset = parse_int_arg(request.args.get("offset"), 0, 0, max(summary["total_ops"], 0))
        show_all = request.args.get("all") == "1"
        limit = (
            max(summary["total_ops"] - offset, 0)
            if show_all
            else parse_int_arg(request.args.get("limit"), HISTORY_OPERATION_PAGE_SIZE, 1, HISTORY_OPERATION_PAGE_SIZE)
        )
        operations = simulator_operations(settings, strategies, limit=limit, offset=offset)
        loaded_count = min(offset + len(operations), summary["total_ops"])
        return jsonify(
            {
                "operations": operations,
                "loaded_count": loaded_count,
                "total_count": summary["total_ops"],
                "has_more": loaded_count < summary["total_ops"],
                "next_offset": loaded_count,
            }
        )

    @app.route("/")
    def index():
        user = current_user()
        has_full_access = member_has_full_access(user)
        query = """
        SELECT id, name, description, risk_level, signal_frequency,
               historical_return, telegram_url, has_telegram, signals_txt_name,
               python_file, auto_execute, schedule_start_time, schedule_end_time,
               schedule_interval_minutes, run_status, run_message, run_at,
               run_txt_updated, run_returncode, include_in_totalizer, public_visible, is_active,
               closed_operations_count, average_close_duration, success_rate, first_operation_display
        FROM strategies
        WHERE is_active = 1
        ORDER BY created_at DESC
        """
        rows = g.db.execute(text(query)).mappings().fetchall()
        user_totalizer_selection = load_user_totalizer_selection(user["id"]) if user else None
        strategies = []
        for row in rows:
            strategy = strategy_with_signals(row)
            strategy["is_locked"] = not has_full_access and not int(strategy.get("public_visible") or 0)
            if user_totalizer_selection is None:
                strategy["selected_for_totalizer"] = int(strategy.get("include_in_totalizer") or 0)
            else:
                strategy["selected_for_totalizer"] = 1 if strategy["id"] in user_totalizer_selection else 0
            strategies.append(strategy)
        refresh_strategy_balances_from_operations(strategies)
        strategies.sort(
            key=lambda strategy: (
                -int(strategy.get("selected_for_totalizer") or 0),
                -int(strategy.get("public_visible") or 0),
                -int(strategy.get("signals_count") or 0),
                strategy.get("name", ""),
            )
        )
        community_url = os.environ.get("COMMUNITY_URL")
        if not community_url and strategies:
            community_url = strategies[0]["telegram_url"]
        donation_url = os.environ.get("DONATION_URL", "").strip()
        top_assets = top_money_volume_assets()
        news_preview = relevant_market_news(limit=60, days=3)
        totalizer = build_totalizer(strategies)
        simulator = load_user_simulator(user["id"], strategies) if user else default_simulator_state(strategies)
        if user and request.args.get("simulator_ops") == "1":
            simulator["operations"] = simulator_operations(simulator["settings"], simulator["strategies"])
            simulator["show_operations"] = True
        return render_template(
            "index.html",
            strategies=strategies,
            totalizer=totalizer,
            simulator=simulator,
            top_money_volume_assets=top_assets["rows"],
            top_money_volume_updated_at=top_assets["updated_at"],
            top_money_volume_source=top_assets["source"],
            operation_status_pilots=operation_status_pilots(),
            upload_file_statuses=local_upload_file_statuses(),
            market_news=news_preview["rows"],
            market_news_updated_at=news_preview["updated_at"],
            community_url=community_url,
            donation_url=donation_url,
            page_refreshed_at=datetime.now(MADRID_TZ).strftime("%H:%M:%S %d/%m/%y"),
            is_public_view=not has_full_access,
            user_totalizer_enabled=user is not None,
        )

    @app.route("/mobile")
    def mobile_index():
        user = current_user()
        has_full_access = member_has_full_access(user)
        query = """
        SELECT id, name, description, risk_level, signal_frequency,
               historical_return, telegram_url, has_telegram, signals_txt_name,
               python_file, auto_execute, schedule_start_time, schedule_end_time,
               schedule_interval_minutes, run_status, run_message, run_at,
               run_txt_updated, run_returncode, include_in_totalizer, public_visible, is_active
        FROM strategies
        WHERE is_active = 1
        ORDER BY created_at DESC
        """
        rows = g.db.execute(text(query)).mappings().fetchall()
        user_totalizer_selection = load_user_totalizer_selection(user["id"]) if user else None
        strategies = []
        for row in rows:
            strategy = strategy_with_signals(row)
            strategy["is_locked"] = not has_full_access and not int(strategy.get("public_visible") or 0)
            if user_totalizer_selection is None:
                strategy["selected_for_totalizer"] = int(strategy.get("include_in_totalizer") or 0)
            else:
                strategy["selected_for_totalizer"] = 1 if strategy["id"] in user_totalizer_selection else 0
            strategies.append(strategy)
        refresh_strategy_balances_from_operations(strategies)
        strategies.sort(
            key=lambda strategy: (
                -int(strategy.get("selected_for_totalizer") or 0),
                -int(strategy.get("public_visible") or 0),
                -int(strategy.get("signals_count") or 0),
                strategy.get("name", ""),
            )
        )
        top_assets = top_money_volume_assets()
        news_preview = relevant_market_news(limit=20, days=3)
        totalizer = build_totalizer(strategies)
        return render_template(
            "mobile/index.html",
            strategies=strategies,
            totalizer=totalizer,
            top_money_volume_assets=top_assets["rows"],
            top_money_volume_updated_at=top_assets["updated_at"],
            market_news=news_preview["rows"],
            market_news_updated_at=news_preview["updated_at"],
            page_refreshed_at=datetime.now(MADRID_TZ).strftime("%H:%M:%S %d/%m/%y"),
            is_public_view=not has_full_access,
        )

    @app.route("/mobile/estrategia/<int:strategy_id>/avisos")
    def mobile_strategy_signals(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not can_view_strategy(strategy):
            flash("Crea una cuenta para ver los avisos completos.", "warning")
            return redirect(url_for("user_login"))
        signals = read_strategy_signals(strategy["signals_txt_name"])
        attach_simulated_operations_to_signals(strategy, signals)
        grouped_mode = strategy_uses_grouped_operations(strategy)
        signal_groups = build_signal_groups(signals, grouped_mode=grouped_mode)
        return render_template(
            "mobile/signals.html",
            strategy=strategy,
            signals=signals,
            signal_groups=signal_groups,
            grouped_mode=grouped_mode,
            page_refreshed_at=datetime.now(MADRID_TZ).strftime("%H:%M:%S %d/%m/%y"),
        )

    @app.route("/mobile/manifest.json")
    def mobile_manifest():
        return jsonify(
            {
                "name": "Code Markets Mobile",
                "short_name": "CodeMarkets",
                "start_url": url_for("mobile_index"),
                "scope": "/mobile",
                "display": "standalone",
                "background_color": "#012456",
                "theme_color": "#012456",
                "description": "Vista movil de modelos de mercado y avisos automaticos.",
            }
        )

    @app.route("/mobile/service-worker.js")
    def mobile_service_worker():
        body = """
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", () => {});
""".strip()
        return app.response_class(body, mimetype="application/javascript")

    @app.route("/terminal-feed")
    def terminal_feed():
        return jsonify(terminal_feed_payload())

    @app.route("/noticias")
    def market_news_page():
        news_data = relevant_market_news(limit=500)
        return render_template(
            "market_news.html",
            news_items=news_data["rows"],
            updated_at=news_data["updated_at"],
        )

    @app.route("/estrategia/<int:strategy_id>/diagnostico/<path:symbol>")
    def strategy_diagnostic(strategy_id, symbol):
        strategy = get_strategy_or_404(strategy_id)
        if not can_view_strategy(strategy):
            flash("Crea una cuenta para acceder a todas las estrategias.", "warning")
            return redirect(url_for("user_login"))
        signals = read_strategy_signals(strategy["signals_txt_name"])
        normalized_symbol = normalize_signal_symbol(symbol)
        selected_key = request.args.get("key", "")
        signal = next(
            (
                item
                for item in signals
                if (
                    item.get("signal_key") == selected_key
                    if selected_key
                    else normalize_signal_symbol(item.get("symbol", "")) == normalized_symbol
                )
            ),
            None,
        )
        if signal is None:
            abort(404)

        attach_v2_diagnostics_to_signal(signal, strategy)
        diagnostic = build_signal_diagnostic(strategy, signal)
        operation = signal.get("open_operation") or simulated_operation_for_signal(strategy, signal)
        return render_template(
            "strategy_diagnostic.html",
            strategy=strategy,
            signal=signal,
            diagnostic=diagnostic,
            operation=operation,
        )

    @app.route("/estrategia/<int:strategy_id>/avisos")
    def strategy_signals(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not can_view_strategy(strategy):
            flash("Crea una cuenta para ver los avisos completos.", "warning")
            return redirect(url_for("user_login"))
        signals = read_strategy_signals(strategy["signals_txt_name"])
        attach_simulated_operations_to_signals(strategy, signals)
        grouped_mode = strategy_uses_grouped_operations(strategy)
        signal_groups = build_signal_groups(signals, grouped_mode=grouped_mode)

        selected_symbol = normalize_signal_symbol(request.args.get("symbol", ""))
        selected_key = request.args.get("key", "")
        selected_signal = None
        if selected_key:
            selected_signal = next(
                (
                    item
                    for item in signals
                    if item.get("signal_key") == selected_key
                ),
                None,
            )
        if selected_symbol:
            selected_signal = next(
                (
                    item
                    for item in signals
                    if normalize_signal_symbol(item.get("symbol", "")) == selected_symbol
                ),
                None,
            ) if selected_signal is None else selected_signal
        if selected_signal is None and signals:
            selected_signal = signals[0]

        diagnostic = None
        operation = None
        selected_group = None
        if selected_signal is not None:
            attach_v2_diagnostics_to_signal(selected_signal, strategy)
            diagnostic = build_signal_diagnostic(strategy, selected_signal)
            operation = selected_signal.get("open_operation") or simulated_operation_for_signal(strategy, selected_signal)
            selected_group = signal_group_for_signal(signal_groups, selected_signal, grouped_mode)

        return render_template(
            "strategy_signals.html",
            strategy=strategy,
            signals=signals,
            signal_groups=signal_groups,
            grouped_mode=grouped_mode,
            selected_signal=selected_signal,
            selected_group=selected_group,
            selected_symbol=normalize_signal_symbol(selected_signal.get("symbol", "")) if selected_signal else "",
            diagnostic=diagnostic,
            operation=operation,
            selected_key=selected_signal.get("signal_key", "") if selected_signal else "",
        )

    @app.route("/estrategia/<int:strategy_id>/historial")
    def strategy_closed_operations(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not can_view_strategy(strategy):
            flash("Crea una cuenta para ver el historial completo.", "warning")
            return redirect(url_for("user_login"))
        txt_name = strategy.get("signals_txt_name", "")
        total_closed_count = int(strategy.get("closed_operations_count") or 0)
        show_all = request.args.get("all") == "1"
        requested_limit = parse_int_arg(request.args.get("limit"), HISTORY_OPERATION_PAGE_SIZE, HISTORY_OPERATION_PAGE_SIZE, 5000)
        operation_limit = None if show_all else requested_limit
        operations = closed_operations_for_strategy(txt_name, limit=operation_limit)
        return_text = strategy.get("historical_return", "")
        total_profit = parse_profit_usd(return_text) if has_profit_usd(return_text) else sum(parse_display_float(operation.get("profit_usd")) for operation in operations)
        capital_base = parse_strategy_capital_usd(return_text)
        total_pct = parse_return_percent(return_text)
        average_pct = (
            sum(parse_display_float(operation.get("profit_pct")) for operation in operations) / len(operations)
            if operations
            else 0.0
        )
        winning_count = sum(1 for operation in operations if parse_display_float(operation.get("profit_usd")) > 0)
        losing_count = sum(1 for operation in operations if parse_display_float(operation.get("profit_usd")) < 0)
        flat_count = max(0, len(operations) - winning_count - losing_count)
        history_totalizer = {
            "count": total_closed_count,
            "loaded_count": len(operations),
            "profit_display": format_signed_money_usd(total_profit),
            "profit_class": profit_color_class(total_profit),
            "invested_display": format_money_usd(capital_base),
            "current_capital_display": format_money_usd(capital_base + total_profit),
            "pct_display": f"{total_pct:+.2f}%",
            "pct_class": profit_color_class(total_pct),
            "max_open_operations": parse_max_open_operations(return_text),
            "average_pct_display": f"{average_pct:+.2f}%",
            "average_pct_class": profit_color_class(average_pct),
            "winning_count": winning_count,
            "losing_count": losing_count,
            "flat_count": flat_count,
        }
        return render_template(
            "strategy_closed_operations.html",
            strategy=strategy,
            operations=operations,
            history_totalizer=history_totalizer,
            history_pagination={
                "page_size": HISTORY_OPERATION_PAGE_SIZE,
                "limit": operation_limit,
                "next_limit": min(requested_limit + HISTORY_OPERATION_PAGE_SIZE, total_closed_count),
                "show_all": show_all,
                "has_more": (not show_all) and len(operations) < total_closed_count,
                "total_count": total_closed_count,
            },
        )

    @app.route("/estrategia/<int:strategy_id>/historial/datos")
    def strategy_closed_operations_data(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not can_view_strategy(strategy):
            return jsonify({"error": "login_required"}), 403
        txt_name = strategy.get("signals_txt_name", "")
        total_closed_count = int(strategy.get("closed_operations_count") or 0)
        offset = parse_int_arg(request.args.get("offset"), 0, 0, max(total_closed_count, 0))
        show_all = request.args.get("all") == "1"
        limit = (
            max(total_closed_count - offset, 0)
            if show_all
            else parse_int_arg(request.args.get("limit"), HISTORY_OPERATION_PAGE_SIZE, 1, HISTORY_OPERATION_PAGE_SIZE)
        )
        operations = closed_operations_for_strategy(txt_name, limit=limit, offset=offset)
        loaded_count = min(offset + len(operations), total_closed_count)
        return jsonify(
            {
                "operations": operations,
                "loaded_count": loaded_count,
                "total_count": total_closed_count,
                "has_more": loaded_count < total_closed_count,
                "next_offset": loaded_count,
            }
        )

    def parse_int_arg(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @app.route("/estrategia/<int:strategy_id>/funcionamiento")
    def strategy_details(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not can_view_strategy(strategy):
            flash("Crea una cuenta para ver el detalle de esta estrategia.", "warning")
            return redirect(url_for("user_login"))
        return render_template(
            "strategy_details.html",
            strategy=strategy,
            explanation=strategy_explanation_for(strategy),
        )

    @app.route("/filtrado-activos")
    def asset_filter():
        filters = {
            "month_window": int(request.args.get("month_window", 1)),
            "min_money_volume": int(request.args.get("min_money_volume", 0)),
            "day_volume_window": int(request.args.get("day_volume_window", 1)),
            "week_volume_window": int(request.args.get("week_volume_window", 1)),
            "limit": int(request.args.get("limit", 10)),
            "sector": request.args.get("sector", "Todos"),
            "market": request.args.get("market", "Todos"),
            "data_source": request.args.get("data_source", "database"),
            "sort_by": request.args.get("sort_by", "money_volume_selected"),
        }
        assets = load_universe_assets()
        csv_total = len(assets)
        database_universe_total = universe_count()
        snapshots_total = snapshot_count()
        sectors = available_sectors(assets)
        markets = available_markets(assets)
        results, data_source, universe_total = filter_assets(filters, assets)
        filter_labels = {
            "money_volume": (
                f"Media volumen monetario {filters['month_window']} "
                f"mes{'es' if filters['month_window'] > 1 else ''}"
            ),
            "day_volume": (
                f"Volumen ultimos {filters['day_volume_window']} "
                f"dia{'s' if filters['day_volume_window'] > 1 else ''}"
            ),
            "week_volume": (
                f"Volumen ultimas {filters['week_volume_window']} "
                f"semana{'s' if filters['week_volume_window'] > 1 else ''}"
            ),
            "ratio": (
                f"Ratio volumen {filters['day_volume_window']}d / "
                f"media {filters['month_window']}m"
            ),
        }
        sort_options = [
            ("money_volume_selected", filter_labels["money_volume"]),
            ("day_money_volume_selected", filter_labels["day_volume"]),
            ("week_money_volume_selected", filter_labels["week_volume"]),
            ("day_to_month_volume_ratio", filter_labels["ratio"]),
            ("price", "Precio"),
        ]
        return render_template(
            "asset_filter.html",
            filters=filters,
            filter_labels=filter_labels,
            sort_options=sort_options,
            results=results,
            sectors=sectors,
            markets=markets,
            data_source=data_source,
            universe_total=universe_total,
            csv_total=csv_total,
            database_universe_total=database_universe_total,
            snapshots_total=snapshots_total,
            csv_updated_at=csv_updated_at(),
        )

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
            text(
            """
            SELECT id, name, description, risk_level, signal_frequency,
                   historical_return, telegram_url, has_telegram, signals_txt_name,
                   python_file, auto_execute, schedule_start_time, schedule_end_time,
                   schedule_interval_minutes, schedule_last_status, schedule_last_message,
                   schedule_last_run_at, run_status, run_message, run_at,
                   run_txt_updated, run_returncode, include_in_totalizer,
                   public_visible, run_locally, is_active, created_at
            FROM strategies
            ORDER BY is_active DESC, created_at DESC
            """
            )
        ).mappings().fetchall()
        strategies = [strategy_with_signals(row) for row in strategies]
        users = g.db.execute(
            text(
                """
                SELECT id, email, name, has_access, payment_status, membership_plan,
                       membership_amount, membership_started_at, membership_expires_at,
                       admin_notes, created_at
                FROM users
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
        ).mappings().fetchall()
        return render_template(
            "admin/dashboard.html",
            strategies=strategies,
            users=users,
            active_visitors=active_visitor_count(),
            schedules=load_automation_schedules(),
            scheduler_tasks=SCHEDULER_TASKS,
            weekdays=WEEKDAYS,
            strategy_failures=load_strategy_failures(),
        )

    @app.route("/admin/system")
    @login_required
    def admin_system():
        return render_template(
            "admin/system.html",
            database=database_status(),
        )

    @app.route("/admin/users/<int:user_id>/toggle-access", methods=["POST"])
    @login_required
    def user_toggle_access(user_id):
        user = g.db.execute(
            text("SELECT has_access FROM users WHERE id = :id"),
            {"id": user_id},
        ).mappings().fetchone()
        if user is None:
            abort(404)
        next_access = 0 if int(user["has_access"] or 0) else 1
        next_status = "active" if next_access else "blocked"
        g.db.execute(
            text(
                """
                UPDATE users
                SET has_access = :has_access,
                    payment_status = :payment_status
                WHERE id = :id
                """
            ),
            {
                "has_access": next_access,
                "payment_status": next_status,
                "id": user_id,
            },
        )
        g.db.commit()
        flash("Acceso de usuario actualizado.", "success")
        return redirect(url_for("admin_dashboard", _anchor="admin-users"))

    @app.route("/admin/users/<int:user_id>/update", methods=["POST"])
    @login_required
    def admin_user_update(user_id):
        existing = g.db.execute(
            text("SELECT id FROM users WHERE id = :id"),
            {"id": user_id},
        ).mappings().fetchone()
        if existing is None:
            abort(404)

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        payment_status = request.form.get("payment_status", "registered").strip() or "registered"
        membership_plan = request.form.get("membership_plan", "Miembro").strip() or "Miembro"
        membership_amount = request.form.get("membership_amount", membership_price_text()).strip()
        membership_started_at = empty_to_none(request.form.get("membership_started_at"))
        membership_expires_at = empty_to_none(request.form.get("membership_expires_at"))
        admin_notes = request.form.get("admin_notes", "").strip()
        has_access = 1 if request.form.get("has_access") == "on" else 0

        if not email or "@" not in email:
            flash("Email de usuario no valido.", "danger")
            return redirect(url_for("admin_dashboard", _anchor="admin-users"))

        duplicated = g.db.execute(
            text(
                """
                SELECT id
                FROM users
                WHERE lower(email) = lower(:email)
                  AND id <> :id
                """
            ),
            {"email": email, "id": user_id},
        ).mappings().fetchone()
        if duplicated:
            flash("Ese email ya pertenece a otro usuario.", "warning")
            return redirect(url_for("admin_dashboard", _anchor="admin-users"))

        g.db.execute(
            text(
                """
                UPDATE users
                SET name = :name,
                    email = :email,
                    has_access = :has_access,
                    payment_status = :payment_status,
                    membership_plan = :membership_plan,
                    membership_amount = :membership_amount,
                    membership_started_at = :membership_started_at,
                    membership_expires_at = :membership_expires_at,
                    admin_notes = :admin_notes
                WHERE id = :id
                """
            ),
            {
                "name": name,
                "email": email,
                "has_access": has_access,
                "payment_status": payment_status,
                "membership_plan": membership_plan,
                "membership_amount": membership_amount,
                "membership_started_at": membership_started_at,
                "membership_expires_at": membership_expires_at,
                "admin_notes": admin_notes,
                "id": user_id,
            },
        )
        g.db.commit()
        flash("Usuario actualizado.", "success")
        return redirect(url_for("admin_dashboard", _anchor=f"user-{user_id}"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @login_required
    def admin_user_delete(user_id):
        existing = g.db.execute(
            text("SELECT * FROM users WHERE id = :id"),
            {"id": user_id},
        ).mappings().fetchone()
        if existing is None:
            abort(404)

        subscription_id = user_stripe_subscription_id(existing)
        if subscription_id:
            cancelled, cancel_response = stripe_cancel_subscription(subscription_id)
            if not cancelled:
                flash(
                    "No se ha eliminado el usuario porque no se pudo cancelar su suscripcion en Stripe.",
                    "danger",
                )
                return redirect(url_for("admin_dashboard", _anchor=f"user-{user_id}"))
            g.db.execute(
                text(
                    """
                    UPDATE payments
                    SET status = 'cancelled',
                        metadata_json = :metadata_json,
                        updated_at = :updated_at
                    WHERE user_id = :user_id
                      AND provider = 'stripe'
                    """
                ),
                {
                    "user_id": user_id,
                    "metadata_json": json.dumps({"admin_delete_cancel": cancel_response}),
                    "updated_at": datetime.now(UTC).replace(tzinfo=None),
                },
            )

        cleanup_tables = (
            "user_strategy_selections",
            "user_simulator_settings",
            "user_simulator_strategies",
            "payments",
        )
        for table_name in cleanup_tables:
            g.db.execute(
                text(f"DELETE FROM {table_name} WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        g.db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        g.db.commit()

        if session.get("user_id") == user_id:
            session.pop("user_id", None)
            session.pop("user_email", None)
            session.pop("user_name", None)

        if subscription_id:
            flash(f"Usuario eliminado y suscripcion Stripe cancelada: {existing['email']}", "success")
        else:
            flash(f"Usuario eliminado: {existing['email']}", "success")
        return redirect(url_for("admin_dashboard", _anchor="admin-users"))

    @app.route("/admin/users/create", methods=["POST"])
    @login_required
    def admin_user_create():
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        has_access = 1 if request.form.get("has_access") == "on" else 0
        payment_status = request.form.get("payment_status", "").strip()
        if not payment_status:
            payment_status = "active" if has_access else "manual_pending"

        if not email or "@" not in email:
            flash("Introduce un email valido para crear el usuario.", "danger")
            return redirect(url_for("admin_dashboard", _anchor="admin-users"))
        if len(password) < 6:
            flash("La contrasena provisional debe tener al menos 6 caracteres.", "danger")
            return redirect(url_for("admin_dashboard", _anchor="admin-users"))

        existing = g.db.execute(
            text("SELECT id FROM users WHERE lower(email) = lower(:email)"),
            {"email": email},
        ).mappings().fetchone()
        if existing:
            flash("Ese email ya existe como usuario.", "warning")
            return redirect(url_for("admin_dashboard", _anchor="admin-users"))

        g.db.execute(
            text(
                """
                INSERT INTO users
                (email, password_hash, name, has_access, payment_status,
                 membership_plan, membership_amount, age_confirmed, risk_accepted, accepted_terms_at)
                VALUES (:email, :password_hash, :name, :has_access, :payment_status,
                        :membership_plan, :membership_amount, 1, 1, :accepted_terms_at)
                """
            ),
            {
                "email": email,
                "password_hash": generate_password_hash(password),
                "name": name,
                "has_access": has_access,
                "payment_status": payment_status,
                "membership_plan": request.form.get("membership_plan", "Miembro").strip() or "Miembro",
                "membership_amount": request.form.get("membership_amount", membership_price_text()).strip(),
                "accepted_terms_at": datetime.now(UTC).replace(tzinfo=None),
            },
        )
        g.db.commit()
        flash("Usuario creado manualmente.", "success")
        return redirect(url_for("admin_dashboard", _anchor="admin-users"))

    @app.route("/admin/market-data/update", methods=["POST"])
    @login_required
    def admin_market_data_update():
        full_update = request.form.get("full_update") == "1"
        result = update_market_data(full=full_update)
        session["last_market_update"] = result
        if result["ok"]:
            flash(
                f"Datos de mercado actualizados correctamente. {result.get('saved_rows', 0)} activos guardados.",
                "success",
            )
        else:
            flash(
                f"No se pudieron actualizar los datos. {result.get('last_error', '')}",
                "danger",
            )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/assets/update-csv", methods=["POST"])
    @login_required
    def admin_assets_update_csv():
        rows, source = build_assets_from_alpaca()
        write_assets(rows)
        save_universe_assets(rows)
        flash(
            f"CSV y universo de activos actualizados correctamente: {len(rows)} activos. Fuente: {source}.",
            "success",
        )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/update", methods=["POST"])
    @login_required
    def admin_schedules_update():
        for task_name in SCHEDULER_TASKS:
            enabled = 1 if request.form.get(f"{task_name}_enabled") == "1" else 0
            start_time = request.form.get(f"{task_name}_start_time", "15:30").strip()
            runs_per_day = parse_schedule_int(
                request.form.get(f"{task_name}_runs_per_day"),
                default=1,
                minimum=1,
                maximum=24,
            )
            interval_minutes = parse_schedule_int(
                request.form.get(f"{task_name}_interval_minutes"),
                default=60,
                minimum=1,
                maximum=1440,
            )
            weekdays = normalize_weekdays(
                request.form.getlist(f"{task_name}_weekdays")
            )
            if not valid_schedule_time(start_time):
                flash(f"Hora no valida para {SCHEDULER_TASKS[task_name]}. Usa formato HH:MM.", "danger")
                return redirect(url_for("admin_dashboard"))

            g.db.execute(
                text(
                    """
                    UPDATE automation_schedules
                    SET is_enabled = :is_enabled,
                        start_time = :start_time,
                        runs_per_day = :runs_per_day,
                        interval_minutes = :interval_minutes,
                        weekdays = :weekdays,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_name = :task_name
                    """
                ),
                {
                    "task_name": task_name,
                    "is_enabled": enabled,
                    "start_time": start_time,
                    "runs_per_day": runs_per_day,
                    "interval_minutes": interval_minutes,
                    "weekdays": weekdays,
                },
            )
        g.db.commit()
        flash("Programacion automatica guardada.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/<task_name>/run-now", methods=["POST"])
    @login_required
    def admin_schedule_run_now(task_name):
        if task_name not in SCHEDULER_TASKS:
            abort(404)
        run_key = f"manual|{task_name}|{datetime.now(MADRID_TZ).isoformat()}"
        record_schedule_running(task_name, run_key)
        launch_scheduler_task_in_background(task_name, run_key)
        flash(f"{SCHEDULER_TASKS[task_name]} iniciado. Refresca el panel para ver si termina en OK o ERROR.", "info")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/schedules/<task_name>/clear-running", methods=["POST"])
    @login_required
    def admin_schedule_clear_running(task_name):
        if task_name not in SCHEDULER_TASKS:
            abort(404)
        g.db.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_status = 'ERROR',
                    last_message = :message
                WHERE task_name = :task_name
                  AND last_status = 'RUNNING'
                """
            ),
            {
                "task_name": task_name,
                "message": "Ejecucion marcada como bloqueada y limpiada manualmente desde admin.",
            },
        )
        g.db.commit()
        flash(f"Estado RUNNING limpiado para {SCHEDULER_TASKS[task_name]}.", "warning")
        return redirect(url_for("admin_dashboard"))

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
            return save_strategy_description(strategy_id)

        return render_template(
            "admin/description_form.html",
            strategy=strategy,
            title="Descripcion de estrategia",
            action=url_for("strategy_edit", strategy_id=strategy_id),
        )

    @app.route("/admin/strategies/<int:strategy_id>/toggle", methods=["POST"])
    @login_required
    def strategy_toggle(strategy_id):
        strategy = get_strategy_or_404(strategy_id)
        next_state = 0 if strategy["is_active"] else 1
        g.db.execute(
            text("UPDATE strategies SET is_active = :is_active WHERE id = :id"),
            {"is_active": next_state, "id": strategy_id},
        )
        g.db.commit()
        flash("Estado actualizado.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/toggle-local", methods=["POST"])
    @login_required
    def strategy_toggle_local(strategy_id):
        strategy = get_strategy_or_404(strategy_id)
        next_state = 0 if strategy["run_locally"] else 1
        g.db.execute(
            text("UPDATE strategies SET run_locally = :run_locally WHERE id = :id"),
            {"run_locally": next_state, "id": strategy_id},
        )
        g.db.commit()
        flash("Ejecucion local actualizada.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/quick-update", methods=["POST"])
    @login_required
    def strategy_quick_update(strategy_id):
        get_strategy_or_404(strategy_id)
        name = request.form.get("name", "").strip()
        risk_level = request.form.get("risk_level", "Medio")
        signal_frequency = request.form.get("signal_frequency", "").strip()
        historical_return = request.form.get("historical_return", "").strip()
        telegram_url = request.form.get("telegram_url", "").strip()
        has_telegram = 1 if request.form.get("has_telegram") == "on" else 0
        signals_txt_name = request.form.get("signals_txt_name", "").strip()
        python_file = request.form.get("python_file", "").strip()
        include_in_totalizer = 1 if request.form.get("include_in_totalizer") == "on" else 0
        public_visible = 1 if request.form.get("public_visible") == "on" else 0
        is_active = 1 if request.form.get("is_active") == "on" else 0
        run_locally = 1 if request.form.get("run_locally") == "on" else 0

        errors = []
        if not name:
            errors.append("El nombre es obligatorio.")
        if risk_level not in {"Bajo", "Medio", "Alto"}:
            errors.append("El nivel de riesgo no es valido.")
        if has_telegram and not telegram_url.startswith(
            ("https://t.me/", "http://t.me/", "https://telegram.me/")
        ):
            errors.append("Usa un enlace valido de Telegram o desmarca Tiene Telegram.")
        if signals_txt_name and not valid_txt_name(signals_txt_name):
            errors.append("El nombre del TXT debe ser un archivo .txt sin carpetas.")
        if python_file and not valid_python_filename(python_file):
            errors.append("El archivo Python debe ser un .py sin carpetas.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return redirect(url_for("admin_dashboard", _anchor=f"strategy-{strategy_id}"))

        g.db.execute(
            text(
                """
                UPDATE strategies
                SET name = :name,
                    risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = :historical_return,
                    telegram_url = :telegram_url,
                    has_telegram = :has_telegram,
                    signals_txt_name = :signals_txt_name,
                    python_file = :python_file,
                    include_in_totalizer = :include_in_totalizer,
                    public_visible = :public_visible,
                    is_active = :is_active,
                    run_locally = :run_locally
                WHERE id = :id
                """
            ),
            {
                "name": name,
                "risk_level": risk_level,
                "signal_frequency": signal_frequency,
                "historical_return": historical_return,
                "telegram_url": telegram_url,
                "has_telegram": has_telegram,
                "signals_txt_name": signals_txt_name,
                "python_file": python_file,
                "include_in_totalizer": include_in_totalizer,
                "public_visible": public_visible,
                "is_active": is_active,
                "run_locally": run_locally,
                "id": strategy_id,
            },
        )
        g.db.commit()
        flash("Estrategia actualizada desde el panel.", "success")
        return redirect(url_for("admin_dashboard", _anchor=f"strategy-{strategy_id}"))

    @app.route("/admin/strategies/bulk-update", methods=["POST"])
    @login_required
    def strategies_bulk_update():
        strategy_ids = []
        for raw_id in request.form.getlist("strategy_id"):
            try:
                strategy_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if strategy_id not in strategy_ids:
                strategy_ids.append(strategy_id)

        if not strategy_ids:
            flash("No hay estrategias para guardar.", "warning")
            return redirect(url_for("admin_dashboard"))

        errors = []
        updates = []
        existing_ids = {
            int(row["id"])
            for row in g.db.execute(
                text("SELECT id FROM strategies WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
                {"ids": strategy_ids},
            ).mappings().fetchall()
        }
        for strategy_id in strategy_ids:
            if strategy_id not in existing_ids:
                errors.append(f"Estrategia {strategy_id}: no existe.")
                continue
            data = strategy_bulk_update_payload(strategy_id)
            row_errors = validate_strategy_admin_payload(data)
            errors.extend([f"{data['name'] or 'Estrategia ' + str(strategy_id)}: {error}" for error in row_errors])
            updates.append({**data, "id": strategy_id})

        if errors:
            for error in errors[:8]:
                flash(error, "danger")
            if len(errors) > 8:
                flash(f"{len(errors) - 8} errores mas. No se ha guardado ningun cambio.", "danger")
            return redirect(url_for("admin_dashboard"))

        for data in updates:
            g.db.execute(
                text(
                    """
                    UPDATE strategies
                    SET name = :name,
                        risk_level = :risk_level,
                        signal_frequency = :signal_frequency,
                        historical_return = :historical_return,
                        telegram_url = :telegram_url,
                        has_telegram = :has_telegram,
                        signals_txt_name = :signals_txt_name,
                        python_file = :python_file,
                        include_in_totalizer = :include_in_totalizer,
                        public_visible = :public_visible,
                        is_active = :is_active,
                        run_locally = :run_locally
                    WHERE id = :id
                    """
                ),
                data,
            )
        g.db.commit()
        flash(f"Guardadas {len(updates)} estrategias.", "success")
        return redirect(url_for("admin_dashboard"))

    def strategy_bulk_update_payload(strategy_id):
        prefix = f"{strategy_id}__"
        return {
            "name": request.form.get(prefix + "name", "").strip(),
            "risk_level": request.form.get(prefix + "risk_level", "Medio"),
            "signal_frequency": request.form.get(prefix + "signal_frequency", "").strip(),
            "historical_return": request.form.get(prefix + "historical_return", "").strip(),
            "telegram_url": request.form.get(prefix + "telegram_url", "").strip(),
            "has_telegram": 1 if request.form.get(prefix + "has_telegram") == "on" else 0,
            "signals_txt_name": request.form.get(prefix + "signals_txt_name", "").strip(),
            "python_file": request.form.get(prefix + "python_file", "").strip(),
            "include_in_totalizer": 1 if request.form.get(prefix + "include_in_totalizer") == "on" else 0,
            "public_visible": 1 if request.form.get(prefix + "public_visible") == "on" else 0,
            "is_active": 1 if request.form.get(prefix + "is_active") == "on" else 0,
            "run_locally": 1 if request.form.get(prefix + "run_locally") == "on" else 0,
        }

    def validate_strategy_admin_payload(data):
        errors = []
        if not data["name"]:
            errors.append("El nombre es obligatorio.")
        if data["risk_level"] not in {"Bajo", "Medio", "Alto"}:
            errors.append("El nivel de riesgo no es valido.")
        if data["has_telegram"] and not data["telegram_url"].startswith(
            ("https://t.me/", "http://t.me/", "https://telegram.me/")
        ):
            errors.append("Usa un enlace valido de Telegram o desmarca Tiene Telegram.")
        if data["signals_txt_name"] and not valid_txt_name(data["signals_txt_name"]):
            errors.append("El nombre del TXT debe ser un archivo .txt sin carpetas.")
        if data["python_file"] and not valid_python_filename(data["python_file"]):
            errors.append("El archivo Python debe ser un .py sin carpetas.")
        return errors

    @app.route("/admin/strategies/deactivate-all", methods=["POST"])
    @login_required
    def strategies_deactivate_all():
        g.db.execute(text("UPDATE strategies SET is_active = 0 WHERE is_active = 1"))
        g.db.commit()
        flash("Todas las estrategias han sido ocultadas/desactivadas en la web. La ejecucion local no cambia.", "warning")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/apply-recommended-schedules", methods=["POST"])
    @login_required
    def strategies_apply_recommended_schedules():
        apply_recommended_strategy_schedules(g.db)
        g.db.commit()
        flash("Horarios recomendados aplicados a las estrategias.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/clear-failures", methods=["POST"])
    @login_required
    def strategies_clear_failures():
        try:
            DEFAULT_STRATEGY_STATUS_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        g.db.execute(
            text(
                """
                UPDATE automation_schedules
                SET last_message = '',
                    last_status = '',
                    last_run_key = ''
                WHERE task_name = 'strategies'
                """
            )
        )
        g.db.execute(
            text(
                """
                UPDATE strategies
                SET schedule_last_message = '',
                    schedule_last_status = '',
                    schedule_last_run_key = '',
                    run_status = '',
                    run_message = '',
                    run_txt_updated = 0,
                    run_returncode = NULL
                """
            )
        )
        g.db.commit()
        flash("Fallos de estrategias limpiados.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/run", methods=["POST"])
    @login_required
    def strategy_run_now(strategy_id):
        strategy = dict(get_strategy_or_404(strategy_id))
        if not strategy["is_active"]:
            flash("Activa la estrategia antes de ejecutarla.", "warning")
            return redirect(url_for("admin_dashboard"))
        run_key = f"manual|strategy|{strategy_id}|{datetime.now(MADRID_TZ).isoformat()}"
        record_strategy_schedule_running(strategy_id, run_key)
        launch_strategy_task_in_background(strategy, run_key)
        flash(f"{strategy['name']} iniciada. Refresca el panel para ver el resultado.", "info")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/strategies/<int:strategy_id>/delete", methods=["POST"])
    @login_required
    def strategy_delete(strategy_id):
        get_strategy_or_404(strategy_id)
        g.db.execute(text("DELETE FROM strategies WHERE id = :id"), {"id": strategy_id})
        g.db.commit()
        flash("Estrategia eliminada.", "info")
        return redirect(url_for("admin_dashboard"))

    def get_strategy_or_404(strategy_id):
        strategy = g.db.execute(
            text("SELECT * FROM strategies WHERE id = :id"), {"id": strategy_id}
        ).mappings().fetchone()
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
        has_telegram = 1 if request.form.get("has_telegram") == "on" else 0
        signals_txt_name = request.form.get("signals_txt_name", "").strip()
        python_file = request.form.get("python_file", "").strip()
        auto_execute = 1 if request.form.get("auto_execute") == "on" else 0
        schedule_start_time = request.form.get("schedule_start_time", "15:30").strip()
        schedule_end_time = request.form.get("schedule_end_time", "21:30").strip()
        schedule_interval_minutes = parse_schedule_int(
            request.form.get("schedule_interval_minutes"),
            default=30,
            minimum=1,
            maximum=1440,
        )
        include_in_totalizer = 1 if request.form.get("include_in_totalizer") == "on" else 0
        public_visible = 1 if request.form.get("public_visible") == "on" else 0
        is_active = 1 if request.form.get("is_active") == "on" else 0

        errors = []
        if not name:
            errors.append("El nombre es obligatorio.")
        if risk_level not in {"Bajo", "Medio", "Alto"}:
            errors.append("El nivel de riesgo no es valido.")
        if has_telegram and not telegram_url.startswith(
            ("https://t.me/", "http://t.me/", "https://telegram.me/")
        ):
            errors.append("Usa un enlace valido de Telegram o desmarca Tiene canal de Telegram.")
        if signals_txt_name and not valid_txt_name(signals_txt_name):
            errors.append("El nombre del TXT debe ser un archivo .txt sin carpetas.")
        if python_file and not valid_python_filename(python_file):
            errors.append("El archivo Python debe ser un .py sin carpetas.")
        if auto_execute and not python_file:
            errors.append("Para ejecutar automaticamente debes indicar el archivo Python.")
        if not valid_schedule_time(schedule_start_time):
            errors.append("La hora inicial de la estrategia no es valida.")
        if not valid_schedule_time(schedule_end_time):
            errors.append("La hora final de la estrategia no es valida.")

        form_strategy = {
            "id": strategy_id,
            "name": name,
            "description": description,
            "risk_level": risk_level,
            "signal_frequency": signal_frequency,
            "historical_return": historical_return,
            "telegram_url": telegram_url,
            "has_telegram": has_telegram,
            "signals_txt_name": signals_txt_name,
            "python_file": python_file,
            "auto_execute": auto_execute,
            "schedule_start_time": schedule_start_time,
            "schedule_end_time": schedule_end_time,
            "schedule_interval_minutes": schedule_interval_minutes,
            "include_in_totalizer": include_in_totalizer,
            "public_visible": public_visible,
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
                text(
                """
                UPDATE strategies
                SET name = :name, description = :description, risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = :historical_return,
                    telegram_url = :telegram_url,
                    has_telegram = :has_telegram,
                    signals_txt_name = :signals_txt_name,
                    python_file = :python_file,
                    auto_execute = :auto_execute,
                    schedule_start_time = :schedule_start_time,
                    schedule_end_time = :schedule_end_time,
                    schedule_interval_minutes = :schedule_interval_minutes,
                    include_in_totalizer = :include_in_totalizer,
                    public_visible = :public_visible,
                    is_active = :is_active
                WHERE id = :id
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "has_telegram": has_telegram,
                    "signals_txt_name": signals_txt_name,
                    "python_file": python_file,
                    "auto_execute": auto_execute,
                    "schedule_start_time": schedule_start_time,
                    "schedule_end_time": schedule_end_time,
                    "schedule_interval_minutes": schedule_interval_minutes,
                    "include_in_totalizer": include_in_totalizer,
                    "public_visible": public_visible,
                    "is_active": is_active,
                    "id": strategy_id,
                },
            )
            flash("Estrategia actualizada.", "success")
        else:
            g.db.execute(
                text(
                """
                INSERT INTO strategies
                (name, description, risk_level, signal_frequency,
                 historical_return, telegram_url, has_telegram, signals_txt_name,
                 python_file, auto_execute, schedule_start_time, schedule_end_time,
                 schedule_interval_minutes, include_in_totalizer, public_visible, is_active)
                VALUES (:name, :description, :risk_level, :signal_frequency,
                        :historical_return, :telegram_url, :has_telegram, :signals_txt_name,
                        :python_file, :auto_execute, :schedule_start_time, :schedule_end_time,
                        :schedule_interval_minutes, :include_in_totalizer, :public_visible, :is_active)
                """,
                ),
                {
                    "name": name,
                    "description": description,
                    "risk_level": risk_level,
                    "signal_frequency": signal_frequency,
                    "historical_return": historical_return,
                    "telegram_url": telegram_url,
                    "has_telegram": has_telegram,
                    "signals_txt_name": signals_txt_name,
                    "python_file": python_file,
                    "auto_execute": auto_execute,
                    "schedule_start_time": schedule_start_time,
                    "schedule_end_time": schedule_end_time,
                    "schedule_interval_minutes": schedule_interval_minutes,
                    "include_in_totalizer": include_in_totalizer,
                    "public_visible": public_visible,
                    "is_active": is_active,
                },
            )
            flash("Estrategia creada.", "success")

        g.db.commit()
        return redirect(url_for("admin_dashboard"))

    def save_strategy_description(strategy_id):
        description = request.form.get("description", "").strip()
        g.db.execute(
            text("UPDATE strategies SET description = :description WHERE id = :id"),
            {"description": description, "id": strategy_id},
        )
        g.db.commit()
        flash("Descripcion actualizada.", "success")
        return redirect(url_for("admin_dashboard", _anchor=f"strategy-{strategy_id}"))

    def track_visitor():
        if request.endpoint == "static":
            return
        visitor_id = session.get("visitor_id")
        if not visitor_id:
            visitor_id = uuid4().hex
            session["visitor_id"] = visitor_id

        now = datetime.now(UTC)
        if engine.dialect.name == "postgresql":
            g.db.execute(
                text(
                    """
                    INSERT INTO active_visitors (visitor_id, last_seen)
                    VALUES (:visitor_id, :last_seen)
                    ON CONFLICT (visitor_id) DO UPDATE SET
                      last_seen = EXCLUDED.last_seen
                    """
                ),
                {"visitor_id": visitor_id, "last_seen": now},
            )
        else:
            g.db.execute(
                text(
                    """
                    INSERT OR REPLACE INTO active_visitors (visitor_id, last_seen)
                    VALUES (:visitor_id, :last_seen)
                    """
                ),
                {"visitor_id": visitor_id, "last_seen": now},
            )
        g.db.commit()

    def active_visitor_count(minutes=5):
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        return g.db.execute(
            text("SELECT COUNT(*) FROM active_visitors WHERE last_seen >= :cutoff"),
            {"cutoff": cutoff},
        ).scalar_one()

    def top_money_volume_assets(limit=20):
        database_data = top_money_volume_assets_from_database(limit)
        if database_data["rows"]:
            return database_data
        txt_data = top_money_volume_assets_from_txt(limit)
        if txt_data["rows"]:
            return txt_data
        return top_money_volume_assets_from_tickers(limit)

    def top_money_volume_assets_from_database(limit=20):
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT asset_rank AS rank, symbol, name, market, price, money_volume
                    FROM top_money_volume_assets
                    ORDER BY asset_rank
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().fetchall()
            updated_at = g.db.execute(
                text("SELECT MAX(updated_at) FROM top_money_volume_assets")
            ).scalar()
        except Exception:
            rollback_request_db()
            return {"rows": [], "updated_at": "", "source": "database"}
        return {
            "rows": [dict(row) for row in rows],
            "updated_at": format_any_madrid_datetime(updated_at),
            "source": "database",
        }

    def top_money_volume_assets_from_txt(limit=20):
        path = Path(os.environ.get("TOP_MONEY_VOLUME_FILE", DEFAULT_TOP_MONEY_VOLUME_FILE)).resolve()
        try:
            if path != DEFAULT_TOP_MONEY_VOLUME_FILE and BASE_DIR not in path.parents:
                return {"rows": [], "updated_at": "", "source": "top_txt"}
            if not path.exists() or not path.is_file():
                return {"rows": [], "updated_at": "", "source": "top_txt"}
            rows = []
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [part.strip() for part in line.split("|")]
                if len(parts) < 6:
                    continue
                rows.append(
                    {
                        "rank": parse_display_int(parts[0], len(rows) + 1),
                        "symbol": parts[1].upper(),
                        "name": parts[2],
                        "market": parts[3],
                        "price": parse_display_float(parts[4]),
                        "money_volume": parse_display_float(parts[5]),
                    }
                )
                if len(rows) >= limit:
                    break
            return {
                "rows": rows,
                "updated_at": format_any_madrid_datetime(path.stat().st_mtime),
                "source": "top_txt",
            }
        except OSError:
            return {"rows": [], "updated_at": "", "source": "top_txt"}

    def top_money_volume_assets_from_tickers(limit=20):
        path = Path(os.environ.get("STRATEGY_TICKERS_FILE", DEFAULT_STRATEGY_TICKERS_FILE)).resolve()
        try:
            if path != DEFAULT_STRATEGY_TICKERS_FILE and BASE_DIR not in path.parents:
                return {"rows": [], "updated_at": "", "source": "tickers"}
            if not path.exists() or not path.is_file():
                return {"rows": [], "updated_at": "", "source": "tickers"}
            rows = []
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                symbol = raw_line.strip().upper()
                if not symbol or symbol.startswith("#"):
                    continue
                rows.append(
                    {
                        "rank": len(rows) + 1,
                        "symbol": symbol,
                        "name": "Pendiente de datos enriquecidos",
                        "market": "",
                        "price": 0,
                        "money_volume": 0,
                        "source": "tickers",
                    }
                )
                if len(rows) >= limit:
                    break
            return {
                "rows": rows,
                "updated_at": format_any_madrid_datetime(path.stat().st_mtime),
                "source": "tickers",
            }
        except OSError:
            return {"rows": [], "updated_at": "", "source": "tickers"}

    def relevant_market_news(limit=10, days=None):
        query_limit = max(limit * 4, 120) if days else limit
        try:
            try:
                rows = g.db.execute(
                    text(
                        """
                        SELECT title, title_es, title_en, source, url, published_at,
                               summary, summary_es, summary_en, impact,
                               symbols, sector_tags, ai_used, created_at
                        FROM market_news
                        ORDER BY COALESCE(published_at, created_at) DESC, created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": query_limit},
                ).mappings().fetchall()
            except Exception:
                rollback_request_db()
                rows = g.db.execute(
                    text(
                        """
                        SELECT title, source, url, published_at, summary, impact,
                               symbols, sector_tags, ai_used, created_at
                        FROM market_news
                        ORDER BY COALESCE(published_at, created_at) DESC, created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": query_limit},
                ).mappings().fetchall()
            updated_at = g.db.execute(text("SELECT MAX(created_at) FROM market_news")).scalar()
        except Exception:
            rollback_request_db()
            return {"rows": [], "updated_at": ""}

        formatted = []
        cutoff = datetime.now(UTC) - timedelta(days=days) if days else None
        for row in rows:
            item = dict(row)
            if cutoff:
                item_datetime = parse_utc_database_datetime(item.get("published_at") or item.get("created_at"))
                if item_datetime and item_datetime < cutoff:
                    continue
            item["published_display"] = format_any_madrid_datetime(item.get("published_at"))
            item["created_display"] = format_any_madrid_datetime(item.get("created_at"))
            item["impact_class"] = news_impact_class(item.get("impact"))
            item["impact_label"] = news_impact_label(item.get("impact"))
            item["title_es"] = item.get("title_es") or item.get("title") or ""
            item["title_en"] = item.get("title_en") or item.get("title") or ""
            item["summary_es"] = item.get("summary_es") or item.get("summary") or ""
            item["summary_en"] = item.get("summary_en") or item.get("summary") or ""
            item["display_summary_es"] = news_display_summary(item)
            item["market_angle"] = news_market_angle(item)
            item["ai_label"] = "Resumen"
            formatted.append(item)
            if len(formatted) >= limit:
                break
        return {
            "rows": formatted,
            "updated_at": format_any_madrid_datetime(updated_at),
        }

    def local_upload_file_statuses():
        signal_files = sorted(DEFAULT_SIGNALS_DIR.glob("*.txt")) if DEFAULT_SIGNALS_DIR.exists() else []
        items = [
            upload_status_item("Signals", files=signal_files),
            upload_status_item("Run", path=DEFAULT_STRATEGY_STATUS_FILE),
            upload_status_item("Selected", path=BASE_DIR / "Estrategias" / "estrategias_a_ejecutar.txt"),
            upload_status_item("Top Vol", path=DEFAULT_TOP_MONEY_VOLUME_FILE),
            upload_status_item("V2 Diag", path=DEFAULT_V2_DIAGNOSTICS_FILE),
            upload_status_item("Diag TXT", path=DEFAULT_V2_DIAGNOSTICS_TXT_FILE),
            upload_status_item("Ops State", path=DEFAULT_SIMULATED_OPERATIONS_FILE),
            upload_status_item("Open Ops", path=DEFAULT_OPEN_OPERATIONS_FILE),
            upload_status_item("Closed Ops", path=DEFAULT_CLOSED_OPERATIONS_FILE),
            upload_status_item("All Ops", path=BASE_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_todas.txt"),
            upload_status_item("Perf", path=DEFAULT_STRATEGY_PERFORMANCE_FILE),
            upload_status_item("Max Cap", path=DEFAULT_CAPITAL_MAX_FILE),
            upload_status_item("BT JSON", path=DEFAULT_BACKTEST_OUTPUT_FILE),
            upload_status_item("Assets", path=BASE_DIR / "data" / "assets.csv"),
        ]
        return items

    def operation_status_pilots():
        now = datetime.now(MADRID_TZ)
        pilot_keys = [
            "strategies",
            "strategies_v2",
            "backtest_5y",
            "universe",
            "market_full",
            "news",
            "sync_sqlite",
            "market-hours",
        ]
        chip_items = chip_status_items_from_database(pilot_keys)
        if chip_items:
            return chip_items
        market_open = time_in_madrid_window(now, "15:30", "22:00")
        return [
            missing_pilot_status_item("strategies", "Strategies"),
            missing_pilot_status_item("strategies_v2", "Engine V2"),
            missing_pilot_status_item("backtest_5y", "Backset"),
            missing_pilot_status_item("universe", "Universe"),
            missing_pilot_status_item("market_full", "Market Full"),
            missing_pilot_status_item("news", "Notices"),
            missing_pilot_status_item("sync_sqlite", "Post Sync"),
            {
                "key": "market-hours",
                "label": "STATUS",
                "ok": market_open,
                "updated_display": "15:30-22:00" if market_open else "22:00-15:30",
                "description": operation_pilot_description("market-hours", "STATUS"),
                "title": f"{operation_pilot_description('market-hours', 'STATUS')} | {'15:30-22:00' if market_open else '22:00-15:30'}",
            },
        ]

    def missing_pilot_status_item(key, label):
        return {
            "key": key,
            "label": label,
            "ok": False,
            "updated_display": "sin fecha",
            "description": operation_pilot_description(key, label),
            "title": f"{operation_pilot_description(key, label)} | sin fecha",
        }

    def chip_status_items_from_database(keys):
        rows_by_key = chip_status_rows_by_key()
        if not rows_by_key:
            return []
        now = datetime.now(MADRID_TZ)
        market_open = time_in_madrid_window(now, "15:30", "22:00")
        items = []
        for key in keys:
            execution_item = operation_execution_status_item(key, rows_by_key.get(key), now)
            if execution_item:
                items.append(execution_item)
                continue
            row = rows_by_key.get(key)
            if not row:
                return []
            updated_at = parse_utc_database_datetime(row.get("updated_at") or row.get("synced_at"))
            updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else None
            updated_today = bool(updated_at and updated_at.date() == now.date())
            ok = bool(row.get("ok")) and updated_today
            if key == "market-hours":
                ok = market_open
            updated_display = row.get("updated_display") or (updated_at.strftime("%H:%M") if updated_at else "sin fecha")
            description = operation_pilot_description(key, row.get("label") or key)
            items.append(
                {
                    "key": key,
                    "label": row.get("label") or key,
                    "ok": ok,
                    "updated_display": updated_display,
                    "description": description,
                    "title": f"{description} | {updated_display}",
                }
            )
        return items

    def operation_execution_status_item(key, chip_row, now):
        if key not in {"strategies", "strategies_v2", "backtest_5y", "universe", "market_full", "news", "sync_sqlite"}:
            return None
        status_row = execution_status_row(key)
        if not status_row and key == "strategies":
            return classic_strategy_run_item(chip_row, now)
        if not status_row:
            return None
        updated_at = parse_utc_database_datetime(status_row.get("last_finished_at") or status_row.get("updated_at"))
        updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else None
        updated_today = bool(updated_at and updated_at.date() == now.date())
        status_ok = str(status_row.get("status") or "").upper() == "OK"
        updated_display = updated_at.strftime("%H:%M %d/%m/%y") if updated_at else "sin fecha"
        label = (chip_row or {}).get("label") or status_row.get("label") or key
        description = operation_pilot_description(key, label)
        return {
            "key": key,
            "label": label,
            "ok": bool(status_ok and updated_today),
            "updated_display": updated_display,
            "description": description,
            "title": f"{description} | {updated_display}",
        }

    def classic_strategy_run_item(chip_row, now):
        try:
            row = g.db.execute(
                text(
                    """
                    SELECT MAX(run_at) AS latest_run_at
                    FROM strategies
                    WHERE COALESCE(run_status, '') = 'OK'
                    """
                )
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            return None
        updated_at = parse_utc_database_datetime(row.get("latest_run_at")) if row else None
        updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else None
        updated_today = bool(updated_at and updated_at.date() == now.date())
        updated_display = updated_at.strftime("%H:%M %d/%m/%y") if updated_at else "sin fecha"
        label = (chip_row or {}).get("label") or "Strategies"
        description = operation_pilot_description("strategies", label)
        return {
            "key": "strategies",
            "label": label,
            "ok": updated_today,
            "updated_display": updated_display,
            "description": description,
            "title": f"{description} | {updated_display}",
        }

    def chip_status_rows_by_key():
        cached = getattr(g, "_chip_status_rows_by_key", None)
        if cached is not None:
            return cached
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT key, label, ok, updated_display, updated_at, file_count, synced_at
                    FROM chip_status
                    """
                )
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            rows = []
        cached = {row.get("key"): dict(row) for row in rows}
        g._chip_status_rows_by_key = cached
        return cached

    def pilot_status_item(key, label, paths=None, table=None):
        status_row = execution_status_row(key)
        updated_at = parse_utc_database_datetime(status_row.get("last_finished_at")) if status_row else None
        updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else latest_file_datetime(paths or [])
        db_updated_at = latest_table_datetime(table) if table else None
        if db_updated_at and (not updated_at or db_updated_at > updated_at):
            updated_at = db_updated_at
        today = datetime.now(MADRID_TZ).date()
        status_ok = str(status_row.get("status") or "").upper() == "OK" if status_row else True
        ok = bool(status_ok and updated_at and updated_at.date() == today)
        updated_display = updated_at.strftime("%H:%M") if updated_at else "sin fecha"
        return {
            "key": key,
            "label": label,
            "ok": ok,
            "updated_display": updated_display,
            "title": f"{label}: {updated_display}",
        }

    def execution_status_row(task_key):
        try:
            row = g.db.execute(
                text(
                    """
                    SELECT task_key, status, last_finished_at, last_returncode, updated_at
                    FROM execution_status
                    WHERE task_key = :task_key
                    """
                ),
                {"task_key": task_key},
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            return None
        return dict(row) if row else None

    def latest_file_datetime(paths):
        latest_mtime = None
        for raw_path in paths or []:
            if not raw_path:
                continue
            try:
                path = Path(raw_path).resolve()
                if not path.exists() or not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            latest_mtime = mtime if latest_mtime is None else max(latest_mtime, mtime)
        return datetime.fromtimestamp(latest_mtime, MADRID_TZ) if latest_mtime else None

    def latest_table_datetime(table):
        table_columns = {
            "asset_universe": "updated_at",
            "asset_snapshots": "updated_at",
            "market_news": "created_at",
            "simulated_operations": "updated_at",
        }
        column = table_columns.get(table)
        if not table or not column:
            return None
        try:
            value = g.db.execute(text(f"SELECT MAX({column}) FROM {table}")).scalar()
        except Exception:
            rollback_request_db()
            return None
        parsed = parse_utc_database_datetime(value)
        return parsed.astimezone(MADRID_TZ) if parsed else None

    def time_in_madrid_window(now, start_text, end_text):
        start_hour, start_minute = [int(part) for part in start_text.split(":", 1)]
        end_hour, end_minute = [int(part) for part in end_text.split(":", 1)]
        start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def upload_status_item(label, path=None, files=None):
        chip_item = upload_status_item_from_chip_status(label)
        if chip_item:
            return chip_item
        db_item = upload_status_item_from_database(label)
        if db_item:
            return db_item
        return {
            "label": label,
            "ok": False,
            "exists": False,
            "count": 0,
            "updated_display": "no file",
            "description": upload_status_description(label),
        }

    def upload_status_item_from_chip_status(label):
        row = chip_status_rows_by_key().get(label)
        if not row:
            return None
        today = datetime.now(MADRID_TZ).date()
        updated_at = parse_utc_database_datetime(row.get("updated_at") or row.get("synced_at"))
        updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else None
        updated_today = bool(updated_at and updated_at.date() == today)
        return {
            "label": row.get("label") or label,
            "ok": bool(row.get("ok")) and updated_today,
            "exists": True,
            "count": int(row.get("file_count") or 0),
            "updated_display": row.get("updated_display") or (updated_at.strftime("%H:%M") if updated_at else "no file"),
            "description": upload_status_description(row.get("label") or label),
        }

    def upload_status_item_from_database(label):
        try:
            row = g.db.execute(
                text(
                    """
                    SELECT label, exists_flag, file_count, latest_updated_at
                    FROM upload_file_status
                    WHERE label = :label
                    """
                ),
                {"label": label},
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            return None
        if not row:
            return None
        updated_at = parse_utc_database_datetime(row.get("latest_updated_at"))
        updated_at = updated_at.astimezone(MADRID_TZ) if updated_at else None
        today = datetime.now(MADRID_TZ).date()
        return {
            "label": label,
            "ok": bool(updated_at and updated_at.date() == today),
            "exists": bool(row.get("exists_flag")),
            "count": int(row.get("file_count") or 0),
            "updated_display": updated_at.strftime("%H:%M") if updated_at else "no file",
            "description": upload_status_description(label),
        }

    def news_impact_class(value):
        value = str(value or "").lower()
        if value == "positivo":
            return "text-success"
        if value == "negativo":
            return "text-danger"
        return "text-secondary"

    def news_impact_label(value):
        value = str(value or "").lower()
        if value == "positivo":
            return "Positivo"
        if value == "negativo":
            return "Negativo"
        return "Neutral"

    def news_display_summary(item):
        title = str(item.get("title_es") or item.get("title") or "").strip()
        summary = str(item.get("summary_es") or item.get("summary") or "").strip()
        if summary and normalize_news_text(summary) != normalize_news_text(title):
            return summary
        affected = str(item.get("symbols") or item.get("sector_tags") or "mercado general").strip()
        impact = news_impact_label(item.get("impact")).lower()
        return f"Lectura {impact}: conviene vigilar {affected} por posible impacto en volatilidad, volumen y gaps de apertura."

    def news_market_angle(item):
        affected = str(item.get("symbols") or item.get("sector_tags") or "").strip()
        if affected:
            return f"Impacto probable sobre {affected}"
        return "Impacto de mercado general"

    def normalize_news_text(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def parse_display_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def parse_display_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def read_top_strategy_tickers(limit=20):
        path = Path(os.environ.get("STRATEGY_TICKERS_FILE", DEFAULT_STRATEGY_TICKERS_FILE)).resolve()
        try:
            if path != DEFAULT_STRATEGY_TICKERS_FILE and BASE_DIR not in path.parents:
                return []
            if not path.exists() or not path.is_file():
                return []
            tickers = []
            seen = set()
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                symbol = raw_line.strip().upper()
                if not symbol or symbol.startswith("#") or symbol in seen:
                    continue
                seen.add(symbol)
                tickers.append(symbol)
                if len(tickers) >= limit:
                    break
            return tickers
        except OSError:
            return []

    def strategy_with_signals(row):
        strategy = dict(row)
        txt_name = strategy.get("signals_txt_name", "")
        signals = read_strategy_signals(txt_name)
        attach_simulated_operations_to_signals(strategy, signals)
        signals_updated_at = strategy_signals_updated_at_datetime(txt_name)
        latest_open_operation_at = latest_open_operation_datetime(txt_name)
        strategy["signals"] = signals
        strategy["signals_count"] = len(signals)
        strategy["closed_operations_count"] = int(strategy.get("closed_operations_count") or 0)
        strategy["average_close_duration"] = strategy.get("average_close_duration") or "Sin cierres todavia"
        strategy["success_rate"] = strategy.get("success_rate") or "Sin cierres todavia"
        strategy["_signals_updated_at_datetime"] = signals_updated_at
        strategy["signals_updated_at"] = format_madrid_datetime(signals_updated_at or latest_open_operation_at)
        strategy["run_status"] = strategy_run_status(strategy, txt_name)
        strategy["first_operation_display"] = strategy.get("first_operation_display") or ""
        strategy["short_name"] = strategy_short_name(strategy.get("name"))
        return_source = strategy.get("historical_return")
        strategy["historical_return"] = return_source
        strategy["historical_return_public"] = clean_public_return_text(return_source)
        strategy["return_metrics"] = build_return_metrics(return_source)
        strategy["return_badge"] = strategy_return_badge(return_source)
        strategy["return_badge_class"] = strategy_return_badge_class(return_source)
        return strategy

    def refresh_strategy_balances_from_operations(strategies):
        txt_names = sorted(
            {
                strategy.get("signals_txt_name")
                for strategy in strategies
                if strategy.get("signals_txt_name")
            }
        )
        if not txt_names:
            return
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT txt_name, status, opened_at, closed_at, signal_date,
                           profit_usd, investment_value, operation_key, close_reason
                    FROM simulated_operations
                    WHERE txt_name IN :txt_names
                    """
                ).bindparams(bindparam("txt_names", expanding=True)),
                {"txt_names": txt_names},
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            return
        grouped = {txt_name: [] for txt_name in txt_names}
        for row in rows:
            grouped.setdefault(row["txt_name"], []).append(dict(row))
        for strategy in strategies:
            operations = grouped.get(strategy.get("signals_txt_name") or "", [])
            if not operations:
                continue
            return_source = build_operation_balance_text(operations)
            strategy["historical_return"] = return_source
            strategy["historical_return_public"] = clean_public_return_text(return_source)
            strategy["return_metrics"] = build_return_metrics(return_source)
            strategy["return_badge"] = strategy_return_badge(return_source)
            strategy["return_badge_class"] = strategy_return_badge_class(return_source)
            strategy["closed_operations_count"] = sum(1 for op in operations if op.get("status") == "CLOSED")

    def build_operation_balance_text(operations):
        total_ops = len(operations)
        open_ops = sum(1 for op in operations if op.get("status") == "OPEN")
        closed_ops = sum(1 for op in operations if op.get("status") == "CLOSED")
        profit_usd = sum(float_or_zero(op.get("profit_usd")) for op in operations)
        max_open = max_open_operations_from_rows(operations)
        capital_base = max(50_000.0, max_open * 1_000.0)
        current_capital = capital_base + profit_usd
        return_pct = (profit_usd / capital_base * 100) if capital_base else 0.0
        return (
            f"{profit_usd:+.2f} USD "
            f"({return_pct:+.2f}%, capital base {capital_base:.2f} USD, "
            f"capital actual {current_capital:.2f} USD, "
            f"max abiertas {max_open}, "
            f"{total_ops} ops, {open_ops} abiertas, {closed_ops} cerradas)"
            f"{operation_period_labels(operations)}"
        )

    def operation_period_labels(operations):
        now = datetime.now(MADRID_TZ)
        ytd_start = datetime(now.year, 1, 1, tzinfo=MADRID_TZ)
        prev_year_start = datetime(now.year - 1, 1, 1, tzinfo=MADRID_TZ)
        prev_year_end = datetime(now.year, 1, 1, tzinfo=MADRID_TZ)
        periods = [
            ("Last 1M", now - timedelta(days=30), now),
            ("Last 3M", now - timedelta(days=90), now),
            ("Last 12M", now - timedelta(days=365), now),
            (str(now.year), ytd_start, now),
            (str(now.year - 1), prev_year_start, prev_year_end),
        ]
        return "".join(
            f" | {format_operation_period_label(label, operations, start, end)}"
            for label, start, end in periods
        )

    def format_operation_period_label(label, operations, start, end):
        selected = []
        for operation in operations:
            opened_at = parse_status_datetime(operation.get("opened_at") or operation.get("signal_date"))
            closed_at = parse_status_datetime(operation.get("closed_at"))
            period_at = opened_at if operation_is_backtest_final_close(operation) else closed_at
            if operation.get("status") == "OPEN":
                period_at = opened_at
            if period_at and start <= period_at < end:
                selected.append(operation)
        profit_usd = sum(float_or_zero(op.get("profit_usd")) for op in selected)
        max_open = max_open_operations_from_rows(selected)
        capital_base = max(50_000.0, max_open * 1_000.0)
        return_pct = (profit_usd / capital_base * 100) if capital_base else 0.0
        closed_ops = sum(1 for op in selected if op.get("status") == "CLOSED")
        open_ops = sum(1 for op in selected if op.get("status") == "OPEN")
        return (
            f"{label} {profit_usd:+.2f} USD "
            f"({return_pct:+.2f}%, capital base {capital_base:.2f} USD, "
            f"max abiertas {max_open}, {closed_ops} cerradas, {open_ops} abiertas)"
        )

    def operation_is_backtest_final_close(operation):
        operation_key = str(operation.get("operation_key") or "")
        close_reason = str(operation.get("close_reason") or "").upper()
        return operation_key.startswith("BACKTEST|") and "FIN_BACKTEST" in close_reason

    def max_open_operations_from_rows(operations):
        events = []
        now = datetime.now(MADRID_TZ)
        for operation in operations:
            opened_at = parse_status_datetime(operation.get("opened_at") or operation.get("signal_date"))
            if not opened_at:
                continue
            closed_at = parse_status_datetime(operation.get("closed_at"))
            if operation.get("status") == "OPEN" or not closed_at or closed_at < opened_at:
                closed_at = now
            events.append((opened_at, 1))
            events.append((closed_at, -1))
        events.sort(key=lambda item: (item[0], -item[1]))
        current = 0
        maximum = 0
        for _event_at, delta in events:
            current += delta
            maximum = max(maximum, current)
        return maximum

    def float_or_zero(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def attach_simulated_operations_to_signals(strategy, signals):
        operations_by_signal = simulated_operations_for_signals(strategy, signals)
        for signal in signals:
            signal_lookup_key = signal.get("_operation_lookup_key", "")
            operation = signal.get("open_operation") or operations_by_signal.get(signal_lookup_key)
            if operation:
                signal["open_operation"] = operation
                signal["operation_summary"] = {
                    "status_label": operation["status_label"],
                    "profit_pct_display": operation["profit_pct_display"],
                    "profit_class": operation["profit_class"],
                }
                signal["is_new"] = signal.get("is_today_signal", False)
            else:
                signal["operation_summary"] = {
                    "status_label": "Pendiente",
                    "profit_pct_display": "Pendiente",
                    "profit_class": "text-warning",
                }
                signal["is_new"] = signal.get("is_today_signal", False)

    def simulated_operations_for_signals(strategy, signals):
        txt_name = strategy.get("signals_txt_name", "")
        if not txt_name or not signals:
            return {}
        lookups = []
        operation_keys = set()
        symbols = set()
        signal_dates = set()
        for index, signal in enumerate(signals):
            symbol = normalize_signal_symbol(signal.get("symbol", ""))
            if not symbol:
                continue
            operation_key = build_signal_operation_key(txt_name, signal)
            legacy_key = build_legacy_signal_operation_key(txt_name, signal)
            signal_date = signal_line_field(signal.get("line", ""), "Fecha")
            signal_line = signal.get("line", "")
            lookup_key = f"{index}:{operation_key}:{legacy_key}:{symbol}:{signal_date}:{signal_line}"
            signal["_operation_lookup_key"] = lookup_key
            lookups.append(
                {
                    "lookup_key": lookup_key,
                    "operation_key": operation_key,
                    "legacy_key": legacy_key,
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "signal_line": signal_line,
                }
            )
            if operation_key:
                operation_keys.add(operation_key)
            if legacy_key:
                operation_keys.add(legacy_key)
            symbols.add(symbol)
            if signal_date:
                signal_dates.add(signal_date)
        if not lookups:
            return {}
        operations = simulated_operations_from_database_batch(txt_name, operation_keys, symbols, signal_dates)
        if not operations:
            return simulated_operations_from_file_batch(txt_name, lookups)
        return match_operations_to_signal_lookups(lookups, operations)

    def simulated_operations_from_database_batch(txt_name, operation_keys, symbols, signal_dates):
        clauses = []
        params = {"txt_name": txt_name}
        if operation_keys:
            params["operation_keys"] = list(operation_keys)
            clauses.append("operation_key = ANY(:operation_keys)")
        if symbols and signal_dates:
            params["symbols"] = list(symbols)
            params["signal_dates"] = list(signal_dates)
            clauses.append("(UPPER(symbol) = ANY(:symbols) AND signal_date = ANY(:signal_dates))")
        if not clauses:
            return []
        try:
            rows = g.db.execute(
                text(
                    f"""
                    SELECT strategy_name, txt_name, symbol, direction, status,
                           operation_key, signal_date, signal_line, opened_at, closed_at, entry_price,
                           target_price, stop_loss, shares, current_price,
                           investment_value, profit_usd, profit_pct,
                           close_reason, updated_at
                    FROM simulated_operations
                    WHERE txt_name = :txt_name
                      AND ({' OR '.join(clauses)})
                    ORDER BY updated_at DESC
                    """
                ),
                params,
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            return []
        return [format_simulated_operation(dict(row)) for row in rows]

    def simulated_operations_from_file_batch(txt_name, lookups):
        path = Path(os.environ.get("SIMULATED_OPERATIONS_FILE", DEFAULT_SIMULATED_OPERATIONS_FILE)).resolve()
        try:
            if path != DEFAULT_SIMULATED_OPERATIONS_FILE and BASE_DIR not in path.parents:
                return {}
            operations = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        lookup_symbols = {item["symbol"] for item in lookups}
        lookup_dates = {item["signal_date"] for item in lookups if item["signal_date"]}
        lookup_keys = {
            key
            for item in lookups
            for key in (item["operation_key"], item["legacy_key"])
            if key
        }
        candidates = []
        for operation in operations:
            if operation.get("txt_name") != txt_name:
                continue
            op_symbol = normalize_signal_symbol(operation.get("symbol", ""))
            op_key = operation.get("operation_key", "")
            op_date = str(operation.get("signal_date", ""))
            if op_key in lookup_keys or (op_symbol in lookup_symbols and op_date in lookup_dates):
                candidates.append(format_simulated_operation(dict(operation)))
        return match_operations_to_signal_lookups(lookups, candidates)

    def match_operations_to_signal_lookups(lookups, operations):
        operation_indexes = build_operation_lookup_indexes(operations)
        matched = {}
        for lookup in lookups:
            best = best_operation_for_lookup(lookup, operation_indexes)
            if best:
                matched[lookup["lookup_key"]] = best
        return matched

    def build_operation_lookup_indexes(operations):
        by_key = {}
        by_symbol_date = {}
        for operation in operations:
            op_key = str(operation.get("operation_key") or "")
            if op_key:
                by_key.setdefault(op_key, []).append(operation)
            symbol_date_key = (
                normalize_signal_symbol(operation.get("symbol", "")),
                str(operation.get("signal_date") or ""),
            )
            by_symbol_date.setdefault(symbol_date_key, []).append(operation)
        return {"by_key": by_key, "by_symbol_date": by_symbol_date}

    def best_operation_for_lookup(lookup, operation_indexes):
        matches = []
        candidates = []
        candidates.extend(operation_indexes["by_key"].get(lookup["operation_key"], []))
        candidates.extend(operation_indexes["by_key"].get(lookup["legacy_key"], []))
        candidates.extend(
            operation_indexes["by_symbol_date"].get(
                (lookup["symbol"], str(lookup["signal_date"] or "")),
                [],
            )
        )
        seen = set()
        for operation in candidates:
            identity = (
                operation.get("operation_key"),
                operation.get("symbol"),
                operation.get("signal_date"),
                operation.get("opened_at"),
                operation.get("updated_at"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            op_key = str(operation.get("operation_key") or "")
            op_symbol = normalize_signal_symbol(operation.get("symbol", ""))
            op_date = str(operation.get("signal_date") or "")
            if op_key == lookup["operation_key"]:
                priority = 0
            elif op_key == lookup["legacy_key"]:
                priority = 1
            elif op_symbol == lookup["symbol"] and op_date == str(lookup["signal_date"] or ""):
                priority = 2 if operation.get("signal_line") == lookup["signal_line"] else 3
            else:
                continue
            status_priority = 0 if str(operation.get("status") or "").upper() == "OPEN" else 1
            matches.append((priority, status_priority, str(operation.get("updated_at") or ""), operation))
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=False)
        same_priority = [item for item in matches if item[0] == matches[0][0] and item[1] == matches[0][1]]
        return max(same_priority, key=lambda item: item[2])[3]

    def strategy_uses_grouped_operations(strategy):
        strategy_name = str(strategy.get("name", "")).strip().lower()
        txt_name = str(strategy.get("signals_txt_name", "")).strip().lower()
        return strategy_name in {
            "acumula metales",
            "acumulacion",
            "reversion rsi 5",
        } or txt_name in {
            "acumula_metales.txt",
            "acumulacion.txt",
            "reversion_rsi_5.txt",
        }

    def build_signal_groups(signals, grouped_mode=False):
        def signal_group_datetime(signal):
            return parse_notice_datetime(signal.get("notice_time"), signal.get("fields", {}))

        def operation_signal_datetime(operation):
            return (
                parse_status_datetime(operation.get("opened_at"))
                or parse_status_datetime(operation.get("signal_date"))
                or parse_status_datetime(operation.get("closed_at"))
                or parse_status_datetime(operation.get("updated_at"))
            )

        def signal_group_sort_key(group):
            signal_dates = [
                parsed.timestamp()
                for parsed in (signal_group_datetime(signal) for signal in group.get("signals", []))
                if parsed
            ]
            operation_dates = [
                parsed.timestamp()
                for parsed in (operation_signal_datetime(operation) for operation in group.get("operations", []))
                if parsed
            ]
            latest_date = max(signal_dates + operation_dates, default=0)
            return (latest_date, group.get("profit_usd") or 0, group.get("symbol") or "")

        if not grouped_mode:
            groups = [build_single_signal_group(signal) for signal in signals if signal.get("symbol")]
            groups.sort(key=signal_group_sort_key, reverse=True)
            return groups

        groups_by_symbol = {}
        ordered_groups = []
        for signal in signals:
            symbol = normalize_signal_symbol(signal.get("symbol", ""))
            if not symbol:
                continue
            group = groups_by_symbol.get(symbol)
            if group is None:
                group = {
                    "symbol": symbol,
                    "display_symbol": signal.get("symbol", symbol),
                    "signals": [],
                    "operations": [],
                    "first_signal": signal,
                    "is_new": False,
                    "notice_short_datetime": signal.get("notice_short_datetime", ""),
                }
                groups_by_symbol[symbol] = group
                ordered_groups.append(group)
            group["signals"].append(signal)
            group["is_new"] = group["is_new"] or bool(signal.get("is_new"))
            if signal.get("notice_short_datetime"):
                group["notice_short_datetime"] = signal["notice_short_datetime"]
            operation = signal.get("open_operation")
            if operation and not operation_in_group(group["operations"], operation):
                group["operations"].append(operation)

        for group in ordered_groups:
            group["signals"].sort(
                key=lambda signal: (
                    signal_group_datetime(signal).timestamp() if signal_group_datetime(signal) else 0,
                    signal.get("symbol") or "",
                ),
                reverse=True,
            )
            group["first_signal"] = group["signals"][0] if group["signals"] else group["first_signal"]
            group["notice_short_datetime"] = group["first_signal"].get("notice_short_datetime", group["notice_short_datetime"])
            group["operations"].sort(key=operation_opened_sort_key, reverse=True)
            group.update(build_operation_group_summary(group["operations"], group["signals"]))
        ordered_groups.sort(key=signal_group_sort_key, reverse=True)
        return ordered_groups

    def build_single_signal_group(signal):
        operation = signal.get("open_operation")
        symbol = normalize_signal_symbol(signal.get("symbol", ""))
        group = {
            "symbol": symbol,
            "display_symbol": signal.get("display_symbol") or signal.get("symbol", symbol),
            "signals": [signal],
            "operations": [operation] if operation else [],
            "first_signal": signal,
            "is_new": bool(signal.get("is_new")),
            "notice_short_datetime": signal.get("notice_short_datetime", ""),
            "single_key": signal.get("signal_key", ""),
            "detail_url_args": {"key": signal.get("signal_key", "")},
        }
        group.update(build_operation_group_summary(group["operations"], group["signals"]))
        return group

    def signal_group_for_signal(signal_groups, signal, grouped_mode=False):
        if grouped_mode:
            return signal_group_for_symbol(signal_groups, signal.get("symbol", ""))
        signal_key = signal.get("signal_key", "")
        if signal_key:
            return next((group for group in signal_groups if group.get("single_key") == signal_key), None)
        return None

    def signal_group_for_symbol(signal_groups, symbol):
        normalized = normalize_signal_symbol(symbol)
        return next((group for group in signal_groups if group["symbol"] == normalized), None)

    def operation_in_group(operations, operation):
        operation_key = str(operation.get("operation_key") or "")
        if operation_key:
            return any(str(item.get("operation_key") or "") == operation_key for item in operations)
        return any(
            item.get("opened_at") == operation.get("opened_at")
            and normalize_signal_symbol(item.get("symbol", "")) == normalize_signal_symbol(operation.get("symbol", ""))
            for item in operations
        )

    def build_operation_group_summary(operations, signals):
        invested = 0.0
        current_value = 0.0
        total_shares = 0.0
        profit_usd = 0.0
        direction = ""
        weighted_target = 0.0
        target_weight = 0.0
        weighted_stop = 0.0
        stop_weight = 0.0
        for operation in operations:
            shares = parse_display_float(operation.get("shares"))
            entry = parse_display_float(operation.get("entry_price"))
            current = parse_display_float(operation.get("current_price"))
            target = parse_display_float(operation.get("target_price"))
            stop = parse_display_float(operation.get("stop_loss"))
            invested += entry * shares
            current_value += current * shares
            total_shares += shares
            profit_usd += parse_display_float(operation.get("profit_usd"))
            direction = direction or normalize_operation_side(operation.get("direction"))
            if target > 0 and shares > 0:
                weighted_target += target * shares
                target_weight += shares
            if stop > 0 and shares > 0:
                weighted_stop += stop * shares
                stop_weight += shares

        average_entry = invested / total_shares if total_shares else 0.0
        current_price = current_value / total_shares if total_shares else 0.0
        profit_pct = (profit_usd / invested * 100) if invested else 0.0
        no_auto_close_group = any(
            str(operation.get("strategy_name") or "").strip().lower() in {"acumula metales", "acumulacion"}
            or str(operation.get("txt_name") or "").strip().lower() in {"acumula_metales.txt", "acumulacion.txt"}
            for operation in operations
        )
        if target_weight:
            target_price = weighted_target / target_weight
            target_label = "Objetivo medio"
        elif no_auto_close_group:
            target_price = 0.0
            target_label = "Sin objetivo"
        else:
            target_price = average_entry * (0.95 if direction == "SHORT" else 1.05) if average_entry else 0.0
            target_label = "Objetivo grupo"
        stop_price = weighted_stop / stop_weight if stop_weight else 0.0
        operation_count = len(operations)
        signal_count = len(signals)
        first_signal = signals[0] if signals else {}
        first_operation = operations[0] if operations else {}
        operation_summary = first_signal.get("operation_summary", {})
        return {
            "operation_count": operation_count,
            "signal_count": signal_count,
            "group_count_label": (
                f"{operation_count} operaciones abiertas"
                if operation_count
                else f"{signal_count} avisos pendientes"
            ),
            "average_entry": average_entry,
            "average_entry_display": f"{average_entry:.2f} USD" if average_entry else "Pendiente",
            "current_price": current_price,
            "current_price_display": f"{current_price:.2f} USD" if current_price else "Pendiente",
            "target_label": target_label,
            "target_price": target_price,
            "target_price_display": f"{target_price:.2f} USD" if target_price else "Pendiente",
            "stop_loss": stop_price,
            "stop_loss_display": f"{stop_price:.2f} USD" if stop_price else "Pendiente",
            "total_shares_display": f"{total_shares:.4f}" if total_shares else "Pendiente",
            "invested_display": format_money_usd(invested),
            "current_value_display": format_money_usd(current_value),
            "profit_usd": profit_usd,
            "profit_usd_display": format_signed_money_usd(profit_usd),
            "profit_pct_display": f"{profit_pct:+.2f}%" if operation_count else operation_summary.get("profit_pct_display", "Pendiente"),
            "profit_class": profit_color_class(profit_usd if operation_count else 0),
            "direction": direction or first_signal.get("side") or "",
            "selected_key": first_signal.get("signal_key", ""),
            "latest_update_display": first_operation.get("updated_at_display", "Pendiente") if first_operation else "Pendiente",
        }

    def operation_is_new_today(operation):
        value = operation.get("opened_at")
        if not value:
            return False
        try:
            opened_at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        except ValueError:
            return False
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=UTC)
        return opened_at.astimezone(MADRID_TZ).date() == datetime.now(MADRID_TZ).date()

    def strategy_run_status(strategy, txt_name):
        db_status = (strategy.get("run_status") or "").strip()
        if db_status:
            ran_at = format_database_datetime(strategy.get("run_at"))
            if db_status == "RUNNING":
                return {
                    "ok": False,
                    "running": True,
                    "label": "En ejecucion",
                    "ran_at": ran_at,
                    "error": "",
                }
            if db_status == "OK":
                return {
                    "ok": True,
                    "running": False,
                    "label": "Correcto",
                    "ran_at": ran_at,
                    "error": "",
                }
            return {
                "ok": False,
                "running": False,
                "label": "Fallo",
                "ran_at": ran_at,
                "error": strategy.get("run_message") or "La estrategia termino con error.",
            }

        strategy_name = strategy.get("name", "")
        data = load_strategy_status_data()
        item = data.get("strategies", {}).get(strategy_name)
        if not item:
            signals_updated_at = strategy.get("_signals_updated_at_datetime")
            if signals_updated_at is not None:
                return {
                    "ok": True,
                    "running": False,
                    "label": "Correcto",
                    "ran_at": format_madrid_datetime(signals_updated_at),
                    "error": "Estado inferido por avisos incorporados; aun no habia run_status guardado.",
                }
            return {
                "ok": False,
                "running": False,
                "pending": True,
                "label": "Sin ejecutar",
                "ran_at": "",
                "error": "Estrategia activa sin registro de ejecucion reciente.",
            }

        ran_at = format_status_datetime(item.get("ran_at", ""))

        if item.get("running"):
            return {
                "ok": False,
                "running": True,
                "label": "En ejecucion",
                "ran_at": ran_at,
                "error": "",
            }
        if item.get("ok"):
            return {
                "ok": True,
                "running": False,
                "label": "Correcto",
                "ran_at": ran_at,
                "error": "",
            }
        return {
            "ok": False,
            "running": False,
            "label": "Fallo",
            "ran_at": ran_at,
            "error": item.get("error", "") or "La estrategia termino con error.",
        }

    def market_is_closed_for_status():
        now = datetime.now(MADRID_TZ)
        start = now.replace(hour=15, minute=30, second=0, microsecond=0)
        end = now.replace(hour=22, minute=0, second=0, microsecond=0)
        return not (start <= now < end)

    def load_strategy_status_data():
        status_path = Path(
            os.environ.get("STRATEGY_STATUS_FILE", DEFAULT_STRATEGY_STATUS_FILE)
        ).resolve()
        try:
            if status_path != DEFAULT_STRATEGY_STATUS_FILE and BASE_DIR not in status_path.parents:
                return {}
            if not status_path.exists() or not status_path.is_file():
                return {}
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def load_strategy_failures():
        db_failures = load_strategy_failures_from_database()
        if db_failures:
            return db_failures

        data = load_strategy_status_data()
        failures = []
        for name, item in data.get("strategies", {}).items():
            is_failure = (
                not item.get("ok")
                or item.get("returncode") not in (None, 0)
            )
            if item.get("running") or not is_failure:
                continue
            failures.append(
                {
                    "name": name,
                    "file": item.get("file", ""),
                    "txt": item.get("txt", ""),
                    "ran_at": format_status_datetime(item.get("ran_at", "")),
                    "returncode": item.get("returncode"),
                    "error": build_strategy_failure_error(item),
                }
            )
        if not failures:
            failures = load_strategy_failures_from_schedule_message()
        failures.sort(key=lambda item: item["name"])
        return failures

    def load_strategy_failures_from_database():
        rows = g.db.execute(
            text(
                """
                SELECT name, python_file, signals_txt_name, run_at,
                       run_returncode, run_message
                FROM strategies
                WHERE run_status = 'ERROR'
                ORDER BY name
                """
            )
        ).mappings().fetchall()
        return [
            {
                "name": row["name"],
                "file": row["python_file"] or "",
                "txt": row["signals_txt_name"] or "",
                "ran_at": format_database_datetime(row["run_at"]),
                "returncode": row["run_returncode"],
                "error": row["run_message"] or "La estrategia termino con error.",
            }
            for row in rows
        ]

    def build_strategy_failure_error(item):
        details = []
        if item.get("returncode") not in (None, 0):
            details.append(f"Codigo de salida: {item.get('returncode')}.")
        if item.get("error"):
            details.append(str(item.get("error")))
        if not details:
            details.append("La estrategia termino marcada como ERROR, pero no devolvio detalle adicional.")
        return "\n".join(details)

    def load_strategy_failures_from_schedule_message():
        row = g.db.execute(
            text(
                """
                SELECT last_run_at, last_message
                FROM automation_schedules
                WHERE task_name = 'strategies'
                  AND last_status = 'ERROR'
                """
            )
        ).mappings().fetchone()
        if not row or not row["last_message"]:
            return []

        failures = []
        pattern = re.compile(r"ERROR - (?P<name>.+?) \((?P<file>.+?)\) \| (?P<txt_status>TXT [^|]+)")
        for match in pattern.finditer(row["last_message"]):
            failures.append(
                {
                    "name": match.group("name").strip(),
                    "file": match.group("file").strip(),
                    "txt": "",
                    "ran_at": format_status_datetime(row["last_run_at"]),
                    "returncode": 1,
                    "error": match.group("txt_status").strip(),
                }
            )
        return failures

    def format_status_datetime(value):
        if not value:
            return ""
        parsed = parse_status_datetime(value)
        if parsed is None:
            return value
        return parsed.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def format_database_datetime(value):
        parsed = parse_database_datetime(value)
        if parsed is None:
            return ""
        return parsed.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def parse_status_datetime(value):
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def mark_strategies_as_running():
        mark_strategies_as_running_file()

    def load_automation_schedules():
        expire_stale_running_schedules()
        rows = g.db.execute(
            text("SELECT * FROM automation_schedules ORDER BY task_name")
        ).mappings().fetchall()
        schedules = {}
        for row in rows:
            schedule = dict(row)
            schedule["last_message"] = clean_schedule_message(
                schedule["task_name"],
                schedule.get("last_message", ""),
            )
            schedules[schedule["task_name"]] = schedule
        return schedules

    def clean_schedule_message(task_name, message):
        if task_name != "strategies" or not message:
            return message
        technical_markers = ["===", "Traceback", "KeyError", "File \"<frozen os>\"", "Ejecutando "]
        if any(marker in message for marker in technical_markers):
            return "Estrategias con errores. Revisa Fallos de estrategias."
        return message

    def expire_stale_running_schedules():
        rows = g.db.execute(
            text(
                """
                SELECT task_name, last_run_at
                FROM automation_schedules
                WHERE last_status = 'RUNNING'
                """
            )
        ).mappings().fetchall()

        for row in rows:
            if row["task_name"] != "strategies":
                continue
            result = completed_strategy_runner_result(row["last_run_at"])
            if not result:
                continue
            g.db.execute(
                text(
                    """
                    UPDATE automation_schedules
                    SET last_status = :last_status,
                        last_message = :last_message
                    WHERE task_name = 'strategies'
                      AND last_status = 'RUNNING'
                    """
                ),
                {
                    "last_status": "OK" if result["ok"] else "ERROR",
                    "last_message": result["message"],
                },
            )
        g.db.commit()

    def completed_strategy_runner_result(last_run_at):
        schedule_started_at = parse_database_datetime(last_run_at)
        data = load_strategy_status_data()
        finished_at = parse_status_datetime(data.get("finished_at", ""))
        if finished_at is None:
            return completed_strategy_runner_result_from_database(schedule_started_at)

        started_at = parse_status_datetime(data.get("started_at", ""))
        if schedule_started_at and started_at and started_at < schedule_started_at:
            return completed_strategy_runner_result_from_database(schedule_started_at)

        results = data.get("strategies", {})
        if not results:
            return {
                "ok": False,
                "message": "Estrategias finalizadas sin resultados guardados.",
            }

        failures = [
            name
            for name, item in results.items()
            if not item.get("ok")
        ]
        if failures:
            return {
                "ok": False,
                "message": f"Estrategias finalizadas con {len(failures)} fallos: {', '.join(failures[:6])}.",
            }
        return {
            "ok": True,
            "message": "Estrategias finalizadas correctamente.",
        }

    def completed_strategy_runner_result_from_database(schedule_started_at):
        if not schedule_started_at:
            return None

        rows = g.db.execute(
            text(
                """
                SELECT name, run_status, run_at, run_message
                FROM strategies
                WHERE run_status IN ('RUNNING', 'OK', 'ERROR')
                  AND run_at IS NOT NULL
                """
            )
        ).mappings().fetchall()

        recent = []
        for row in rows:
            run_at = parse_database_datetime(row["run_at"])
            if run_at and run_at >= schedule_started_at - timedelta(minutes=1):
                recent.append(row)

        if not recent:
            timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
            if datetime.now(UTC) - schedule_started_at > timedelta(seconds=timeout_seconds + 300):
                return {
                    "ok": False,
                    "message": "Ejecucion de estrategias sin resultado final. Posible reinicio o corte de Render.",
                }
            return None

        running = [row for row in recent if row["run_status"] == "RUNNING"]
        if running:
            timeout_seconds = int(os.environ.get("STRATEGY_RUN_TIMEOUT_SECONDS", "3600"))
            if datetime.now(UTC) - schedule_started_at <= timedelta(seconds=timeout_seconds + 300):
                return None
            g.db.execute(
                text(
                    """
                    UPDATE strategies
                    SET run_status = 'ERROR',
                        run_message = 'Ejecucion cortada o bloqueada antes de finalizar.',
                        run_returncode = 1
                    WHERE run_status = 'RUNNING'
                    """
                )
            )
            return {
                "ok": False,
                "message": f"Ejecucion bloqueada o cortada. Aun figuraban RUNNING: {', '.join(row['name'] for row in running[:6])}.",
            }

        failures = [row for row in recent if row["run_status"] == "ERROR"]
        if failures:
            return {
                "ok": False,
                "message": f"Lote finalizado con {len(failures)} fallos: {', '.join(row['name'] for row in failures[:6])}.",
            }

        return {
            "ok": True,
            "message": f"Lote finalizado correctamente. Estrategias ejecutadas: {len(recent)}.",
        }

    def parse_database_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_status_datetime(value)
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def parse_schedule_int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def valid_schedule_time(value):
        try:
            hour, minute = [int(part) for part in value.split(":", 1)]
        except (AttributeError, ValueError):
            return False
        return 0 <= hour <= 23 and 0 <= minute <= 59

    def normalize_weekdays(values):
        days = []
        for value in values:
            try:
                day = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7 and day not in days:
                days.append(day)
        if not days:
            days = [1, 2, 3, 4, 5]
        return ",".join(str(day) for day in sorted(days))

    def read_strategy_signals(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None and not valid_txt_name_global(txt_name):
            return []

        today = datetime.now(MADRID_TZ).date().isoformat()
        rollback_request_db()
        try:
            rows = g.db.execute(
                text(
                    """
                        SELECT line, created_at
                        FROM strategy_signals
                    WHERE txt_name = :txt_name
                      AND signal_date = :signal_date
                    ORDER BY created_at DESC, id DESC
                    """
                ),
                {"txt_name": txt_name, "signal_date": today},
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            rows = []
        if rows:
            return merge_open_operations_with_signals(txt_name, deduplicate_signals([
                parse_signal_line(row["line"], row.get("created_at"))
                for row in rows
            ]))
        if path is None:
            return merge_open_operations_with_signals(txt_name, [])

        try:
            return merge_open_operations_with_signals(txt_name, deduplicate_signals([
                parse_signal_line(line.strip(), path.stat().st_mtime)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and signal_line_is_today(line.strip())
            ]))
        except OSError:
            return merge_open_operations_with_signals(txt_name, [])

    def merge_open_operations_with_signals(txt_name, signals):
        merged = list(signals)
        for signal in merged:
            signal["signal_key"] = build_signal_operation_key(txt_name, signal)
        seen = {signal_identity(signal) for signal in merged}
        for operation in open_operations_for_txt(txt_name):
            line = operation.get("signal_line") or operation_line_as_signal(operation)
            signal = parse_signal_line(line, operation.get("opened_at"))
            signal["from_open_operation"] = True
            signal["open_operation"] = format_simulated_operation(dict(operation))
            signal["signal_key"] = operation.get("operation_key") or build_signal_operation_key(txt_name, signal)
            key = signal_identity(signal)
            if key in seen:
                continue
            seen.add(key)
            merged.append(signal)
        merged.sort(
            key=lambda signal: (
                parse_notice_datetime(signal.get("notice_time"), signal.get("fields", {})).timestamp()
                if parse_notice_datetime(signal.get("notice_time"), signal.get("fields", {}))
                else 0,
                normalize_signal_symbol(signal.get("symbol", "")),
            ),
            reverse=True,
        )
        add_duplicate_signal_labels(merged)
        return merged

    def add_duplicate_signal_labels(signals):
        totals = {}
        counters = {}
        for signal in signals:
            symbol = normalize_signal_symbol(signal.get("symbol", ""))
            if symbol:
                totals[symbol] = totals.get(symbol, 0) + 1
        for signal in signals:
            symbol = normalize_signal_symbol(signal.get("symbol", ""))
            if not symbol:
                signal["display_symbol"] = signal.get("symbol", "")
                continue
            counters[symbol] = counters.get(symbol, 0) + 1
            signal["display_symbol"] = (
                f"{signal.get('symbol', symbol)} #{counters[symbol]}"
                if totals.get(symbol, 0) > 1
                else signal.get("symbol", symbol)
            )

    def open_operations_for_txt(txt_name):
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT strategy_name, txt_name, symbol, direction, status,
                           operation_key,
                           signal_date, signal_line, opened_at, closed_at,
                           entry_price, target_price, stop_loss, shares,
                           current_price, investment_value, profit_usd,
                           profit_pct, close_reason, updated_at
                    FROM simulated_operations
                    WHERE txt_name = :txt_name
                      AND status = 'OPEN'
                    ORDER BY signal_date DESC, opened_at DESC, symbol ASC
                    """
                ),
                {"txt_name": txt_name},
            ).mappings().fetchall()
            return [dict(row) for row in rows]
        except Exception:
            rollback_request_db()
            return []

    def operation_line_as_signal(operation):
        return (
            f"{operation.get('symbol', '')} | "
            f"Direccion: {operation.get('direction', '')} | "
            f"Precio actual: {operation.get('current_price', 0)} | "
            f"Apertura: {operation.get('entry_price', 0)} | "
            f"Cierre: {operation.get('target_price', 0)} | "
            f"Stop Loss: {operation.get('stop_loss', 0)} | "
            f"Fecha: {operation.get('signal_date', '')}"
        )

    def deduplicate_signals(signals):
        unique = []
        seen = set()
        for signal in signals:
            key = signal_identity(signal)
            if key in seen:
                continue
            seen.add(key)
            unique.append(signal)
        return unique

    def signal_identity(signal):
        symbol = normalize_signal_symbol(signal.get("symbol", ""))
        date_value = first_existing(signal.get("fields", {}), ["Fecha"]) or signal.get("notice_datetime", "")
        if symbol:
            line_hash = hashlib.sha1(str(signal.get("line", "")).strip().encode("utf-8")).hexdigest()[:12]
            return ("symbol", symbol, date_value, line_hash)
        return ("line", str(signal.get("line", "")).strip())

    def signal_line_is_today(line):
        date_value = signal_line_field(line, "Fecha")
        if not date_value:
            return False
        return date_value == datetime.now(MADRID_TZ).date().isoformat()

    def signal_line_field(line, field_name):
        expected = f"{field_name.lower()}:"
        for part in line.split("|"):
            part = part.strip()
            if part.lower().startswith(expected):
                return part.split(":", 1)[1].strip()
        return ""

    def parse_signal_line(line, notice_time=None):
        parts = [part.strip() for part in line.split("|") if part.strip()]
        side = ""
        symbol = ""
        field_parts = parts

        if parts:
            first_clean = parts[0].strip().lstrip("-").strip()
            first = first_clean.upper()
            if first in SIGNAL_SIDE_WORDS and len(parts) > 1:
                side = parts[0]
                symbol = parts[1]
                field_parts = parts[2:]
            elif SIGNAL_SYMBOL_RE.match(first_clean):
                symbol = first_clean
                field_parts = parts[1:]

        fields = {}
        for part in field_parts:
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            fields[key.strip()] = value.strip()

        return {
            "line": line,
            "symbol": symbol,
            "side": side,
            "fields": fields,
            "notice_time": notice_time,
            "detail_fields": detail_signal_fields(fields),
            "common": common_signal_fields(side, fields),
            "notice_datetime": format_notice_datetime(notice_time, fields),
            "notice_short_datetime": format_notice_short_datetime(notice_time, fields),
            "is_today_signal": signal_is_from_today(notice_time, fields),
        }

    def detail_signal_fields(fields):
        skip_names = {
            "fecha", "direccion", "dirección", "side", "tipo",
            "precio actual", "precio", "price", "current price",
            "apertura", "entrada", "precio entrada", "entry",
            "salida", "cierre", "tp1", "objetivo", "take profit", "target",
            "stop", "stop loss", "sl",
        }
        detail = {}
        for key, value in fields.items():
            if str(key).strip().lower() in skip_names:
                continue
            detail[key] = value
        return detail

    def format_notice_datetime(notice_time, fields):
        if notice_time:
            if isinstance(notice_time, (int, float)):
                return datetime.fromtimestamp(notice_time, MADRID_TZ).strftime("%d/%m/%Y %H:%M")
            return format_any_madrid_datetime(notice_time)
        date_value = first_existing(fields, ["Fecha"])
        return date_value or "No indicada"

    def format_notice_short_datetime(notice_time, fields):
        date_value = first_existing(fields, ["Fecha"])
        if not notice_time and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_value or "").strip()):
            try:
                parsed_date = datetime.fromisoformat(str(date_value).strip()).date()
                return parsed_date.strftime("%d/%m/%y")
            except ValueError:
                pass
        parsed = parse_notice_datetime(notice_time, fields)
        if parsed:
            return parsed.astimezone(MADRID_TZ).strftime("%H:%M %d/%m/%y")
        return date_value or "Sin fecha"

    def signal_is_from_today(notice_time, fields):
        parsed = parse_notice_datetime(notice_time, fields)
        if parsed:
            return parsed.astimezone(MADRID_TZ).date() == datetime.now(MADRID_TZ).date()
        date_value = first_existing(fields, ["Fecha"])
        return date_value == datetime.now(MADRID_TZ).date().isoformat()

    def parse_notice_datetime(notice_time, fields):
        if notice_time:
            if isinstance(notice_time, (int, float)):
                return datetime.fromtimestamp(notice_time, MADRID_TZ)
            parsed = parse_status_datetime(notice_time)
            if parsed:
                return parsed
        date_value = first_existing(fields, ["Fecha"])
        if not date_value:
            return None
        try:
            parsed = datetime.fromisoformat(str(date_value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MADRID_TZ)
        return parsed

    def common_signal_fields(side, fields):
        return {
            "direccion": side or first_existing(fields, ["Direccion", "Dirección", "Side", "Tipo"]),
            "precio_actual": first_existing(fields, ["Precio actual", "Precio", "Price", "Current Price"]),
            "apertura": first_existing(fields, ["Apertura", "Entrada", "Precio entrada", "Precio actual", "Precio", "Entry"]),
            "cierre": first_existing(fields, ["Salida", "Cierre", "TP1", "Objetivo", "Take Profit", "Target"]),
            "stop": first_existing(fields, ["Stop", "Stop Loss", "SL"]),
        }

    def first_existing(fields, keys):
        lower_fields = {str(key).lower(): value for key, value in fields.items()}
        for key in keys:
            lookup_key = key.lower()
            value = lower_fields.get(lookup_key)
            if value:
                return value
            for field_key, field_value in lower_fields.items():
                if field_key.startswith(f"{lookup_key} ") and field_value:
                    return field_value
        return ""

    def normalize_signal_symbol(symbol):
        return str(symbol).strip().upper().replace(" ", "")

    def simulated_operation_for_signal(strategy, signal):
        txt_name = strategy.get("signals_txt_name", "")
        symbol = normalize_signal_symbol(signal.get("symbol", ""))
        if not txt_name or not symbol:
            return None
        operation_key = build_signal_operation_key(txt_name, signal)
        legacy_key = build_legacy_signal_operation_key(txt_name, signal)
        signal_date = signal_line_field(signal.get("line", ""), "Fecha")
        signal_line = signal.get("line", "")
        operation = simulated_operation_from_database(txt_name, symbol, operation_key, legacy_key, signal_date, signal_line)
        if operation:
            return operation
        return simulated_operation_from_file(
            txt_name,
            symbol,
            operation_key,
            legacy_key,
            signal_date,
            signal_line,
        )

    def simulated_operation_from_database(txt_name, symbol, operation_key="", legacy_key="", signal_date="", signal_line=""):
        try:
            row = g.db.execute(
                text(
                    """
                    SELECT strategy_name, txt_name, symbol, direction, status,
                           operation_key, signal_date, signal_line, opened_at, closed_at, entry_price,
                           target_price, stop_loss, shares, current_price,
                           investment_value, profit_usd, profit_pct,
                           close_reason, updated_at
                    FROM simulated_operations
                    WHERE txt_name = :txt_name
                      AND (
                        operation_key = :operation_key
                        OR operation_key = :legacy_key
                        OR (
                          UPPER(symbol) = :symbol
                          AND signal_date = :signal_date
                        )
                      )
                    ORDER BY
                      CASE
                        WHEN operation_key = :operation_key THEN 0
                        WHEN operation_key = :legacy_key THEN 1
                        WHEN signal_line = :signal_line THEN 2
                        ELSE 3
                      END,
                      CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END,
                      updated_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "txt_name": txt_name,
                    "symbol": symbol,
                    "operation_key": operation_key,
                    "legacy_key": legacy_key,
                    "signal_date": signal_date,
                    "signal_line": signal_line,
                },
            ).mappings().fetchone()
        except Exception:
            rollback_request_db()
            return None
        if not row:
            return None
        return format_simulated_operation(dict(row))

    def operation_opened_sort_key(item):
        opened_at = parse_status_datetime(item.get("opened_at"))
        closed_at = parse_status_datetime(item.get("closed_at"))
        updated_at = parse_status_datetime(item.get("updated_at"))
        primary = opened_at or closed_at or updated_at
        secondary = closed_at or updated_at or opened_at
        return (
            primary.timestamp() if primary else 0,
            secondary.timestamp() if secondary else 0,
            str(item.get("symbol") or ""),
        )

    def closed_operations_for_strategy(txt_name, limit=500, offset=0):
        if not txt_name:
            return []
        operations = []
        loaded_from_database = False
        try:
            params = {"txt_name": txt_name}
            limit_clause = ""
            if limit is not None:
                params["limit"] = int(limit)
                limit_clause = "LIMIT :limit"
            offset_clause = ""
            if offset:
                params["offset"] = int(offset)
                offset_clause = "OFFSET :offset"
            rows = g.db.execute(
                text(
                    f"""
                    SELECT strategy_name, txt_name, symbol, direction, status,
                           signal_date, opened_at, closed_at, entry_price,
                           target_price, stop_loss, shares, current_price,
                           investment_value, profit_usd, profit_pct,
                           close_reason, updated_at
                    FROM simulated_operations
                    WHERE txt_name = :txt_name
                      AND status = 'CLOSED'
                    ORDER BY CASE WHEN opened_at IS NULL THEN 1 ELSE 0 END ASC,
                             opened_at DESC,
                             closed_at DESC,
                             updated_at DESC,
                             symbol ASC
                    {limit_clause}
                    {offset_clause}
                    """
                ),
                params,
            ).mappings().fetchall()
            operations = [format_simulated_operation(dict(row)) for row in rows]
            loaded_from_database = True
        except Exception:
            rollback_request_db()
            operations = closed_operations_from_file(txt_name, limit=None)
        operations.sort(key=operation_opened_sort_key, reverse=True)
        if offset and not loaded_from_database:
            operations = operations[offset:]
        if limit is None:
            return operations
        return operations[:limit]

    def closed_operations_from_file(txt_name, limit=500):
        path = Path(os.environ.get("SIMULATED_OPERATIONS_FILE", DEFAULT_SIMULATED_OPERATIONS_FILE)).resolve()
        try:
            if path != DEFAULT_SIMULATED_OPERATIONS_FILE and BASE_DIR not in path.parents:
                return []
            operations = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = [
            operation
            for operation in operations
            if operation.get("txt_name") == txt_name
            and operation.get("status") == "CLOSED"
        ]
        rows.sort(key=operation_opened_sort_key, reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return [format_simulated_operation(dict(row)) for row in rows]

    def simulated_operation_from_file(txt_name, symbol, operation_key="", legacy_key="", signal_date="", signal_line=""):
        path = Path(os.environ.get("SIMULATED_OPERATIONS_FILE", DEFAULT_SIMULATED_OPERATIONS_FILE)).resolve()
        try:
            if path != DEFAULT_SIMULATED_OPERATIONS_FILE and BASE_DIR not in path.parents:
                return None
            operations = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        matches = []
        for operation in operations:
            if operation.get("txt_name") != txt_name:
                continue
            if normalize_signal_symbol(operation.get("symbol", "")) != symbol:
                continue
            op_key = operation.get("operation_key", "")
            if op_key in {operation_key, legacy_key}:
                matches.append(operation)
                continue
            if signal_line and operation.get("signal_line") == signal_line:
                matches.append(operation)
                continue
            if signal_date and operation.get("signal_date") == signal_date:
                matches.append(operation)
        if not matches:
            return None
        open_matches = [item for item in matches if item.get("status") == "OPEN"]
        selected = open_matches or matches
        selected.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return format_simulated_operation(selected[0])

    def build_signal_operation_key(txt_name, signal):
        line_hash = hashlib.sha1(str(signal.get("line", "")).strip().encode("utf-8")).hexdigest()[:12]
        return "|".join(
            [
                txt_name,
                normalize_signal_symbol(signal.get("symbol", "")),
                normalize_operation_side(signal.get("side") or signal.get("common", {}).get("direccion") or "LONG"),
                signal_line_field(signal.get("line", ""), "Fecha") or "sin_fecha",
                line_hash,
            ]
        )

    def build_legacy_signal_operation_key(txt_name, signal):
        return "|".join(
            [
                txt_name,
                normalize_signal_symbol(signal.get("symbol", "")),
                normalize_operation_side(signal.get("side") or signal.get("common", {}).get("direccion") or "LONG"),
                signal_line_field(signal.get("line", ""), "Fecha") or "sin_fecha",
            ]
        )

    def normalize_operation_side(value):
        normalized = str(value or "").strip().upper()
        if normalized in {"BUY", "COMPRA"}:
            return "LONG"
        if normalized in {"SELL", "VENTA"}:
            return "SHORT"
        return normalized or "LONG"

    def format_simulated_operation(operation):
        profit_pct = parse_display_float(operation.get("profit_pct"))
        status = operation.get("status", "")
        return {
            **operation,
            "opened_at_display": format_any_madrid_datetime(operation.get("opened_at")),
            "closed_at_display": format_any_madrid_datetime(operation.get("closed_at")),
            "updated_at_display": format_any_madrid_datetime(operation.get("updated_at")),
            "entry_price_display": f"{parse_display_float(operation.get('entry_price')):.2f} USD",
            "target_price_display": f"{parse_display_float(operation.get('target_price')):.2f} USD",
            "stop_loss_display": f"{parse_display_float(operation.get('stop_loss')):.2f} USD",
            "shares_display": f"{parse_display_float(operation.get('shares')):.4f}",
            "current_price_display": f"{parse_display_float(operation.get('current_price')):.2f} USD",
            "exit_price_display": f"{parse_display_float(operation.get('current_price')):.2f} USD",
            "investment_value_display": format_money_usd(operation.get("investment_value")),
            "profit_usd_display": format_signed_money_usd(operation.get("profit_usd")),
            "profit_pct_display": f"{profit_pct:.2f}%",
            "profit_class": "text-success" if profit_pct >= 0 else "text-danger",
            "status_label": "Abierta" if status == "OPEN" else "Cerrada",
        }

    def format_any_madrid_datetime(value):
        if not value:
            return "No indicada"
        if isinstance(value, datetime):
            dt_value = value
        else:
            try:
                dt_value = datetime.fromisoformat(str(value))
            except ValueError:
                return str(value)
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=UTC)
        return dt_value.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def build_signal_diagnostic(strategy, signal):
        fields = signal.get("fields", {})
        strategy_name = strategy["name"].lower()
        points = []

        if signal.get("side"):
            points.append(f"Direccion detectada: {signal['side']}.")
        if "Precio" in fields:
            points.append(f"Precio de referencia del aviso: {fields['Precio']}.")
        if "Score" in fields:
            points.append(f"Score de la estrategia: {fields['Score']}. Cuanto mayor sea, mas arriba quedo en el ranking interno.")
        if "Vol$" in fields:
            points.append(f"Volumen monetario observado: {fields['Vol$']}.")
        if "Vol xMedia" in fields:
            points.append(f"Volumen relativo frente a la media: {fields['Vol xMedia']}x.")
        if "Stop" in fields:
            points.append(f"Nivel tecnico de stop sugerido por el modelo: {fields['Stop']}.")
        if "TP1" in fields:
            points.append(f"Primer objetivo tecnico: {fields['TP1']}.")
        if "TP2" in fields:
            points.append(f"Segundo objetivo tecnico: {fields['TP2']}.")
        if "TP1 VWAP" in fields:
            points.append(f"Primer objetivo hacia VWAP: {fields['TP1 VWAP']}.")

        if "momentum" in strategy_name:
            focus = "El diagnostico se centra en fuerza relativa, impulso del precio y volumen."
        elif "breakout" in strategy_name or "gap" in strategy_name:
            focus = "El diagnostico se centra en ruptura, rango inicial, gap y continuidad del movimiento."
        elif "reversion" in strategy_name:
            focus = "El diagnostico se centra en sobreextension, vuelta a medias/VWAP y agotamiento."
        elif "value" in strategy_name or "quality" in strategy_name or "dividend" in strategy_name:
            focus = "El diagnostico se centra en filtros de calidad/fundamentales y confirmacion tecnica."
        elif "pairs" in strategy_name:
            focus = "El diagnostico se centra en relacion estadistica entre activos, z-score y convergencia."
        elif "sector" in strategy_name:
            focus = "El diagnostico se centra en fuerza relativa sectorial y liderazgo dentro del sector."
        else:
            focus = "El diagnostico resume los datos clave generados por la estrategia."

        if not points:
            points.append("El aviso no trae campos estructurados suficientes; se muestra la linea original para revision manual.")

        return {
            "focus": focus,
            "points": points,
            "warning": "Lectura automatica informativa. No es asesoramiento financiero ni recomendacion de compra o venta.",
        }

    def attach_v2_diagnostics_to_signal(signal, strategy=None):
        symbol = normalize_signal_symbol(signal.get("symbol", ""))
        if not symbol:
            signal["diagnostic_all_fields"] = []
            signal["diagnostic_key_fields"] = []
            return
        metrics = v2_diagnostics_for_symbol(symbol)
        strategy_keys = diagnostic_strategy_keys(strategy, signal)
        if not metrics:
            signal["diagnostic_all_fields"] = diagnostic_all_rows_from_signal(signal, strategy_keys)
            signal["diagnostic_key_fields"] = diagnostic_rows_from_detail_fields(signal, strategy_keys)
            return

        rows = []
        key_rows = []
        for key, value in sorted(metrics.items()):
            if value is None:
                continue
            is_strategy_param = normalize_field_name(key) in strategy_keys
            row = {
                "key": key,
                "value": format_diagnostic_metric(value),
                "is_strategy_param": is_strategy_param,
            }
            if is_strategy_param:
                key_rows.append(row)
            rows.append(
                row
            )
        signal["diagnostic_all_fields"] = rows
        signal["diagnostic_key_fields"] = key_rows or diagnostic_rows_from_detail_fields(signal, strategy_keys)

    def diagnostic_strategy_keys(strategy, signal):
        keys = {
            normalize_field_name(key)
            for key in signal.get("detail_fields", {})
        }
        strategy_name = normalize_field_name((strategy or {}).get("name", ""))
        strategy_metric_keys = {
            "momentum": [
                "daily_sma20", "daily_sma50", "momentum_20d_pct", "momentum_60d_pct",
                "relative_strength_60d_pct", "avg_dollar_volume_20d",
            ],
            "swing_trading": [
                "daily_sma20", "daily_sma50", "daily_rsi14", "momentum_50d_pct",
                "high_20d_including_current", "recent_high_10d", "recent_low_5d",
            ],
            "breakout": [
                "resistance_20d", "breakout_20d_pct", "daily_sma20", "daily_sma50",
                "volume_ratio_vs_20d", "avg_dollar_volume_20d",
            ],
            "mean_reversion": [
                "daily_rsi14", "daily_sma20", "daily_sma100", "distance_daily_sma20_pct",
                "distance_daily_sma100_pct", "daily_bollinger_lower20", "daily_bollinger_mid20",
                "daily_atr14",
            ],
            "value_trading": [
                "fmp_pe_ratio", "fmp_pb_ratio", "fmp_ps_ratio", "fmp_roe_pct",
                "fmp_debt_to_equity", "fmp_revenue_growth_pct", "daily_sma20", "daily_sma50",
            ],
            "dividend_growth": [
                "fmp_dividend_yield_pct", "fmp_payout_ratio_pct", "fmp_dividend_growth_3y_pct",
                "fmp_roe_pct", "fmp_debt_to_equity", "fmp_revenue_growth_pct",
                "daily_sma50", "daily_sma100",
            ],
            "trend_following": [
                "daily_sma50", "daily_sma200", "daily_sma200_slope_20d_pct",
                "resistance_55d", "daily_atr14", "momentum_60d_pct",
            ],
            "sector_rotation": [
                "fmp_sector", "relative_strength_60d_pct", "relative_strength_60d_pct_percentile",
                "avg_dollar_volume_20d_percentile", "daily_sma50",
            ],
            "quality_investing": [
                "fmp_roe_pct", "fmp_roic_pct", "fmp_operating_margin_pct",
                "fmp_net_margin_pct", "fmp_debt_to_equity", "fmp_revenue_growth_pct",
                "fmp_eps_growth_pct", "fmp_pe_ratio", "fmp_ps_ratio", "daily_sma50", "daily_sma100",
            ],
            "opening_range_breakout": [
                "opening_range_15m_high", "opening_range_15m_low",
                "opening_range_15m_breakout_pct", "opening_range_15m_breakdown_pct",
                "opening_range_15m_dollar_volume", "intraday_1m_volume_ratio_20m",
            ],
            "vwap_reversion": [
                "intraday_1m_vwap", "intraday_1m_distance_vwap_pct", "intraday_1m_rsi14",
                "intraday_1m_volume_ratio_20m", "intraday_day_dollar_volume",
            ],
            "momentum_intradia": [
                "intraday_1m_vwap", "intraday_1m_momentum_15m_pct",
                "intraday_1m_volume_ratio_20m", "intraday_day_dollar_volume",
                "intraday_1m_recent_high_20m", "intraday_1m_recent_low_20m",
            ],
            "scalping_the_pullbacks": [
                "intraday_1m_ema9", "intraday_1m_ema21", "intraday_1m_vwap",
                "intraday_1m_rsi14", "intraday_1m_volume_ratio_20m", "intraday_day_dollar_volume",
            ],
            "gap_and_go": [
                "daily_gap_pct", "previous_close", "opening_range_15m_high",
                "opening_range_15m_low", "opening_range_15m_dollar_volume",
                "opening_range_15m_breakout_pct", "opening_range_15m_breakdown_pct",
                "intraday_1m_volume_ratio_20m",
            ],
            "follow_the_money": [
                "current_dollar_volume", "prev_avg_dollar_volume_21d",
                "prev_avg_dollar_volume_42d", "prev_avg_dollar_volume_63d",
                "dollar_volume_ratio_vs_prev_21d", "dollar_volume_ratio_vs_prev_42d",
                "dollar_volume_ratio_vs_prev_63d", "dollar_volume_ratio_vs_prev_21d_rank",
            ],
            "acumula_metales": [
                "daily_sma180", "weekly_sma120", "daily_rsi14",
                "distance_daily_sma180_pct", "distance_weekly_sma120_pct",
            ],
            "acumulacion": [
                "daily_sma180", "weekly_sma120", "daily_rsi14",
                "distance_daily_sma180_pct", "distance_weekly_sma120_pct",
            ],
            "reversion_rsi_5": [
                "intraday_1m_rsi5", "intraday_1m_rsi14", "intraday_1m_distance_sma120_pct",
                "intraday_1m_sma120",
            ],
        }
        for name, metric_keys in strategy_metric_keys.items():
            if strategy_name == name or name in strategy_name:
                keys.update(normalize_field_name(key) for key in metric_keys)
        return keys

    def diagnostic_rows_from_detail_fields(signal, strategy_keys=None):
        rows = []
        for key, value in signal.get("detail_fields", {}).items():
            rows.append(
                {
                    "key": key,
                    "value": value,
                    "is_strategy_param": True,
                }
            )
        return rows

    def diagnostic_all_rows_from_signal(signal, strategy_keys=None):
        strategy_keys = strategy_keys or set()
        rows = []
        common = signal.get("common", {})
        base_fields = {
            "Ticker": signal.get("symbol", ""),
            "Direccion": common.get("direccion", ""),
            "Precio actual": common.get("precio_actual", ""),
            "Entrada": common.get("apertura", ""),
            "Cierre objetivo": common.get("cierre", ""),
            "Stop loss": common.get("stop", ""),
            "Fecha aviso": signal.get("notice_datetime", ""),
        }
        for key, value in base_fields.items():
            if value:
                rows.append(
                    {
                        "key": key,
                        "value": value,
                        "is_strategy_param": normalize_field_name(key) in strategy_keys,
                    }
                )
        for key, value in signal.get("fields", {}).items():
            if value:
                rows.append(
                    {
                        "key": key,
                        "value": value,
                        "is_strategy_param": normalize_field_name(key) in strategy_keys,
                    }
                )
        if signal.get("line"):
            rows.append(
                {
                    "key": "Linea original",
                    "value": signal["line"],
                    "is_strategy_param": False,
                }
            )
        return rows

    def v2_diagnostics_for_symbol(symbol):
        diagnostics_path = Path(os.environ.get("TRADING_V2_DIAGNOSTICS_FILE", DEFAULT_V2_DIAGNOSTICS_FILE)).resolve()
        if diagnostics_path.exists() and diagnostics_path.is_file():
            try:
                payload = json.loads(diagnostics_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            tickers = payload.get("tickers", {}) if isinstance(payload, dict) else {}
            if isinstance(tickers, dict):
                metrics = diagnostics_metrics_from_mapping(tickers, symbol)
                if metrics:
                    return metrics
        txt_metrics = v2_diagnostics_from_txt(symbol)
        if txt_metrics:
            return txt_metrics
        return v2_diagnostics_from_database(symbol)

    def diagnostics_metrics_from_mapping(tickers, symbol):
        normalized_symbol = symbol.upper()
        metrics = tickers.get(normalized_symbol, {}) or {}
        if metrics:
            return metrics
        base_symbol = re.split(r"[/,]", normalized_symbol, maxsplit=1)[0].strip()
        if base_symbol and base_symbol != normalized_symbol:
            return tickers.get(base_symbol, {}) or {}
        return {}

    def v2_diagnostics_from_txt(symbol):
        diagnostics_path = Path(os.environ.get("TRADING_V2_DIAGNOSTICS_TXT_FILE", DEFAULT_V2_DIAGNOSTICS_TXT_FILE)).resolve()
        if not diagnostics_path.exists() or not diagnostics_path.is_file():
            return {}
        normalized_symbol = symbol.upper()
        candidates = [normalized_symbol]
        base_symbol = re.split(r"[/,]", normalized_symbol, maxsplit=1)[0].strip()
        if base_symbol and base_symbol not in candidates:
            candidates.append(base_symbol)
        try:
            return diagnostics_txt_metrics_for_symbols(diagnostics_path, candidates)
        except OSError:
            return {}

    def diagnostics_txt_metrics_for_symbols(path, symbols):
        current_symbol = ""
        metrics = {}
        wanted = set(symbols)
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                if current_symbol and metrics:
                    break
                continue
            match = re.fullmatch(r"\[([A-Z0-9./-]+)\]", line)
            if match:
                current_symbol = match.group(1).upper()
                metrics = {}
                continue
            if current_symbol not in wanted or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key:
                metrics[key] = parse_diagnostic_txt_value(value)
        return metrics if current_symbol in wanted else {}

    def parse_diagnostic_txt_value(value):
        text_value = str(value or "").strip()
        if text_value.lower() in {"null", "none", "nan", ""}:
            return ""
        try:
            return float(text_value)
        except ValueError:
            return text_value

    def v2_diagnostics_from_database(symbol):
        normalized_symbol = symbol.upper()
        candidates = [normalized_symbol]
        base_symbol = re.split(r"[/,]", normalized_symbol, maxsplit=1)[0].strip()
        if base_symbol and base_symbol not in candidates:
            candidates.append(base_symbol)
        try:
            with engine.connect() as connection:
                for candidate in candidates:
                    row = connection.execute(
                        text(
                            """
                            SELECT metrics_json
                            FROM strategy_diagnostics
                            WHERE symbol = :symbol
                            LIMIT 1
                            """
                        ),
                        {"symbol": candidate},
                    ).mappings().fetchone()
                    if not row:
                        continue
                    try:
                        metrics = json.loads(row["metrics_json"] or "{}")
                    except json.JSONDecodeError:
                        metrics = {}
                    if isinstance(metrics, dict):
                        return metrics
        except Exception:
            return {}
        return {}

    def normalize_field_name(value):
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    def format_diagnostic_metric(value):
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    def generate_ai_signal_analysis(strategy, signal, diagnostic):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return ""

        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip()
        prompt = build_ai_analysis_prompt(strategy, signal, diagnostic)
        payload = {
            "model": model,
            "input": prompt,
            "max_output_tokens": 260,
        }
        request_data = json.dumps(payload).encode("utf-8")
        request_obj = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=request_data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            return f"No se pudo generar analisis IA ahora mismo: {error}"

        text_output = data.get("output_text", "").strip()
        if text_output:
            return text_output

        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunks).strip()

    def build_ai_analysis_prompt(strategy, signal, diagnostic):
        fields = "\n".join(
            f"- {key}: {value}"
            for key, value in signal.get("fields", {}).items()
        ) or "- Sin campos estructurados."
        points = "\n".join(f"- {point}" for point in diagnostic["points"])
        return f"""
Eres un analista tecnico prudente. Redacta un analisis breve en espanol para una web de senales de trading.

No des asesoramiento financiero. No digas que hay que comprar o vender. No prometas resultados.
Usa solo los datos recibidos. Si falta informacion, dilo.

Estrategia: {strategy['name']}
Descripcion estrategia: {strategy['description']}
Riesgo estrategia: {strategy['risk_level']}
Ticker: {signal['symbol']}
Direccion detectada: {signal.get('side') or 'No indicada'}
Aviso original: {signal['line']}

Campos:
{fields}

Diagnostico automatico:
{points}

Devuelve 4 bloques cortos:
1. Lectura rapida
2. Puntos a favor
3. Riesgos o dudas
4. Niveles/datos a vigilar
""".strip()

    def strategy_signals_updated_at(txt_name):
        updated_at = strategy_signals_updated_at_datetime(txt_name)
        return format_madrid_datetime(updated_at)

    def format_madrid_datetime(value):
        if value is None:
            return ""
        return value.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M")

    def strategy_signals_updated_at_datetime(txt_name):
        path = strategy_signals_path(txt_name)
        if path is None:
            row = g.db.execute(
                text(
                    """
                    SELECT MAX(created_at) AS updated_at
                    FROM strategy_signals
                    WHERE txt_name = :txt_name
                    """
                ),
                {"txt_name": txt_name},
            ).mappings().fetchone()
            return parse_utc_database_datetime(row["updated_at"]) if row and row["updated_at"] else None

        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return None

    def latest_open_operation_datetime(txt_name):
        if not txt_name:
            return None
        try:
            rows = g.db.execute(
                text(
                    """
                    SELECT opened_at, signal_date, updated_at
                    FROM simulated_operations
                    WHERE txt_name = :txt_name
                      AND status = 'OPEN'
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """
                ),
                {"txt_name": txt_name},
            ).mappings().fetchall()
        except Exception:
            rollback_request_db()
            return None

        latest = None
        for row in rows:
            parsed = (
                parse_status_datetime(row.get("opened_at"))
                or parse_status_datetime(row.get("signal_date"))
                or parse_status_datetime(row.get("updated_at"))
            )
            if parsed and (latest is None or parsed > latest):
                latest = parsed
        return latest

    def parse_utc_database_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
        else:
            parsed = parse_status_datetime(value)
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def strategy_signals_path(txt_name):
        if not txt_name or not valid_txt_name(txt_name):
            return None

        signals_dir = Path(os.environ.get("STRATEGY_SIGNALS_DIR", DEFAULT_SIGNALS_DIR)).resolve()
        path = (signals_dir / txt_name).resolve()

        try:
            if signals_dir not in path.parents and path != signals_dir:
                return None
            if not path.exists() or not path.is_file():
                return None
        except OSError:
            return None
        return path

    def valid_txt_name(txt_name):
        path = Path(txt_name)
        return (
            path.name == txt_name
            and txt_name.lower().endswith(".txt")
            and "/" not in txt_name
            and "\\" not in txt_name
        )

    return app


def init_db():
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS strategies (
                    id {id_column},
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    risk_level TEXT NOT NULL CHECK (risk_level IN ('Bajo', 'Medio', 'Alto')),
                    signal_frequency TEXT NOT NULL DEFAULT '',
                    historical_return TEXT NOT NULL DEFAULT '',
                    telegram_url TEXT NOT NULL DEFAULT '',
                    has_telegram INTEGER NOT NULL DEFAULT 1,
                    signals_txt_name TEXT NOT NULL DEFAULT '',
                    python_file TEXT NOT NULL DEFAULT '',
                    auto_execute INTEGER NOT NULL DEFAULT 0,
                    schedule_start_time TEXT NOT NULL DEFAULT '15:30',
                    schedule_end_time TEXT NOT NULL DEFAULT '21:30',
                    schedule_interval_minutes INTEGER NOT NULL DEFAULT 30,
                    schedule_last_run_key TEXT NOT NULL DEFAULT '',
                    schedule_last_run_at TIMESTAMP,
                    schedule_last_status TEXT NOT NULL DEFAULT '',
                    schedule_last_message TEXT NOT NULL DEFAULT '',
                    run_status TEXT NOT NULL DEFAULT '',
                    run_message TEXT NOT NULL DEFAULT '',
                    run_at TIMESTAMP,
                    run_txt_updated INTEGER NOT NULL DEFAULT 0,
                    run_returncode INTEGER,
                    include_in_totalizer INTEGER NOT NULL DEFAULT 0,
                    public_visible INTEGER NOT NULL DEFAULT 0,
                    run_locally INTEGER NOT NULL DEFAULT 1,
                    closed_operations_count INTEGER NOT NULL DEFAULT 0,
                    average_close_duration TEXT NOT NULL DEFAULT '',
                    success_rate TEXT NOT NULL DEFAULT '',
                    first_operation_display TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        ensure_users_table(connection)
        ensure_user_strategy_selections_table(connection)
        ensure_user_simulator_tables(connection)
        add_user_column(connection, "age_confirmed", "INTEGER NOT NULL DEFAULT 0")
        add_user_column(connection, "risk_accepted", "INTEGER NOT NULL DEFAULT 0")
        add_user_column(connection, "accepted_terms_at", "TIMESTAMP")
        add_user_column(connection, "membership_plan", "TEXT NOT NULL DEFAULT 'Miembro'")
        add_user_column(connection, "membership_amount", "TEXT NOT NULL DEFAULT ''")
        add_user_column(connection, "membership_started_at", "TIMESTAMP")
        add_user_column(connection, "membership_expires_at", "TIMESTAMP")
        add_user_column(connection, "admin_notes", "TEXT NOT NULL DEFAULT ''")
        add_user_column(connection, "stripe_customer_id", "TEXT NOT NULL DEFAULT ''")
        add_user_column(connection, "stripe_subscription_id", "TEXT NOT NULL DEFAULT ''")
        ensure_payments_table(connection)
        ensure_universe_table(connection)
        ensure_strategy_signals_table(connection)
        ensure_simulated_operations_table(connection)
        ensure_top_money_volume_table(connection)
        ensure_strategy_diagnostics_table(connection)
        ensure_market_news_table(connection)
        ensure_execution_status_table(connection)
        ensure_upload_file_status_table(connection)
        ensure_chip_status_table(connection)
        add_strategy_column(connection, "signals_txt_name")
        add_strategy_column(connection, "has_telegram", "INTEGER NOT NULL DEFAULT 1")
        add_strategy_column(connection, "python_file")
        add_strategy_column(connection, "auto_execute", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "schedule_start_time", "TEXT NOT NULL DEFAULT '15:30'")
        add_strategy_column(connection, "schedule_end_time", "TEXT NOT NULL DEFAULT '21:30'")
        add_strategy_column(connection, "schedule_interval_minutes", "INTEGER NOT NULL DEFAULT 30")
        add_strategy_column(connection, "schedule_last_run_key")
        add_strategy_column(connection, "schedule_last_run_at", "TIMESTAMP")
        add_strategy_column(connection, "schedule_last_status")
        add_strategy_column(connection, "schedule_last_message")
        add_strategy_column(connection, "run_status")
        add_strategy_column(connection, "run_message")
        add_strategy_column(connection, "run_at", "TIMESTAMP")
        add_strategy_column(connection, "run_txt_updated", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "run_returncode", "INTEGER")
        add_strategy_column(connection, "include_in_totalizer", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "public_visible", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "run_locally", "INTEGER NOT NULL DEFAULT 1")
        add_strategy_column(connection, "closed_operations_count", "INTEGER NOT NULL DEFAULT 0")
        add_strategy_column(connection, "average_close_duration", "TEXT NOT NULL DEFAULT ''")
        add_strategy_column(connection, "success_rate", "TEXT NOT NULL DEFAULT ''")
        add_strategy_column(connection, "first_operation_display", "TEXT NOT NULL DEFAULT ''")
        ensure_default_real_strategies(connection)

        count = connection.execute(text("SELECT COUNT(*) FROM strategies")).scalar_one()
        if count == 0:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :is_active)
                    """
                ),
                [
                    {
                        "name": "Momentum Intradia",
                        "description": "Entrada en activos con ruptura de volumen y confirmacion de tendencia a corto plazo.",
                        "risk_level": "Medio",
                        "signal_frequency": "3-6 senales por semana",
                        "historical_return": "+18.4% anualizado",
                        "telegram_url": "https://t.me/tu_canal_momentum",
                        "is_active": 1,
                    },
                    {
                        "name": "Swing Conservador",
                        "description": "Operativa de varios dias con gestion estricta del riesgo y objetivos parciales.",
                        "risk_level": "Bajo",
                        "signal_frequency": "1-3 senales por semana",
                        "historical_return": "+11.2% anualizado",
                        "telegram_url": "https://t.me/tu_canal_swing",
                        "is_active": 1,
                    },
                    {
                        "name": "Crypto Breakout",
                        "description": "Seguimiento de rupturas en criptomonedas liquidas con stops dinamicos.",
                        "risk_level": "Alto",
                        "signal_frequency": "5-10 senales por semana",
                        "historical_return": "+34.7% anualizado",
                        "telegram_url": "https://t.me/tu_canal_crypto",
                        "is_active": 1,
                    },
                ],
            )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS asset_snapshots (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    market TEXT NOT NULL,
                    price FLOAT NOT NULL,
                    money_volume FLOAT NOT NULL,
                    money_volume_1m FLOAT NOT NULL DEFAULT 0,
                    money_volume_2m FLOAT NOT NULL DEFAULT 0,
                    money_volume_3m FLOAT NOT NULL DEFAULT 0,
                    day_money_volume FLOAT NOT NULL DEFAULT 0,
                    week_money_volume FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_1d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_2d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_3d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_4d FLOAT NOT NULL DEFAULT 0,
                    day_money_volume_5d FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_1w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_2w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_3w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_4w FLOAT NOT NULL DEFAULT 0,
                    week_money_volume_5w FLOAT NOT NULL DEFAULT 0,
                    day_volume_score FLOAT NOT NULL,
                    week_volume_score FLOAT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        for column_name in [
            "money_volume_1m",
            "money_volume_2m",
            "money_volume_3m",
            "day_money_volume",
            "week_money_volume",
            "day_money_volume_1d",
            "day_money_volume_2d",
            "day_money_volume_3d",
            "day_money_volume_4d",
            "day_money_volume_5d",
            "week_money_volume_1w",
            "week_money_volume_2w",
            "week_money_volume_3w",
            "week_money_volume_4w",
            "week_money_volume_5w",
        ]:
            add_asset_snapshot_column(connection, column_name)
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS active_visitors (
                    visitor_id TEXT PRIMARY KEY,
                    last_seen TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS automation_schedules (
                    id {id_column},
                    task_name TEXT NOT NULL UNIQUE,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    start_time TEXT NOT NULL DEFAULT '15:30',
                    runs_per_day INTEGER NOT NULL DEFAULT 1,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5',
                    batch_cursor INTEGER NOT NULL DEFAULT 0,
                    last_run_key TEXT NOT NULL DEFAULT '',
                    last_run_at TIMESTAMP,
                    last_status TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        add_automation_schedule_column(connection, "weekdays", f"TEXT NOT NULL DEFAULT '{DEFAULT_WEEKDAYS}'")
        add_automation_schedule_column(connection, "batch_cursor", "INTEGER NOT NULL DEFAULT 0")
        existing_schedules = {
            row[0]
            for row in connection.execute(
                text("SELECT task_name FROM automation_schedules")
            ).fetchall()
        }
        for task_name in SCHEDULER_TASKS:
            if task_name in existing_schedules:
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO automation_schedules
                    (task_name, is_enabled, start_time, runs_per_day, interval_minutes, weekdays)
                    VALUES (:task_name, 0, '15:30', 1, 60, :weekdays)
                    """
                ),
                {"task_name": task_name, "weekdays": DEFAULT_WEEKDAYS},
            )


def ensure_default_real_strategies(connection):
    existing = {
        row["name"]: row
        for row in connection.execute(
            text("SELECT name, telegram_url FROM strategies")
        ).mappings().fetchall()
    }
    for strategy in DEFAULT_REAL_STRATEGIES:
        schedule = DEFAULT_STRATEGY_SCHEDULES.get(
            strategy["name"],
            {"start": "15:30", "end": "21:30", "interval": 30},
        )
        strategy = {
            **strategy,
            "python_file": DEFAULT_STRATEGY_FILES.get(strategy["name"], ""),
            "schedule_start_time": schedule["start"],
            "schedule_end_time": schedule["end"],
            "schedule_interval_minutes": schedule["interval"],
        }
        if strategy["name"] not in existing:
            connection.execute(
                text(
                    """
                    INSERT INTO strategies
                    (name, description, risk_level, signal_frequency,
                     historical_return, telegram_url, signals_txt_name, python_file,
                     schedule_start_time, schedule_end_time, schedule_interval_minutes, is_active)
                    VALUES (:name, :description, :risk_level, :signal_frequency,
                            :historical_return, :telegram_url, :signals_txt_name, :python_file,
                            :schedule_start_time, :schedule_end_time, :schedule_interval_minutes, 1)
                    """
                ),
                strategy,
            )
            continue

        connection.execute(
            text(
                """
                UPDATE strategies
                SET description = :description,
                    risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = CASE
                        WHEN historical_return = '' THEN :historical_return
                        ELSE historical_return
                    END,
                    signals_txt_name = :signals_txt_name,
                    python_file = CASE
                        WHEN python_file = '' THEN :python_file
                        ELSE python_file
                    END,
                    schedule_start_time = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_start_time
                        ELSE schedule_start_time
                    END,
                    schedule_end_time = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_end_time
                        ELSE schedule_end_time
                    END,
                    schedule_interval_minutes = CASE
                        WHEN schedule_start_time IN ('', '15:30') AND schedule_end_time IN ('', '21:30') AND schedule_interval_minutes = 30
                        THEN :schedule_interval_minutes
                        ELSE schedule_interval_minutes
                    END
                WHERE name = :name
                """
            ),
            strategy,
        )


def apply_recommended_strategy_schedules(connection):
    for name, schedule in DEFAULT_STRATEGY_SCHEDULES.items():
        connection.execute(
            text(
                """
                UPDATE strategies
                SET schedule_start_time = :start_time,
                    schedule_end_time = :end_time,
                    schedule_interval_minutes = :interval_minutes
                WHERE name = :name
                """
            ),
            {
                "name": name,
                "start_time": schedule["start"],
                "end_time": schedule["end"],
                "interval_minutes": schedule["interval"],
            },
        )


def add_asset_snapshot_column(connection, column_name):
    if asset_snapshot_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE asset_snapshots ADD COLUMN {column_name} FLOAT NOT NULL DEFAULT 0"
        )
    )


def add_automation_schedule_column(connection, column_name, definition):
    if automation_schedule_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE automation_schedules ADD COLUMN {column_name} {definition}"
        )
    )


def ensure_strategy_signals_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
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
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_strategy_signals_txt_date
            ON strategy_signals(txt_name, signal_date, created_at DESC, id DESC)
            """
        )
    )


def ensure_simulated_operations_table(connection):
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
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_sim_ops_txt_status
            ON simulated_operations(txt_name, status)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_sim_ops_txt_symbol_date
            ON simulated_operations(txt_name, UPPER(symbol), signal_date)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_sim_ops_txt_updated
            ON simulated_operations(txt_name, updated_at DESC)
            """
        )
    )


def ensure_top_money_volume_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS top_money_volume_assets (
                asset_rank INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT '',
                price FLOAT NOT NULL DEFAULT 0,
                money_volume FLOAT NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_strategy_diagnostics_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS strategy_diagnostics (
                symbol TEXT PRIMARY KEY,
                metrics_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_market_news_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS market_news (
                id {id_column},
                title TEXT NOT NULL,
                title_es TEXT NOT NULL DEFAULT '',
                title_en TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL UNIQUE,
                published_at TIMESTAMP,
                summary TEXT NOT NULL DEFAULT '',
                summary_es TEXT NOT NULL DEFAULT '',
                summary_en TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT 'neutral',
                symbols TEXT NOT NULL DEFAULT '',
                sector_tags TEXT NOT NULL DEFAULT '',
                ai_used INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    for name in ("title_es", "title_en", "summary_es", "summary_en"):
        if market_news_column_exists(connection, name):
            continue
        connection.execute(text(f"ALTER TABLE market_news ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"))


def market_news_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'market_news'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        ).scalar()
        return bool(result)
    result = connection.execute(text("PRAGMA table_info(market_news)")).fetchall()
    return any(row[1] == column_name for row in result)


def ensure_execution_status_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS execution_status (
                task_key TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'IDLE',
                last_finished_at TIMESTAMP,
                last_returncode INTEGER,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_upload_file_status_table(connection):
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS upload_file_status (
                label TEXT PRIMARY KEY,
                exists_flag INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                latest_name TEXT NOT NULL DEFAULT '',
                latest_updated_at TIMESTAMP,
                synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def ensure_users_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id {id_column},
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                has_access INTEGER NOT NULL DEFAULT 1,
                payment_status TEXT NOT NULL DEFAULT 'trial',
                membership_plan TEXT NOT NULL DEFAULT 'Miembro',
                membership_amount TEXT NOT NULL DEFAULT '',
                membership_started_at TIMESTAMP,
                membership_expires_at TIMESTAMP,
                admin_notes TEXT NOT NULL DEFAULT '',
                stripe_customer_id TEXT NOT NULL DEFAULT '',
                stripe_subscription_id TEXT NOT NULL DEFAULT '',
                age_confirmed INTEGER NOT NULL DEFAULT 0,
                risk_accepted INTEGER NOT NULL DEFAULT 0,
                accepted_terms_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_payments_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS payments (
                id {id_column},
                user_id INTEGER,
                product_key TEXT NOT NULL DEFAULT '',
                plan_key TEXT NOT NULL DEFAULT '',
                subject_type TEXT NOT NULL DEFAULT '',
                subject_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'stripe',
                provider_session_id TEXT NOT NULL DEFAULT '',
                provider_customer_id TEXT NOT NULL DEFAULT '',
                provider_subscription_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'created',
                mode TEXT NOT NULL DEFAULT 'payment',
                amount_text TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT 'USD',
                metadata_json TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def ensure_user_strategy_selections_table(connection):
    id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS user_strategy_selections (
                id {id_column},
                user_id INTEGER NOT NULL,
                strategy_id INTEGER NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, strategy_id)
            )
            """
        )
    )


def ensure_user_simulator_tables(connection):
    settings_id_column = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    selection_id_column = settings_id_column
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS user_simulator_settings (
                id {settings_id_column},
                user_id INTEGER NOT NULL UNIQUE,
                initial_capital FLOAT NOT NULL DEFAULT 10000,
                monthly_contribution FLOAT NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS user_simulator_strategies (
                id {selection_id_column},
                user_id INTEGER NOT NULL,
                strategy_id INTEGER NOT NULL,
                selected INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, strategy_id)
            )
            """
        )
    )


def add_user_column(connection, column_name, definition):
    if user_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE users ADD COLUMN {column_name} {definition}"
        )
    )


def user_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'users'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(users)")).fetchall()
    return any(row[1] == column_name for row in rows)


def automation_schedule_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'automation_schedules'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(automation_schedules)")).fetchall()
    return any(row[1] == column_name for row in rows)


def add_strategy_column(connection, column_name, definition="TEXT NOT NULL DEFAULT ''"):
    if strategy_column_exists(connection, column_name):
        return
    connection.execute(
        text(
            f"ALTER TABLE strategies ADD COLUMN {column_name} {definition}"
        )
    )


def strategy_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
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


def asset_snapshot_column_exists(connection, column_name):
    if engine.dialect.name == "postgresql":
        result = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'asset_snapshots'
                  AND column_name = :column_name
                """
            ),
            {"column_name": column_name},
        )
        return result.scalar_one() > 0

    rows = connection.execute(text("PRAGMA table_info(asset_snapshots)")).fetchall()
    return any(row[1] == column_name for row in rows)


init_db()
app = create_app()
if os.environ.get("ENABLE_WEB_SCHEDULER", "0") == "1":
    start_scheduler_thread()


if __name__ == "__main__":
    debug_enabled = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
