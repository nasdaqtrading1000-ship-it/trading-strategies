import os
import time

from alpaca.data.requests import StockBarsRequest


DEFAULT_BATCH_SIZE = 100
DEFAULT_RETRIES = 2


def get_stock_bars_data(client, request):
    """
    Pide barras a Alpaca en tandas para evitar URLs enormes, timeouts y cortes.

    Devuelve el mismo tipo de dato que client.get_stock_bars(request).data:
    un diccionario symbol -> barras.
    """
    symbols = getattr(request, "symbol_or_symbols", None)
    if not isinstance(symbols, list):
        data = client.get_stock_bars(request).data
        log_alpaca_data("Alpaca peticion individual", [symbols], data)
        return data

    batch_size = int(os.environ.get("ALPACA_BARS_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    retries = int(os.environ.get("ALPACA_BARS_RETRIES", DEFAULT_RETRIES))
    merged = {}
    batches = list(chunks(symbols, batch_size))

    if verbose_analysis_enabled():
        print(
            f"Alpaca: solicitando {len(symbols)} activos "
            f"en {len(batches)} tandas de hasta {batch_size}."
        )

    for batch_number, batch in enumerate(batches, start=1):
        if verbose_analysis_enabled():
            preview = ", ".join(batch[:8])
            print(f"Alpaca tanda {batch_number}/{len(batches)}: {len(batch)} activos ({preview}...)")
        batch_request = clone_request_for_symbols(request, batch)
        data = get_batch_with_retries(client, batch_request, batch, retries)
        log_alpaca_data(f"Alpaca tanda {batch_number}/{len(batches)}", batch, data)
        merged.update(data)

    if verbose_analysis_enabled():
        print(f"Alpaca: datos recibidos para {len(merged)} de {len(symbols)} activos solicitados.")

    return merged


def clone_request_for_symbols(request, symbols):
    params = {"symbol_or_symbols": symbols}
    for key in (
        "timeframe",
        "start",
        "end",
        "limit",
        "currency",
        "sort",
        "adjustment",
        "feed",
        "asof",
    ):
        value = getattr(request, key, None)
        if value is not None:
            params[key] = value
    return StockBarsRequest(**params)


def get_batch_with_retries(client, request, batch, retries):
    for attempt in range(retries + 1):
        try:
            return client.get_stock_bars(request).data
        except Exception as error:
            if attempt >= retries:
                preview = ",".join(batch[:5])
                print(
                    f"Alpaca omitio tanda de {len(batch)} simbolos "
                    f"({preview}...): {error}"
                )
                return {}
            time.sleep(2 + attempt * 3)
    return {}


def verbose_analysis_enabled():
    return os.environ.get("TRADING_VERBOSE_ANALYSIS", "1").lower() not in {"0", "false", "no"}


def log_alpaca_data(title, requested_symbols, data):
    if not verbose_analysis_enabled():
        return

    print(f"{title}: recibidos {len(data)} de {len(requested_symbols)} activos.")
    for symbol in requested_symbols:
        symbol_bars = data.get(symbol) if isinstance(data, dict) else None
        if symbol_bars:
            first = symbol_bars[0].timestamp if len(symbol_bars) else ""
            last = symbol_bars[-1].timestamp if len(symbol_bars) else ""
            print(f"ANALISIS ACTIVO | {symbol} | barras={len(symbol_bars)} | desde={first} | hasta={last}")
        else:
            print(f"ANALISIS ACTIVO | {symbol} | SIN DATOS")


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]
