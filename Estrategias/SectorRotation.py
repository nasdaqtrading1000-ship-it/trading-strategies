"""
Estrategia Sector Rotation.

Objetivo:
- Detectar qué sectores del mercado están liderando.
- Comparar ETFs sectoriales contra un benchmark como SPY o QQQ.
- Seleccionar sectores fuertes.
- Opcionalmente, buscar acciones fuertes dentro de esos sectores.

Este script NO compra ni vende.
Solo analiza y muestra sectores/candidatos.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_lines_to_txt
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# Benchmark general.
# SPY = S&P 500.
# QQQ = Nasdaq 100.
BENCHMARK = "SPY"

# ETFs sectoriales.
SECTOR_ETFS = {
    "Tecnologia": "XLK",
    "Financiero": "XLF",
    "Energia": "XLE",
    "Salud": "XLV",
    "Consumo discrecional": "XLY",
    "Consumo defensivo": "XLP",
    "Industrial": "XLI",
    "Materiales": "XLB",
    "Utilities": "XLU",
    "Real estate": "XLRE",
    "Comunicacion": "XLC",
}

# Archivo opcional con acciones y sector.
# Formato:
# AAPL,Tecnologia
# MSFT,Tecnologia
# JPM,Financiero
STOCKS_FILE = "sector_stocks.txt"

# Días hacia atrás.
LOOKBACK_DAYS = 180

# Ventanas de fuerza relativa.
SHORT_WINDOW = 20
MEDIUM_WINDOW = 60
LONG_WINDOW = 120

# Medias para confirmar tendencia.
SMA_FAST = 20
SMA_SLOW = 50

# Número de sectores líderes.
TOP_SECTORS = 3

# Número de acciones por sector.
TOP_STOCKS_PER_SECTOR = 5

# Volumen monetario mínimo para acciones.
MIN_AVG_DOLLAR_VOLUME = 20_000_000

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def get_symbols_to_download():
    """
    Construye la lista de símbolos necesarios:
    - benchmark
    - ETFs sectoriales
    - acciones opcionales del archivo
    """
    symbols = {BENCHMARK}
    symbols.update(SECTOR_ETFS.values())

    for symbol, _sector in load_sector_stocks(STOCKS_FILE):
        symbols.add(symbol)

    return sorted(symbols)


def load_sector_stocks(path):
    """
    Lee acciones con su sector desde archivo.

    Ejemplo:
    AAPL,Tecnologia
    MSFT,Tecnologia
    JPM,Financiero

    Si el archivo no existe, devuelve lista vacía.
    """
    if not os.path.exists(path):
        return []

    stocks = []

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            symbol, sector = line.split(",", 1)
            stocks.append((symbol.strip().upper(), sector.strip()))

    return stocks


def get_daily_bars(client, symbols):
    """
    Descarga velas diarias desde Alpaca.
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
    Rentabilidad porcentual en una ventana.
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
    Volumen monetario medio.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def trend_ok(df):
    """
    Confirma tendencia alcista simple:
    - precio > SMA 50
    - SMA 20 > SMA 50
    """
    if len(df) < SMA_SLOW + 5:
        return False

    data = df.copy()
    data["sma_fast"] = data["close"].rolling(SMA_FAST).mean()
    data["sma_slow"] = data["close"].rolling(SMA_SLOW).mean()
    data = data.dropna()

    if data.empty:
        return False

    latest = data.iloc[-1]

    return (
        float(latest["close"]) > float(latest["sma_slow"])
        and float(latest["sma_fast"]) > float(latest["sma_slow"])
    )


def analyze_sector(sector_name, etf_symbol, sector_df, benchmark_df):
    """
    Analiza un ETF sectorial frente al benchmark.

    Mide:
    - rentabilidad 20d, 60d, 120d
    - fuerza relativa frente al benchmark
    - tendencia técnica
    """
    required = max(SHORT_WINDOW, MEDIUM_WINDOW, LONG_WINDOW, SMA_SLOW) + 5

    if len(sector_df) < required or len(benchmark_df) < required:
        return None

    sector_short = pct_change(sector_df["close"], SHORT_WINDOW)
    sector_medium = pct_change(sector_df["close"], MEDIUM_WINDOW)
    sector_long = pct_change(sector_df["close"], LONG_WINDOW)

    bench_short = pct_change(benchmark_df["close"], SHORT_WINDOW)
    bench_medium = pct_change(benchmark_df["close"], MEDIUM_WINDOW)
    bench_long = pct_change(benchmark_df["close"], LONG_WINDOW)

    values = [
        sector_short,
        sector_medium,
        sector_long,
        bench_short,
        bench_medium,
        bench_long,
    ]

    if any(value is None for value in values):
        return None

    rs_short = sector_short - bench_short
    rs_medium = sector_medium - bench_medium
    rs_long = sector_long - bench_long

    sector_trend_ok = trend_ok(sector_df)

    if not sector_trend_ok:
        return None

    # Score:
    # prioriza fuerza reciente, pero sin ignorar medio/largo plazo.
    score = (
        rs_short * 0.45
        + rs_medium * 0.35
        + rs_long * 0.20
    )

    return {
        "sector": sector_name,
        "etf": etf_symbol,
        "return_20d": sector_short,
        "return_60d": sector_medium,
        "return_120d": sector_long,
        "rs_20d": rs_short,
        "rs_60d": rs_medium,
        "rs_120d": rs_long,
        "score": score,
    }


