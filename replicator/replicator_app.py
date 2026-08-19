from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, request, url_for


BASE_DIR = Path(__file__).resolve().parents[1]
if os.environ.get("REPLICATOR_DATA_DIR"):
    APP_DIR = Path(os.environ["REPLICATOR_DATA_DIR"]).expanduser().resolve()
elif getattr(sys, "frozen", False):
    APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Code Markets" / "Replicator"
else:
    APP_DIR = Path(__file__).resolve().parent
APP_DIR.mkdir(parents=True, exist_ok=True)
MAIN_DB = BASE_DIR / "strategies.db"
STATE_DB = APP_DIR / "replicator_state.db"
CONFIG_PATH = APP_DIR / "config.json"
MADRID_TZ = ZoneInfo("Europe/Madrid")
TRADING_START_HOUR = 15
TRADING_START_MINUTE = 30
TRADING_END_HOUR = 22
TRADING_END_MINUTE = 0
DEFAULT_CONFIG = {
    "alpaca_api_key": "",
    "alpaca_base_url": "https://paper-api.alpaca.markets",
    "alpaca_secret_key": "",
    "auto_enabled": False,
    "capital_profile": "normal",
    "capital_per_operation": 1000.0,
    "mode": "paper",
    "order_generation": "initial",
    "last_connect_error": "",
    "poll_seconds": 60,
    "selected_txt_names": [],
    "source_mode": "web",
    "web_access_token": "",
    "web_base_url": "https://nasdaq-trading-strategies-pro.onrender.com",
    "web_user_email": "",
}
CAPITAL_PROFILES = {
    "conservador": 500.0,
    "normal": 1000.0,
    "agresivo": 2000.0,
}
CAPITAL_PROFILE_LABELS = {
    "conservador": "Conservador 1/400",
    "normal": "Normal 1/300",
    "agresivo": "Agresivo 1/200",
}

app = Flask(__name__)
SCAN_LOCK = threading.Lock()
AUTO_WAKE_EVENT = threading.Event()
AUTO_THREAD_STARTED = False
PROFILE_ID = os.environ.get("REPLICATOR_PROFILE", "default")
PROFILE_NAME = os.environ.get("REPLICATOR_PROFILE_NAME", PROFILE_ID)
APP_VERSION = os.environ.get("REPLICATOR_VERSION", "1.0.6")
INSTALLER_URL = "https://github.com/nasdaqtrading1000-ship-it/trading-strategies/releases/latest/download/CodeMarketsReplicatorSetup.exe"


@app.after_request
def allow_local_health_check(response):
    """Allow the publication page to check this local-only service."""
    if request.path == "/health":
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "code-markets-replicator", "profile": PROFILE_ID})


