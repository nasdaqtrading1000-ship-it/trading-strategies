"""
Estrategia Dividend Growth.

Objetivo:
- Buscar empresas con dividendos crecientes y sostenibles.
- Evitar trampas de dividendo.
- Filtrar por calidad, payout, deuda y estabilidad.
- Devolver candidatos ordenados por score.

Este script NO compra ni vende.
Solo analiza y muestra candidatos.

IMPORTANTE:
- Alpaca sirve para precio/volumen.
- Para dividendos y fundamentales necesitas una API externa.
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
LOOKBACK_DAYS = 150

# Medias móviles.
SMA_FAST = 50
SMA_SLOW = 100

# Volumen monetario mínimo.
MIN_AVG_DOLLAR_VOLUME = 10_000_000

# Filtros de dividendo.
MIN_DIVIDEND_YIELD = 1.0
MAX_DIVIDEND_YIELD = 6.0
MIN_DIVIDEND_GROWTH_3Y = 3.0
MAX_PAYOUT_RATIO = 75.0

# Filtros de calidad.
MIN_ROE = 8.0
MAX_DEBT_TO_EQUITY = 180.0
MIN_REVENUE_GROWTH = -5.0

# Número máximo de resultados.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# API de fundamentales/dividendos.
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
    - tendencia
    - volumen monetario
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
    Calcula volumen monetario medio.

    Volumen monetario = cierre x volumen.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def get_company_profile(symbol):
    """
    Obtiene datos básicos y ratios desde Financial Modeling Prep.

    Incluye:
    - yield
    - beta
    - sector
    - ratios de valoración/calidad si están disponibles
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
        "dividend_yield": safe_float(item.get("lastDiv")) / safe_float(item.get("price"), 1) * 100
        if safe_float(item.get("lastDiv")) is not None else None,
        "beta": safe_float(item.get("beta")),
        "price": safe_float(item.get("price")),
    }


def get_key_metrics(symbol):
    """
    Obtiene métricas fundamentales.

    En FMP se puede usar key-metrics-ttm.
    """
    url = "https://financialmodelingprep.com/stable/key-metrics-ttm"

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
        "payout_ratio": safe_float(item.get("payoutRatioTTM")) * 100
        if safe_float(item.get("payoutRatioTTM")) is not None else None,
        "roe": safe_float(item.get("roeTTM")) * 100
        if safe_float(item.get("roeTTM")) is not None else None,
        "debt_to_equity": safe_float(item.get("debtToEquityTTM")),
    }