def find_leading_sectors(data):
    """
    Busca los sectores líderes.
    """
    benchmark_df = data.get(BENCHMARK)

    if benchmark_df is None:
        raise RuntimeError(f"No hay datos para benchmark {BENCHMARK}")

    sectors = []

    for sector_name, etf_symbol in SECTOR_ETFS.items():
        sector_df = data.get(etf_symbol)

        if sector_df is None:
            continue

        result = analyze_sector(
            sector_name,
            etf_symbol,
            sector_df,
            benchmark_df,
        )

        if result:
            sectors.append(result)

    sectors = sorted(
        sectors,
        key=lambda item: item["score"],
        reverse=True,
    )

    return sectors[:TOP_SECTORS]


def analyze_stock(symbol, sector, df, sector_etf_df):
    """
    Analiza una acción dentro de un sector líder.

    Busca acciones que:
    - estén en tendencia alcista
    - superen al ETF de su sector
    - tengan liquidez suficiente
    """
    required = max(SHORT_WINDOW, SMA_SLOW) + 5

    if len(df) < required or len(sector_etf_df) < required:
        return None

    stock_return = pct_change(df["close"], SHORT_WINDOW)
    sector_return = pct_change(sector_etf_df["close"], SHORT_WINDOW)

    if stock_return is None or sector_return is None:
        return None

    relative_strength_vs_sector = stock_return - sector_return
    avg_dollar_volume = average_dollar_volume(df, 20)

    if not trend_ok(df):
        return None

    if relative_strength_vs_sector <= 0:
        return None

    if avg_dollar_volume < MIN_AVG_DOLLAR_VOLUME:
        return None

    price = float(df["close"].iloc[-1])

    score = (
        relative_strength_vs_sector * 0.6
        + stock_return * 0.4
    )

    return {
        "symbol": symbol,
        "sector": sector,
        "price": price,
        "return_20d": stock_return,
        "rs_vs_sector": relative_strength_vs_sector,
        "avg_dollar_volume": avg_dollar_volume,
        "score": score,
    }


def find_stocks_in_leading_sectors(data, leading_sectors):
    """
    Busca acciones fuertes dentro de los sectores líderes.
    """
    sector_by_name = {
        item["sector"]: item
        for item in leading_sectors
    }

    stocks = load_sector_stocks(STOCKS_FILE)
    results = []

    for symbol, sector in stocks:
        if sector not in sector_by_name:
            continue

        df = data.get(symbol)
        etf_symbol = sector_by_name[sector]["etf"]
        sector_etf_df = data.get(etf_symbol)

        if df is None or sector_etf_df is None:
            continue

        result = analyze_stock(symbol, sector, df, sector_etf_df)

        if result:
            results.append(result)

    results = sorted(
        results,
        key=lambda item: item["score"],
        reverse=True,
    )

    output = {}

    for sector in sector_by_name:
        sector_stocks = [
            item for item in results
            if item["sector"] == sector
        ]
        output[sector] = sector_stocks[:TOP_STOCKS_PER_SECTOR]

    return output


def run_sector_rotation():
    """
    Función principal.

    1. Descarga benchmark, ETFs sectoriales y acciones.
    2. Busca sectores líderes.
    3. Busca acciones fuertes dentro de esos sectores.
    """
    symbols = get_symbols_to_download()

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_daily_bars(client, symbols)

    leading_sectors = find_leading_sectors(data)
    leading_stocks = find_stocks_in_leading_sectors(data, leading_sectors)

    return leading_sectors, leading_stocks


def format_sector(sector):
    """
    Formatea un sector líder.
    """
    return (
        f"{sector['sector']} ({sector['etf']}) | "
        f"RS 20d: {sector['rs_20d']:.2f}% | "
        f"RS 60d: {sector['rs_60d']:.2f}% | "
        f"RS 120d: {sector['rs_120d']:.2f}% | "
        f"Score: {sector['score']:.2f}"
    )


def format_stock(stock):
    """
    Formatea una acción candidata.
    """
    return (
        f"{stock['symbol']} | "
        f"Precio: {stock['price']:.2f} | "
        f"Ret 20d: {stock['return_20d']:.2f}% | "
        f"RS vs sector: {stock['rs_vs_sector']:.2f}% | "
        f"Vol$: {stock['avg_dollar_volume'] / 1_000_000:.1f}M | "
        f"Score: {stock['score']:.2f}"
    )


if __name__ == "__main__":
    sectors, stocks_by_sector = run_sector_rotation()
    output_lines = []

    if not sectors:
        print("No hay sectores líderes con los filtros actuales.")
    else:
        print("Sectores líderes:")
        for sector in sectors:
            sector_line = format_sector(sector)
            output_lines.append(sector_line)
            print(sector_line)

            stocks = stocks_by_sector.get(sector["sector"], [])

            if stocks:
                print("  Acciones fuertes:")
                for stock in stocks:
                    stock_line = "  - " + format_stock(stock)
                    output_lines.append(stock_line)
                    print(stock_line)

    output_path, output_count = write_lines_to_txt("SectorRotation", output_lines)
    print(f"TXT actualizado: {output_path} ({output_count})")
