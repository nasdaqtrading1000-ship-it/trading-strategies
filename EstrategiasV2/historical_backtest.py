from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config_loader import load_config, load_env_files  # noqa: E402
from data_engine import bars_to_dataframes, build_ticker_dataset, chunked, load_tickers, resolve_adjustment  # noqa: E402
from indicators import distance_pct, rsi, sma  # noqa: E402
from market_scanner import filter_assets, load_universe_assets  # noqa: E402
from run_engine_v2 import enabled_strategies, extend_with_strategy_symbols  # noqa: E402
from strategy_rules.entrada_dinero_direccional import EXCLUDED_SYMBOLS  # noqa: E402
from strategy_rules import STRATEGY_REGISTRY  # noqa: E402


DEFAULT_OUTPUT_FILE = BASE_DIR / "outputs" / "historical_backtest_5y.json"
DEFAULT_TRADE_USD = 1000.0
DEFAULT_MAX_HOLDING_DAYS = 60
DEFAULT_WARMUP_BARS = 260
NO_AUTO_CLOSE_STRATEGY_NAMES = {"Acumulacion", "Acumula Metales"}


@dataclass
class BacktestOperation:
    id: int
    strategy: str
    strategy_key: str
    symbol: str
    direction: str
    signal_date: str
    entry_date: str
    entry_price: float
    target_price: float | None
    stop_loss: float | None
    shares: float
    score: float
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "OPEN"
    exit_date: str | None = None
    exit_price: float | None = None
    close_reason: str | None = None
    holding_days: int = 0
    profit_usd: float = 0.0
    profit_pct: float = 0.0


