from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from config_loader import load_config, load_env_files
from download_historical_data import DEFAULT_DATA_DIR, DEFAULT_MANIFEST_FILE
from historical_backtest import (
    DEFAULT_MAX_HOLDING_DAYS,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_TRADE_USD,
    DEFAULT_WARMUP_BARS,
    build_rolling_asset_filter_windows,
    json_default,
    merge_strategy_backtest_result,
    parse_date_value,
    run_historical_backtest,
)
from strategy_rules import STRATEGY_REGISTRY


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_historical_txt_data(
    data_dir: Path,
    manifest: dict[str, Any] | None = None,
    max_tickers: int | None = None,
) -> dict[str, pd.DataFrame]:
    manifest_files = (manifest or {}).get("files", [])
    items: list[tuple[str, Path]] = []
    if isinstance(manifest_files, list):
        for item in manifest_files:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            filename = str(item.get("file") or "").strip()
            if symbol and filename:
                items.append((symbol, data_dir / filename))
    if not items:
        items = [(path.stem.upper(), path) for path in sorted(data_dir.glob("*.txt"))]

    daily_data: dict[str, pd.DataFrame] = {}
    for index, (symbol, path) in enumerate(items, start=1):
        if max_tickers is not None and len(daily_data) >= max_tickers:
            break
        if not path.exists():
            print(f"TXT historico | {symbol} | OMITIDO: no existe {path}", flush=True)
            continue
        try:
            df = pd.read_csv(path)
        except Exception as error:
            print(f"TXT historico | {symbol} | OMITIDO: {error}", flush=True)
            continue
        if df.empty or "timestamp" not in df.columns:
            print(f"TXT historico | {symbol} | OMITIDO: sin columna timestamp", flush=True)
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
        columns = [column for column in ["open", "high", "low", "close", "volume"] if column in df.columns]
        if len(columns) < 5:
            print(f"TXT historico | {symbol} | OMITIDO: columnas OHLCV incompletas", flush=True)
            continue
        daily_data[symbol] = df[columns].apply(pd.to_numeric, errors="coerce").dropna()
        print(f"TXT historico | {index}/{len(items)} | {symbol} | filas={len(daily_data[symbol])}", flush=True)
    return daily_data


def run_txt_backtest(
    data_dir: Path = DEFAULT_DATA_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_FILE,
    output_path: Path = DEFAULT_OUTPUT_FILE,
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
    strategy_keys: list[str] | None = None,
) -> dict[str, Any]:
    load_env_files()
    config = load_config()
    if strategy_keys:
        config["enabled_strategies"] = normalize_strategy_keys(strategy_keys)
    manifest = load_manifest(manifest_path)
    daily_data = load_historical_txt_data(data_dir, manifest, max_tickers=max_tickers)
    if not daily_data:
        raise RuntimeError(f"No hay TXT historicos validos en {data_dir}")
    print(f"TXT historico | carga completa | activos={len(daily_data)}", flush=True)

    cutoff_date = end_date or parse_date_value(manifest.get("backtest_cutoff_date")) or date.today()
    live_operations_from_date = parse_date_value(manifest.get("live_operations_from_date"))
    asset_filter_windows = None
    if rolling_filter:
        print(
            f"Backtest historico | construyendo filtro rolling | years={years} | ventana={filter_window_months} meses",
            flush=True,
        )
        asset_filter_windows = build_rolling_asset_filter_windows(
            daily_data=daily_data,
            config=config,
            years=years,
            window_months=filter_window_months,
            asset_filters=asset_filters,
            as_of_date=cutoff_date,
        )
        print(f"Backtest historico | filtro rolling listo | ventanas={len(asset_filter_windows)}", flush=True)

    print(
        "Backtest historico | ejecutando estrategias"
        + (f" | seleccion={','.join(strategy_keys)}" if strategy_keys else " | seleccion=config.json"),
        flush=True,
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
        as_of_date=cutoff_date,
        live_operations_from_date=live_operations_from_date,
        cutoff_reason=f"txt_{manifest.get('cutoff_reason') or 'manual'}",
    )
    result["source_data"] = {
        "type": "historical_txt",
        "data_dir": str(data_dir),
        "manifest_path": str(manifest_path),
        "txt_files_loaded": len(daily_data),
        "manifest_generated_at": manifest.get("generated_at"),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if strategy_keys:
        print(f"Backtest historico | fusionando resultado incremental en {output_path}", flush=True)
        result = merge_strategy_backtest_result(output_path, result)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
    result["output_path"] = str(output_path)
    return result


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ejecuta el backtest usando TXT historicos ya descargados.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Carpeta con los TXT por activo.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_FILE), help="Manifest de la descarga historica.")
    parser.add_argument("--years", type=int, default=5, help="Anos que se simulan dentro del historico.")
    parser.add_argument("--max-tickers", type=int, default=None, help="Limite opcional de TXT cargados.")
    parser.add_argument("--trade-usd", type=float, default=DEFAULT_TRADE_USD, help="Importe simulado por operacion.")
    parser.add_argument("--max-holding-days", type=int, default=DEFAULT_MAX_HOLDING_DAYS, help="Maximo de dias por operacion.")
    parser.add_argument("--filter-window-months", type=int, default=6, help="Meses por ventana de filtrado rolling.")
    parser.add_argument("--no-rolling-filter", action="store_true", help="Desactiva el filtrado rolling de activos.")
    parser.add_argument("--end-date", default="", help="Fecha final manual del backtest, formato YYYY-MM-DD.")
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
    result = run_txt_backtest(
        data_dir=Path(args.data_dir),
        manifest_path=Path(args.manifest),
        output_path=Path(args.output),
        years=args.years,
        max_tickers=args.max_tickers,
        trade_usd=args.trade_usd,
        max_holding_days=args.max_holding_days,
        rolling_filter=not args.no_rolling_filter,
        filter_window_months=args.filter_window_months,
        end_date=parse_date_value(args.end_date) if args.end_date else None,
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
        strategy_keys=normalize_strategy_keys([args.strategies]) if args.strategies else None,
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "years": result["years"],
                "backtest_cutoff_date": result["backtest_cutoff_date"],
                "sessions": result["sessions"],
                "totals": result["totals"],
                "source_data": result["source_data"],
                "output_path": result["output_path"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
