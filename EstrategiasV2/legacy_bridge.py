from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import StrategySignal, TickerData


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
LEGACY_DIR = PROJECT_DIR / "Estrategias"
LEGACY_SIGNALS_DIR = LEGACY_DIR / "salidas_txt"
LEGACY_STATUS_FILE = LEGACY_DIR / "strategy_run_status.json"
TOP_MONEY_VOLUME_FILE = LEGACY_DIR / "top_money_volume_assets.txt"

if str(LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(LEGACY_DIR))

from txt_output import append_new_lines  # noqa: E402


LEGACY_STRATEGIES = {
    "Momentum": {"key": "momentum", "file": "Momentum.py", "txt": "Momentum.txt"},
    "Swing Trading": {"key": "swing_trading", "file": "SwingTrading.py", "txt": "SwingTrading.txt"},
    "BreaKout": {"key": "breakout", "file": "BreaKout.py", "txt": "BreaKout.txt"},
    "Mean Reversion": {"key": "mean_reversion", "file": "Mean Reversion.py", "txt": "Mean_Reversion.txt"},
    "Value Trading": {"key": "value_trading", "file": "ValueTrading.py", "txt": "ValueTrading.txt"},
    "Dividend Growth": {"key": "dividend_growth", "file": "DividenGrowth.py", "txt": "DividenGrowth.txt"},
    "Trend Following": {"key": "trend_following", "file": "TrendFollowing.py", "txt": "TrendFollowing.txt"},
    "Pairs Trading": {"key": "pairs_trading", "file": "PairsTrading.py", "txt": "PairsTrading.txt"},
    "Sector Rotation": {"key": "sector_rotation", "file": "SectorRotation.py", "txt": "SectorRotation.txt"},
    "Quality Investing": {"key": "quality_investing", "file": "QualityInvesting.py", "txt": "QualityInvesting.txt"},
    "Opening Range BreaKout": {
        "key": "opening_range_breakout",
        "file": "OpeningRangeBreaKout.py",
        "txt": "OpeningRangeBreaKout.txt",
    },
    "VWAP Reversion": {"key": "vwap_reversion", "file": "VWAP Reversion.py", "txt": "VWAP_Reversion.txt"},
    "Momentum Intradia": {"key": "momentum_intradia", "file": "MomentumIntradia.py", "txt": "MomentumIntradia.txt"},
    "Scalping The PullBacks": {
        "key": "scalping_pullbacks",
        "file": "ScalpingThePullBacKs.py",
        "txt": "ScalpingThePullBacKs.txt",
    },
    "Gap and Go": {"key": "gap_and_go", "file": "Gap and Go.py", "txt": "Gap_and_Go.txt"},
    "Follow The Money": {"key": "follow_the_money", "file": "FollowTheMoney.py", "txt": "Follow_The_Money.txt"},
    "Entrada Dinero Direccional": {
        "key": "entrada_dinero_direccional",
        "file": "EntradaDineroDireccional.py",
        "txt": "Entrada_Dinero_Direccional.txt",
    },
    "Acumula Metales": {"key": "acumula_metales", "file": "AcumulaMetales.py", "txt": "Acumula_Metales.txt"},
    "Acumulacion": {"key": "acumulacion", "file": "Acumulacion.py", "txt": "Acumulacion.txt"},
    "Reversion RSI 5": {"key": "extension_reversal", "file": "ReversionRSI5.py", "txt": "Reversion_RSI_5.txt"},
}

STRATEGY_ALIASES = {
    "RSI14 Two Hour Reversion": "Reversion RSI 5",
}


