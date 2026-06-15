"""
Estrategia Opening Range Breakout.

Objetivo:
- Detectar rupturas del rango inicial de la sesión.
- Usar los primeros minutos del mercado como zona clave.
- Enviar señal cuando el precio rompe ese rango con volumen.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Horario USA:
- Mercado abre a las 09:30 Nueva York.
- El opening range típico puede ser de 5, 15 o 30 minutos.
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

# Zona horaria del mercado USA.
NY_TZ = ZoneInfo("America/New_York")

# Horario de mercado.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Minutos que forman el opening range.
OPENING_RANGE_MINUTES = 15

# Descargamos barras intradía del día actual.
LOOKBACK_DAYS = 3

# Confirmación de volumen.
# La vela de ruptura debe tener volumen al menos X veces
# la media de volumen de las velas previas del día.
MIN_VOLUME_MULTIPLIER = 1.5

# Margen mínimo por encima/debajo del rango.
# 0.05 = 0.05%.
MIN_BREAKOUT_PCT = 0.05

# Evita activos demasiado ilíquidos intradía.
MIN_OPENING_RANGE_DOLLAR_VOLUME = 1_000_000

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


def get_market_times_for_today():
    """
    Devuelve apertura y cierre de mercado para hoy en horario NY.
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

    opening_range_end = market_open + timedelta(
        minutes=OPENING_RANGE_MINUTES
    )

    return market_open, opening_range_end, market_close


