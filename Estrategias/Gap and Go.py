"""
Estrategia Gap & Go.

Objetivo:
- Detectar acciones que abren con gap relevante.
- Confirmar que mantienen la fuerza tras la apertura.
- Buscar continuación del movimiento durante la sesión.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Concepto:
- Gap alcista: abre por encima del cierre anterior.
- Gap bajista: abre por debajo del cierre anterior.
- Go: continúa en la dirección del gap en vez de rellenarlo.
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

# Zona horaria USA.
NY_TZ = ZoneInfo("America/New_York")

# Horario regular.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Histórico para cierre previo y barras intradía.
LOOKBACK_DAYS = 5

# Gap mínimo.
# 2.0 = gap de al menos 2%.
MIN_GAP_PCT = 2.0

# Gap máximo.
# Evita gaps demasiado extremos donde el riesgo puede ser enorme.
MAX_GAP_PCT = 20.0

# Minutos iniciales para confirmar que el gap aguanta.
OPENING_CONFIRMATION_MINUTES = 15

# Ruptura posterior:
# la señal llega si rompe máximo/mínimo del rango inicial.
BREAKOUT_BUFFER_PCT = 0.05

# Volumen mínimo del rango inicial.
MIN_OPENING_DOLLAR_VOLUME = 2_000_000

# Volumen actual respecto al volumen medio intradía.
VOLUME_LOOKBACK = 20
MIN_VOLUME_RATIO = 1.5

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

    confirmation_end = market_open + timedelta(
        minutes=OPENING_CONFIRMATION_MINUTES
    )

    return market_open, confirmation_end, market_close


def get_intraday_bars(client, symbols):
    """
    Descarga barras de 1 minuto.

    Necesitamos:
    - cierre anterior
    - apertura de hoy
    - rango inicial
    - ruptura posterior
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


def regular_session_mask(df, date):
    """
    Crea máscara para una sesión regular concreta.
    """
    market_open = datetime.combine(date, MARKET_OPEN, tzinfo=NY_TZ)
    market_close = datetime.combine(date, MARKET_CLOSE, tzinfo=NY_TZ)

    ny_time = df.index.tz_convert(NY_TZ)

    return (
        (ny_time >= market_open)
        & (ny_time <= market_close)
    )


def get_today_session(df):
    """
    Devuelve barras de hoy en sesión regular.
    """
    today = datetime.now(NY_TZ).date()
    return df.loc[regular_session_mask(df, today)]


