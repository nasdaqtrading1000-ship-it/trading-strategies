"""
Estrategia Momentum Intradía.

Objetivo:
- Detectar acciones que se están moviendo con fuerza durante la sesión.
- Buscar continuidad del movimiento.
- Confirmar con volumen, VWAP y ruptura de máximos intradía.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Uso típico:
- Ejecutar durante mercado abierto.
- Revisar señales LONG/SHORT.
- Enviar las mejores por Telegram.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, time, UTC
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# Archivo de tickers.
TICKERS_FILE = "tickers.txt"

# Zona horaria de mercado USA.
NY_TZ = ZoneInfo("America/New_York")

# Horario regular.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Descargamos barras intradía recientes.
LOOKBACK_DAYS = 3

# Momentum intradía:
# compara precio actual con precio de hace X minutos.
MOMENTUM_MINUTES = 15

# Ruptura de máximos/mínimos recientes.
BREAKOUT_LOOKBACK_MINUTES = 20

# Volumen medio intradía.
VOLUME_LOOKBACK = 20

# Volumen actual mínimo respecto a la media.
MIN_VOLUME_RATIO = 2.0

# Movimiento mínimo en 15 minutos.
MIN_MOMENTUM_PCT = 1.0

# Volumen monetario acumulado mínimo del día.
MIN_DAY_DOLLAR_VOLUME = 3_000_000

# Evita señales demasiado cerca de apertura si quieres.
MIN_MINUTES_AFTER_OPEN = 10

# Máximo de señales.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


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


def market_is_open():
    """
    Comprueba si el mercado USA está abierto.
    """
    now = datetime.now(NY_TZ).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def get_market_times_for_today():
    """
    Devuelve apertura y cierre de hoy en horario NY.
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
    Descarga velas de 1 minuto desde Alpaca.
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
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
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df.set_index("timestamp", inplace=True)
            data[symbol] = df

    return data


def only_today_regular_session(df):
    """
    Filtra solo las barras de hoy dentro del horario regular.
    """
    market_open, market_close = get_market_times_for_today()

    data = df.copy()
    ny_time = data.index.tz_convert(NY_TZ)

    mask = (
        (ny_time >= market_open)
        & (ny_time <= market_close)
    )

    return data.loc[mask]


def minutes_since_open():
    """
    Minutos transcurridos desde apertura.
    """
    market_open, _market_close = get_market_times_for_today()
    now = datetime.now(NY_TZ)

    return (now - market_open).total_seconds() / 60


