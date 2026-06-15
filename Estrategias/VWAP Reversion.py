"""
Estrategia VWAP Reversion.

Objetivo:
- Detectar activos que se han alejado demasiado del VWAP intradía.
- Buscar una posible vuelta hacia el VWAP.
- Evitar entradas mientras el precio sigue cayendo/subiendo sin freno.
- Generar señales con stop y objetivo.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Concepto:
- VWAP = precio medio ponderado por volumen.
- Muchos traders intradía lo usan como referencia de valor.
- Si el precio se aleja mucho del VWAP, puede volver hacia él.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, time, UTC
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca_request import get_stock_bars_data
from alpaca.data.timeframe import TimeFrame
from analysis_debug import log_strategy_summary, log_symbol_decision


# Archivo de tickers.
TICKERS_FILE = "tickers.txt"

# Zona horaria USA.
NY_TZ = ZoneInfo("America/New_York")

# Horario regular.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Histórico intradía.
LOOKBACK_DAYS = 3

# Distancia mínima al VWAP para considerar reversión.
# 1.0 = precio al menos 1% lejos del VWAP.
MIN_DISTANCE_FROM_VWAP_PCT = 1.0

# RSI intradía para confirmar sobrecompra/sobreventa.
RSI_PERIOD = 14
LONG_MAX_RSI = 35
SHORT_MIN_RSI = 65

# Ventana para volumen medio intradía.
VOLUME_LOOKBACK = 20

# Volumen actual mínimo respecto a la media.
# En reversión no queremos ruptura explosiva contra nosotros,
# pero sí liquidez suficiente.
MIN_VOLUME_RATIO = 0.8

# Volumen monetario acumulado mínimo del día.
MIN_DAY_DOLLAR_VOLUME = 2_000_000

# Máximo de señales.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_tickers(path):
    """
    Lee tickers desde archivo.
    """
    with open(path, "r", encoding="utf-8") as file:
        return sorted(
            {
                line.strip().upper()
                for line in file
                if line.strip() and not line.strip().startswith("#")
            }
        )


def market_is_open():
    """
    Comprueba si estamos en horario regular USA.
    """
    now = datetime.now(NY_TZ).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def get_market_times_for_today():
    """
    Devuelve apertura/cierre de hoy en NY.
    """
    now = datetime.now(NY_TZ)

    market_open = datetime.combine(
        now.date(),
        MARKET_OPEN,
        tzinfo=NY_TZ,
    )

    market_close = datetime.combine(
        now.date(),
        MARKET_CLOSE,
        tzinfo=NY_TZ,
    )

    return market_open, market_close


def get_intraday_bars(client, symbols):
    """
    Descarga barras de 1 minuto desde Alpaca.
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        adjustment=Adjustment.RAW,
        start=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
        end=datetime.now(UTC),
        feed=DataFeed.IEX,
    )

    bars = get_stock_bars_data(client, request)
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
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df.set_index("timestamp", inplace=True)
            data[symbol] = df

    return data


def only_today_regular_session(df):
    """
    Filtra barras de hoy dentro del horario regular.
    """
    market_open, market_close = get_market_times_for_today()

    data = df.copy()
    ny_time = data.index.tz_convert(NY_TZ)

    mask = (
        (ny_time >= market_open)
        & (ny_time <= market_close)
    )

    return data.loc[mask]