def get_previous_session(df):
    """
    Devuelve la sesión regular anterior disponible.

    Recorre hacia atrás hasta encontrar barras.
    """
    today = datetime.now(NY_TZ).date()

    for days_back in range(1, 6):
        session_date = today - timedelta(days=days_back)
        session = df.loc[regular_session_mask(df, session_date)]

        if not session.empty:
            return session

    return pd.DataFrame()


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando Gap & Go.

    LONG:
    - Gap alcista.
    - El rango inicial mantiene fuerza.
    - Rompe máximo inicial.

    SHORT:
    - Gap bajista.
    - El rango inicial mantiene debilidad.
    - Rompe mínimo inicial.
    """
    market_open, confirmation_end, market_close = get_market_times_for_today()
    now = datetime.now(NY_TZ)

    # Esperamos a que termine el rango de confirmación.
    if now < confirmation_end:
        return None

    today = get_today_session(df)
    previous_session = get_previous_session(df)

    if today.empty or previous_session.empty:
        return None

    previous_close = float(previous_session["close"].iloc[-1])
    opening_price = float(today["open"].iloc[0])

    if previous_close <= 0:
        return None

    gap_pct = ((opening_price / previous_close) - 1) * 100

    # Filtramos gaps demasiado pequeños o demasiado extremos.
    if abs(gap_pct) < MIN_GAP_PCT or abs(gap_pct) > MAX_GAP_PCT:
        return None

    # Rango inicial.
    opening_range = today[
        today.index.tz_convert(NY_TZ) < confirmation_end
    ]

    after_range = today[
        today.index.tz_convert(NY_TZ) >= confirmation_end
    ]

    if opening_range.empty or after_range.empty:
        return None

    range_high = float(opening_range["high"].max())
    range_low = float(opening_range["low"].min())
    range_close = float(opening_range["close"].iloc[-1])

    opening_dollar_volume = float(
        (opening_range["close"] * opening_range["volume"]).sum()
    )

    if opening_dollar_volume < MIN_OPENING_DOLLAR_VOLUME:
        return None

    latest = after_range.iloc[-1]

    price = float(latest["close"])
    volume = float(latest["volume"])

    after_range = after_range.copy()
    after_range["avg_volume"] = after_range["volume"].rolling(VOLUME_LOOKBACK).mean()
    after_range = after_range.dropna()

    if after_range.empty:
        return None

    avg_volume = float(after_range["avg_volume"].iloc[-1])

    if avg_volume <= 0:
        return None

    volume_ratio = volume / avg_volume

    if volume_ratio < MIN_VOLUME_RATIO:
        return None

    # Gap alcista:
    # buscamos que no rellene el gap y rompa máximo inicial.
    if gap_pct > 0:
        gap_holds = range_low > previous_close
        breakout_level = range_high
        breakout_pct = ((price / breakout_level) - 1) * 100

        if (
            gap_holds
            and breakout_pct >= BREAKOUT_BUFFER_PCT
        ):
            side = "LONG"
            stop_loss = range_low

            if stop_loss >= price:
                return None

            risk_per_share = price - stop_loss
            take_profit_1 = price + risk_per_share * 1.5
            take_profit_2 = price + risk_per_share * 2.5
        else:
            return None

    # Gap bajista:
    # buscamos que no rellene el gap y rompa mínimo inicial.
    else:
        gap_holds = range_high < previous_close
        breakout_level = range_low
        breakout_pct = ((breakout_level / price) - 1) * 100

        if (
            gap_holds
            and breakout_pct >= BREAKOUT_BUFFER_PCT
        ):
            side = "SHORT"
            stop_loss = range_high

            if stop_loss <= price:
                return None

            risk_per_share = stop_loss - price
            take_profit_1 = price - risk_per_share * 1.5
            take_profit_2 = price - risk_per_share * 2.5
        else:
            return None

    # Score:
    # prioriza tamaño del gap, volumen y ruptura.
    score = (
        abs(gap_pct) * 0.35
        + volume_ratio * 0.35
        + breakout_pct * 0.30
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "previous_close": previous_close,
        "opening_price": opening_price,
        "gap_pct": gap_pct,
        "range_high": range_high,
        "range_low": range_low,
        "range_close": range_close,
        "breakout_level": breakout_level,
        "breakout_pct": breakout_pct,
        "volume_ratio": volume_ratio,
        "opening_dollar_volume": opening_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_gap_and_go_signals():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga barras intradía.
    3. Calcula gap.
    4. Confirma que el gap aguanta.
    5. Busca continuación.
    6. Ordena por score.
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
        f"Cierre previo: {signal['previous_close']:.2f} | "
        f"Apertura: {signal['opening_price']:.2f} | "
        f"Gap: {signal['gap_pct']:.2f}% | "
        f"Rango inicial: {signal['range_low']:.2f}-{signal['range_high']:.2f} | "
        f"Ruptura: {signal['breakout_pct']:.2f}% | "
        f"Vol xMedia: {signal['volume_ratio']:.2f} | "
        f"Stop: {signal['stop_loss']:.2f} | "
        f"TP1: {signal['take_profit_1']:.2f} | "
        f"TP2: {signal['take_profit_2']:.2f} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    if not market_is_open():
        output_path, output_count = write_results_to_txt("Gap_and_Go", [], format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")
        print("Mercado cerrado. Gap & Go se usa durante la sesión.")
    else:
        results = find_gap_and_go_signals()
        output_path, output_count = write_results_to_txt("Gap_and_Go", results, format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")

        if not results:
            print("No hay señales Gap & Go con los filtros actuales.")
        else:
            print("Señales Gap & Go:")
            for signal in results:
                print(format_signal(signal))
