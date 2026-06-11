"""
Estrategia Quality Investing.

Objetivo:
- Buscar empresas de alta calidad financiera.
- Priorizar buenos márgenes, ROE/ROIC altos, crecimiento estable y deuda controlada.
- Evitar empresas con deterioro fuerte.
- Ordenar candidatos por calidad.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.

IMPORTANTE:
- Alpaca sirve para precio/volumen.
- Para calidad financiera necesitas una API de fundamentales.
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

# Histórico de precio.
LOOKBACK_DAYS = 180

# Medias técnicas para evitar activos completamente deteriorados.
SMA_FAST = 50
SMA_SLOW = 100

# Liquidez mínima.
MIN_AVG_DOLLAR_VOLUME = 10_000_000

# Filtros de calidad.
MIN_ROE = 12.0
MIN_ROIC = 8.0
MIN_OPERATING_MARGIN = 12.0
MIN_NET_MARGIN = 8.0
MAX_DEBT_TO_EQUITY = 150.0
MIN_REVENUE_GROWTH_3Y = 0.0
MIN_EPS_GROWTH_3Y = 0.0

# Valuation guardrail:
# Quality puede pagar múltiplos más altos,
# pero evitamos extremos.
MAX_PE_RATIO = 45.0
MAX_PRICE_TO_SALES = 15.0

# Máximo de candidatos.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# API fundamentales.
FMP_API_KEY = os.environ["FMP_API_KEY"]


def load_tickers(path):
    """
    Lee tickers desde archivo de texto.
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
    Descarga velas diarias desde Alpaca.

    Se usa para:
    - precio actual
    - tendencia básica
    - liquidez
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


def safe_float(value, default=None):
    """
    Convierte a float evitando errores.
    """
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def average_dollar_volume(df, window=20):
    """
    Volumen monetario medio = cierre x volumen.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def get_profile(symbol):
    """
    Perfil de empresa:
    - nombre
    - sector
    - ratios de valoración si están disponibles
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
        "company_name": item.get("companyName", symbol),
        "sector": item.get("sector", "Sin sector"),
        "industry": item.get("industry", "Sin industria"),
        "pe_ratio": safe_float(item.get("pe")),
        "price_to_sales": safe_float(item.get("priceToSalesRatio")),
        "beta": safe_float(item.get("beta")),
    }


def get_ratios(symbol):
    """
    Ratios TTM de calidad:
    - ROE
    - ROIC
    - margen operativo
    - margen neto
    - deuda/equity
    """
    url = "https://financialmodelingprep.com/stable/ratios-ttm"

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
        "roe": safe_float(item.get("returnOnEquityTTM")) * 100
        if safe_float(item.get("returnOnEquityTTM")) is not None else None,
        "roic": safe_float(item.get("returnOnCapitalEmployedTTM")) * 100
        if safe_float(item.get("returnOnCapitalEmployedTTM")) is not None else None,
        "operating_margin": safe_float(item.get("operatingProfitMarginTTM")) * 100
        if safe_float(item.get("operatingProfitMarginTTM")) is not None else None,
        "net_margin": safe_float(item.get("netProfitMarginTTM")) * 100
        if safe_float(item.get("netProfitMarginTTM")) is not None else None,
        "debt_to_equity": safe_float(item.get("debtEquityRatioTTM")),
    }


def get_growth(symbol):
    """
    Calcula crecimiento anualizado de ingresos y EPS en 3 años.

    Usa income statement anual.
    """
    url = "https://financialmodelingprep.com/stable/income-statement"

    data = fmp_get_json(
        url,
        FMP_API_KEY,
        params={
            "symbol": symbol,
            "period": "annual",
            "limit": 4,
        },
        timeout=15,
    )

    if len(data) < 4:
        return None

    latest = data[0]
    old = data[3]

    latest_revenue = safe_float(latest.get("revenue"))
    old_revenue = safe_float(old.get("revenue"))

    latest_eps = safe_float(latest.get("eps"))
    old_eps = safe_float(old.get("eps"))

    revenue_growth = annualized_growth(latest_revenue, old_revenue, 3)
    eps_growth = annualized_growth(latest_eps, old_eps, 3)

    return {
        "revenue_growth_3y": revenue_growth,
        "eps_growth_3y": eps_growth,
    }


def annualized_growth(latest, old, years):
    """
    Calcula crecimiento anualizado.
    """
    if latest is None or old is None:
        return None

    if latest <= 0 or old <= 0:
        return None

    return ((latest / old) ** (1 / years) - 1) * 100


def get_fundamentals(symbol):
    """
    Une perfil, ratios y crecimiento.
    """
    profile = get_profile(symbol)
    ratios = get_ratios(symbol)
    growth = get_growth(symbol)

    if not profile or not ratios or not growth:
        return None

    return {
        **profile,
        **ratios,
        **growth,
    }


