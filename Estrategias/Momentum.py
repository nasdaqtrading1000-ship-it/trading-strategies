"""
Estrategia Momentum con fuerza relativa alta y tendencia alcista.

Objetivo:
- Buscar acciones que se comportan mejor que el mercado.
- Confirmar que tienen tendencia alcista.
- Filtrar por volumen monetario.
- Devolver los mejores candidatos.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# Archivo de texto desde donde se leen los tickers.
# Un ticker por línea.
TICKERS_FILE = "tickers.txt"

# Benchmark contra el que comparamos la fuerza relativa.
# QQQ suele ir bien para acciones Nasdaq/tecnológicas.
# SPY
BENCHMARK = "QQQ"

# Días hacia atrás que pedimos a Alpaca.
# Debe ser mayor que las medias y ventanas que vamos a calcular.
LOOKBACK_DAYS = 90

# Ventana de momentum.
# Mide cuánto ha subido/bajado el activo en los últimos 20 días.
MOMENTUM_WINDOW = 20

# Medias móviles para confirmar tendencia.
# Las medias moviles 20 y 50 diario
SMA_FAST = 20
SMA_SLOW = 50

# Volumen monetario mínimo:
# precio x volumen medio.
# 20M significa 20 millones de dólares diarios aproximados.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Número máximo de resultados que queremos mostrar.
TOP_N = 20

# Claves de Alpaca.
# Deben estar definidas como variables de entorno.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde un archivo de texto.

    Ignora:
    - líneas vacías
    - líneas que empiezan por #

    Añade el benchmark si no está en la lista,
    porque lo necesitamos para comparar fuerza relativa.
    """
    with open(path, "r", encoding="utf-8") as file:
        tickers = [
            line.strip().upper()
            for line in file
            if line.strip() and not line.strip().startswith("#")
        ]

    if BENCHMARK not in tickers:
        tickers.append(BENCHMARK)

    return sorted(set(tickers))


def get_daily_bars(client, symbols):
    """
    Descarga velas diarias desde Alpaca para todos los símbolos.

    Devuelve un diccionario:
    {
        "AAPL": DataFrame,
        "NVDA": DataFrame,
        ...
    }
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
        end=datetime.now(UTC),
        feed=DataFeed.IEX,
    )

    bars = client.get_stock_bars(request).data
    data = {}

    for symbol, symbol_bars in bars.items():
        rows = []

        for bar in symbol_bars:
            rows.append(
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )

        if rows:
            df = pd.DataFrame(rows).sort_values("timestamp")
            df.set_index("timestamp", inplace=True)
            data[symbol] = df

    return data


def pct_change(series, window):
    """
    Calcula la rentabilidad porcentual en una ventana.

    Ejemplo:
    Si hace 20 días valía 100 y hoy vale 115:
    devuelve 15.0
    """
    if len(series) <= window:
        return None

    start_price = float(series.iloc[-window - 1])
    end_price = float(series.iloc[-1])

    if start_price <= 0:
        return None

    return ((end_price / start_price) - 1) * 100


def average_dollar_volume(df, window=20):
    """
    Calcula volumen monetario medio.

    Volumen monetario = precio de cierre x volumen.

    Esto es más útil que mirar solo volumen de acciones,
    porque no es lo mismo mover 1 millón de acciones a 2 USD
    que 1 millón de acciones a 200 USD.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def analyze_symbol(symbol, df, benchmark_return):
    """
    Analiza un único activo.

    Condiciones para aceptar candidato:
    - Tiene suficientes datos.
    - Precio por encima de SMA 50.
    - SMA 20 por encima de SMA 50.
    - Momentum positivo.
    - Mejor comportamiento que QQQ/SPY.
    - Volumen monetario suficiente.
    """
    if len(df) < SMA_SLOW + 2:
        return None

    df = df.copy()

    # Calculamos medias móviles.
    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()

    latest = df.iloc[-1]

    price = float(latest["close"])
    sma_fast = float(latest["sma_fast"])
    sma_slow = float(latest["sma_slow"])

    # Rentabilidad del activo en la ventana momentum.
    momentum_return = pct_change(df["close"], MOMENTUM_WINDOW)

    if momentum_return is None or benchmark_return is None:
        return None

    # Fuerza relativa:
    # rentabilidad del activo menos rentabilidad del benchmark.
    relative_strength = momentum_return - benchmark_return

    # Volumen monetario medio.
    avg_dollar_volume = average_dollar_volume(df, 20)

    # Tendencia alcista.
    trend_ok = (
        price > sma_slow
        and sma_fast > sma_slow
    )

    # Filtros principales.
    volume_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME
    momentum_ok = momentum_return > 0
    relative_strength_ok = relative_strength > 0

    if not all([trend_ok, volume_ok, momentum_ok, relative_strength_ok]):
        return None

    # Score para ordenar candidatos.
    # Damos más peso a fuerza relativa,
    # luego momentum y luego distancia positiva sobre SMA lenta.
    score = (
        relative_strength * 0.5
        + momentum_return * 0.3
        + ((price / sma_slow - 1) * 100) * 0.2
    )

    return {
        "symbol": symbol,
        "price": price,
        "momentum_return_pct": momentum_return,
        "benchmark_return_pct": benchmark_return,
        "relative_strength_pct": relative_strength,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "avg_dollar_volume": avg_dollar_volume,
        "score": score,
    }


def find_momentum_candidates():
    """
    Función principal de búsqueda.

    1. Lee tickers.
    2. Descarga datos.
    3. Calcula rentabilidad del benchmark.
    4. Analiza cada activo.
    5. Ordena por score.
    6. Devuelve el top.
    """
    symbols = load_tickers(TICKERS_FILE)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_daily_bars(client, symbols)

    if BENCHMARK not in data:
        raise RuntimeError(f"No hay datos para benchmark {BENCHMARK}")

    benchmark_return = pct_change(data[BENCHMARK]["close"], MOMENTUM_WINDOW)

    candidates = []

    for symbol in symbols:
        if symbol == BENCHMARK:
            continue

        df = data.get(symbol)

        if df is None or df.empty:
            continue

        result = analyze_symbol(symbol, df, benchmark_return)

        if result:
            candidates.append(result)

    candidates = sorted(
        candidates,
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates[:TOP_N]


def format_candidate(candidate):
    """
    Formatea un candidato para imprimirlo o mandarlo por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Momentum: {candidate['momentum_return_pct']:.2f}% | "
        f"RS: {candidate['relative_strength_pct']:.2f}% | "
        f"Vol$: {candidate['avg_dollar_volume'] / 1_000_000:.1f}M | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_momentum_candidates()
    output_path, output_count = write_results_to_txt("Momentum", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos momentum con los filtros actuales.")
    else:
        print("Candidatos Momentum:")
        for candidate in results:
            print(format_candidate(candidate))
