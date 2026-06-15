import os


def analysis_enabled():
    return os.environ.get("TRADING_VERBOSE_ANALYSIS", "1").lower() not in {"0", "false", "no"}


def fmt_number(value, decimals=2):
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "-"


def fmt_money(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}"


def log_symbol_decision(strategy, symbol, status, details=""):
    if not analysis_enabled():
        return
    message = f"ANALISIS ESTRATEGIA | {strategy} | {symbol} | {status}"
    if details:
        message = f"{message} | {details}"
    print(message, flush=True)


def log_strategy_summary(strategy, total, with_data, accepted, returned):
    if not analysis_enabled():
        return
    discarded = max(0, with_data - accepted)
    print(
        f"RESUMEN ANALISIS | {strategy} | "
        f"universo={total} | con_datos={with_data} | "
        f"aceptados={accepted} | descartados={discarded} | "
        f"devueltos={returned}",
        flush=True,
    )

