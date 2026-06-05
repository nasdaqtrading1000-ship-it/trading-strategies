"""
Estrategia Value Trading / Value Investing cuantitativo.

Objetivo:
- Buscar empresas aparentemente infravaloradas.
- Combinar valoración barata con calidad financiera mínima.
- Evitar empresas con deuda excesiva o rentabilidad negativa.
- Devolver candidatos ordenados por score.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.

IMPORTANTE:
- Alpaca no suele dar fundamentales completos.
- Para esta estrategia se necesita una API de fundamentales.
- Este ejemplo usa endpoints compatibles con Financial Modeling Prep.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from fmp_client import fmp_get_json
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# Archivo de tickers.
TICKERS_FILE = "tickers.txt"

# Días de precio para calcular tendencia/liquidez.
LOOKBACK_DAYS = 120

# Medias para evitar empresas baratas pero en caída libre.
SMA_FAST = 20
SMA_SLOW = 50

# Volumen monetario mínimo.
MIN_AVG_DOLLAR_VOLUME = 10_000_000

# Filtros value.
MAX_PE_RATIO = 18
MAX_PRICE_TO_BOOK = 3
MAX_PRICE_TO_SALES = 4
MIN_ROE = 8
MAX_DEBT_TO_EQUITY = 150
MIN_REVENUE_GROWTH = -5

# Número máximo de resultados.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# API de fundamentales.
# Por ejemplo Financial Modeling Prep.
FMP_API_KEY = os.environ["FMP_API_KEY"]


def load_tickers(path):
    """
    Lee tickers desde un archivo de texto.
    """
    with open(path, "r", encoding="utf-8") as file:
        return sorted(
            {
                line.strip().upper()
                for line in file
                if line.strip() and not line.strip().startswith("#")
            }
        )


def get_daily_bars(client, symbols):
    """
    Descarga velas diarias desde Alpaca para calcular:
    - precio actual
    - medias móviles
    - volumen monetario medio
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


def average_dollar_volume(df, window=20):
    """
    Calcula volumen monetario medio.

    Volumen monetario = cierre x volumen.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def get_fundamentals(symbol):
    """
    Obtiene fundamentales de una empresa.

    Este ejemplo usa Financial Modeling Prep.

    Endpoint profile:
    - PER
    - Price to book
    - Price to sales
    - ROE
    - Debt to equity
    - Revenue growth

    Ojo:
    los nombres exactos pueden variar según proveedor/API.
    """
    url = "https://financialmodelingprep.com/stable/profile"

    data = fmp_get_json(
        url,
        FMP_API_KEY,
        params={"symbol": symbol},
        timeout=15,
    )

    if not data:
        return None

    item = data[0]

    return {
        "pe_ratio": safe_float(item.get("pe")),
        "price_to_book": safe_float(item.get("priceToBookRatio")),
        "price_to_sales": safe_float(item.get("priceToSalesRatio")),
        "roe": safe_float(item.get("returnOnEquity")) * 100,
        "debt_to_equity": safe_float(item.get("debtToEquity")),
        "revenue_growth": safe_float(item.get("revenueGrowth")) * 100,
        "company_name": item.get("companyName", symbol),
        "sector": item.get("sector", "Sin sector"),
        "industry": item.get("industry", "Sin industria"),
    }


def safe_float(value, default=None):
    """
    Convierte valores a float evitando errores.
    """
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def analyze_symbol(symbol, df, fundamentals):
    """
    Analiza si una empresa cumple criterios value.

    Condiciones:
    - Valoración razonable.
    - ROE positivo.
    - Deuda controlada.
    - Crecimiento no demasiado negativo.
    - Liquidez suficiente.
    - Precio no completamente destruido.
    """
    if fundamentals is None:
        return None

    if len(df) < SMA_SLOW + 5:
        return None

    df = df.copy()

    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()

    df = df.dropna()

    if df.empty:
        return None

    latest = df.iloc[-1]

    price = float(latest["close"])
    sma_fast = float(latest["sma_fast"])
    sma_slow = float(latest["sma_slow"])
    avg_dollar_volume = average_dollar_volume(df, 20)

    pe = fundamentals["pe_ratio"]
    pb = fundamentals["price_to_book"]
    ps = fundamentals["price_to_sales"]
    roe = fundamentals["roe"]
    debt_to_equity = fundamentals["debt_to_equity"]
    revenue_growth = fundamentals["revenue_growth"]

    # Si faltan datos clave, descartamos.
    required = [pe, pb, ps, roe, debt_to_equity, revenue_growth]

    if any(value is None for value in required):
        return None

    value_ok = (
        pe > 0
        and pe <= MAX_PE_RATIO
        and pb > 0
        and pb <= MAX_PRICE_TO_BOOK
        and ps > 0
        and ps <= MAX_PRICE_TO_SALES
    )

    quality_ok = (
        roe >= MIN_ROE
        and debt_to_equity <= MAX_DEBT_TO_EQUITY
        and revenue_growth >= MIN_REVENUE_GROWTH
    )

    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    # Evitamos comprar algo que parece barato
    # pero está cayendo sin estabilizar.
    technical_ok = (
        price > sma_slow * 0.90
        or sma_fast > sma_slow
    )

    if not all([value_ok, quality_ok, liquidity_ok, technical_ok]):
        return None

    # Score value:
    # mejor cuanto más barato y más calidad.
    valuation_score = (
        (MAX_PE_RATIO - pe) / MAX_PE_RATIO * 35
        + (MAX_PRICE_TO_BOOK - pb) / MAX_PRICE_TO_BOOK * 20
        + (MAX_PRICE_TO_SALES - ps) / MAX_PRICE_TO_SALES * 15
    )

    quality_score = (
        min(roe, 30) / 30 * 20
        + max(0, 100 - debt_to_equity) / 100 * 10
    )

    score = valuation_score + quality_score

    return {
        "symbol": symbol,
        "company_name": fundamentals["company_name"],
        "sector": fundamentals["sector"],
        "industry": fundamentals["industry"],
        "price": price,
        "pe_ratio": pe,
        "price_to_book": pb,
        "price_to_sales": ps,
        "roe": roe,
        "debt_to_equity": debt_to_equity,
        "revenue_growth": revenue_growth,
        "avg_dollar_volume": avg_dollar_volume,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "score": score,
    }


def find_value_candidates():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga precio/volumen desde Alpaca.
    3. Descarga fundamentales.
    4. Filtra por value/calidad/liquidez.
    5. Ordena por score.
    """
    symbols = load_tickers(TICKERS_FILE)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    price_data = get_daily_bars(client, symbols)

    candidates = []

    for symbol in symbols:
        df = price_data.get(symbol)

        if df is None or df.empty:
            continue

        try:
            fundamentals = get_fundamentals(symbol)
        except Exception as error:
            print(f"No se pudieron obtener fundamentales de {symbol}: {error}")
            continue

        result = analyze_symbol(symbol, df, fundamentals)

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
    Formatea un candidato para imprimirlo o enviarlo por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"{candidate['company_name']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"PER: {candidate['pe_ratio']:.1f} | "
        f"P/B: {candidate['price_to_book']:.1f} | "
        f"P/S: {candidate['price_to_sales']:.1f} | "
        f"ROE: {candidate['roe']:.1f}% | "
        f"Deuda/Equity: {candidate['debt_to_equity']:.1f} | "
        f"Crec. ingresos: {candidate['revenue_growth']:.1f}% | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_value_candidates()
    output_path, output_count = write_results_to_txt("ValueTrading", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos value con los filtros actuales.")
    else:
        print("Candidatos Value:")
        for candidate in results:
            print(format_candidate(candidate))