def market_is_open():
    """
    Comprueba si estamos dentro del horario regular.
    """
    now = datetime.now(NY_TZ).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def get_intraday_bars(client, symbols):
    """
    Descarga barras de 1 minuto desde Alpaca.

    Usamos TimeFrame.Minute porque esta estrategia es intradía.
    """
    start = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    end = datetime.now(UTC)

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        adjustment=Adjustment.RAW,
        start=start,
        end=end,
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
    Filtra solo barras de hoy dentro del horario regular NY.
    """
    market_open, _range_end, market_close = get_market_times_for_today()

    # Convertimos índice UTC a NY para comparar horarios.
    data = df.copy()
    data["ny_time"] = data.index.tz_convert(NY_TZ)


    # En pandas no se puede usar 'and' con Series,
    # así que lo hacemos correctamente:
    mask = (
        (data["ny_time"] >= market_open)
        & (data["ny_time"] <= market_close)
    )

    return data.loc[mask].drop(columns=["ny_time"])


def analyze_symbol(symbol, df):
    """
    Analiza un activo buscando ruptura del opening range.

    Condiciones:
    - Hay barras suficientes de hoy.
    - Ya ha terminado el opening range.
    - El precio rompe máximo o mínimo del rango.
    - La ruptura se confirma con volumen.
    - El rango inicial tuvo volumen monetario suficiente.
    """
    if df.empty:
        return None

    market_open, range_end, market_close = get_market_times_for_today()
    now = datetime.now(NY_TZ)

    # No analizamos antes de que termine el rango inicial.
    if now < range_end:
        return None

    today = only_today_regular_session(df)

    if today.empty:
        return None

    # Barras que forman el opening range.
    opening_range = today[
        (today.index.tz_convert(NY_TZ) >= market_open)
        & (today.index.tz_convert(NY_TZ) < range_end)
    ]

    # Barras posteriores al opening range.
    after_range = today[
        today.index.tz_convert(NY_TZ) >= range_end
    ]

    if opening_range.empty or after_range.empty:
        return None

    range_high = float(opening_range["high"].max())
    range_low = float(opening_range["low"].min())

    opening_range_dollar_volume = float(
        (opening_range["close"] * opening_range["volume"]).sum()
    )

    if opening_range_dollar_volume < MIN_OPENING_RANGE_DOLLAR_VOLUME:
        return None

    latest = after_range.iloc[-1]

    price = float(latest["close"])
    volume = float(latest["volume"])

    # Volumen medio de las barras posteriores hasta ahora.
    avg_intraday_volume = float(after_range["volume"].mean())

    if avg_intraday_volume <= 0:
        return None

    volume_ratio = volume / avg_intraday_volume

    long_breakout_pct = ((price / range_high) - 1) * 100
    short_breakout_pct = ((range_low / price) - 1) * 100

    volume_ok = volume_ratio >= MIN_VOLUME_MULTIPLIER

    if not volume_ok:
        return None

    # Señal LONG:
    # precio rompe por encima del máximo del opening range.
    if long_breakout_pct >= MIN_BREAKOUT_PCT:
        side = "LONG"
        breakout_pct = long_breakout_pct
        breakout_level = range_high

        # Stop orientativo debajo del rango.
        stop_loss = range_low

        risk_per_share = price - stop_loss

        if risk_per_share <= 0:
            return None

        take_profit_1 = price + risk_per_share * 1.5
        take_profit_2 = price + risk_per_share * 2.5

    # Señal SHORT:
    # precio rompe por debajo del mínimo del opening range.
    elif short_breakout_pct >= MIN_BREAKOUT_PCT:
        side = "SHORT"
        breakout_pct = short_breakout_pct
        breakout_level = range_low

        # Stop orientativo encima del rango.
        stop_loss = range_high

        risk_per_share = stop_loss - price

        if risk_per_share <= 0:
            return None

        take_profit_1 = price - risk_per_share * 1.5
        take_profit_2 = price - risk_per_share * 2.5

    else:
        return None

    # Score:
    # prioriza ruptura clara y volumen fuerte.
    score = (
        breakout_pct * 0.55
        + volume_ratio * 0.45
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "range_high": range_high,
        "range_low": range_low,
        "breakout_level": breakout_level,
        "breakout_pct": breakout_pct,
        "volume_ratio": volume_ratio,
        "opening_range_dollar_volume": opening_range_dollar_volume,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "score": score,
    }


def find_opening_range_breakouts():
    """
    Función principal.

    1. Lee tickers.
    2. Descarga velas de 1 minuto.
    3. Calcula opening range.
    4. Busca rupturas.
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
            log_symbol_decision("Opening Range BreaKout", symbol, "SIN DATOS", "Alpaca no devolvio velas intradia")
            continue

        with_data_count += 1
        result = analyze_symbol(symbol, df)

        if result:
            accepted_count += 1
            log_symbol_decision("Opening Range BreaKout", symbol, "OK", format_signal(result))
            signals.append(result)
        else:
            log_symbol_decision("Opening Range BreaKout", symbol, "DESCARTADO", "No rompe rango inicial con volumen o tendencia suficiente")

    signals = sorted(
        signals,
        key=lambda item: item["score"],
        reverse=True,
    )

    selected = signals[:TOP_N]
    log_strategy_summary("Opening Range BreaKout", len(symbols), with_data_count, accepted_count, len(selected))
    return selected


def format_signal(signal):
    """
    Formatea una señal para imprimir o enviar por Telegram.
    """
    return (
        f"{signal['side']} | {signal['symbol']} | "
        f"Precio: {signal['price']:.2f} | "
        f"Rango: {signal['range_low']:.2f}-{signal['range_high']:.2f} | "
        f"Ruptura: {signal['breakout_pct']:.2f}% | "
        f"Vol xMedia: {signal['volume_ratio']:.2f}x | "
        f"Stop: {signal['stop_loss']:.2f} | "
        f"TP1: {signal['take_profit_1']:.2f} | "
        f"TP2: {signal['take_profit_2']:.2f} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    if not market_is_open():
        output_path, output_count = write_results_to_txt("OpeningRangeBreaKout", [], format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")
        print("Mercado cerrado. Esta estrategia se usa durante la sesión regular.")
    else:
        results = find_opening_range_breakouts()
        output_path, output_count = write_results_to_txt("OpeningRangeBreaKout", results, format_signal)
        print(f"TXT actualizado: {output_path} ({output_count})")

        if not results:
            print("No hay señales Opening Range Breakout con los filtros actuales.")
        else:
            print("Señales Opening Range Breakout:")
            for signal in results:
                print(format_signal(signal))