def calculate_vwap(df):
    """
    Calcula VWAP intradía acumulado.

    VWAP = suma(precio típico x volumen) / suma(volumen)

    Precio típico = (high + low + close) / 3
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3

    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_volume = df["volume"].cumsum()

    return cumulative_pv / cumulative_volume


def calculate_rsi(close, period):
    """
    Calcula RSI.
    """
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando reversión al VWAP.

    Señal LONG:
    - Precio está bastante por debajo del VWAP.
    - RSI bajo.
    - Última vela muestra recuperación.

    Señal SHORT:
    - Precio está bastante por encima del VWAP.
    - RSI alto.
    - Última vela muestra rechazo.
    """
    today = only_today_regular_session(df)

    if len(today) < max(RSI_PERIOD, VOLUME_LOOKBACK) + 5:
        return None

    today = today.copy()

    today["vwap"] = calculate_vwap(today)
    today["rsi"] = calculate_rsi(today["close"], RSI_PERIOD)
    today["avg_volume"] = today["volume"].rolling(VOLUME_LOOKBACK).mean()

    today = today.dropna()

    if today.empty:
        return None

    latest = today.iloc[-1]
    previous = today.iloc[-2]

    price = float(latest["close"])
    open_price = float(latest["open"])
    high = float(latest["high"])
    low = float(latest["low"])
    vwap = float(latest["vwap"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    avg_volume = float(latest["avg_volume"])

    if vwap <= 0 or avg_volume <= 0:
        return None

    distance_vwap_pct = ((price / vwap) - 1) * 100
    volume_ratio = volume / avg_volume

    day_dollar_volume = float(
        (today["close"] * today["volume"]).sum()
    )

    if day_dollar_volume < MIN_DAY_DOLLAR_VOLUME:
        return None

    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    # Señales de vela sencillas:
    # LONG: cierra por encima de apertura y mejora cierre previo.
    bullish_reversal = (
        price > open_price
        and price > float(previous["close"])
    )

    # SHORT: cierra por debajo de apertura y pierde cierre previo.
    bearish_reversal = (
        price < open_price
        and price < float(previous["close"])
    )

    # LONG mean reversion:
    # precio muy por debajo del VWAP y empieza a rebotar.
    if (
        distance_vwap_pct <= -MIN_DISTANCE_FROM_VWAP_PCT
        and rsi <= LONG_MAX_RSI
        and bullish_reversal
    ):
        side = "LONG"

        # Stop debajo del mínimo reciente.
        stop_loss = float(today["low"].tail(5).min())

        if stop_loss >= price:
            return None

        risk_per_share = price - stop_loss
        take_profit_1 = vwap
        take_profit_2 = price + risk_per_share * 2

    # SHORT mean reversion:
    # precio muy por encima del VWAP y empieza a girarse.
    elif (
        distance_vwap_pct >= MIN_DISTANCE_FROM_VWAP_PCT
        and rsi >= SHORT_MIN_RSI
        and bearish_reversal
    ):
        side = "SHORT"

        # Stop encima del máximo reciente.
        stop_loss = float(today["high"].tail(5).max())

        if stop_loss <= price:
            return None

        risk_per_share = stop_loss - price
        take_profit_1 = vwap
        take_profit_2 = price - risk_per_share * 2

    else:
        return None

    # Score:
    # cuanto más alejado del VWAP, más extremo.
    # añadimos fuerza de RSI y volumen.
    score = (
        abs(distance_vwap_pct) * 0.45
        + abs(50 - rsi) * 0.25
        + volume_ratio * 0.30
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "vwap": vwap,
        "distance_vwap_pct": distance_vwap_pct,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "day_dollar_volume": day_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_vwap_reversion_signals():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga velas de 1 minuto.
    3. Calcula VWAP.
    4. Busca reversión.
    5. Ordena por score.
    """
    symbols = load_tickers(TICKERS_FILE)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_intraday_bars(client, symbols)

    signals = []
    with_data_count = 0
    accepted_count = 0

    for symbol in symbols:
        df = data.get(symbol)

        if df is None or df.empty:
            log_symbol_decision("VWAP Reversion", symbol, "SIN DATOS", "Alpaca no devolvio velas intradia")
            continue

        with_data_count += 1
        result = analyze_symbol(symbol, df)

        if result:
            accepted_count += 1
            log_symbol_decision("VWAP Reversion", symbol, "OK", format_signal(result))
            signals.append(result)
        else:
            log_symbol_decision("VWAP Reversion", symbol, "DESCARTADO", "No cumple distancia a VWAP, RSI, volumen o rebote")

    signals = sorted(
        signals,
        key=lambda item: item["score"],
        reverse=True,
    )

    selected = signals[:TOP_N]
    log_strategy_summary("VWAP Reversion", len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def format_signal(signal):
    """
    Formatea señal para imprimir o enviar por Telegram.
    """
    return (
        f"{signal['side']} | {signal['symbol']} | "
        f"Precio: {signal['price']:.2f} | "
        f"VWAP: {signal['vwap']:.2f} | "
        f"Dist VWAP: {signal['distance_vwap_pct']:.2f}% | "
        f"RSI: {signal['rsi']:.1f} | "
        f"Vol xMedia: {signal['volume_ratio']:.2f} | "
        f"Stop: {signal['stop_loss']:.2f} | "
        f"TP1 VWAP: {signal['take_profit_1']:.2f} | "
        f"TP2: {signal['take_profit_2']:.2f} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    if not market_is_open():
        output_path, output_count = write_results_to_txt("VWAP_Reversion", [], format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")
        print("Mercado cerrado. VWAP Reversion es una estrategia intradía.")
    else:
        results = find_vwap_reversion_signals()
        output_path, output_count = write_results_to_txt("VWAP_Reversion", results, format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")

        if not results:
            print("No hay señales VWAP Reversion con los filtros actuales.")
        else:
            print("Señales VWAP Reversion:")
            for signal in results:
                print(format_signal(signal))