def run_last_5_years_backtest(
    config_path: str | Path | None = None,
    output_path: str | Path | None = DEFAULT_OUTPUT_FILE,
    years: int = 5,
    max_tickers: int | None = None,
    trade_usd: float = DEFAULT_TRADE_USD,
    max_holding_days: int = DEFAULT_MAX_HOLDING_DAYS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    force_close_at_end: bool = True,
    rolling_filter: bool = True,
    filter_window_months: int = 6,
    asset_filters: dict[str, Any] | None = None,
    end_date: date | None = None,
    auto_cutoff_from_live_operations: bool = True,
    strategy_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Download the last years of daily data and run every V2 strategy historically.

    This function is intentionally independent from Flask and from the databases.
    It returns a plain dict and, optionally, writes the same payload as JSON.
    """
    load_env_files()
    config = load_config(Path(config_path) if config_path else None)
    if strategy_keys:
        config["enabled_strategies"] = normalize_strategy_keys(strategy_keys)
    if max_tickers is not None:
        config["max_tickers"] = max_tickers

    tickers = load_tickers(
        config["tickers_path"],
        benchmark=str(config.get("benchmark", "QQQ")),
        max_tickers=int(config.get("max_tickers") or 0) or None,
    )
    tickers = extend_with_strategy_symbols(tickers, config)
    live_operations_from_date = None
    cutoff_reason = "today"
    if end_date is None:
        if auto_cutoff_from_live_operations:
            live_operations_from_date = first_live_operation_date()
        if live_operations_from_date:
            end_date = live_operations_from_date - timedelta(days=1)
            cutoff_reason = "day_before_first_live_operation"
        else:
            end_date = date.today()
    else:
        cutoff_reason = "manual"
    start_date = end_date - timedelta(days=int(years * 365.25) + 420)
    fetch_end_date = end_date + timedelta(days=1)

    daily_data = fetch_daily_data_between(tickers, config, start_date, fetch_end_date)
    asset_filter_windows = None
    if rolling_filter:
        asset_filter_windows = build_rolling_asset_filter_windows(
            daily_data=daily_data,
            config=config,
            years=years,
            window_months=filter_window_months,
            asset_filters=asset_filters,
            as_of_date=end_date,
        )
    result = run_historical_backtest(
        daily_data=daily_data,
        config=config,
        years=years,
        trade_usd=trade_usd,
        max_holding_days=max_holding_days,
        warmup_bars=warmup_bars,
        force_close_at_end=force_close_at_end,
        asset_filter_windows=asset_filter_windows,
        as_of_date=end_date,
        live_operations_from_date=live_operations_from_date,
        cutoff_reason=cutoff_reason,
    )
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if strategy_keys:
            result = merge_strategy_backtest_result(path, result)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
        result["output_path"] = str(path)
    return result


def run_historical_backtest(
    daily_data: dict[str, pd.DataFrame],
    config: dict[str, Any],
    years: int = 5,
    trade_usd: float = DEFAULT_TRADE_USD,
    max_holding_days: int = DEFAULT_MAX_HOLDING_DAYS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    force_close_at_end: bool = True,
    asset_filter_windows: list[dict[str, Any]] | None = None,
    as_of_date: date | None = None,
    live_operations_from_date: date | None = None,
    cutoff_reason: str = "today",
) -> dict[str, Any]:
    strategies = enabled_strategies(config)
    strategy_by_name = {strategy.name: strategy for strategy in strategies}
    strategy_keys = {strategy.name: strategy.key for strategy in strategies}
    benchmark = str(config.get("benchmark", "QQQ")).upper()
    effective_as_of_date = as_of_date or date.today()
    sessions = backtest_sessions(daily_data, benchmark, years, effective_as_of_date)
    if asset_filter_windows:
        sessions = [
            session
            for session in sessions
            if active_asset_window(asset_filter_windows, session)[1] is not None
        ]
    print(
        "Backtest historico | inicio calculo | "
        f"sesiones={len(sessions)} | activos={len(daily_data)} | "
        f"estrategias={','.join(strategy.key for strategy in strategies)}",
        flush=True,
    )
    if is_fast_accumulation_backtest(strategies):
        return run_fast_accumulation_backtest(
            daily_data=daily_data,
            config=config,
            strategies=strategies,
            sessions=sessions,
            years=years,
            trade_usd=trade_usd,
            max_holding_days=max_holding_days,
            force_close_at_end=force_close_at_end,
            asset_filter_windows=asset_filter_windows,
            effective_as_of_date=effective_as_of_date,
            live_operations_from_date=live_operations_from_date,
            cutoff_reason=cutoff_reason,
        )
    if is_fast_entrada_dinero_backtest(strategies):
        return run_fast_entrada_dinero_backtest(
            daily_data=daily_data,
            config=config,
            strategy=strategies[0],
            sessions=sessions,
            years=years,
            trade_usd=trade_usd,
            max_holding_days=max_holding_days,
            force_close_at_end=force_close_at_end,
            asset_filter_windows=asset_filter_windows,
            effective_as_of_date=effective_as_of_date,
            live_operations_from_date=live_operations_from_date,
            cutoff_reason=cutoff_reason,
        )

    operations: list[BacktestOperation] = []
    open_operations: list[BacktestOperation] = []
    daily_history: dict[str, list[dict[str, Any]]] = {strategy.name: [] for strategy in strategies}
    errors: list[dict[str, str]] = []
    next_operation_id = 1
    seen_daily_entries: set[tuple[str, str, str]] = set()
    window_runtime_stats = {
        index: {"signals": 0, "operations": 0}
        for index, _window in enumerate(asset_filter_windows or [])
    }

    for index, session_date in enumerate(sessions, start=1):
        active_window_index, active_window = active_asset_window(asset_filter_windows, session_date)
        allowed_symbols = active_window.get("symbols") if active_window else None
        current_prices = prices_for_session(daily_data, session_date)
        close_open_operations(open_operations, operations, daily_data, session_date, max_holding_days)

        dataset = build_dataset_for_session(daily_data, config, session_date, warmup_bars, allowed_symbols)
        signals_today = []
        for symbol in sorted(dataset):
            ticker = dataset[symbol]
            for strategy in strategies:
                try:
                    signal = strategy.analyze(ticker, config)
                except Exception as error:
                    errors.append(
                        {
                            "date": session_date.isoformat(),
                            "strategy": strategy.name,
                            "symbol": symbol,
                            "error": str(error),
                        }
                    )
                    continue
                if signal:
                    signals_today.append(signal)
        if active_window_index is not None:
            window_runtime_stats[active_window_index]["signals"] += len(signals_today)

        for signal in signals_today:
            entry = as_float(signal.entry) or current_prices.get(signal.symbol)
            if not entry or entry <= 0:
                continue
            daily_key = (signal.strategy, signal.symbol, session_date.isoformat())
            if daily_key in seen_daily_entries:
                continue
            seen_daily_entries.add(daily_key)
            operation = BacktestOperation(
                id=next_operation_id,
                strategy=signal.strategy,
                strategy_key=strategy_keys.get(signal.strategy, ""),
                symbol=signal.symbol,
                direction=str(signal.direction or "LONG").upper(),
                signal_date=session_date.isoformat(),
                entry_date=session_date.isoformat(),
                entry_price=float(entry),
                target_price=as_float(signal.target),
                stop_loss=as_float(signal.stop),
                shares=round(float(trade_usd) / float(entry), 6),
                score=float(signal.score or 0),
                reason=signal.reason,
                metrics=safe_metrics(signal.metrics),
            )
            next_operation_id += 1
            operations.append(operation)
            open_operations.append(operation)
            if active_window_index is not None:
                window_runtime_stats[active_window_index]["operations"] += 1

        for strategy_name in daily_history:
            closed = [operation for operation in operations if operation.strategy == strategy_name and operation.status == "CLOSED"]
            open_count = sum(1 for operation in open_operations if operation.strategy == strategy_name and operation.status == "OPEN")
            daily_history[strategy_name].append(
                {
                    "date": session_date.isoformat(),
                    "closed_operations": len(closed),
                    "open_operations": open_count,
                    "realized_profit_usd": round(sum(operation.profit_usd for operation in closed), 2),
                    "realized_return_pct_on_trades": round(sum(operation.profit_pct for operation in closed), 4),
                }
            )

        if index == 1 or index % 5 == 0 or index == len(sessions):
            print(
                f"Backtest historico | {index}/{len(sessions)} sesiones | "
                f"activos_sesion={len(dataset)} | operaciones={len(operations)}",
                flush=True,
            )

    if force_close_at_end and sessions:
        final_date = sessions[-1]
        for operation in list(open_operations):
            if operation.status != "OPEN":
                continue
            price = price_on_or_before(daily_data, operation.symbol, final_date) or operation.entry_price
            close_operation(operation, final_date, float(price), "FIN_BACKTEST")

    closed_operations = [operation for operation in operations if operation.status == "CLOSED"]
    open_operations_payload = [operation for operation in operations if operation.status == "OPEN"]
    summary_by_strategy = {
        strategy.name: summarize_strategy(strategy.name, operations)
        for strategy in strategies
    }
    total_profit = sum(operation.profit_usd for operation in operations)
    window_payload = enrich_window_payload(asset_filter_windows, operations, window_runtime_stats)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "rolling_asset_filter_backtest" if asset_filter_windows else "historical_backtest_daily",
        "years": years,
        "backtest_cutoff_date": effective_as_of_date.isoformat(),
        "live_operations_from_date": live_operations_from_date.isoformat() if live_operations_from_date else None,
        "cutoff_reason": cutoff_reason,
        "sessions": len(sessions),
        "tickers_loaded": len(daily_data),
        "asset_filter_applied": bool(asset_filter_windows),
        "filter_window_months": window_payload["window_months"] if window_payload else None,
        "asset_filter_windows": window_payload["windows"] if window_payload else [],
        "enabled_strategies": [strategy.key for strategy in strategies],
        "notes": [
            "Backtest independiente de Flask, SQLite y PostgreSQL.",
            "Usa velas diarias; las estrategias intradia pueden dar 0 senales si no tienen datos intradia historicos.",
            "Las estrategias con datos fundamentales FMP historicos no se enriquecen aqui; se ejecutan, pero pueden no generar senales.",
        ],
        "totals": {
            "operations": len(operations),
            "closed_operations": len(closed_operations),
            "open_operations": len(open_operations_payload),
            "profit_usd": round(total_profit, 2),
            "average_profit_pct": round(average([operation.profit_pct for operation in closed_operations]), 4),
            "win_rate_pct": win_rate(closed_operations),
        },
        "summary_by_strategy": summary_by_strategy,
        "daily_history_by_strategy": daily_history,
        "closed_operations": [asdict(operation) for operation in closed_operations],
        "open_operations": [asdict(operation) for operation in open_operations_payload],
        "errors": errors[:500],
        "errors_truncated": max(0, len(errors) - 500),
    }


def is_fast_accumulation_backtest(strategies: list[Any]) -> bool:
    keys = {getattr(strategy, "key", "") for strategy in strategies}
    return bool(keys) and keys.issubset({"acumulacion", "acumula_metales"})


def is_fast_entrada_dinero_backtest(strategies: list[Any]) -> bool:
    keys = {getattr(strategy, "key", "") for strategy in strategies}
    return keys == {"entrada_dinero_direccional"}


def run_fast_entrada_dinero_backtest(
    daily_data: dict[str, pd.DataFrame],
    config: dict[str, Any],
    strategy: Any,
    sessions: list[date],
    years: int,
    trade_usd: float,
    max_holding_days: int,
    force_close_at_end: bool,
    asset_filter_windows: list[dict[str, Any]] | None,
    effective_as_of_date: date,
    live_operations_from_date: date | None,
    cutoff_reason: str,
) -> dict[str, Any]:
    print("Backtest historico | modo rapido entrada dinero direccional activado", flush=True)
    prepared = prepare_entrada_dinero_frames(daily_data)
    operations: list[BacktestOperation] = []
    open_operations: list[BacktestOperation] = []
    daily_history: dict[str, list[dict[str, Any]]] = {strategy.name: []}
    seen_daily_entries: set[tuple[str, str, str]] = set()
    next_operation_id = 1
    window_runtime_stats = {
        index: {"signals": 0, "operations": 0}
        for index, _window in enumerate(asset_filter_windows or [])
    }

    min_price = float(config.get("entrada_dinero_min_price", 2))
    min_dollar_volume = float(config.get("entrada_dinero_min_dollar_volume_20d", 5_000_000))
    top_liquidity = int(config.get("entrada_dinero_top_liquidity", 100))
    top_money = int(config.get("entrada_dinero_top_money", 20))
    top_final = int(config.get("entrada_dinero_top_final", 10))
    target_pct = float(config.get("entrada_dinero_target_pct", 10))
    stop_pct = float(config.get("entrada_dinero_stop_pct", 8))

    for index, session_date in enumerate(sessions, start=1):
        active_window_index, active_window = active_asset_window(asset_filter_windows, session_date)
        allowed_symbols = active_window.get("symbols") if active_window else None
        allowed = {str(symbol).upper() for symbol in allowed_symbols} if allowed_symbols else None
        close_fast_open_operations(open_operations, prepared, session_date, max_holding_days)

        candidates = []
        for symbol, rows_by_date in prepared.items():
            if symbol in EXCLUDED_SYMBOLS:
                continue
            if allowed is not None and symbol not in allowed:
                continue
            row = rows_by_date.get(session_date)
            if row is None:
                continue
            payload = entrada_dinero_candidate_from_row(symbol, row, min_price, min_dollar_volume)
            if payload:
                candidates.append(payload)

        liquidity_pool = sorted(candidates, key=lambda item: item["avg_dollar_volume_20d"], reverse=True)[:top_liquidity]
        money_pool = sorted(liquidity_pool, key=lambda item: item["money_in_ratio"], reverse=True)[:top_money]
        final_signals = sorted(money_pool, key=lambda item: item["money_in_ratio"], reverse=True)[:top_final]
        if active_window_index is not None:
            window_runtime_stats[active_window_index]["signals"] += len(final_signals)

        for rank, signal_payload in enumerate(final_signals, start=1):
            daily_key = (strategy.name, signal_payload["symbol"], session_date.isoformat())
            if daily_key in seen_daily_entries:
                continue
            seen_daily_entries.add(daily_key)
            price = signal_payload["price"]
            score = round((top_final + 1 - rank) * 10 + signal_payload["money_in_ratio"] * 5, 2)
            operation = BacktestOperation(
                id=next_operation_id,
                strategy=strategy.name,
                strategy_key=strategy.key,
                symbol=signal_payload["symbol"],
                direction="LONG",
                signal_date=session_date.isoformat(),
                entry_date=session_date.isoformat(),
                entry_price=price,
                target_price=round(price * (1 + target_pct / 100), 4),
                stop_loss=round(price * (1 - stop_pct / 100), 4),
                shares=round(float(trade_usd) / price, 6),
                score=score,
                reason="Entrada de dinero con direccion alcista: liquidez alta, ratio 5D/120D destacado, precio sobre SMA20, SMA20 sobre SMA50 y rentabilidad 5D positiva.",
                metrics={
                    "price": price,
                    "avg_dollar_volume_20d": signal_payload["avg_dollar_volume_20d"],
                    "avg_dollar_volume_120d": signal_payload["avg_dollar_volume_120d"],
                    "dollar_volume_ma5_vs_ma120": signal_payload["money_in_ratio"],
                    "daily_sma20": signal_payload["daily_sma20"],
                    "daily_sma50": signal_payload["daily_sma50"],
                    "daily_return_5d_pct": signal_payload["daily_return_5d_pct"],
                    "rank_money_in_ratio": rank,
                },
            )
            next_operation_id += 1
            operations.append(operation)
            open_operations.append(operation)
            if active_window_index is not None:
                window_runtime_stats[active_window_index]["operations"] += 1

        closed = [operation for operation in operations if operation.strategy == strategy.name and operation.status == "CLOSED"]
        open_count = sum(1 for operation in open_operations if operation.strategy == strategy.name and operation.status == "OPEN")
        daily_history[strategy.name].append(
            {
                "date": session_date.isoformat(),
                "closed_operations": len(closed),
                "open_operations": open_count,
                "realized_profit_usd": round(sum(operation.profit_usd for operation in closed), 2),
                "realized_return_pct_on_trades": round(sum(operation.profit_pct for operation in closed), 4),
            }
        )

        if index == 1 or index % 25 == 0 or index == len(sessions):
            print(
                f"Backtest historico | rapido entrada dinero | {index}/{len(sessions)} sesiones | "
                f"candidatos={len(candidates)} | operaciones={len(operations)}",
                flush=True,
            )

    if force_close_at_end and sessions:
        final_date = sessions[-1]
        for operation in list(open_operations):
            if operation.status != "OPEN":
                continue
            row = prepared.get(operation.symbol, {}).get(final_date)
            price = as_float(row.get("close")) if row is not None else operation.entry_price
            close_operation(operation, final_date, float(price or operation.entry_price), "FIN_BACKTEST")

    closed_operations = [operation for operation in operations if operation.status == "CLOSED"]
    open_operations_payload = [operation for operation in operations if operation.status == "OPEN"]
    total_profit = sum(operation.profit_usd for operation in operations)
    window_payload = enrich_window_payload(asset_filter_windows, operations, window_runtime_stats)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "fast_entrada_dinero_direccional_backtest",
        "years": years,
        "backtest_cutoff_date": effective_as_of_date.isoformat(),
        "live_operations_from_date": live_operations_from_date.isoformat() if live_operations_from_date else None,
        "cutoff_reason": cutoff_reason,
        "sessions": len(sessions),
        "tickers_loaded": len(daily_data),
        "asset_filter_applied": bool(asset_filter_windows),
        "filter_window_months": window_payload["window_months"] if window_payload else None,
        "asset_filter_windows": window_payload["windows"] if window_payload else [],
        "enabled_strategies": [strategy.key],
        "notes": [
            "Backtest rapido para Entrada Dinero Direccional.",
            "Precalcula SMA20, SMA50, volumen monetario 5/20/120 y rentabilidad 5D por activo.",
        ],
        "totals": {
            "operations": len(operations),
            "closed_operations": len(closed_operations),
            "open_operations": len(open_operations_payload),
            "profit_usd": round(total_profit, 2),
            "average_profit_pct": round(average([operation.profit_pct for operation in closed_operations]), 4),
            "win_rate_pct": win_rate(closed_operations),
        },
        "summary_by_strategy": {strategy.name: summarize_strategy(strategy.name, operations)},
        "daily_history_by_strategy": daily_history,
        "closed_operations": [asdict(operation) for operation in closed_operations],
        "open_operations": [asdict(operation) for operation in open_operations_payload],
        "errors": [],
        "errors_truncated": 0,
    }


def run_fast_accumulation_backtest(
    daily_data: dict[str, pd.DataFrame],
    config: dict[str, Any],
    strategies: list[Any],
    sessions: list[date],
    years: int,
    trade_usd: float,
    max_holding_days: int,
    force_close_at_end: bool,
    asset_filter_windows: list[dict[str, Any]] | None,
    effective_as_of_date: date,
    live_operations_from_date: date | None,
    cutoff_reason: str,
) -> dict[str, Any]:
    print("Backtest historico | modo rapido acumulacion activado", flush=True)
    prepared = prepare_accumulation_frames(daily_data)
    strategy_keys = {strategy.name: strategy.key for strategy in strategies}
    strategy_names = [strategy.name for strategy in strategies]
    metals_symbols = {
        str(symbol).strip().upper()
        for symbol in (config.get("metals_symbols") or ["GLD", "SLV", "IAU", "GDX", "GDXJ", "SIL", "SILJ", "PPLT", "PALL", "COPX"])
        if str(symbol).strip()
    }
    min_volume = float(config.get("min_avg_dollar_volume", 20_000_000))

    operations: list[BacktestOperation] = []
    open_operations: list[BacktestOperation] = []
    daily_history: dict[str, list[dict[str, Any]]] = {name: [] for name in strategy_names}
    next_operation_id = 1
    seen_daily_entries: set[tuple[str, str, str]] = set()
    window_runtime_stats = {
        index: {"signals": 0, "operations": 0}
        for index, _window in enumerate(asset_filter_windows or [])
    }

    for index, session_date in enumerate(sessions, start=1):
        active_window_index, active_window = active_asset_window(asset_filter_windows, session_date)
        allowed_symbols = active_window.get("symbols") if active_window else None
        allowed = {str(symbol).upper() for symbol in allowed_symbols} if allowed_symbols else None
        close_fast_open_operations(open_operations, prepared, session_date, max_holding_days)
        signals_today = 0

        for symbol, rows_by_date in prepared.items():
            if allowed is not None and symbol not in allowed:
                continue
            row = rows_by_date.get(session_date)
            if row is None:
                continue
            for strategy in strategies:
                if strategy.key == "acumula_metales" and symbol not in metals_symbols:
                    continue
                signal_payload = accumulation_signal_from_row(strategy.name, strategy.key, symbol, row, min_volume)
                if not signal_payload:
                    continue
                daily_key = (strategy.name, symbol, session_date.isoformat())
                if daily_key in seen_daily_entries:
                    continue
                seen_daily_entries.add(daily_key)
                signals_today += 1
                operation = BacktestOperation(
                    id=next_operation_id,
                    strategy=strategy.name,
                    strategy_key=strategy.key,
                    symbol=symbol,
                    direction="LONG",
                    signal_date=session_date.isoformat(),
                    entry_date=session_date.isoformat(),
                    entry_price=signal_payload["price"],
                    target_price=None,
                    stop_loss=None,
                    shares=round(float(trade_usd) / signal_payload["price"], 6),
                    score=signal_payload["score"],
                    reason=signal_payload["reason"],
                    metrics=signal_payload["metrics"],
                )
                next_operation_id += 1
                operations.append(operation)
                open_operations.append(operation)
                if active_window_index is not None:
                    window_runtime_stats[active_window_index]["operations"] += 1
        if active_window_index is not None:
            window_runtime_stats[active_window_index]["signals"] += signals_today

        for strategy_name in daily_history:
            closed = [operation for operation in operations if operation.strategy == strategy_name and operation.status == "CLOSED"]
            open_count = sum(1 for operation in open_operations if operation.strategy == strategy_name and operation.status == "OPEN")
            daily_history[strategy_name].append(
                {
                    "date": session_date.isoformat(),
                    "closed_operations": len(closed),
                    "open_operations": open_count,
                    "realized_profit_usd": round(sum(operation.profit_usd for operation in closed), 2),
                    "realized_return_pct_on_trades": round(sum(operation.profit_pct for operation in closed), 4),
                }
            )

        if index == 1 or index % 25 == 0 or index == len(sessions):
            print(
                f"Backtest historico | rapido acumulacion | {index}/{len(sessions)} sesiones | "
                f"operaciones={len(operations)}",
                flush=True,
            )

    if force_close_at_end and sessions:
        final_date = sessions[-1]
        for operation in list(open_operations):
            if operation.status != "OPEN":
                continue
            row = prepared.get(operation.symbol, {}).get(final_date)
            price = as_float(row.get("close")) if row is not None else operation.entry_price
            close_operation(operation, final_date, float(price or operation.entry_price), "FIN_BACKTEST")

    closed_operations = [operation for operation in operations if operation.status == "CLOSED"]
    open_operations_payload = [operation for operation in operations if operation.status == "OPEN"]
    total_profit = sum(operation.profit_usd for operation in operations)
    window_payload = enrich_window_payload(asset_filter_windows, operations, window_runtime_stats)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "fast_accumulation_backtest",
        "years": years,
        "backtest_cutoff_date": effective_as_of_date.isoformat(),
        "live_operations_from_date": live_operations_from_date.isoformat() if live_operations_from_date else None,
        "cutoff_reason": cutoff_reason,
        "sessions": len(sessions),
        "tickers_loaded": len(daily_data),
        "asset_filter_applied": bool(asset_filter_windows),
        "filter_window_months": window_payload["window_months"] if window_payload else None,
        "asset_filter_windows": window_payload["windows"] if window_payload else [],
        "enabled_strategies": [strategy.key for strategy in strategies],
        "notes": [
            "Backtest rapido para estrategias de acumulacion.",
            "Precalcula SMA180 diaria, SMA120 semanal, RSI14 y liquidez por activo.",
        ],
        "totals": {
            "operations": len(operations),
            "closed_operations": len(closed_operations),
            "open_operations": len(open_operations_payload),
            "profit_usd": round(total_profit, 2),
            "average_profit_pct": round(average([operation.profit_pct for operation in closed_operations]), 4),
            "win_rate_pct": win_rate(closed_operations),
        },
        "summary_by_strategy": {strategy.name: summarize_strategy(strategy.name, operations) for strategy in strategies},
        "daily_history_by_strategy": daily_history,
        "closed_operations": [asdict(operation) for operation in closed_operations],
        "open_operations": [asdict(operation) for operation in open_operations_payload],
        "errors": [],
        "errors_truncated": 0,
    }


def prepare_accumulation_frames(daily_data: dict[str, pd.DataFrame]) -> dict[str, dict[date, pd.Series]]:
    prepared = {}
    for symbol, raw_df in daily_data.items():
        if raw_df.empty:
            continue
        df = raw_df.copy().sort_index()
        df["daily_sma180"] = sma(df["close"], 180)
        df["daily_rsi14"] = rsi(df["close"], 14)
        df["avg_dollar_volume_20d"] = (df["close"] * df["volume"]).rolling(20).mean()
        weekly = (
            df[["open", "high", "low", "close", "volume"]]
            .resample("W-FRI")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
        )
        if weekly.empty:
            df["weekly_sma120"] = None
        else:
            weekly["weekly_sma120"] = sma(weekly["close"], 120)
            df["weekly_sma120"] = weekly["weekly_sma120"].reindex(df.index, method="ffill")
        df["session_date"] = pd.to_datetime(df.index).date
        day_rows = df.drop_duplicates("session_date", keep="last").set_index("session_date")
        prepared[str(symbol).upper()] = {item: row for item, row in day_rows.iterrows()}
    return prepared


def prepare_entrada_dinero_frames(daily_data: dict[str, pd.DataFrame]) -> dict[str, dict[date, pd.Series]]:
    prepared = {}
    for symbol, raw_df in daily_data.items():
        if raw_df.empty:
            continue
        df = raw_df.copy().sort_index()
        dollar_volume = df["close"] * df["volume"]
        df["avg_dollar_volume_5d"] = dollar_volume.rolling(5).mean()
        df["avg_dollar_volume_20d"] = dollar_volume.rolling(20).mean()
        df["avg_dollar_volume_120d"] = dollar_volume.rolling(120).mean()
        df["money_in_ratio"] = df["avg_dollar_volume_5d"] / df["avg_dollar_volume_120d"]
        df["daily_sma20"] = sma(df["close"], 20)
        df["daily_sma50"] = sma(df["close"], 50)
        df["daily_return_5d_pct"] = df["close"].pct_change(5) * 100
        df["session_date"] = pd.to_datetime(df.index).date
        day_rows = df.drop_duplicates("session_date", keep="last").set_index("session_date")
        prepared[str(symbol).upper()] = {item: row for item, row in day_rows.iterrows()}
    return prepared


def entrada_dinero_candidate_from_row(
    symbol: str,
    row: pd.Series,
    min_price: float,
    min_dollar_volume: float,
) -> dict[str, Any] | None:
    price = as_float(row.get("close"))
    avg_dollar_volume_20d = as_float(row.get("avg_dollar_volume_20d"))
    avg_dollar_volume_120d = as_float(row.get("avg_dollar_volume_120d"))
    money_in_ratio = as_float(row.get("money_in_ratio"))
    daily_sma20 = as_float(row.get("daily_sma20"))
    daily_sma50 = as_float(row.get("daily_sma50"))
    daily_return_5d = as_float(row.get("daily_return_5d_pct"))
    required = [price, avg_dollar_volume_20d, avg_dollar_volume_120d, money_in_ratio, daily_sma20, daily_sma50, daily_return_5d]
    if any(value is None for value in required):
        return None
    if price <= min_price:
        return None
    if avg_dollar_volume_20d < min_dollar_volume:
        return None
    if not (price > daily_sma20 > daily_sma50):
        return None
    if daily_return_5d <= 0:
        return None
    return {
        "symbol": symbol,
        "price": float(price),
        "avg_dollar_volume_20d": float(avg_dollar_volume_20d),
        "avg_dollar_volume_120d": float(avg_dollar_volume_120d),
        "money_in_ratio": float(money_in_ratio),
        "daily_sma20": float(daily_sma20),
        "daily_sma50": float(daily_sma50),
        "daily_return_5d_pct": float(daily_return_5d),
    }


def accumulation_signal_from_row(strategy_name: str, strategy_key: str, symbol: str, row: pd.Series, min_volume: float) -> dict[str, Any] | None:
    price = as_float(row.get("close"))
    daily_sma180 = as_float(row.get("daily_sma180"))
    weekly_sma120 = as_float(row.get("weekly_sma120"))
    rsi14 = as_float(row.get("daily_rsi14"))
    avg_dollar_volume = as_float(row.get("avg_dollar_volume_20d"))
    if None in [price, daily_sma180, weekly_sma120, rsi14, avg_dollar_volume] or avg_dollar_volume < min_volume:
        return None
    if not (price < daily_sma180 and price < weekly_sma120 and rsi14 < 30):
        return None
    distance_daily = distance_pct(price, daily_sma180)
    distance_weekly = distance_pct(price, weekly_sma120)
    reason = (
        "Metales por debajo de SMA180 diaria y SMA120 semanal con RSI14 bajo."
        if strategy_key == "acumula_metales"
        else "Acumulacion de activo castigado bajo medias largas y RSI14 bajo."
    )
    return {
        "price": float(price),
        "score": round((30 - rsi14) * 2 + abs(distance_daily or 0), 2),
        "reason": reason,
        "metrics": {
            "price": price,
            "daily_sma180": daily_sma180,
            "weekly_sma120": weekly_sma120,
            "daily_rsi14": rsi14,
            "distance_daily_sma180_pct": distance_daily,
            "distance_weekly_sma120_pct": distance_weekly,
        },
    }


def close_fast_open_operations(
    open_operations: list[BacktestOperation],
    prepared: dict[str, dict[date, pd.Series]],
    session_date: date,
    max_holding_days: int,
) -> None:
    for operation in list(open_operations):
        if operation.status != "OPEN" or operation.entry_date == session_date.isoformat():
            continue
        if is_no_auto_close_operation(operation):
            continue
        row = prepared.get(operation.symbol, {}).get(session_date)
        if row is None:
            continue
        close_price, reason = close_price_and_reason(operation, row, session_date, max_holding_days)
        if reason:
            close_operation(operation, session_date, close_price, reason)
            open_operations.remove(operation)


def fetch_daily_data_between(
    symbols: list[str],
    config: dict[str, Any],
    start_date: date,
    end_date: date,
) -> dict[str, pd.DataFrame]:
    try:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as error:
        raise RuntimeError("Falta alpaca-py para descargar datos historicos.") from error

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Faltan ALPACA_API_KEY y ALPACA_SECRET_KEY.")

    client = StockHistoricalDataClient(api_key, secret_key)
    batch_size = int(config.get("batch_size", 120))
    feed_name = str(config.get("data_feed", "IEX")).upper()
    feed = DataFeed.IEX if feed_name == "IEX" else DataFeed.SIP
    adjustment = resolve_adjustment(Adjustment, config)

    result: dict[str, pd.DataFrame] = {}
    for batch_number, batch in enumerate(chunked(symbols, batch_size), start=1):
        print(f"Historico diario | tanda {batch_number} | {len(batch)} activos | {start_date} -> {end_date}")
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            adjustment=adjustment,
            start=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
            end=datetime.combine(end_date, datetime.min.time(), tzinfo=UTC),
            feed=feed,
        )
        try:
            result.update(bars_to_dataframes(client.get_stock_bars(request).data))
        except Exception as error:
            print(f"Historico diario | tanda {batch_number} | ERROR lote: {error}")
            for symbol in batch:
                try:
                    request = StockBarsRequest(
                        symbol_or_symbols=[symbol],
                        timeframe=TimeFrame.Day,
                        adjustment=adjustment,
                        start=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
                        end=datetime.combine(end_date, datetime.min.time(), tzinfo=UTC),
                        feed=feed,
                    )
                    result.update(bars_to_dataframes(client.get_stock_bars(request).data))
                except Exception as symbol_error:
                    print(f"Historico diario | {symbol} | OMITIDO: {symbol_error}")
    return result


def backtest_sessions(daily_data: dict[str, pd.DataFrame], benchmark: str, years: int, as_of_date: date | None = None) -> list[date]:
    effective_as_of_date = as_of_date or date.today()
    cutoff = effective_as_of_date - timedelta(days=int(years * 365.25))
    benchmark_df = daily_data.get(benchmark)
    if benchmark_df is not None and not benchmark_df.empty:
        raw_dates = pd.to_datetime(benchmark_df.index).date
    else:
        raw_dates = sorted({item for df in daily_data.values() for item in pd.to_datetime(df.index).date})
    return [item for item in raw_dates if cutoff <= item <= effective_as_of_date]


def build_dataset_for_session(
    daily_data: dict[str, pd.DataFrame],
    config: dict[str, Any],
    session_date: date,
    warmup_bars: int,
    allowed_symbols: set[str] | list[str] | None = None,
) -> dict:
    windowed = {}
    allowed = {str(symbol).upper() for symbol in allowed_symbols} if allowed_symbols else None
    benchmark = str(config.get("benchmark", "QQQ")).upper()
    for symbol, df in daily_data.items():
        if allowed is not None and symbol != benchmark and symbol not in allowed:
            continue
        dates = pd.to_datetime(df.index).date
        current = df[dates <= session_date].tail(warmup_bars).copy()
        if len(current) >= 50:
            windowed[symbol] = current
    return build_ticker_dataset(windowed, {}, config)


def build_rolling_asset_filter_windows(
    daily_data: dict[str, pd.DataFrame],
    config: dict[str, Any],
    years: int,
    window_months: int = 6,
    asset_filters: dict[str, Any] | None = None,
    as_of_date: date | None = None,
) -> list[dict[str, Any]]:
    benchmark = str(config.get("benchmark", "QQQ")).upper()
    sessions = backtest_sessions(daily_data, benchmark, years, as_of_date)
    if not sessions:
        return []

    metadata = asset_metadata_by_symbol()
    filters = default_asset_filters(asset_filters)
    windows = []
    start = sessions[0]
    last_session = sessions[-1]
    window_index = 1
    while start <= last_session:
        raw_end = add_months(start, max(1, int(window_months))) - timedelta(days=1)
        end = min(raw_end, last_session)
        candidates = historical_asset_rows(daily_data, metadata, start)
        filtered, source, universe_total = filter_assets(filters, candidates)
        symbols = [asset["symbol"] for asset in filtered if asset.get("symbol")]
        windows.append(
            {
                "index": window_index,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "window_months": max(1, int(window_months)),
                "filter_reference_date": previous_session_date(daily_data, benchmark, start),
                "filter_source": source,
                "filters": dict(filters),
                "assets_before_filter": universe_total,
                "assets_after_filter": len(symbols),
                "symbols": symbols,
                "top_assets": filtered[:20],
            }
        )
        print(
            "Filtro rolling | "
            f"ventana {window_index} | {start} -> {end} | "
            f"activos {universe_total} -> {len(symbols)}",
            flush=True,
        )
        start = add_months(start, max(1, int(window_months)))
        window_index += 1
    return windows


def default_asset_filters(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = {
        "month_window": 1,
        "min_money_volume": 0,
        "day_volume_window": 1,
        "week_volume_window": 1,
        "limit": 0,
        "sector": "Todos",
        "market": "Todos",
        "data_source": "csv",
        "sort_by": "money_volume_selected",
    }
    for key, value in (overrides or {}).items():
        if value is not None:
            filters[key] = value
    return filters


def first_live_operation_date() -> date | None:
    dates = []
    dates.extend(live_operation_dates_from_sqlite(PROJECT_DIR / "strategies.db"))
    dates.extend(live_operation_dates_from_json(PROJECT_DIR / "Estrategias" / "operaciones_simuladas" / "operaciones_estado.json"))
    return min(dates) if dates else None


def live_operation_dates_from_sqlite(path: Path) -> list[date]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(path)
        rows = connection.execute(
            """
            SELECT opened_at, signal_date
            FROM simulated_operations
            WHERE status IN ('OPEN', 'CLOSED')
            """
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return []
    dates = []
    for opened_at, signal_date in rows:
        parsed = parse_date_value(opened_at) or parse_date_value(signal_date)
        if parsed:
            dates.append(parsed)
    return dates


def live_operation_dates_from_json(path: Path) -> list[date]:
    try:
        operations = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(operations, list):
        return []
    dates = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("status") not in {"OPEN", "CLOSED"}:
            continue
        parsed = parse_date_value(operation.get("opened_at")) or parse_date_value(operation.get("signal_date"))
        if parsed:
            dates.append(parsed)
    return dates


def parse_date_value(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def historical_asset_rows(
    daily_data: dict[str, pd.DataFrame],
    metadata: dict[str, dict[str, Any]],
    reference_date: date,
) -> list[dict[str, Any]]:
    rows = []
    for symbol, df in daily_data.items():
        dates = pd.to_datetime(df.index).date
        history = df[dates < reference_date].tail(90).copy()
        if len(history) < 21:
            continue
        meta = metadata.get(symbol, {})
        dollar_volume = history["close"] * history["volume"]
        latest = history.iloc[-1]
        rows.append(
            {
                "symbol": symbol,
                "name": meta.get("name") or symbol,
                "sector": meta.get("sector") or "Sin clasificar",
                "market": meta.get("market") or "Otro",
                "price": float(latest["close"]),
                "money_volume": average_tail(dollar_volume, 21),
                "money_volume_1m": average_tail(dollar_volume, 21),
                "money_volume_2m": average_tail(dollar_volume, 42),
                "money_volume_3m": average_tail(dollar_volume, 63),
                "day_money_volume": average_tail(dollar_volume, 1),
                "week_money_volume": average_tail(dollar_volume, 5),
                "day_money_volume_1d": average_tail(dollar_volume, 1),
                "day_money_volume_2d": average_tail(dollar_volume, 2),
                "day_money_volume_3d": average_tail(dollar_volume, 3),
                "day_money_volume_4d": average_tail(dollar_volume, 4),
                "day_money_volume_5d": average_tail(dollar_volume, 5),
                "week_money_volume_1w": average_tail(dollar_volume, 5),
                "week_money_volume_2w": average_tail(dollar_volume, 10),
                "week_money_volume_3w": average_tail(dollar_volume, 15),
                "week_money_volume_4w": average_tail(dollar_volume, 20),
                "week_money_volume_5w": average_tail(dollar_volume, 25),
                "day_volume_score": 1.0,
                "week_volume_score": 1.0,
            }
        )
    return rows


def asset_metadata_by_symbol() -> dict[str, dict[str, Any]]:
    try:
        assets = load_universe_assets()
    except Exception:
        assets = []
    return {str(asset.get("symbol", "")).upper(): asset for asset in assets if asset.get("symbol")}


def active_asset_window(
    windows: list[dict[str, Any]] | None,
    session_date: date,
) -> tuple[int | None, dict[str, Any] | None]:
    if not windows:
        return None, None
    for index, window in enumerate(windows):
        start = date.fromisoformat(window["start"])
        end = date.fromisoformat(window["end"])
        if start <= session_date <= end:
            return index, window
    return None, None


def enrich_window_payload(
    windows: list[dict[str, Any]] | None,
    operations: list[BacktestOperation],
    runtime_stats: dict[int, dict[str, int]],
) -> dict[str, Any] | None:
    if not windows:
        return None
    payload = []
    for index, window in enumerate(windows):
        start = date.fromisoformat(window["start"])
        end = date.fromisoformat(window["end"])
        window_operations = [
            operation
            for operation in operations
            if start <= date.fromisoformat(operation.entry_date) <= end
        ]
        closed = [operation for operation in window_operations if operation.status == "CLOSED"]
        stats = runtime_stats.get(index, {})
        item = dict(window)
        item.pop("symbols", None)
        item["symbols_sample"] = window.get("symbols", [])[:50]
        item["signals"] = stats.get("signals", 0)
        item["operations"] = len(window_operations)
        item["closed_operations"] = len(closed)
        item["profit_usd"] = round(sum(operation.profit_usd for operation in window_operations), 2)
        item["win_rate_pct"] = win_rate(closed)
        payload.append(item)
    return {
        "window_months": windows[0].get("window_months", 6),
        "windows": payload,
    }


def previous_session_date(daily_data: dict[str, pd.DataFrame], benchmark: str, reference_date: date) -> str | None:
    df = daily_data.get(benchmark)
    if df is None or df.empty:
        return None
    dates = [item for item in pd.to_datetime(df.index).date if item < reference_date]
    return dates[-1].isoformat() if dates else None


def average_tail(series: pd.Series, periods: int) -> float:
    values = series.tail(periods)
    return float(values.mean()) if not values.empty else 0.0


def add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, days_in_month(year, month))
    return date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def months_between(start: date, end: date) -> int:
    return max(1, (end.year - start.year) * 12 + end.month - start.month)


def close_open_operations(
    open_operations: list[BacktestOperation],
    all_operations: list[BacktestOperation],
    daily_data: dict[str, pd.DataFrame],
    session_date: date,
    max_holding_days: int,
) -> None:
    for operation in list(open_operations):
        if operation.status != "OPEN" or operation.entry_date == session_date.isoformat():
            continue
        if is_no_auto_close_operation(operation):
            continue
        row = row_for_session(daily_data, operation.symbol, session_date)
        if row is None:
            continue
        close_price, reason = close_price_and_reason(operation, row, session_date, max_holding_days)
        if reason:
            close_operation(operation, session_date, close_price, reason)
            open_operations.remove(operation)


def close_price_and_reason(
    operation: BacktestOperation,
    row: pd.Series,
    session_date: date,
    max_holding_days: int,
) -> tuple[float, str | None]:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    target = operation.target_price
    stop = operation.stop_loss
    direction = operation.direction.upper()
    entry_day = date.fromisoformat(operation.entry_date)
    holding_days = (session_date - entry_day).days

    if direction == "SHORT":
        target_hit = target is not None and low <= target
        stop_hit = stop is not None and high >= stop
    else:
        target_hit = target is not None and high >= target
        stop_hit = stop is not None and low <= stop

    if target_hit and stop_hit:
        return float(stop), "STOP_LOSS_MISMA_VELA"
    if stop_hit:
        return float(stop), "STOP_LOSS"
    if target_hit:
        return float(target), "OBJETIVO"
    if holding_days >= max_holding_days:
        return close, "MAX_DIAS"
    return close, None


def close_operation(operation: BacktestOperation, session_date: date, exit_price: float, reason: str) -> None:
    operation.status = "CLOSED"
    operation.exit_date = session_date.isoformat()
    operation.exit_price = round(float(exit_price), 4)
    operation.close_reason = reason
    operation.holding_days = (session_date - date.fromisoformat(operation.entry_date)).days
    if operation.direction.upper() == "SHORT":
        profit_per_share = operation.entry_price - float(exit_price)
    else:
        profit_per_share = float(exit_price) - operation.entry_price
    operation.profit_usd = round(profit_per_share * operation.shares, 2)
    operation.profit_pct = round((profit_per_share / operation.entry_price) * 100, 4)


def mark_operation_to_market(operation: BacktestOperation, session_date: date, current_price: float) -> None:
    operation.exit_price = round(float(current_price), 4)
    operation.holding_days = (session_date - date.fromisoformat(operation.entry_date)).days
    if operation.direction.upper() == "SHORT":
        profit_per_share = operation.entry_price - float(current_price)
    else:
        profit_per_share = float(current_price) - operation.entry_price
    operation.profit_usd = round(profit_per_share * operation.shares, 2)
    operation.profit_pct = round((profit_per_share / operation.entry_price) * 100, 4)


def is_no_auto_close_operation(operation: BacktestOperation) -> bool:
    return operation.strategy in NO_AUTO_CLOSE_STRATEGY_NAMES


def summarize_strategy(strategy_name: str, operations: list[BacktestOperation]) -> dict[str, Any]:
    items = [operation for operation in operations if operation.strategy == strategy_name]
    closed = [operation for operation in items if operation.status == "CLOSED"]
    profit_values = [operation.profit_pct for operation in closed]
    profit_usd = sum(operation.profit_usd for operation in items)
    return {
        "operations": len(items),
        "closed_operations": len(closed),
        "open_operations": sum(1 for operation in items if operation.status == "OPEN"),
        "wins": sum(1 for operation in closed if operation.profit_usd > 0),
        "losses": sum(1 for operation in closed if operation.profit_usd < 0),
        "win_rate_pct": win_rate(closed),
        "profit_usd": round(profit_usd, 2),
        "average_profit_pct": round(average(profit_values), 4),
        "best_trade_pct": round(max(profit_values), 4) if profit_values else 0.0,
        "worst_trade_pct": round(min(profit_values), 4) if profit_values else 0.0,
        "average_holding_days": round(average([operation.holding_days for operation in closed]), 2),
    }


def prices_for_session(daily_data: dict[str, pd.DataFrame], session_date: date) -> dict[str, float]:
    prices = {}
    for symbol, df in daily_data.items():
        row = row_for_session(daily_data, symbol, session_date)
        if row is not None:
            prices[symbol] = float(row["close"])
    return prices


def row_for_session(daily_data: dict[str, pd.DataFrame], symbol: str, session_date: date) -> pd.Series | None:
    df = daily_data.get(symbol)
    if df is None or df.empty:
        return None
    dates = pd.to_datetime(df.index).date
    rows = df[dates == session_date]
    if rows.empty:
        return None
    return rows.iloc[-1]


def price_on_or_before(daily_data: dict[str, pd.DataFrame], symbol: str, session_date: date) -> float | None:
    df = daily_data.get(symbol)
    if df is None or df.empty:
        return None
    dates = pd.to_datetime(df.index).date
    rows = df[dates <= session_date]
    if rows.empty:
        return None
    return float(rows.iloc[-1]["close"])


def as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {str(key): json_safe(value) for key, value in (metrics or {}).items()}


def json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def json_default(value: Any) -> Any:
    return json_safe(value)


def merge_strategy_backtest_result(output_path: Path, strategy_result: dict[str, Any]) -> dict[str, Any]:
    if not output_path.exists():
        return strategy_result
    try:
        existing = json.loads(output_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return strategy_result
    if not isinstance(existing, dict):
        return strategy_result

    strategy_keys = {str(key) for key in strategy_result.get("enabled_strategies", []) if key}
    strategy_names = set((strategy_result.get("summary_by_strategy") or {}).keys())
    if not strategy_keys and not strategy_names:
        return strategy_result

    merged = dict(existing)
    merged.update(
        {
            "generated_at": strategy_result.get("generated_at"),
            "mode": strategy_result.get("mode", existing.get("mode")),
            "years": strategy_result.get("years", existing.get("years")),
            "backtest_cutoff_date": strategy_result.get("backtest_cutoff_date", existing.get("backtest_cutoff_date")),
            "live_operations_from_date": strategy_result.get("live_operations_from_date", existing.get("live_operations_from_date")),
            "cutoff_reason": strategy_result.get("cutoff_reason", existing.get("cutoff_reason")),
            "sessions": strategy_result.get("sessions", existing.get("sessions")),
            "tickers_loaded": strategy_result.get("tickers_loaded", existing.get("tickers_loaded")),
            "asset_filter_applied": strategy_result.get("asset_filter_applied", existing.get("asset_filter_applied")),
            "filter_window_months": strategy_result.get("filter_window_months", existing.get("filter_window_months")),
            "source_data": strategy_result.get("source_data", existing.get("source_data")),
        }
    )

    merged["enabled_strategies"] = merge_enabled_strategy_keys(
        existing.get("enabled_strategies", []),
        strategy_result.get("enabled_strategies", []),
    )
    merged["summary_by_strategy"] = merge_strategy_mapping(
        existing.get("summary_by_strategy", {}),
        strategy_result.get("summary_by_strategy", {}),
        strategy_names,
    )
    merged["daily_history_by_strategy"] = merge_strategy_mapping(
        existing.get("daily_history_by_strategy", {}),
        strategy_result.get("daily_history_by_strategy", {}),
        strategy_names,
    )
    merged["closed_operations"] = merge_operations(
        existing.get("closed_operations", []),
        strategy_result.get("closed_operations", []),
        strategy_keys,
        strategy_names,
    )
    merged["open_operations"] = merge_operations(
        existing.get("open_operations", []),
        strategy_result.get("open_operations", []),
        strategy_keys,
        strategy_names,
    )
    renumber_operations(merged["closed_operations"], merged["open_operations"])
    merged["totals"] = summarize_all_operations(merged["closed_operations"], merged["open_operations"])
    merged["asset_filter_windows"] = merge_asset_filter_windows(
        existing.get("asset_filter_windows", []),
        strategy_result.get("asset_filter_windows", []),
        merged["closed_operations"],
        merged["open_operations"],
    )
    merged["notes"] = merge_notes(existing.get("notes", []), strategy_result.get("notes", []))
    merged["errors"] = merge_errors(
        existing.get("errors", []),
        strategy_result.get("errors", []),
        strategy_names,
    )
    merged["errors_truncated"] = int(existing.get("errors_truncated") or 0) + int(strategy_result.get("errors_truncated") or 0)
    merged["merge_mode"] = "incremental_strategy_update"
    merged["last_incremental_update"] = {
        "generated_at": strategy_result.get("generated_at"),
        "strategy_keys": sorted(strategy_keys),
        "strategy_names": sorted(strategy_names),
    }
    return merged


def merge_enabled_strategy_keys(existing: list[Any], incoming: list[Any]) -> list[str]:
    output = []
    for value in list(existing or []) + list(incoming or []):
        key = str(value or "").strip()
        if key and key not in output:
            output.append(key)
    return output


def merge_strategy_mapping(existing: dict[str, Any], incoming: dict[str, Any], strategy_names: set[str]) -> dict[str, Any]:
    merged = {
        key: value
        for key, value in (existing or {}).items()
        if key not in strategy_names
    }
    merged.update(incoming or {})
    return merged


def merge_operations(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    strategy_keys: set[str],
    strategy_names: set[str],
) -> list[dict[str, Any]]:
    kept = [
        operation
        for operation in (existing or [])
        if not operation_matches_strategy(operation, strategy_keys, strategy_names)
    ]
    return kept + list(incoming or [])


def operation_matches_strategy(operation: dict[str, Any], strategy_keys: set[str], strategy_names: set[str]) -> bool:
    key = str(operation.get("strategy_key") or "")
    name = str(operation.get("strategy") or "")
    return (key and key in strategy_keys) or (name and name in strategy_names)


def renumber_operations(closed_operations: list[dict[str, Any]], open_operations: list[dict[str, Any]]) -> None:
    for index, operation in enumerate(closed_operations + open_operations, start=1):
        operation["id"] = index


def summarize_all_operations(closed_operations: list[dict[str, Any]], open_operations: list[dict[str, Any]]) -> dict[str, Any]:
    closed = list(closed_operations or [])
    open_items = list(open_operations or [])
    all_items = closed + open_items
    profit_usd = sum(float(operation.get("profit_usd") or 0) for operation in all_items)
    profit_pct_values = [float(operation.get("profit_pct") or 0) for operation in closed]
    return {
        "operations": len(closed) + len(open_items),
        "closed_operations": len(closed),
        "open_operations": len(open_items),
        "profit_usd": round(profit_usd, 2),
        "average_profit_pct": round(average(profit_pct_values), 4),
        "win_rate_pct": win_rate_from_payload(closed),
    }


def win_rate_from_payload(operations: list[dict[str, Any]]) -> float:
    if not operations:
        return 0.0
    wins = sum(1 for operation in operations if float(operation.get("profit_usd") or 0) > 0)
    return round((wins / len(operations)) * 100, 2)


def merge_asset_filter_windows(
    existing_windows: list[dict[str, Any]],
    incoming_windows: list[dict[str, Any]],
    closed_operations: list[dict[str, Any]],
    open_operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    windows = list(existing_windows or incoming_windows or [])
    operations = list(closed_operations or []) + list(open_operations or [])
    for window in windows:
        try:
            start = date.fromisoformat(str(window.get("start")))
            end = date.fromisoformat(str(window.get("end")))
        except ValueError:
            continue
        window_operations = [
            operation
            for operation in operations
            if operation_date_in_window(operation, start, end)
        ]
        closed = [operation for operation in window_operations if str(operation.get("status") or "").upper() == "CLOSED"]
        window["operations"] = len(window_operations)
        window["closed_operations"] = len(closed)
        window["profit_usd"] = round(sum(float(operation.get("profit_usd") or 0) for operation in window_operations), 2)
        window["win_rate_pct"] = win_rate_from_payload(closed)
    return windows


def operation_date_in_window(operation: dict[str, Any], start: date, end: date) -> bool:
    parsed = parse_date_value(operation.get("entry_date") or operation.get("signal_date"))
    return bool(parsed and start <= parsed <= end)


def merge_notes(existing: list[Any], incoming: list[Any]) -> list[str]:
    output = []
    for value in list(existing or []) + list(incoming or []):
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def merge_errors(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], strategy_names: set[str]) -> list[dict[str, Any]]:
    kept = [
        error
        for error in (existing or [])
        if str(error.get("strategy") or "") not in strategy_names
    ]
    return (kept + list(incoming or []))[:500]


def normalize_strategy_keys(values: list[str]) -> list[str]:
    keys = []
    for value in values:
        for part in str(value or "").split(","):
            key = part.strip().lower()
            if not key:
                continue
            if key not in STRATEGY_REGISTRY:
                raise ValueError(f"Estrategia V2 desconocida para backtest: {key}")
            if key not in keys:
                keys.append(key)
    return keys


def average(values: list[float | int]) -> float:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else 0.0


def win_rate(operations: list[BacktestOperation]) -> float:
    if not operations:
        return 0.0
    return round((sum(1 for operation in operations if operation.profit_usd > 0) / len(operations)) * 100, 2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest historico independiente del motor V2.")
    parser.add_argument("--years", type=int, default=5, help="Anos a simular.")
    parser.add_argument("--max-tickers", type=int, default=None, help="Limite de tickers para pruebas rapidas.")
    parser.add_argument("--trade-usd", type=float, default=DEFAULT_TRADE_USD, help="Capital simulado por operacion.")
    parser.add_argument("--max-holding-days", type=int, default=DEFAULT_MAX_HOLDING_DAYS, help="Dias maximos por operacion.")
    parser.add_argument("--filter-window-months", type=int, default=6, help="Meses de cada ventana de filtrado rolling.")
    parser.add_argument("--no-rolling-filter", action="store_true", help="Desactiva el filtrado rolling de activos.")
    parser.add_argument("--end-date", default="", help="Fecha final manual del backtest, formato YYYY-MM-DD.")
    parser.add_argument("--no-auto-cutoff", action="store_true", help="No corta el backtest antes de la primera operacion actual.")
    parser.add_argument("--asset-limit", type=int, default=0, help="Numero de activos que pasan el filtro en cada ventana. 0 = sin limite.")
    parser.add_argument("--min-money-volume", type=int, default=0, help="Minimo de volumen monetario en millones USD.")
    parser.add_argument("--month-window", type=int, default=1, choices=[1, 2, 3], help="Ventana mensual del filtro.")
    parser.add_argument("--day-volume-window", type=int, default=1, choices=[1, 2, 3, 4, 5], help="Ventana diaria del filtro.")
    parser.add_argument("--week-volume-window", type=int, default=1, choices=[1, 2, 3, 4, 5], help="Ventana semanal del filtro.")
    parser.add_argument("--market", default="Todos", help="Mercado del filtro de activos.")
    parser.add_argument("--sector", default="Todos", help="Sector del filtro de activos.")
    parser.add_argument(
        "--sort-by",
        default="money_volume_selected",
        choices=[
            "money_volume_selected",
            "day_money_volume_selected",
            "week_money_volume_selected",
            "day_to_month_volume_ratio",
            "price",
        ],
        help="Orden del filtro de activos.",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_FILE), help="Ruta del JSON de salida.")
    parser.add_argument(
        "--strategies",
        default="",
        help="Claves V2 separadas por coma para ejecutar solo esas estrategias. Vacio = config.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manual_end_date = parse_date_value(args.end_date) if args.end_date else None
    result = run_last_5_years_backtest(
        output_path=args.output,
        years=args.years,
        max_tickers=args.max_tickers,
        trade_usd=args.trade_usd,
        max_holding_days=args.max_holding_days,
        rolling_filter=not args.no_rolling_filter,
        filter_window_months=args.filter_window_months,
        end_date=manual_end_date,
        auto_cutoff_from_live_operations=not args.no_auto_cutoff,
        strategy_keys=normalize_strategy_keys([args.strategies]) if args.strategies else None,
        asset_filters={
            "month_window": args.month_window,
            "min_money_volume": args.min_money_volume,
            "day_volume_window": args.day_volume_window,
            "week_volume_window": args.week_volume_window,
            "limit": args.asset_limit,
            "sector": args.sector,
            "market": args.market,
            "data_source": "csv",
            "sort_by": args.sort_by,
        },
    )
    print(json.dumps({key: result[key] for key in ["mode", "years", "backtest_cutoff_date", "live_operations_from_date", "cutoff_reason", "sessions", "totals", "output_path"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
