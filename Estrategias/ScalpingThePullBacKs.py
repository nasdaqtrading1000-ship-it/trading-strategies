"""
Estrategia Scalping de Pullbacks.

Objetivo:
- Detectar tendencias intradía fuertes.
- Esperar retrocesos pequeños y controlados.
- Entrar cuando el precio vuelve a impulsarse.
- Buscar operaciones rápidas con stop cercano.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Concepto:
- No se compra el máximo.
- Se espera un retroceso dentro de tendencia.
- Se busca reentrada con confirmación de vela y volumen.
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

# Zona horaria NY.
NY_TZ = ZoneInfo("America/New_York")

# Horario regular.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Histórico intradía.
LOOKBACK_DAYS = 3

# EMAs intradía.
EMA_FAST = 9
EMA_SLOW = 21

# RSI intradía.
RSI_PERIOD = 14

# Ventana de volumen.
VOLUME_LOOKBACK = 20

# Volumen actual respecto a la media.
MIN_VOLUME_RATIO = 1.2

# Volumen monetario mínimo acumulado del día.
MIN_DAY_DOLLAR_VOLUME = 2_000_000

# Distancia máxima permitida a EMA rápida después del pullback.
# Evita entrar demasiado lejos.
MAX_DISTANCE_FROM_EMA_FAST_PCT = 0.4

# Pullback mínimo desde máximo/mínimo reciente.
MIN_PULLBACK_PCT = 0.3

# Ventana para medir máximo/mínimo reciente.
PULLBACK_LOOKBACK = 20

# Evitar primeros minutos si quieres menos ruido.
MIN_MINUTES_AFTER_OPEN = 10

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
    Comprueba si el mercado USA está abierto.
    """
    now = datetime.now(NY_TZ).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def get_market_times_for_today():
    """
    Devuelve apertura/cierre de hoy en horario NY.
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


def minutes_since_open():
    """
    Minutos desde apertura.
    """
    market_open, _market_close = get_market_times_for_today()
    now = datetime.now(NY_TZ)

    return (now - market_open).total_seconds() / 60


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
    Filtra barras de hoy en horario regular.
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
    Busca pullbacks dentro de tendencia intradía.

    LONG:
    - Precio por encima de VWAP.
    - EMA 9 por encima de EMA 21.
    - Retroceso hacia EMA 9/21.
    - Última vela recupera fuerza.

    SHORT:
    - Precio por debajo de VWAP.
    - EMA 9 por debajo de EMA 21.
    - Rebote hacia EMA 9/21.
    - Última vela vuelve a caer.
    """
    if minutes_since_open() < MIN_MINUTES_AFTER_OPEN:
        return None

    today = only_today_regular_session(df)

    min_required = max(
        EMA_SLOW,
        RSI_PERIOD,
        VOLUME_LOOKBACK,
        PULLBACK_LOOKBACK,
    ) + 5

    if len(today) < min_required:
        return None

    today = today.copy()

    today["ema_fast"] = today["close"].ewm(span=EMA_FAST, adjust=False).mean()
    today["ema_slow"] = today["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    today["vwap"] = calculate_vwap(today)
    today["rsi"] = calculate_rsi(today["close"], RSI_PERIOD)
    today["avg_volume"] = today["volume"].rolling(VOLUME_LOOKBACK).mean()

    today = today.dropna()

    if today.empty or len(today) < min_required:
        return None

    latest = today.iloc[-1]
    previous = today.iloc[-2]

    price = float(latest["close"])
    open_price = float(latest["open"])
    high = float(latest["high"])
    low = float(latest["low"])
    ema_fast = float(latest["ema_fast"])
    ema_slow = float(latest["ema_slow"])
    vwap = float(latest["vwap"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    avg_volume = float(latest["avg_volume"])

    if ema_fast <= 0 or ema_slow <= 0 or vwap <= 0 or avg_volume <= 0:
        return None

    volume_ratio = volume / avg_volume

    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    day_dollar_volume = float(
        (today["close"] * today["volume"]).sum()
    )

    if day_dollar_volume < MIN_DAY_DOLLAR_VOLUME:
        return None

    recent = today.tail(PULLBACK_LOOKBACK)

    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())

    # Distancia del precio actual a la EMA rápida.
    distance_ema_fast_pct = ((price / ema_fast) - 1) * 100

    # LONG trend.
    long_trend_ok = (
        price > vwap
        and ema_fast > ema_slow
        and rsi >= 45
    )

    # Pullback long:
    # el precio ha retrocedido desde máximo reciente
    # y ha tocado o se ha acercado a la EMA rápida.
    long_pullback_pct = ((recent_high - price) / recent_high) * 100

    long_pullback_ok = (
        long_pullback_pct >= MIN_PULLBACK_PCT
        and abs(distance_ema_fast_pct) <= MAX_DISTANCE_FROM_EMA_FAST_PCT
    )

    # Reanudación long:
    # vela alcista y cierre por encima de cierre anterior.
    long_resume_ok = (
        price > open_price
        and price > float(previous["close"])
    )

    # SHORT trend.
    short_trend_ok = (
        price < vwap
        and ema_fast < ema_slow
        and rsi <= 55
    )

    # Pullback short:
    # el precio ha rebotado desde mínimo reciente
    # y vuelve cerca de EMA rápida.
    short_pullback_pct = ((price - recent_low) / recent_low) * 100

    short_pullback_ok = (
        short_pullback_pct >= MIN_PULLBACK_PCT
        and abs(distance_ema_fast_pct) <= MAX_DISTANCE_FROM_EMA_FAST_PCT
    )

    # Reanudación short:
    # vela bajista y cierre por debajo de cierre anterior.
    short_resume_ok = (
        price < open_price
        and price < float(previous["close"])
    )

    if long_trend_ok and long_pullback_ok and long_resume_ok:
        side = "LONG"

        stop_loss = min(
            float(recent["low"].tail(5).min()),
            ema_slow
        )

        if stop_loss >= price:
            return None

        risk_per_share = price - stop_loss
        take_profit_1 = price + risk_per_share * 1.0
        take_profit_2 = price + risk_per_share * 1.8

        pullback_pct = long_pullback_pct

    elif short_trend_ok and short_pullback_ok and short_resume_ok:
        side = "SHORT"

        stop_loss = max(
            float(recent["high"].tail(5).max()),
            ema_slow
        )

        if stop_loss <= price:
            return None

        risk_per_share = stop_loss - price
        take_profit_1 = price - risk_per_share * 1.0
        take_profit_2 = price - risk_per_share * 1.8

        pullback_pct = short_pullback_pct

    else:
        return None

    # Score:
    # prioriza tendencia limpia, volumen y pullback controlado.
    score = (
        volume_ratio * 0.40
        + pullback_pct * 0.30
        + abs((price / vwap - 1) * 100) * 0.30
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "vwap": vwap,
        "rsi": rsi,
        "pullback_pct": pullback_pct,
        "volume_ratio": volume_ratio,
        "day_dollar_volume": day_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_scalping_pullback_signals():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga velas de 1 minuto.
    3. Calcula EMA, VWAP, RSI y volumen.
    4. Busca pullbacks.
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
        f"EMA{EMA_FAST}: {signal['ema_fast']:.2f} | "
        f"EMA{EMA_SLOW}: {signal['ema_slow']:.2f} | "
        f"VWAP: {signal['vwap']:.2f} | "
        f"RSI: {signal['rsi']:.1f} | "
        f"Pullback: {signal['pullback_pct']:.2f}% | "
        f"Vol xMedia: {signal['volume_ratio']:.2f} | "
        f"Stop: {signal['stop_loss']:.2f} | "
        f"TP1: {signal['take_profit_1']:.2f} | "
        f"TP2: {signal['take_profit_2']:.2f} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    if not market_is_open():
        output_path, output_count = write_results_to_txt("ScalpingThePullBacKs", [], format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")
        print("Mercado cerrado. Scalping de Pullbacks se usa durante la sesión.")
    else:
        results = find_scalping_pullback_signals()
        output_path, output_count = write_results_to_txt("ScalpingThePullBacKs", results, format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")

        if not results:
            print("No hay señales Scalping de Pullbacks con los filtros actuales.")
        else:
            print("Señales Scalping de Pullbacks:")
            for signal in results:
                print(format_signal(signal))