def sync_legacy_outputs(
    signals: list[StrategySignal],
    dataset: dict[str, TickerData],
    summary: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    LEGACY_SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = parse_datetime(summary.get("started_at")) or datetime.now(UTC)
    finished_at = parse_datetime(summary.get("finished_at")) or datetime.now(UTC)
    signal_date = finished_at.astimezone().date().isoformat()

    grouped: dict[str, list[str]] = {}
    for signal in signals:
        legacy_name = legacy_strategy_name(signal.strategy)
        if legacy_name not in LEGACY_STRATEGIES:
            continue
        grouped.setdefault(legacy_name, []).append(format_legacy_line(signal, signal_date))

    status_items = {}
    enabled_names = legacy_enabled_names(summary, grouped)
    for strategy_name in enabled_names:
        legacy = LEGACY_STRATEGIES[strategy_name]
        txt_path = LEGACY_SIGNALS_DIR / legacy["txt"]
        lines = grouped.get(strategy_name, [])
        _path, new_count = append_new_lines(txt_path, lines)
        status_items[strategy_name] = {
            "file": legacy["file"],
            "txt": legacy["txt"],
            "ok": True,
            "txt_updated": new_count > 0,
            "returncode": 0,
            "error": "",
            "log": f"Motor V2 OK. Avisos generados: {len(lines)}. Avisos nuevos TXT: {new_count}.",
            "ran_at": finished_at.isoformat(),
        }
        print(f"[LEGACY] {strategy_name} | {legacy['txt']} | avisos={len(lines)} | nuevos={new_count}")

    status_payload = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "strategies": status_items,
    }
    LEGACY_STATUS_FILE.write_text(json.dumps(status_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[LEGACY] Estado guardado en: {LEGACY_STATUS_FILE}")
    top_count = write_top_money_volume_assets(dataset, int(config.get("top_money_volume_limit", 20)))
    print(f"[LEGACY] Top volumen monetario guardado: {top_count}")

    sync_result = {"txt_status": status_items}
    if truthy(config.get("legacy_sync_database", True)) and not truthy(os.environ.get("TRADING_V2_SKIP_DB_SYNC")):
        sync_result["database_sync"] = run_project_script("sync_signals_to_db.py")
    if truthy(config.get("legacy_run_simulation", True)) and not truthy(os.environ.get("TRADING_V2_SKIP_SIMULATION")):
        sync_result["simulation"] = run_project_script("Estrategias/simulate_operations.py")
    if truthy(config.get("legacy_sync_database_after_simulation", False)) and not truthy(os.environ.get("TRADING_V2_SKIP_DB_SYNC")):
        sync_result["database_sync_after_simulation"] = run_project_script("sync_signals_to_db.py")
    return sync_result


def write_top_money_volume_assets(dataset: dict[str, TickerData], limit: int = 20) -> int:
    rows = []
    for symbol, ticker in dataset.items():
        metrics = ticker.metrics
        money_volume = first_number(
            metrics.get("current_dollar_volume"),
            metrics.get("avg_dollar_volume_20d"),
            metrics.get("avg_dollar_volume_21d"),
        )
        price = first_number(metrics.get("price"), metrics.get("close"))
        if money_volume is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(metrics.get("fmp_company_name") or symbol),
                "market": str(metrics.get("market") or metrics.get("exchange") or ""),
                "price": price or 0.0,
                "money_volume": money_volume,
            }
        )
    rows.sort(key=lambda item: item["money_volume"], reverse=True)
    selected = rows[: max(1, limit)]
    lines = [
        f"{index}|{row['symbol']}|{row['name']}|{row['market']}|{row['price']:.4f}|{row['money_volume']:.2f}"
        for index, row in enumerate(selected, start=1)
    ]
    TOP_MONEY_VOLUME_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def legacy_enabled_names(summary: dict[str, Any], grouped: dict[str, list[str]]) -> list[str]:
    enabled_keys = {str(key).strip().lower() for key in summary.get("enabled_strategies", [])}
    names = [
        name
        for name, legacy in LEGACY_STRATEGIES.items()
        if legacy["key"] in enabled_keys
    ]
    if summary.get("pairs_loaded"):
        names.append("Pairs Trading")
    for name in grouped:
        if name not in names:
            names.append(name)
    return list(dict.fromkeys(names))


def format_legacy_line(signal: StrategySignal, signal_date: str) -> str:
    parts = [
        signal.symbol,
        f"Fecha: {signal_date}",
        f"Direccion: {signal.direction}",
        f"Precio actual: {format_number(signal.entry)}",
        f"Apertura: {format_number(signal.entry)}",
        f"Cierre: {format_number(signal.target)}",
        f"Stop Loss: {format_number(signal.stop)}",
        f"Score: {signal.score:.2f}",
        f"Estrategia: {legacy_strategy_name(signal.strategy)}",
        f"Motivo: {signal.reason}",
    ]
    for key, value in sorted(signal.metrics.items()):
        if value is None:
            continue
        parts.append(f"{key}: {format_metric(value)}")
    return " | ".join(parts)


def legacy_strategy_name(name: str) -> str:
    return STRATEGY_ALIASES.get(name, name)


def run_project_script(script: str) -> dict[str, Any]:
    command = [sys.executable, str(PROJECT_DIR / script)]
    print(f"[LEGACY] Ejecutando: {' '.join(command)}")
    env = os.environ.copy()
    if env.get("DATABASE_URL"):
        env["TRADING_DATABASE_MODE"] = "postgres"
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_DIR),
        env=env,
        text=True,
        capture_output=True,
        timeout=None,
    )
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return {
        "script": script,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_number(value: Any) -> str:
    if value is None:
        return "NO"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def format_metric(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def first_number(*values: Any) -> float | None:
    for value in values:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}