def analyze_symbol(symbol, df, fundamentals):
    """
    Analiza si una empresa cumple criterios Quality Investing.

    Condiciones:
    - ROE y ROIC altos.
    - Márgenes altos.
    - Deuda controlada.
    - Crecimiento positivo.
    - Valoración no absurda.
    - Liquidez suficiente.
    - Técnica no deteriorada.
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

    roe = fundamentals["roe"]
    roic = fundamentals["roic"]
    operating_margin = fundamentals["operating_margin"]
    net_margin = fundamentals["net_margin"]
    debt_to_equity = fundamentals["debt_to_equity"]
    revenue_growth = fundamentals["revenue_growth_3y"]
    eps_growth = fundamentals["eps_growth_3y"]
    pe_ratio = fundamentals["pe_ratio"]
    price_to_sales = fundamentals["price_to_sales"]

    required = [
        roe,
        roic,
        operating_margin,
        net_margin,
        debt_to_equity,
        revenue_growth,
        eps_growth,
        pe_ratio,
        price_to_sales,
    ]

    if any(value is None for value in required):
        return None

    quality_ok = (
        roe >= MIN_ROE
        and roic >= MIN_ROIC
        and operating_margin >= MIN_OPERATING_MARGIN
        and net_margin >= MIN_NET_MARGIN
    )

    balance_ok = debt_to_equity <= MAX_DEBT_TO_EQUITY

    growth_ok = (
        revenue_growth >= MIN_REVENUE_GROWTH_3Y
        and eps_growth >= MIN_EPS_GROWTH_3Y
    )

    valuation_ok = (
        pe_ratio > 0
        and pe_ratio <= MAX_PE_RATIO
        and price_to_sales > 0
        and price_to_sales <= MAX_PRICE_TO_SALES
    )

    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    technical_ok = (
        price > sma_slow * 0.90
        or sma_fast > sma_slow
    )

    if not all([
        quality_ok,
        balance_ok,
        growth_ok,
        valuation_ok,
        liquidity_ok,
        technical_ok,
    ]):
        return None

    # Score:
    # más peso a rentabilidad y márgenes,
    # luego crecimiento, deuda y valoración.
    profitability_score = (
        min(roe / 30, 1) * 20
        + min(roic / 25, 1) * 20
    )

    margin_score = (
        min(operating_margin / 35, 1) * 15
        + min(net_margin / 25, 1) * 15
    )

    growth_score = (
        min(revenue_growth / 20, 1) * 10
        + min(eps_growth / 20, 1) * 10
    )

    debt_score = max(0, (MAX_DEBT_TO_EQUITY - debt_to_equity) / MAX_DEBT_TO_EQUITY) * 5

    valuation_score = (
        max(0, (MAX_PE_RATIO - pe_ratio) / MAX_PE_RATIO) * 3
        + max(0, (MAX_PRICE_TO_SALES - price_to_sales) / MAX_PRICE_TO_SALES) * 2
    )

    score = (
        profitability_score
        + margin_score
        + growth_score
        + debt_score
        + valuation_score
    )
    stop_loss = min(price * 0.90, sma_slow * 0.96)
    take_profit_1 = price * 1.18
    take_profit_2 = price * 1.32

    return {
        "symbol": symbol,
        "company_name": fundamentals["company_name"],
        "sector": fundamentals["sector"],
        "industry": fundamentals["industry"],
        "price": price,
        "roe": roe,
        "roic": roic,
        "operating_margin": operating_margin,
        "net_margin": net_margin,
        "debt_to_equity": debt_to_equity,
        "revenue_growth_3y": revenue_growth,
        "eps_growth_3y": eps_growth,
        "pe_ratio": pe_ratio,
        "price_to_sales": price_to_sales,
        "avg_dollar_volume": avg_dollar_volume,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_quality_candidates():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga precio/volumen.
    3. Descarga fundamentales.
    4. Filtra calidad.
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
    Formatea candidato para imprimir o mandar por Telegram.
    """
    return (
        f"{candidate['symbol']} | "
        f"{candidate['company_name']} | "
        f"Precio: {candidate['price']:.2f} | "
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1 Calidad: {candidate['take_profit_1']:.2f} | "
        f"TP2 Calidad: {candidate['take_profit_2']:.2f} | "
        f"ROE: {candidate['roe']:.1f}% | "
        f"ROIC: {candidate['roic']:.1f}% | "
        f"Margen Op: {candidate['operating_margin']:.1f}% | "
        f"Margen Neto: {candidate['net_margin']:.1f}% | "
        f"Crec EPS 3Y: {candidate['eps_growth_3y']:.1f}% | "
        f"PER: {candidate['pe_ratio']:.1f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_quality_candidates()
    output_path, output_count = write_results_to_txt("QualityInvesting", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Quality Investing con los filtros actuales.")
    else:
        print("Candidatos Quality Investing:")
        for candidate in results:
            print(format_candidate(candidate))