def calculate_vwap(df):
    """
    Calcula VWAP intradía acumulado.

    VWAP ayuda a distinguir fuerza real:
    - Precio por encima de VWAP = sesgo alcista.
    - Precio por debajo de VWAP = sesgo bajista.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3

    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_volume = df["volume"].cumsum()

    return cumulative_pv / cumulative_volume


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando Momentum Intradía.

    Señal LONG:
    - Precio sube fuerte en los últimos X minutos.
    - Precio por encima del VWAP.
    - Rompe máximos recientes.
    - Volumen actual fuerte.

    Señal SHORT:
    - Precio cae fuerte en los últimos X minutos.
    - Precio por debajo del VWAP.
    - Rompe mínimos recientes.
    - Volumen actual fuerte.
    """
    if minutes_since_open() < MIN_MINUTES_AFTER_OPEN:
        return None

    today = only_today_regular_session(df)

    min_required = max(
        MOMENTUM_MINUTES,
        BREAKOUT_LOOKBACK_MINUTES,
        VOLUME_LOOKBACK,
    ) + 5

    if len(today) < min_required:
        return None

    today = today.copy()

    today["vwap"] = calculate_vwap(today)
    today["avg_volume"] = today["volume"].rolling(VOLUME_LOOKBACK).mean()

    today = today.dropna()

    if today.empty or len(today) < min_required:
        return None

    latest = today.iloc[-1]
    past = today.iloc[-MOMENTUM_MINUTES]

    price = float(latest["close"])
    past_price = float(past["close"])
    vwap = float(latest["vwap"])
    volume = float(latest["volume"])
    avg_volume = float(latest["avg_volume"])

    if past_price <= 0 or vwap <= 0 or avg_volume <= 0:
        return None

    momentum_pct = ((price / past_price) - 1) * 100
    volume_ratio = volume / avg_volume

    day_dollar_volume = float(
        (today["close"] * today["volume"]).sum()
    )

    if day_dollar_volume < MIN_DAY_DOLLAR_VOLUME:
        return None

    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    # Ventana reciente excluyendo la vela actual.
    recent_window = today.iloc[-BREAKOUT_LOOKBACK_MINUTES - 1:-1]

    recent_high = float(recent_window["high"].max())
    recent_low = float(recent_window["low"].min())

    # Señal LONG:
    # momentum positivo, encima de VWAP y ruptura de máximos recientes.
    if (
        momentum_pct >= MIN_MOMENTUM_PCT
        and price > vwap
        and price > recent_high
    ):
        side = "LONG"

        stop_loss = max(
            recent_low,
            vwap
        )

        if stop_loss >= price:
            return None

        risk_per_share = price - stop_loss
        take_profit_1 = price + risk_per_share * 1.5
        take_profit_2 = price + risk_per_share * 2.5

    # Señal SHORT:
    # momentum negativo, debajo de VWAP y ruptura de mínimos recientes.
    elif (
        momentum_pct <= -MIN_MOMENTUM_PCT
        and price < vwap
        and price < recent_low
    ):
        side = "SHORT"

        stop_loss = min(
            recent_high,
            vwap
        )

        if stop_loss <= price:
            return None

        risk_per_share = stop_loss - price
        take_profit_1 = price - risk_per_share * 1.5
        take_profit_2 = price - risk_per_share * 2.5

    else:
        return None

    # Score:
    # prioriza movimiento fuerte y volumen.
    score = (
        abs(momentum_pct) * 0.50
        + volume_ratio * 0.35
        + abs((price / vwap - 1) * 100) * 0.15
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "vwap": vwap,
        "momentum_pct": momentum_pct,
        "volume_ratio": volume_ratio,
        "day_dollar_volume": day_dollar_volume,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_intraday_momentum_signals():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga barras de 1 minuto.
    3. Calcula VWAP y volumen.
    4. Busca momentum intradía.
    5. Ordena por score.
    """
    symbols = load_tickers(TICKERS_FILE)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_intraday_bars(client, symbols)

    signals = []

    for symbol in symbols:
        df = data.get(symbol)

        if df is None or df.empty:
            continue

        result = analyze_symbol(symbol, df)

        if result:
            signals.append(result)

    signals = sorted(
        signals,
        key=lambda item: item["score"],
        reverse=True,
    )

    return signals[:TOP_N]


def format_signal(signal):
    """
    Formatea señal para imprimir o mandar por Telegram.
    """
    return (
        f"{signal['side']} | {signal['symbol']} | "
        f"Precio: {signal['price']:.2f} | "
        f"Momentum {MOMENTUM_MINUTES}m: {signal['momentum_pct']:.2f}% | "
        f"VWAP: {signal['vwap']:.2f} | "
        f"Vol xMedia: {signal['volume_ratio']:.2f} | "
        f"Stop: {signal['stop_loss']:.2f} | "
        f"TP1: {signal['take_profit_1']:.2f} | "
        f"TP2: {signal['take_profit_2']:.2f} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    if not market_is_open():
        output_path, output_count = write_results_to_txt("MomentumIntradia", [], format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")
        print("Mercado cerrado. Momentum Intradía se usa durante la sesión.")
    else:
        results = find_intraday_momentum_signals()
        output_path, output_count = write_results_to_txt("MomentumIntradia", results, format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")

        if not results:
            print("No hay señales Momentum Intradía con los filtros actuales.")
        else:
            print("Señales Momentum Intradía:")
            for signal in results:
                print(format_signal(signal))