def version_parts(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


@app.route("/api/update")
def api_update():
    config = load_config()
    latest_version = APP_VERSION
    installer_url = INSTALLER_URL
    error = ""
    try:
        base_url = str(config.get("web_base_url") or "https://nasdaq-trading-strategies-pro.onrender.com").rstrip("/")
        update_request = urllib.request.Request(
            base_url + "/api/replicator/latest",
            headers={"User-Agent": "Code-Markets-Replicator"},
        )
        with urllib.request.urlopen(update_request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latest_version = str(payload.get("version") or APP_VERSION)
        installer_url = str(payload.get("installer_url") or INSTALLER_URL)
    except Exception as exc:
        error = str(exc)
    return jsonify(
        {
            "ok": not error,
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "update_available": version_parts(latest_version) > version_parts(APP_VERSION),
            "installer_url": installer_url,
            "error": error,
        }
    )


@dataclass
class PaperOrder:
    broker_order_id: str
    status: str
    message: str


class PaperBroker:
    def place_order(
        self,
        operation: dict[str, Any],
        action: str,
        capital_per_operation: float,
        qty_override: float | None = None,
    ) -> PaperOrder:
        direction = str(operation.get("direction") or "LONG").upper()
        symbol = str(operation.get("symbol") or "").upper()
        broker_side = broker_side_for(direction, action)
        shares = qty_override if qty_override is not None else suggested_shares(operation, capital_per_operation, action)
        now = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return PaperOrder(
            broker_order_id=f"PAPER-{now}",
            status="PAPER_FILLED",
            message=f"{broker_side} {shares:.4f} {symbol}",
        )


class AlpacaPaperBroker:
    def __init__(self, api_key: str, secret_key: str, base_url: str, order_generation: str = "") -> None:
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.base_url = base_url.strip().rstrip("/") or "https://paper-api.alpaca.markets"
        self.order_generation = order_generation.strip()
        if not self.api_key or not self.secret_key:
            raise ValueError("Faltan claves de Alpaca paper.")

    def place_order(
        self,
        operation: dict[str, Any],
        action: str,
        capital_per_operation: float,
        qty_override: float | None = None,
    ) -> PaperOrder:
        direction = str(operation.get("direction") or "LONG").upper()
        symbol = str(operation.get("symbol") or "").upper()
        side = alpaca_side_for(direction, action)
        qty = qty_override if qty_override is not None else suggested_shares(operation, capital_per_operation, action)
        if not symbol:
            raise ValueError("Operacion sin simbolo.")
        if qty <= 0:
            raise ValueError(f"No se pudo calcular cantidad para {symbol}.")
        client_order_id = alpaca_client_order_id(replication_id(operation, action), self.order_generation)
        payload = {
            "symbol": symbol,
            "qty": format_qty(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        response = self._request("POST", "/v2/orders", payload)
        return PaperOrder(
            broker_order_id=str(response.get("id") or response.get("client_order_id") or client_order_id),
            status=str(response.get("status") or "submitted"),
            message=f"ALPACA {side.upper()} {payload['qty']} {symbol}",
        )

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def positions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/positions")
        return response if isinstance(response, list) else []

    def portfolio_history(self, period: str, timeframe: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "period": period,
                "timeframe": timeframe,
                "intraday_reporting": "market_hours",
                "pnl_reset": "no_reset",
            }
        )
        response = self._request("GET", f"/v2/account/portfolio/history?{query}")
        return response if isinstance(response, dict) else {}

    def position(self, symbol: str) -> dict[str, Any] | None:
        clean_symbol = str(symbol or "").strip().upper()
        if not clean_symbol:
            return None
        try:
            response = self._request("GET", f"/v2/positions/{urllib.parse.quote(clean_symbol)}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return None
            raise
        return response if isinstance(response, dict) else None

    def close_position(self, operation: dict[str, Any]) -> PaperOrder:
        symbol = str(operation.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("Operacion sin simbolo.")
        response = self._request("DELETE", f"/v2/positions/{urllib.parse.quote(symbol)}")
        broker_order_id = str(response.get("id") or response.get("client_order_id") or "")
        status = str(response.get("status") or "submitted")
        qty = str(response.get("qty") or "")
        side = str(response.get("side") or "close").upper()
        detail = f" {qty}" if qty else ""
        return PaperOrder(
            broker_order_id=broker_order_id,
            status=status,
            message=f"ALPACA CLOSE_POSITION {side}{detail} {symbol}",
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Alpaca HTTP {error.code}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Alpaca conexion: {error}") from error
        return json.loads(body or "{}")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    config = dict(DEFAULT_CONFIG)
    config.update(data)
    config["selected_txt_names"] = [
        str(value).strip()
        for value in config.get("selected_txt_names", [])
        if str(value).strip()
    ]
    profile = str(config.get("capital_profile") or "normal").strip().lower()
    if profile not in CAPITAL_PROFILES:
        profile = "normal"
    config["capital_profile"] = profile
    config["capital_per_operation"] = CAPITAL_PROFILES[profile]
    config["capital_profile_label"] = CAPITAL_PROFILE_LABELS[profile]
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=True), encoding="utf-8")


def set_auto_enabled(enabled: bool) -> dict[str, Any]:
    config = load_config()
    config["auto_enabled"] = bool(enabled)
    save_config(config)
    AUTO_WAKE_EVENT.set()
    return config


def safe_scan_once() -> dict[str, Any]:
    with SCAN_LOCK:
        return scan_once()


def main_db_connection() -> sqlite3.Connection:
    uri = f"file:{MAIN_DB.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def state_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replicated_operations (
            replication_id TEXT PRIMARY KEY,
            operation_key TEXT NOT NULL,
            txt_name TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            broker_side TEXT NOT NULL,
            source_status TEXT NOT NULL,
            broker_mode TEXT NOT NULL,
            broker_order_id TEXT NOT NULL DEFAULT '',
            broker_status TEXT NOT NULL DEFAULT '',
            order_price REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            raw_operation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    ensure_state_column(conn, "replicated_operations", "order_price", "REAL NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replicated_positions (
            operation_key TEXT PRIMARY KEY,
            txt_name TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            shares REAL NOT NULL DEFAULT 0,
            open_price REAL NOT NULL DEFAULT 0,
            open_replication_id TEXT NOT NULL DEFAULT '',
            close_replication_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'OPEN',
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def ensure_state_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def available_strategies(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or load_config()
    if str(config.get("source_mode") or "web").lower() == "web":
        return fetch_web_strategies(config)
    with main_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, signals_txt_name, COALESCE(web_visible, is_active, 1) AS visible
            FROM strategies
            WHERE COALESCE(web_visible, is_active, 1) = 1
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_web_strategies(config: dict[str, Any]) -> list[dict[str, Any]]:
    payload = web_request(config, "GET", "/api/replicator/strategies")
    return list(payload.get("strategies") or [])


def today_window() -> tuple[str, str]:
    start = datetime.now(MADRID_TZ).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    end = start + timedelta(days=1)
    return start.isoformat(sep=" "), end.isoformat(sep=" ")


def trading_window_status() -> dict[str, Any]:
    now = datetime.now(MADRID_TZ)
    start = now.replace(
        hour=TRADING_START_HOUR,
        minute=TRADING_START_MINUTE,
        second=0,
        microsecond=0,
    )
    end = now.replace(
        hour=TRADING_END_HOUR,
        minute=TRADING_END_MINUTE,
        second=0,
        microsecond=0,
    )
    is_weekday = now.weekday() < 5
    is_open = is_weekday and start <= now <= end
    if not is_weekday:
        label = "mercado cerrado por fin de semana"
    elif is_open:
        label = "mercado operativo"
    else:
        label = "fuera de horario"
    return {
        "is_open": is_open,
        "is_weekday": is_weekday,
        "now": now.strftime("%H:%M:%S %d/%m/%Y"),
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "label": label,
    }


def fetch_today_operations(config: dict[str, Any], selected_txt_names: list[str]) -> list[dict[str, Any]]:
    if str(config.get("source_mode") or "web").lower() == "web":
        return fetch_web_operations(config, selected_txt_names)
    return fetch_local_today_operations(selected_txt_names)


def fetch_web_operations(config: dict[str, Any], selected_txt_names: list[str]) -> list[dict[str, Any]]:
    if not selected_txt_names:
        return []
    payload = web_request(
        config,
        "POST",
        "/api/replicator/operations",
        {"selected_txt_names": selected_txt_names, "days": 1},
    )
    return list(payload.get("operations") or [])


def fetch_local_today_operations(selected_txt_names: list[str]) -> list[dict[str, Any]]:
    if not selected_txt_names:
        return []
    start, end = today_window()
    placeholders = ",".join("?" for _ in selected_txt_names)
    params: list[Any] = list(selected_txt_names)
    params.extend([start, end, start, end, start, end])
    query = f"""
        SELECT operation_key, strategy_name, txt_name, symbol, direction, status,
               signal_date, opened_at, closed_at, entry_price, target_price, stop_loss,
               shares, current_price, investment_value, profit_usd, profit_pct,
               close_reason, updated_at
        FROM simulated_operations
        WHERE txt_name IN ({placeholders})
          AND (
            (opened_at >= ? AND opened_at < ?)
            OR (closed_at >= ? AND closed_at < ?)
            OR (updated_at >= ? AND updated_at < ?)
          )
        ORDER BY COALESCE(updated_at, closed_at, opened_at) ASC
    """
    with main_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def web_request(
    config: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url = str(config.get("web_base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Falta URL de la web.")
    token = str(config.get("web_access_token") or "").strip()
    if path != "/api/replicator/login" and not token:
        raise ValueError("Conecta primero la cuenta de Code Markets.")
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request_obj = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=25) as response:
            body = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Web HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Web conexion: {error}") from error
    data_payload = json.loads(body or "{}")
    if not data_payload.get("ok"):
        raise RuntimeError(f"Respuesta web no valida: {data_payload}")
    return data_payload


def connect_web_account(config: dict[str, Any], email: str, password: str) -> dict[str, Any]:
    if not email or not password:
        raise ValueError("Introduce email y contrasena de tu cuenta Code Markets.")
    payload = web_request(
        config,
        "POST",
        "/api/replicator/login",
        {"email": email, "password": password},
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"La web no devolvio conexion valida: {payload}")
    config["web_user_email"] = email
    config["web_access_token"] = token
    save_config(config)
    return payload


def operation_actions(operation: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    opened_at = operation_datetime_in_madrid(operation.get("opened_at"))
    closed_at = operation_datetime_in_madrid(operation.get("closed_at"))
    today = datetime.now(MADRID_TZ).date()
    if opened_at and opened_at.date() == today:
        actions.append("OPEN")
    if str(operation.get("status") or "").upper() == "CLOSED" and closed_at and closed_at.date() == today:
        actions.append("CLOSE")
    return actions


def operation_datetime_in_madrid(value: Any) -> datetime | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text_value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MADRID_TZ)
    return parsed.astimezone(MADRID_TZ)


def broker_side_for(direction: str, action: str) -> str:
    if action == "CLOSE":
        return "SELL" if direction == "LONG" else "BUY_TO_COVER"
    return "BUY" if direction == "LONG" else "SELL_SHORT"


def alpaca_side_for(direction: str, action: str) -> str:
    if action == "CLOSE":
        return "sell" if direction == "LONG" else "buy"
    return "buy" if direction == "LONG" else "sell"


def alpaca_client_order_id(rep_id: str, generation: str = "") -> str:
    value = f"{generation}|{rep_id}" if generation else rep_id
    safe = "".join(char if char.isalnum() else "-" for char in value)
    return f"CM-{safe}"[:128]


def format_qty(qty: float) -> str:
    return f"{qty:.6f}".rstrip("0").rstrip(".")


def suggested_shares(operation: dict[str, Any], capital_per_operation: float, action: str = "") -> float:
    existing_shares = float(operation.get("shares") or 0)
    if str(action or "").upper() == "CLOSE" and existing_shares > 0:
        return existing_shares
    price = float(operation.get("entry_price") or operation.get("current_price") or 0)
    if capital_per_operation > 0 and price > 0:
        return capital_per_operation / price
    return existing_shares if existing_shares > 0 else 0.0


def order_price(operation: dict[str, Any], action: str = "") -> float:
    if action == "CLOSE":
        value = operation.get("current_price") or operation.get("entry_price") or 0
    else:
        value = operation.get("entry_price") or operation.get("current_price") or 0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_price(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:.2f} USD" if amount else ""


def format_money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{abs(amount):.2f} USD"


def format_percent(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{abs(amount):.2f}%"


def operation_profit_value(operation: dict[str, Any]) -> float:
    try:
        profit = float(operation.get("profit_usd") or 0)
    except (TypeError, ValueError):
        profit = 0.0
    if profit:
        return profit
    try:
        entry = float(operation.get("entry_price") or 0)
        current = float(operation.get("current_price") or operation.get("exit_price") or 0)
        shares = float(operation.get("shares") or 0)
    except (TypeError, ValueError):
        return 0.0
    if not entry or not current or not shares:
        return 0.0
    direction = str(operation.get("direction") or "LONG").upper()
    if direction == "SHORT":
        return (entry - current) * shares
    return (current - entry) * shares


def operation_profit_pct_value(operation: dict[str, Any]) -> float:
    try:
        value = float(operation.get("profit_pct") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value:
        return value
    try:
        entry_value = float(operation.get("entry_price") or 0) * float(operation.get("shares") or 0)
    except (TypeError, ValueError):
        entry_value = 0.0
    profit = operation_profit_value(operation)
    return (profit / entry_value * 100) if entry_value else 0.0


def close_profit_display(operation: dict[str, Any], action: str) -> str:
    if str(action or "").upper() != "CLOSE":
        return ""
    profit = operation_profit_value(operation)
    pct = operation_profit_pct_value(operation)
    return f"{format_money(profit)} ({format_percent(pct)})"


def profit_class(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return "status-done" if amount >= 0 else "status-unselected"


def source_operation_is_closed(operation: dict[str, Any]) -> bool:
    return str(operation.get("status") or "").upper() == "CLOSED" or bool(operation.get("closed_at"))


def alpaca_position_matches_close(operation: dict[str, Any], position: dict[str, Any] | None) -> bool:
    if not position:
        return False
    operation_direction = str(operation.get("direction") or "LONG").strip().upper()
    alpaca_side = str(position.get("side") or "").strip().lower()
    if operation_direction == "SHORT":
        return alpaca_side == "short"
    return alpaca_side == "long"


def replication_id(operation: dict[str, Any], action: str) -> str:
    return f"{operation.get('operation_key')}|{action}"


def already_replicated(conn: sqlite3.Connection, rep_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM replicated_operations WHERE replication_id = ?",
        (rep_id,),
    ).fetchone()
    return row is not None


def has_open_position(conn: sqlite3.Connection, operation_key: str) -> bool:
    return open_position_row(conn, operation_key) is not None


def open_position_row(conn: sqlite3.Connection, operation_key: str) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT *
        FROM replicated_positions
        WHERE operation_key = ? AND status = 'OPEN'
        """,
        (operation_key,),
    ).fetchone()
    return row


def record_position_open(
    conn: sqlite3.Connection,
    operation: dict[str, Any],
    action: str,
    broker_result: PaperOrder,
    capital_per_operation: float,
) -> None:
    operation_key = str(operation.get("operation_key") or "")
    if not operation_key:
        return
    conn.execute(
        """
        INSERT INTO replicated_positions (
            operation_key, txt_name, strategy_name, symbol, direction, shares,
            open_price, open_replication_id, status, opened_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        ON CONFLICT(operation_key) DO UPDATE SET
            status = 'OPEN',
            shares = excluded.shares,
            open_price = excluded.open_price,
            open_replication_id = excluded.open_replication_id,
            opened_at = excluded.opened_at,
            closed_at = NULL
        """
        if sqlite3.sqlite_version_info >= (3, 24, 0)
        else """
        REPLACE INTO replicated_positions (
            operation_key, txt_name, strategy_name, symbol, direction, shares,
            open_price, open_replication_id, status, opened_at, closed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, NULL)
        """,
        (
            operation_key,
            str(operation.get("txt_name") or ""),
            str(operation.get("strategy_name") or ""),
            str(operation.get("symbol") or "").upper(),
            str(operation.get("direction") or "LONG").upper(),
            suggested_shares(operation, capital_per_operation, action),
            order_price(operation, action),
            replication_id(operation, action),
            datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
        ),
    )


def record_position_close(conn: sqlite3.Connection, operation: dict[str, Any], action: str) -> None:
    operation_key = str(operation.get("operation_key") or "")
    if not operation_key:
        return
    conn.execute(
        """
        UPDATE replicated_positions
        SET status = 'CLOSED',
            close_replication_id = ?,
            closed_at = ?
        WHERE operation_key = ? AND status = 'OPEN'
        """,
        (
            replication_id(operation, action),
            datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
            operation_key,
        ),
    )


def record_replication(
    conn: sqlite3.Connection,
    operation: dict[str, Any],
    action: str,
    broker_result: PaperOrder,
    mode: str,
) -> None:
    direction = str(operation.get("direction") or "LONG").upper()
    conn.execute(
        """
        INSERT INTO replicated_operations (
            replication_id, operation_key, txt_name, strategy_name, symbol, action,
            broker_side, source_status, broker_mode, broker_order_id, broker_status,
            order_price, message, raw_operation, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            replication_id(operation, action),
            str(operation.get("operation_key") or ""),
            str(operation.get("txt_name") or ""),
            str(operation.get("strategy_name") or ""),
            str(operation.get("symbol") or "").upper(),
            action,
            broker_side_for(direction, action),
            str(operation.get("status") or ""),
            mode,
            broker_result.broker_order_id,
            broker_result.status,
            order_price(operation, action),
            broker_result.message,
            json.dumps(operation, default=str, ensure_ascii=True),
            datetime.now().isoformat(sep=" ", timespec="seconds"),
        ),
    )
    conn.commit()


def scan_once() -> dict[str, Any]:
    config = load_config()
    window = trading_window_status()
    if not window["is_open"]:
        return {
            "ok": True,
            "blocked_by_schedule": True,
            "message": (
                f"{window['label']}. El replicador solo opera de lunes a viernes "
                f"de {window['start']} a {window['end']} hora Madrid."
            ),
            "trading_window": window,
            "created_count": 0,
            "error_count": 0,
            "skipped_count": 0,
            "created": [],
            "errors": [],
            "scanned_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
        }
    mode = str(config.get("mode") or "paper").lower()
    if mode not in {"paper", "alpaca_paper"}:
        mode = "paper"
    selected = config["selected_txt_names"]
    operations = fetch_today_operations(config, selected)
    broker = broker_for_config(config, mode)
    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped_details: list[dict[str, Any]] = []
    skipped = 0
    action_items = [
        (operation, action)
        for operation in operations
        for action in operation_actions(operation)
    ]
    action_items.sort(key=lambda item: 0 if item[1] == "OPEN" else 1)
    with state_connection() as conn:
        for operation, action in action_items:
                rep_id = replication_id(operation, action)
                if already_replicated(conn, rep_id):
                    skipped += 1
                    continue
                operation_key = str(operation.get("operation_key") or "")
                position = open_position_row(conn, operation_key)
                has_position = position is not None
                if action == "CLOSE" and not has_position:
                    skipped += 1
                    skipped_details.append(
                        {
                            "replication_id": rep_id,
                            "strategy_name": operation.get("strategy_name"),
                            "symbol": operation.get("symbol"),
                            "action": action,
                            "message": "CLOSE de hoy omitido: el replicador no tiene esa posicion abierta.",
                        }
                    )
                    continue
                if action == "OPEN" and has_position:
                    skipped += 1
                    skipped_details.append(
                        {
                            "replication_id": rep_id,
                            "strategy_name": operation.get("strategy_name"),
                            "symbol": operation.get("symbol"),
                            "action": action,
                            "message": "OPEN omitido: la posicion ya esta abierta en este replicador.",
                        }
                    )
                    continue
                qty_override = None
                if action == "CLOSE" and position is not None:
                    qty_override = float(position["shares"] or 0)
                try:
                    result = broker.place_order(
                        operation,
                        action,
                        float(config.get("capital_per_operation") or 0),
                        qty_override,
                    )
                except Exception as error:
                    errors.append(
                        {
                            "replication_id": rep_id,
                            "strategy_name": operation.get("strategy_name"),
                            "symbol": operation.get("symbol"),
                            "action": action,
                            "broker_status": "ERROR",
                            "message": str(error),
                        }
                    )
                    continue
                record_replication(conn, operation, action, result, mode)
                if action == "OPEN":
                    record_position_open(
                        conn,
                        operation,
                        action,
                        result,
                        float(config.get("capital_per_operation") or 0),
                    )
                    conn.commit()
                elif action == "CLOSE":
                    record_position_close(conn, operation, action)
                    conn.commit()
                created.append(
                    {
                        "replication_id": rep_id,
                        "strategy_name": operation.get("strategy_name"),
                        "symbol": operation.get("symbol"),
                        "action": action,
                        "broker_status": result.status,
                        "message": result.message,
                    }
                )
    return {
        "ok": True,
        "mode": mode,
        "source_mode": str(config.get("source_mode") or "web").lower(),
        "selected_count": len(selected),
        "source_operations": len(operations),
        "created_count": len(created),
        "error_count": len(errors),
        "skipped_count": skipped,
        "created": created,
        "errors": errors,
        "skipped": skipped_details,
        "trading_window": window,
        "scanned_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
    }


def broker_for_config(config: dict[str, Any], mode: str):
    if mode == "alpaca_paper":
        return AlpacaPaperBroker(
            str(config.get("alpaca_api_key") or ""),
            str(config.get("alpaca_secret_key") or ""),
            str(config.get("alpaca_base_url") or "https://paper-api.alpaca.markets"),
            str(config.get("order_generation") or ""),
        )
    return PaperBroker()


def alpaca_account_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    broker = AlpacaPaperBroker(
        str(config.get("alpaca_api_key") or ""),
        str(config.get("alpaca_secret_key") or ""),
        str(config.get("alpaca_base_url") or "https://paper-api.alpaca.markets"),
    )
    account = broker.account()
    positions = broker.positions()
    formatted_positions = []
    positions_profit_total = 0.0
    for position in positions:
        unrealized_pl = safe_float(position.get("unrealized_pl"))
        positions_profit_total += unrealized_pl
        unrealized_plpc = safe_float(position.get("unrealized_plpc")) * 100
        formatted_positions.append(
            {
                "symbol": str(position.get("symbol") or "").upper(),
                "side": str(position.get("side") or ""),
                "qty": str(position.get("qty") or ""),
                "market_value": format_plain_money(position.get("market_value")),
                "avg_entry_price": format_plain_price(position.get("avg_entry_price")),
                "current_price": format_plain_price(position.get("current_price")),
                "unrealized_pl": format_money(unrealized_pl),
                "unrealized_plpc": format_percent(unrealized_plpc),
                "profit_class": profit_class(unrealized_pl),
            }
        )
    history_charts, history_error = alpaca_portfolio_charts(broker)
    return {
        "account": account,
        "positions": formatted_positions,
        "positions_profit_total": format_money(positions_profit_total),
        "positions_profit_total_class": profit_class(positions_profit_total),
        "portfolio_charts": history_charts,
        "portfolio_history_error": history_error,
        "summary": {
            "status": str(account.get("status") or ""),
            "currency": str(account.get("currency") or "USD"),
            "equity": format_plain_money(account.get("equity")),
            "cash": format_plain_money(account.get("cash")),
            "buying_power": format_plain_money(account.get("buying_power")),
            "portfolio_value": format_plain_money(account.get("portfolio_value")),
            "last_equity": format_plain_money(account.get("last_equity")),
        },
        "loaded_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
    }


def alpaca_portfolio_charts(broker: AlpacaPaperBroker) -> tuple[list[dict[str, Any]], str]:
    specs = [
        ("1D", "1 dia", "1D", "5Min"),
        ("1M", "1 mes", "1M", "1D"),
        ("3M", "3 meses", "3M", "1D"),
        ("1A", "1 ano", "1A", "1D"),
        ("HIST", "Historico", "10A", "1D"),
    ]
    charts = []
    errors = []
    for key, label, period, timeframe in specs:
        try:
            raw = broker.portfolio_history(period, timeframe)
        except Exception as error:
            raw = {}
            errors.append(f"{label}: {error}")
        charts.append(format_alpaca_portfolio_chart(key, label, period, timeframe, raw))
    return charts, " | ".join(errors)


def format_alpaca_portfolio_chart(
    key: str,
    label: str,
    period: str,
    timeframe: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    timestamps = raw.get("timestamp") or []
    equity = raw.get("equity") or []
    profit_loss = raw.get("profit_loss") or []
    profit_loss_pct = raw.get("profit_loss_pct") or []
    points = []
    for index, timestamp in enumerate(timestamps):
        equity_value = safe_float(equity[index] if index < len(equity) else 0)
        profit_value = safe_float(profit_loss[index] if index < len(profit_loss) else 0)
        profit_pct_value = safe_float(profit_loss_pct[index] if index < len(profit_loss_pct) else 0) * 100
        points.append(
            {
                "timestamp": timestamp,
                "equity": round(equity_value, 4),
                "profit_loss": round(profit_value, 4),
                "profit_loss_pct": round(profit_pct_value, 4),
            }
        )
    first = points[0] if points else {}
    last = points[-1] if points else {}
    last_profit = safe_float(last.get("profit_loss"))
    return {
        "key": key,
        "label": label,
        "period": period,
        "timeframe": timeframe,
        "points": points,
        "count": len(points),
        "base_value": safe_float(raw.get("base_value")),
        "equity_display": format_plain_money(last.get("equity")) if points else "Sin datos",
        "profit_display": format_money(last_profit) if points else "Sin datos",
        "profit_pct_display": format_percent(last.get("profit_loss_pct")) if points else "",
        "profit_class": profit_class(last_profit),
        "range_display": alpaca_chart_range_display(first.get("timestamp"), last.get("timestamp")),
    }


def alpaca_chart_range_display(first_timestamp: Any, last_timestamp: Any) -> str:
    first = format_alpaca_timestamp(first_timestamp)
    last = format_alpaca_timestamp(last_timestamp)
    if first and last and first != last:
        return f"{first} -> {last}"
    return first or last or "Sin rango"


def format_alpaca_timestamp(value: Any) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp, MADRID_TZ).strftime("%d/%m/%Y %H:%M")


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_plain_money(value: Any) -> str:
    amount = safe_float(value)
    return f"{amount:,.2f} USD"


def format_plain_price(value: Any) -> str:
    amount = safe_float(value)
    return f"{amount:.2f} USD" if amount else ""


def replication_rows(selected_txt_names: list[str] | None = None) -> list[dict[str, Any]]:
    selected = set(selected_txt_names or [])
    with state_connection() as conn:
        rows = conn.execute(
            """
            SELECT replication_id, strategy_name, txt_name, symbol, action, broker_side,
                   source_status, broker_mode, broker_order_id, broker_status,
                   order_price, message, raw_operation, created_at
            FROM replicated_operations
            ORDER BY created_at DESC
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["selection_status"] = (
            "Seleccionada"
            if item.get("txt_name") in selected
            else "Estrategia no seleccionada"
        )
        if not item.get("order_price"):
            try:
                raw_operation = json.loads(item.get("raw_operation") or "{}")
            except json.JSONDecodeError:
                raw_operation = {}
            item["order_price"] = order_price(raw_operation, item.get("action") or "")
        else:
            try:
                raw_operation = json.loads(item.get("raw_operation") or "{}")
            except json.JSONDecodeError:
                raw_operation = {}
        item["order_price_display"] = format_price(item.get("order_price"))
        item["profit_display"] = close_profit_display(raw_operation, item.get("action") or "")
        item["profit_class"] = profit_class(operation_profit_value(raw_operation))
        result.append(item)
    return result


def operation_status_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    selected = list(config.get("selected_txt_names") or [])
    if not selected:
        return []
    operations = fetch_today_operations(config, selected)
    rows: list[dict[str, Any]] = []
    with state_connection() as conn:
        for operation in operations:
            for action in operation_actions(operation):
                rep_id = replication_id(operation, action)
                stored = conn.execute(
                    """
                    SELECT broker_status, message, created_at, order_price
                    FROM replicated_operations
                    WHERE replication_id = ?
                    """,
                    (rep_id,),
                ).fetchone()
                operation_key = str(operation.get("operation_key") or "")
                has_position = has_open_position(conn, operation_key)
                if stored:
                    status = "Operacion realizada"
                elif action == "CLOSE" and not has_position:
                    if str(config.get("mode") or "paper").lower() == "alpaca_paper":
                        status = "CLOSE pendiente: comprobar Alpaca"
                    else:
                        status = (
                            "Operacion cerrada en Code Markets"
                            if source_operation_is_closed(operation)
                            else "CLOSE omitido - sin posicion local"
                        )
                elif action == "OPEN" and has_position:
                    status = "Posicion abierta local"
                else:
                    status = "Operacion pendiente"
                rows.append(
                    {
                        "replication_id": rep_id,
                        "strategy_name": operation.get("strategy_name"),
                        "txt_name": operation.get("txt_name"),
                        "symbol": str(operation.get("symbol") or "").upper(),
                        "action": action,
                        "direction": str(operation.get("direction") or "").upper(),
                        "order_price": order_price(operation, action),
                        "order_price_display": format_price(order_price(operation, action)),
                        "profit_display": close_profit_display(operation, action),
                        "profit_value": operation_profit_value(operation),
                        "profit_class": profit_class(operation_profit_value(operation)),
                        "is_open": action == "OPEN" and not source_operation_is_closed(operation),
                        "status": status,
                        "broker_status": stored["broker_status"] if stored else "",
                        "message": stored["message"] if stored else "",
                        "created_at": stored["created_at"] if stored else "",
                        "source_updated_at": operation.get("updated_at") or operation.get("opened_at") or operation.get("closed_at"),
                    }
                )
    rows.sort(key=lambda item: str(item.get("source_updated_at") or ""), reverse=True)
    return rows


def clear_today_replications() -> int:
    start, end = today_window()
    with state_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM replicated_operations
            WHERE created_at >= ? AND created_at < ?
            """,
            (start, end),
        )
        conn.commit()
        deleted = int(cursor.rowcount or 0)
    config = load_config()
    config["order_generation"] = datetime.now(MADRID_TZ).strftime("%Y%m%d%H%M%S%f")
    save_config(config)
    return deleted


PAGE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Code Markets Replicator</title>
  <style>
    :root { color-scheme: dark; }
    body { background:#012456; color:#d8e7ff; font-family:Consolas,monospace; margin:0; padding:22px; }
    .shell { max-width:1180px; margin:0 auto; }
    .panel { border:1px solid rgba(125,211,252,.38); padding:14px; margin-bottom:14px; background:rgba(0,15,40,.22); }
    h1,h2 { margin:0 0 10px; }
    label { display:block; }
    .field { display:flex; flex-direction:column; gap:5px; margin:0; }
    .field-label { color:#7dd3fc; font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
    .field-control { width:100%; box-sizing:border-box; min-height:34px; }
    input, select, button { background:#001b3f; color:#d8e7ff; border:1px solid rgba(125,211,252,.45); padding:7px; font-family:inherit; }
    button { cursor:pointer; }
    button.primary { color:#16ff7a; border-color:#16ff7a; }
    button.auto-on { color:#02140a; background:#16ff7a; border-color:#16ff7a; font-weight:700; }
    button.auto-off { color:#fff; background:#b91c1c; border-color:#ff4d4d; font-weight:700; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; }
    .muted { color:#8fa9d6; }
    .ok { color:#16ff7a; }
    .status-done { color:#16ff7a; font-weight:700; }
    .status-pending { color:#facc15; font-weight:700; }
    .status-unselected { color:#ff8a8a; font-weight:700; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; }
    a.button { display:inline-block; background:#001b3f; color:#d8e7ff; border:1px solid rgba(125,211,252,.45); padding:7px; text-decoration:none; }
    a.button.primary { color:#16ff7a; border-color:#16ff7a; font-weight:700; }
    .summary-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; }
    .summary-box { border:1px solid rgba(125,211,252,.24); padding:10px; background:rgba(0,15,40,.22); }
    .summary-box span { display:block; color:#7dd3fc; font-size:11px; text-transform:uppercase; }
    .summary-box strong { display:block; margin-top:5px; }
    .chart-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-rows:repeat(2, minmax(230px, auto)); gap:12px; }
    .chart-card { border:1px solid rgba(125,211,252,.28); padding:10px; background:rgba(0,15,40,.24); min-height:230px; min-width:0; }
    .chart-card.is-wide { grid-column:1 / -1; }
    .chart-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:8px; }
    .chart-head strong { color:#fff; display:block; }
    .chart-meta { color:#8fa9d6; font-size:11px; text-align:right; }
    .chart-stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:8px; font-size:11px; }
    .chart-stats span { color:#7dd3fc; display:block; text-transform:uppercase; }
    .chart-stats strong { display:block; margin-top:3px; }
    canvas.alpaca-chart { display:block; width:100%; height:145px; border:1px solid rgba(125,211,252,.18); background:#001b3f; }
    table { border-collapse:collapse; width:100%; font-size:12px; }
    th,td { border-bottom:1px solid rgba(125,211,252,.2); padding:6px; text-align:left; vertical-align:top; }
    th { color:#7dd3fc; }
    .warn { border:1px solid #f59e0b; color:#facc15; padding:10px; margin:10px 0; background:rgba(245,158,11,.08); }
    @media (max-width: 760px) { .chart-grid { grid-template-columns:1fr; grid-template-rows:none; } .chart-card.is-wide { grid-column:auto; } }
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h1>Code Markets Replicator</h1>
      <p class="muted">Cuenta activa: <strong>{{ profile_name }}</strong></p>
      <p class="muted">Version instalada: <strong>{{ app_version }}</strong></p>
      <div id="update-notice" class="warn" hidden>
        Hay una nueva version: <strong id="latest-version"></strong>.
        <a id="update-link" class="button primary" href="#">Descargar actualizacion</a>
      </div>
      <p class="muted">Modo seguro: lee operaciones del dia y registra replicas en paper. No envia dinero real.</p>
      <p>DB origen: <span class="ok">{{ main_db }}</span></p>
      <p>
        Horario operativo:
        <span class="{% if trading_window.is_open %}ok{% else %}muted{% endif %}">
          {{ trading_window.start }} - {{ trading_window.end }} Madrid · {{ trading_window.label }} · {{ trading_window.now }}
        </span>
      </p>
      <p class="actions">
        <a class="button primary" href="{{ url_for('alpaca_account') }}">Cuenta Alpaca</a>
      </p>
    </div>

    <form class="panel" method="post" action="{{ url_for('settings') }}">
      <h2>Configuracion</h2>
      <div class="grid">
        <label class="field"><span class="field-label">Origen de datos</span>
          <select class="field-control" name="source_mode">
            <option value="web" {% if config.source_mode == 'web' %}selected{% endif %}>web</option>
            <option value="local" {% if config.source_mode == 'local' %}selected{% endif %}>sqlite local</option>
          </select>
        </label>
        <label class="field"><span class="field-label">URL web</span>
          <input class="field-control" name="web_base_url" value="{{ config.web_base_url }}">
        </label>
        <label class="field"><span class="field-label">Email cuenta Code Markets</span>
          <input class="field-control" name="web_user_email" value="{{ config.web_user_email }}">
        </label>
        <label class="field"><span class="field-label">Contrasena cuenta Code Markets</span>
          <input class="field-control" type="password" name="web_user_password" value="">
        </label>
        <label class="field"><span class="field-label">Modo</span>
          <select class="field-control" name="mode">
            <option value="paper" {% if config.mode == 'paper' %}selected{% endif %}>paper</option>
            <option value="alpaca_paper" {% if config.mode == 'alpaca_paper' %}selected{% endif %}>alpaca paper</option>
          </select>
        </label>
        <label class="field"><span class="field-label">Capital operacion</span>
          <select class="field-control" name="capital_profile">
            <option value="conservador" {% if config.capital_profile == 'conservador' %}selected{% endif %}>Conservador 1/400</option>
            <option value="normal" {% if config.capital_profile == 'normal' %}selected{% endif %}>Normal 1/300</option>
            <option value="agresivo" {% if config.capital_profile == 'agresivo' %}selected{% endif %}>Agresivo 1/200</option>
          </select>
        </label>
        <label class="field"><span class="field-label">Refresco automatico segundos</span>
          <input class="field-control" name="poll_seconds" value="{{ config.poll_seconds }}">
        </label>
        <div class="field"><span class="field-label">Automatico 24/7</span>
          <button class="field-control {% if config.auto_enabled %}auto-on{% else %}auto-off{% endif %}" id="toggle-auto" type="button">
            {% if config.auto_enabled %}Auto ON{% else %}Auto OFF{% endif %}
          </button>
        </div>
        <label class="field"><span class="field-label">Alpaca API Key</span>
          <input class="field-control" type="password" name="alpaca_api_key" value="" placeholder="{% if config.alpaca_api_key %}Clave guardada{% else %}Sin configurar{% endif %}">
        </label>
        <label class="field"><span class="field-label">Alpaca Secret Key</span>
          <input class="field-control" type="password" name="alpaca_secret_key" value="" placeholder="{% if config.alpaca_secret_key %}Clave guardada{% else %}Sin configurar{% endif %}">
        </label>
        <label class="field"><span class="field-label">Alpaca Base URL</span>
          <input class="field-control" name="alpaca_base_url" value="{{ config.alpaca_base_url }}">
        </label>
      </div>
      <p class="muted">
        Origen web lee operaciones ya calculadas por la pagina. La cuenta se conecta automaticamente con email y contrasena;
        no tienes que copiar tokens de clientes. Estado: {% if config.web_access_token %}<span class="ok">cuenta conectada</span>{% else %}cuenta sin conectar{% endif %}.
      </p>
      <p class="muted">Auto ON funciona 24/7 mientras este proceso local siga abierto. Fuera de mercado escanea sin replicar.</p>
      <p class="muted">La pantalla se actualiza automaticamente cada 30 minutos. Si estas editando la configuracion, el refresco se pospone para no perder cambios.</p>
      {% if config.last_connect_error %}
        <div class="warn">Ultimo error de conexion web: {{ config.last_connect_error }}</div>
      {% endif %}
      <h2>Estrategias a replicar</h2>
      {% if strategy_error %}
        <div class="warn">{{ strategy_error }}</div>
      {% endif %}
      {% if not strategies %}
        <p class="muted">
          No hay estrategias cargadas para este origen.
          Si usas origen web, escribe URL web, email y contrasena de Code Markets y pulsa Guardar para conectar la cuenta.
          Si la URL es Render, primero tiene que estar desplegado el API del replicador.
          Para probar sin web, cambia Origen de datos a sqlite local.
        </p>
      {% endif %}
      <div class="grid">
        {% for strategy in strategies %}
          <label>
            <input type="checkbox" name="selected_txt_names" value="{{ strategy.signals_txt_name or strategy.name }}"
              {% if (strategy.signals_txt_name or strategy.name) in config.selected_txt_names %}checked{% endif %}>
            {{ strategy.name }} <span class="muted">{{ strategy.signals_txt_name or strategy.name }}</span>
          </label>
        {% endfor %}
      </div>
      <button class="primary" type="submit">Guardar</button>
    </form>

    <div class="panel">
      <h2>Escaneo</h2>
      <button class="primary" id="scan-now" type="button">Escanear ahora</button>
      <pre id="scan-result" class="muted">{{ last_scan }}</pre>
    </div>

    <div class="panel">
      <h2>Operaciones del dia</h2>
      <button id="clear-today" type="button">Limpiar operaciones del dia</button>
      {% if not operation_rows %}
        <p class="muted">No hay operaciones pendientes o realizadas para las estrategias seleccionadas.</p>
      {% endif %}
      <table>
        <thead>
          <tr><th>Estado</th><th>Hora fuente</th><th>Estrategia</th><th>Activo</th><th>Accion</th><th>Direccion</th><th>Precio orden</th><th>P/L cierre</th><th>Broker</th><th>Mensaje</th></tr>
        </thead>
        <tbody>
          {% for row in operation_rows %}
            <tr>
              <td class="{% if row.status in ['Operacion realizada', 'Operacion cerrada en Code Markets'] %}status-done{% else %}status-pending{% endif %}">{{ row.status }}</td>
              <td>{{ row.source_updated_at }}</td>
              <td>{{ row.strategy_name }}</td>
              <td>{{ row.symbol }}</td>
              <td>{{ row.action }}</td>
              <td>{{ row.direction }}</td>
              <td>{{ row.order_price_display }}</td>
              <td class="{{ row.profit_class }}">{{ row.profit_display }}</td>
              <td>{{ row.broker_status }}</td>
              <td>{{ row.message }}</td>
            </tr>
          {% endfor %}
        </tbody>
        <tfoot>
          <tr>
            <th colspan="7">Total P/L cierres del dia</th>
            <th class="{{ closed_operations_profit_total_class }}">{{ closed_operations_profit_total }}</th>
            <th colspan="2"></th>
          </tr>
        </tfoot>
      </table>
    </div>

    <div class="panel">
      <h2>Replicas registradas</h2>
      <p class="muted">Sin limite visual: se muestran todas las replicas guardadas en este equipo.</p>
      <table>
        <thead>
          <tr><th>Hora</th><th>Seleccion</th><th>Estrategia</th><th>Activo</th><th>Accion</th><th>Precio orden</th><th>P/L cierre</th><th>Lado broker</th><th>Estado</th><th>Mensaje</th></tr>
        </thead>
        <tbody>
          {% for row in rows %}
            <tr>
              <td>{{ row.created_at }}</td>
              <td class="{% if row.selection_status == 'Seleccionada' %}status-done{% else %}status-unselected{% endif %}">{{ row.selection_status }}</td>
              <td>{{ row.strategy_name }}</td>
              <td>{{ row.symbol }}</td>
              <td>{{ row.action }}</td>
              <td>{{ row.order_price_display }}</td>
              <td class="{{ row.profit_class }}">{{ row.profit_display }}</td>
              <td>{{ row.broker_side }}</td>
              <td>{{ row.broker_status }}</td>
              <td>{{ row.message }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  <script>
    let auto = {{ "true" if config.auto_enabled else "false" }};
    const toggleButton = document.getElementById("toggle-auto");
    async function checkForUpdate() {
      try {
        const response = await fetch("{{ url_for('api_update') }}", { cache: "no-store" });
        const data = await response.json();
        if (!data.update_available) return;
        document.getElementById("latest-version").textContent = data.latest_version;
        document.getElementById("update-link").href = data.installer_url;
        document.getElementById("update-notice").hidden = false;
      } catch (_) {}
    }
    function paintAuto(nextAuto) {
      auto = Boolean(nextAuto);
      toggleButton.textContent = auto ? "Auto ON" : "Auto OFF";
      toggleButton.classList.toggle("auto-on", auto);
      toggleButton.classList.toggle("auto-off", !auto);
    }
    async function scan() {
      const response = await fetch("{{ url_for('api_scan') }}", { method: "POST" });
      const data = await response.json();
      document.getElementById("scan-result").textContent = JSON.stringify(data, null, 2);
      if (data.created_count > 0) setTimeout(() => window.location.reload(), 900);
    }
    async function clearToday() {
      if (!window.confirm("Esto solo borra el registro local de replicas de hoy. Las operaciones pueden volver a quedar pendientes. Continuar?")) return;
      const response = await fetch("{{ url_for('api_clear_today') }}", { method: "POST" });
      const data = await response.json();
      document.getElementById("scan-result").textContent = JSON.stringify(data, null, 2);
      setTimeout(() => window.location.reload(), 700);
    }
    async function toggleAuto() {
      const response = await fetch("{{ url_for('api_auto') }}", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !auto }),
      });
      const data = await response.json();
      paintAuto(Boolean(data.auto_enabled));
      document.getElementById("scan-result").textContent = JSON.stringify(data, null, 2);
    }
    document.getElementById("scan-now").addEventListener("click", scan);
    document.getElementById("clear-today").addEventListener("click", clearToday);
    document.getElementById("toggle-auto").addEventListener("click", () => {
      toggleAuto();
    });
    const settingsForm = document.querySelector('form[action="{{ url_for("settings") }}"]');
    let settingsDirty = false;
    settingsForm?.addEventListener("input", () => { settingsDirty = true; });
    settingsForm?.addEventListener("submit", () => { settingsDirty = false; });
    window.setInterval(() => {
      if (!settingsDirty && document.visibilityState === "visible") {
        window.location.reload();
      }
    }, 1800 * 1000);
    paintAuto(auto);
    checkForUpdate();
  </script>
</body>
</html>
"""


ALPACA_ACCOUNT_PAGE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cuenta Alpaca - Code Markets Replicator</title>
  <style>
    :root { color-scheme: dark; }
    body { background:#012456; color:#d8e7ff; font-family:Consolas,monospace; margin:0; padding:22px; }
    .shell { max-width:1180px; margin:0 auto; }
    .panel { border:1px solid rgba(125,211,252,.38); padding:14px; margin-bottom:14px; background:rgba(0,15,40,.22); }
    h1,h2 { margin:0 0 10px; }
    .muted { color:#8fa9d6; }
    .ok { color:#16ff7a; }
    .status-done { color:#16ff7a; font-weight:700; }
    .status-unselected { color:#ff8a8a; font-weight:700; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; }
    a.button { display:inline-block; background:#001b3f; color:#d8e7ff; border:1px solid rgba(125,211,252,.45); padding:7px; text-decoration:none; }
    a.button.primary { color:#16ff7a; border-color:#16ff7a; font-weight:700; }
    .summary-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; }
    .summary-box { border:1px solid rgba(125,211,252,.24); padding:10px; background:rgba(0,15,40,.22); }
    .summary-box span { display:block; color:#7dd3fc; font-size:11px; text-transform:uppercase; }
    .summary-box strong { display:block; margin-top:5px; }
    .chart-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-rows:repeat(2, minmax(230px, auto)); gap:12px; }
    .chart-card { border:1px solid rgba(125,211,252,.28); padding:10px; background:rgba(0,15,40,.24); min-height:230px; min-width:0; }
    .chart-card.is-wide { grid-column:1 / -1; }
    .chart-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:8px; }
    .chart-head strong { color:#fff; display:block; }
    .chart-meta { color:#8fa9d6; font-size:11px; text-align:right; }
    .chart-stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:8px; font-size:11px; }
    .chart-stats span { color:#7dd3fc; display:block; text-transform:uppercase; }
    .chart-stats strong { display:block; margin-top:3px; }
    canvas.alpaca-chart { display:block; width:100%; height:145px; border:1px solid rgba(125,211,252,.18); background:#001b3f; }
    table { border-collapse:collapse; width:100%; font-size:12px; }
    th,td { border-bottom:1px solid rgba(125,211,252,.2); padding:6px; text-align:left; vertical-align:top; }
    th { color:#7dd3fc; }
    .warn { border:1px solid #f59e0b; color:#facc15; padding:10px; margin:10px 0; background:rgba(245,158,11,.08); }
    @media (max-width: 760px) { .chart-grid { grid-template-columns:1fr; grid-template-rows:none; } .chart-card.is-wide { grid-column:auto; } }
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h1>Cuenta Alpaca</h1>
      <p class="muted">Consulta directa de la cuenta configurada en Code Markets Replicator. Solo lectura.</p>
      <p class="actions">
        <a class="button" href="{{ url_for('index') }}">Volver</a>
        <a class="button primary" href="{{ url_for('alpaca_account') }}">Actualizar cuenta</a>
      </p>
    </div>

    {% if error %}
      <div class="warn">{{ error }}</div>
    {% else %}
      <div class="panel">
        <h2>Resumen</h2>
        <div class="summary-grid">
          <div class="summary-box"><span>Estado</span><strong>{{ snapshot.summary.status }}</strong></div>
          <div class="summary-box"><span>Valor cuenta</span><strong>{{ snapshot.summary.portfolio_value }}</strong></div>
          <div class="summary-box"><span>Equity</span><strong>{{ snapshot.summary.equity }}</strong></div>
          <div class="summary-box"><span>Cash</span><strong>{{ snapshot.summary.cash }}</strong></div>
          <div class="summary-box"><span>Buying power</span><strong>{{ snapshot.summary.buying_power }}</strong></div>
          <div class="summary-box"><span>Ultima lectura</span><strong>{{ snapshot.loaded_at }}</strong></div>
        </div>
      </div>

      <div class="panel">
        <h2>Graficas Alpaca</h2>
        <p class="muted">Series de portfolio history de Alpaca: valor de cuenta, beneficio/perdida y porcentaje.</p>
        {% if snapshot.portfolio_history_error %}
          <div class="warn">{{ snapshot.portfolio_history_error }}</div>
        {% endif %}
        <div class="chart-grid">
          {% for chart in snapshot.portfolio_charts %}
            <div class="chart-card {% if chart.key == 'HIST' %}is-wide{% endif %}">
              <div class="chart-head">
                <div>
                  <strong>{{ chart.label }}</strong>
                  <span class="muted">{{ chart.count }} puntos</span>
                </div>
                <div class="chart-meta">
                  {{ chart.period }} / {{ chart.timeframe }}<br>
                  {{ chart.range_display }}
                </div>
              </div>
              <canvas class="alpaca-chart" data-chart-key="{{ chart.key }}"></canvas>
              <div class="chart-stats">
                <div><span>Equity</span><strong>{{ chart.equity_display }}</strong></div>
                <div><span>P/L</span><strong class="{{ chart.profit_class }}">{{ chart.profit_display }}</strong></div>
                <div><span>P/L %</span><strong class="{{ chart.profit_class }}">{{ chart.profit_pct_display }}</strong></div>
              </div>
            </div>
          {% endfor %}
        </div>
      </div>

      <div class="panel">
        <h2>Operaciones abiertas en Alpaca</h2>
        {% if not snapshot.positions %}
          <p class="muted">No hay posiciones abiertas en Alpaca.</p>
        {% endif %}
        <table>
          <thead>
            <tr><th>Activo</th><th>Lado</th><th>Acciones</th><th>Valor mercado</th><th>Entrada media</th><th>Precio actual</th><th>P/L</th><th>P/L %</th></tr>
          </thead>
          <tbody>
            {% for position in snapshot.positions %}
              <tr>
                <td>{{ position.symbol }}</td>
                <td>{{ position.side }}</td>
                <td>{{ position.qty }}</td>
                <td>{{ position.market_value }}</td>
                <td>{{ position.avg_entry_price }}</td>
                <td>{{ position.current_price }}</td>
                <td class="{{ position.profit_class }}">{{ position.unrealized_pl }}</td>
                <td class="{{ position.profit_class }}">{{ position.unrealized_plpc }}</td>
              </tr>
            {% endfor %}
          </tbody>
          <tfoot>
            <tr>
              <th colspan="6">Total P/L posiciones abiertas</th>
              <th class="{{ snapshot.positions_profit_total_class }}">{{ snapshot.positions_profit_total }}</th>
              <th></th>
            </tr>
          </tfoot>
        </table>
      </div>
    {% endif %}
  </div>
  {% if not error %}
    <script>
      const alpacaCharts = {{ snapshot.portfolio_charts|tojson }};

      function moneyLabel(value) {
        const amount = Number(value || 0);
        const abs = Math.abs(amount);
        if (abs >= 1000000) return `${(amount / 1000000).toFixed(2)}M`;
        if (abs >= 1000) return `${(amount / 1000).toFixed(2)}K`;
        return amount.toFixed(2);
      }

      function timestampLabel(value) {
        let stamp = Number(value || 0);
        if (!stamp) return "";
        if (stamp < 10000000000) stamp *= 1000;
        const date = new Date(stamp);
        return `${String(date.getDate()).padStart(2, "0")}/${String(date.getMonth() + 1).padStart(2, "0")}`;
      }

      function drawChart(canvas, chart) {
        const rect = canvas.getBoundingClientRect();
        const ratio = window.devicePixelRatio || 1;
        const width = Math.max(320, Math.floor(rect.width));
        const height = Math.max(145, Math.floor(rect.height || 145));
        canvas.width = width * ratio;
        canvas.height = height * ratio;
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "#001b3f";
        ctx.fillRect(0, 0, width, height);

        const points = Array.isArray(chart.points) ? chart.points : [];
        const pad = { left: 58, right: 12, top: 12, bottom: 28 };
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;

        ctx.strokeStyle = "rgba(125,211,252,.18)";
        ctx.lineWidth = 1;
        ctx.font = "11px Consolas, monospace";
        ctx.fillStyle = "#bfeaff";
        for (let i = 0; i <= 4; i += 1) {
          const y = pad.top + (plotH * i / 4);
          ctx.beginPath();
          ctx.moveTo(pad.left, y);
          ctx.lineTo(width - pad.right, y);
          ctx.stroke();
        }

        if (points.length < 2) {
          ctx.fillStyle = "#8fa9d6";
          ctx.textAlign = "center";
          ctx.fillText("Sin datos suficientes", width / 2, height / 2);
          return;
        }

        const values = points.map((point) => Number(point.equity || 0)).filter((value) => Number.isFinite(value));
        let min = Math.min(...values);
        let max = Math.max(...values);
        if (min === max) {
          min -= Math.max(1, Math.abs(min) * 0.005);
          max += Math.max(1, Math.abs(max) * 0.005);
        } else {
          const margin = (max - min) * 0.08;
          min -= margin;
          max += margin;
        }

        ctx.fillStyle = "#bfeaff";
        ctx.textAlign = "right";
        for (let i = 0; i <= 4; i += 1) {
          const value = max - ((max - min) * i / 4);
          const y = pad.top + (plotH * i / 4) + 4;
          ctx.fillText(moneyLabel(value), pad.left - 7, y);
        }

        const xFor = (index) => pad.left + (points.length === 1 ? 0 : plotW * index / (points.length - 1));
        const yFor = (value) => pad.top + plotH - ((Number(value || 0) - min) / (max - min) * plotH);

        ctx.beginPath();
        points.forEach((point, index) => {
          const x = xFor(index);
          const y = yFor(point.equity);
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = "#16ff7a";
        ctx.lineWidth = 2;
        ctx.stroke();

        const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
        gradient.addColorStop(0, "rgba(22,255,122,.24)");
        gradient.addColorStop(1, "rgba(22,255,122,0)");
        ctx.lineTo(width - pad.right, height - pad.bottom);
        ctx.lineTo(pad.left, height - pad.bottom);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        ctx.fillStyle = "#8fa9d6";
        ctx.textAlign = "left";
        ctx.fillText(timestampLabel(points[0].timestamp), pad.left, height - 8);
        ctx.textAlign = "right";
        ctx.fillText(timestampLabel(points[points.length - 1].timestamp), width - pad.right, height - 8);
      }

      function drawAllCharts() {
        document.querySelectorAll("canvas.alpaca-chart").forEach((canvas) => {
          const chart = alpacaCharts.find((item) => item.key === canvas.dataset.chartKey);
          if (chart) drawChart(canvas, chart);
        });
      }

      window.addEventListener("resize", drawAllCharts);
      drawAllCharts();
    </script>
  {% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    config = load_config()
    strategy_error = ""
    operation_error = ""
    try:
        strategies = available_strategies(config)
    except Exception as error:
        strategies = []
        strategy_error = str(error)
    try:
        operation_rows = operation_status_rows(config)
    except Exception as error:
        operation_rows = []
        operation_error = str(error)
    closed_operations_profit_value = sum(
        safe_float(row.get("profit_value"))
        for row in operation_rows
        if str(row.get("action") or "").upper() == "CLOSE"
    )
    return render_template_string(
        PAGE,
        config=config,
        profile_id=PROFILE_ID,
        profile_name=PROFILE_NAME,
        app_version=APP_VERSION,
        strategies=strategies,
        rows=replication_rows(config.get("selected_txt_names") or []),
        operation_rows=operation_rows,
        closed_operations_profit_total=format_money(closed_operations_profit_value),
        closed_operations_profit_total_class=profit_class(closed_operations_profit_value),
        main_db=str(MAIN_DB),
        last_scan=strategy_error or operation_error or "Pulsa Escanear ahora para probar.",
        strategy_error=strategy_error or operation_error,
        trading_window=trading_window_status(),
    )


@app.route("/alpaca")
def alpaca_account():
    config = load_config()
    snapshot = {}
    error = ""
    try:
        snapshot = alpaca_account_snapshot(config)
    except Exception as exc:
        error = str(exc)
    return render_template_string(
        ALPACA_ACCOUNT_PAGE,
        config=config,
        snapshot=snapshot,
        error=error,
    )


@app.route("/settings", methods=["POST"])
def settings():
    selected = request.form.getlist("selected_txt_names")
    config = load_config()
    config["selected_txt_names"] = selected
    config["source_mode"] = request.form.get("source_mode", "web")
    config["web_base_url"] = (
        request.form.get("web_base_url", "https://nasdaq-trading-strategies-pro.onrender.com").strip()
        or "https://nasdaq-trading-strategies-pro.onrender.com"
    )
    config["web_user_email"] = request.form.get("web_user_email", "").strip().lower()
    config["last_connect_error"] = ""
    web_password = request.form.get("web_user_password", "")
    config["mode"] = request.form.get("mode", "paper")
    capital_profile = request.form.get("capital_profile", "normal").strip().lower()
    if capital_profile not in CAPITAL_PROFILES:
        capital_profile = "normal"
    config["capital_profile"] = capital_profile
    config["capital_per_operation"] = CAPITAL_PROFILES[capital_profile]
    config["poll_seconds"] = max(10, int(float(request.form.get("poll_seconds") or 60)))
    alpaca_api_key = request.form.get("alpaca_api_key", "").strip()
    alpaca_secret_key = request.form.get("alpaca_secret_key", "").strip()
    if alpaca_api_key:
        config["alpaca_api_key"] = alpaca_api_key
    if alpaca_secret_key:
        config["alpaca_secret_key"] = alpaca_secret_key
    config["alpaca_base_url"] = (
        request.form.get("alpaca_base_url", "https://paper-api.alpaca.markets").strip()
        or "https://paper-api.alpaca.markets"
    )
    connect_error = ""
    save_config(config)
    if config["source_mode"] == "web" and config["web_user_email"] and web_password:
        try:
            connect_web_account(config, config["web_user_email"], web_password)
        except Exception as error:
            connect_error = str(error)
    if connect_error:
        config["last_connect_error"] = connect_error
        save_config(config)
    return redirect(url_for("index"))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        return jsonify(safe_scan_once())
    except Exception as error:
        return jsonify(
            {
                "ok": False,
                "error": str(error),
                "scanned_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
            }
        )


@app.route("/api/auto", methods=["POST"])
def api_auto():
    payload = request.get_json(silent=True) or {}
    config = set_auto_enabled(bool(payload.get("enabled")))
    result: dict[str, Any] = {
        "ok": True,
        "auto_enabled": bool(config.get("auto_enabled")),
        "message": "Auto ON 24/7 activado." if config.get("auto_enabled") else "Auto OFF.",
        "updated_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
    }
    return jsonify(result)


@app.route("/api/clear-today", methods=["POST"])
def api_clear_today():
    deleted = clear_today_replications()
    return jsonify(
        {
            "ok": True,
            "deleted_count": deleted,
            "message": "Registro local de replicas de hoy limpiado.",
            "cleared_at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
        }
    )


def auto_worker() -> None:
    while True:
        seconds = 60
        try:
            config = load_config()
            seconds = max(10, int(float(config.get("poll_seconds") or 60)))
            if config.get("auto_enabled"):
                safe_scan_once()
        except Exception as error:
            print(
                json.dumps(
                    {
                        "auto_worker_error": str(error),
                        "at": datetime.now(MADRID_TZ).isoformat(sep=" ", timespec="seconds"),
                    },
                    ensure_ascii=True,
                )
            )
        AUTO_WAKE_EVENT.wait(seconds)
        AUTO_WAKE_EVENT.clear()


def start_auto_worker() -> None:
    global AUTO_THREAD_STARTED
    if AUTO_THREAD_STARTED:
        return
    AUTO_THREAD_STARTED = True
    thread = threading.Thread(target=auto_worker, name="replicator-auto-worker", daemon=True)
    thread.start()


def run_loop(seconds: int) -> None:
    while True:
        print(json.dumps(safe_scan_once(), indent=2, ensure_ascii=True))
        time.sleep(seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--port", type=int, default=5075)
    args = parser.parse_args()
    if args.loop:
        run_loop(load_config()["poll_seconds"])
    else:
        start_auto_worker()
        app.run(host="127.0.0.1", port=args.port, debug=False)