def get_income_growth(symbol):
    """
    Calcula crecimiento de ingresos aproximado usando income statement anual.

    Compara el último año contra hace 3 años si hay datos.
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

    latest_revenue = safe_float(data[0].get("revenue"))
    old_revenue = safe_float(data[3].get("revenue"))

    if not latest_revenue or not old_revenue or old_revenue <= 0:
        return None

    total_growth = (latest_revenue / old_revenue - 1) * 100
    annualized_growth = ((latest_revenue / old_revenue) ** (1 / 3) - 1) * 100

    return {
        "revenue_growth_3y_total": total_growth,
        "revenue_growth_3y_annualized": annualized_growth,
    }


def get_dividend_growth(symbol):
    """
    Calcula crecimiento de dividendo de 3 años.

    Usa dividendos históricos.
    Agrupa por año y compara dividendo anual reciente
    contra dividendo de hace 3 años.
    """
    url = "https://financialmodelingprep.com/stable/dividends"

    data = fmp_get_json(
        url,
        FMP_API_KEY,
        params={"symbol": symbol},
        timeout=15,
    )
    if isinstance(data, dict):
        data = data.get("historical", data.get("data", []))

    if not data:
        return None

    df = pd.DataFrame(data)

    if df.empty or "date" not in df or "dividend" not in df:
        return None

    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    df["dividend"] = pd.to_numeric(df["dividend"], errors="coerce")

    annual = df.groupby("year")["dividend"].sum().sort_index()

    if len(annual) < 4:
        return None

    latest_year = annual.index[-1]
    old_year = latest_year - 3

    if old_year not in annual.index:
        return None

    latest_dividend = float(annual.loc[latest_year])
    old_dividend = float(annual.loc[old_year])

    if old_dividend <= 0:
        return None

    total_growth = (latest_dividend / old_dividend - 1) * 100
    annualized_growth = ((latest_dividend / old_dividend) ** (1 / 3) - 1) * 100

    return {
        "dividend_growth_3y_total": total_growth,
        "dividend_growth_3y_annualized": annualized_growth,
        "latest_annual_dividend": latest_dividend,
    }


def get_fundamentals(symbol):
    """
    Une todos los datos fundamentales en un único diccionario.
    """
    profile = get_company_profile(symbol)
    metrics = get_key_metrics(symbol)
    revenue_growth = get_income_growth(symbol)
    dividend_growth = get_dividend_growth(symbol)

    if not profile or not metrics or not revenue_growth or not dividend_growth:
        return None

    return {
        **profile,
        **metrics,
        **revenue_growth,
        **dividend_growth,
    }


def analyze_symbol(symbol, df, fundamentals):
    """
    Analiza si una empresa cumple criterios Dividend Growth.

    Condiciones:
    - Yield razonable.
    - Dividendo creciente.
    - Payout sostenible.
    - ROE positivo.
    - Deuda controlada.
    - Ingresos no deteriorándose gravemente.
    - Liquidez.
    - Tendencia técnica no destruida.
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

    dividend_yield = fundamentals["dividend_yield"]
    payout_ratio = fundamentals["payout_ratio"]
    roe = fundamentals["roe"]
    debt_to_equity = fundamentals["debt_to_equity"]
    revenue_growth = fundamentals["revenue_growth_3y_annualized"]
    dividend_growth = fundamentals["dividend_growth_3y_annualized"]

    required = [
        dividend_yield,
        payout_ratio,
        roe,
        debt_to_equity,
        revenue_growth,
        dividend_growth,
    ]

    if any(value is None for value in required):
        return None

    yield_ok = (
        MIN_DIVIDEND_YIELD <= dividend_yield <= MAX_DIVIDEND_YIELD
    )

    dividend_growth_ok = dividend_growth >= MIN_DIVIDEND_GROWTH_3Y

    payout_ok = 0 < payout_ratio <= MAX_PAYOUT_RATIO

    quality_ok = (
        roe >= MIN_ROE
        and debt_to_equity <= MAX_DEBT_TO_EQUITY
        and revenue_growth >= MIN_REVENUE_GROWTH
    )

    liquidity_ok = avg_dollar_volume >= MIN_AVG_DOLLAR_VOLUME

    # Filtro técnico:
    # no exige momentum fuerte,
    # solo evita activos completamente deteriorados.
    technical_ok = (
        price > sma_slow * 0.90
        or sma_fast > sma_slow
    )

    if not all([
        yield_ok,
        dividend_growth_ok,
        payout_ok,
        quality_ok,
        liquidity_ok,
        technical_ok,
    ]):
        return None

    # Score:
    # equilibrio entre crecimiento de dividendo,
    # yield razonable, payout bajo y calidad.
    yield_score = min(dividend_yield / MAX_DIVIDEND_YIELD, 1) * 25
    growth_score = min(dividend_growth / 12, 1) * 30
    payout_score = max(0, (MAX_PAYOUT_RATIO - payout_ratio) / MAX_PAYOUT_RATIO) * 20
    quality_score = min(roe / 25, 1) * 15
    debt_score = max(0, (MAX_DEBT_TO_EQUITY - debt_to_equity) / MAX_DEBT_TO_EQUITY) * 10

    score = yield_score + growth_score + payout_score + quality_score + debt_score
    stop_loss = min(price * 0.90, sma_slow * 0.96)
    take_profit_1 = price * 1.12
    take_profit_2 = price * 1.22

    return {
        "symbol": symbol,
        "company_name": fundamentals["company_name"],
        "sector": fundamentals["sector"],
        "industry": fundamentals["industry"],
        "price": price,
        "dividend_yield": dividend_yield,
        "dividend_growth_3y": dividend_growth,
        "latest_annual_dividend": fundamentals["latest_annual_dividend"],
        "payout_ratio": payout_ratio,
        "roe": roe,
        "debt_to_equity": debt_to_equity,
        "revenue_growth_3y": revenue_growth,
        "avg_dollar_volume": avg_dollar_volume,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_dividend_growth_candidates():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga precio/volumen.
    3. Descarga fundamentales/dividendos.
    4. Filtra por dividend growth.
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
            print(f"No se pudieron obtener datos de {symbol}: {error}")
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
        f"Stop: {candidate['stop_loss']:.2f} | "
        f"TP1 Dividendo: {candidate['take_profit_1']:.2f} | "
        f"TP2 Dividendo: {candidate['take_profit_2']:.2f} | "
        f"Yield: {candidate['dividend_yield']:.2f}% | "
        f"Crec Div 3Y: {candidate['dividend_growth_3y']:.2f}% | "
        f"Payout: {candidate['payout_ratio']:.1f}% | "
        f"ROE: {candidate['roe']:.1f}% | "
        f"Deuda/Equity: {candidate['debt_to_equity']:.1f} | "
        f"Score: {candidate['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_dividend_growth_candidates()
    output_path, output_count = write_results_to_txt("DividenGrowth", results, format_candidate)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay candidatos Dividend Growth con los filtros actuales.")
    else:
        print("Candidatos Dividend Growth:")
        for candidate in results:
            print(format_candidate(candidate))
