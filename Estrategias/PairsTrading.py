"""
Estrategia Pairs Trading.

Objetivo:
- Buscar dos activos relacionados que históricamente se mueven parecido.
- Detectar cuándo uno se aleja demasiado del otro.
- Apostar a que la relación vuelve a su media.

Este script NO compra ni vende.
Solo analiza y muestra señales.

Idea básica:
- Si el spread está demasiado alto:
    vender el activo A y comprar el activo B.
- Si el spread está demasiado bajo:
    comprar el activo A y vender el activo B.
"""

import os
from env_loader import load_env
load_env()
from txt_output import write_results_to_txt
from datetime import datetime, timedelta, UTC

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca_request import get_stock_bars_data
from alpaca.data.timeframe import TimeFrame


# Archivo de pares.
# Formato por línea:
# KO,PEP
# V,MA
# XOM,CVX
PAIRS_FILE = "pairs.txt"

# Días de histórico.
LOOKBACK_DAYS = 180

# Ventana para calcular media y desviación del spread.
SPREAD_WINDOW = 60

# Z-score mínimo para generar señal.
# 2.0 significa que el spread está a 2 desviaciones estándar.
ENTRY_ZSCORE = 2.0

# Z-score para salida teórica.
# Cuando vuelve cerca de 0, el spread se ha normalizado.
EXIT_ZSCORE = 0.3

# Correlación mínima entre los dos activos.
MIN_CORRELATION = 0.70

# Volumen monetario mínimo por activo.
MIN_AVG_DOLLAR_VOLUME = 10_000_000

# Máximo de señales.
TOP_N = 20

# Alpaca.
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]


def load_pairs(path):
    """
    Lee pares desde archivo de texto.

    Ejemplo de pairs.txt:
    KO,PEP
    V,MA
    XOM,CVX

    Ignora líneas vacías y comentarios.
    """
    pairs = []

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            left, right = line.split(",", 1)

            symbol_a = left.strip().upper()
            symbol_b = right.strip().upper()

            if symbol_a and symbol_b:
                pairs.append((symbol_a, symbol_b))

    return pairs


def unique_symbols_from_pairs(pairs):
    """
    Extrae todos los símbolos únicos de la lista de pares.
    """
    symbols = set()

    for symbol_a, symbol_b in pairs:
        symbols.add(symbol_a)
        symbols.add(symbol_b)

    return sorted(symbols)


def get_daily_bars(client, symbols):
    """
    Descarga velas diarias desde Alpaca para todos los símbolos.
    """
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
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
            df.set_index("timestamp", inplace=True)
            data[symbol] = df

    return data


def average_dollar_volume(df, window=20):
    """
    Calcula volumen monetario medio.
    """
    recent = df.tail(window)
    dollar_volume = recent["close"] * recent["volume"]
    return float(dollar_volume.mean())


def align_pair_data(df_a, df_b):
    """
    Une dos DataFrames por fecha para que tengan las mismas sesiones.

    Devuelve un DataFrame con:
    close_a, close_b, volume_a, volume_b
    """
    data = pd.DataFrame(
        {
            "close_a": df_a["close"],
            "close_b": df_b["close"],
            "volume_a": df_a["volume"],
            "volume_b": df_b["volume"],
        }
    )

    return data.dropna()


def analyze_pair(symbol_a, symbol_b, df_a, df_b):
    """
    Analiza un par.

    Pasos:
    - Alinear datos.
    - Calcular retornos y correlación.
    - Calcular hedge ratio simple.
    - Calcular spread.
    - Calcular z-score.
    - Generar señal si el z-score es extremo.
    """
    data = align_pair_data(df_a, df_b)

    if len(data) < SPREAD_WINDOW + 5:
        return None

    avg_dollar_volume_a = average_dollar_volume(df_a, 20)
    avg_dollar_volume_b = average_dollar_volume(df_b, 20)

    liquidity_ok = (
        avg_dollar_volume_a >= MIN_AVG_DOLLAR_VOLUME
        and avg_dollar_volume_b >= MIN_AVG_DOLLAR_VOLUME
    )

    if not liquidity_ok:
        return None

    # Retornos diarios.
    returns_a = data["close_a"].pct_change()
    returns_b = data["close_b"].pct_change()

    correlation = returns_a.tail(SPREAD_WINDOW).corr(
        returns_b.tail(SPREAD_WINDOW)
    )

    if correlation is None or correlation < MIN_CORRELATION:
        return None

    # Hedge ratio simple:
    # aproximamos cuántas unidades de B equivalen a A.
    #
    # Para una versión profesional se usaría regresión lineal:
    # close_a = alpha + beta * close_b.
    hedge_ratio = (
        data["close_a"].tail(SPREAD_WINDOW).mean()
        / data["close_b"].tail(SPREAD_WINDOW).mean()
    )

    if hedge_ratio <= 0:
        return None

    # Spread:
    # precio de A menos precio de B ajustado por hedge ratio.
    data["spread"] = data["close_a"] - hedge_ratio * data["close_b"]

    spread_mean = data["spread"].tail(SPREAD_WINDOW).mean()
    spread_std = data["spread"].tail(SPREAD_WINDOW).std()

    if spread_std <= 0:
        return None

    current_spread = float(data["spread"].iloc[-1])
    zscore = (current_spread - spread_mean) / spread_std

    price_a = float(data["close_a"].iloc[-1])
    price_b = float(data["close_b"].iloc[-1])

    # Si zscore alto:
    # A está caro respecto a B.
    # Señal: vender A, comprar B.
    if zscore >= ENTRY_ZSCORE:
        action = f"SHORT {symbol_a} / LONG {symbol_b}"
        direction = "SHORT"
        target_zscore = EXIT_ZSCORE
        stop_zscore = ENTRY_ZSCORE + 1.0
    # Si zscore bajo:
    # A está barato respecto a B.
    # Señal: comprar A, vender B.
    elif zscore <= -ENTRY_ZSCORE:
        action = f"LONG {symbol_a} / SHORT {symbol_b}"
        direction = "LONG"
        target_zscore = -EXIT_ZSCORE
        stop_zscore = -(ENTRY_ZSCORE + 1.0)
    else:
        return None

    target_spread = spread_mean + target_zscore * spread_std
    stop_spread = spread_mean + stop_zscore * spread_std
    target_price_a = target_spread + hedge_ratio * price_b
    stop_price_a = stop_spread + hedge_ratio * price_b

    # Score:
    # cuanto más extremo el zscore y mayor correlación, más interesante.
    score = abs(zscore) * 0.7 + correlation * 0.3

    return {
        "symbol_a": symbol_a,
        "symbol_b": symbol_b,
        "price_a": price_a,
        "price_b": price_b,
        "hedge_ratio": hedge_ratio,
        "correlation": correlation,
        "current_spread": current_spread,
        "spread_mean": spread_mean,
        "spread_std": spread_std,
        "zscore": zscore,
        "direction": direction,
        "action": action,
        "exit_zscore": EXIT_ZSCORE,
        "target_price_a": target_price_a,
        "stop_price_a": stop_price_a,
        "target_zscore": target_zscore,
        "stop_zscore": stop_zscore,
        "avg_dollar_volume_a": avg_dollar_volume_a,
        "avg_dollar_volume_b": avg_dollar_volume_b,
        "score": score,
    }


def find_pairs_trading_signals():
    """
    Función principal.

    1. Lee pares.
    2. Descarga datos.
    3. Analiza cada par.
    4. Ordena por score.
    """
    pairs = load_pairs(PAIRS_FILE)
    symbols = unique_symbols_from_pairs(pairs)

    client = StockHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
    )

    data = get_daily_bars(client, symbols)

    signals = []

    for symbol_a, symbol_b in pairs:
        df_a = data.get(symbol_a)
        df_b = data.get(symbol_b)

        if df_a is None or df_b is None:
            continue

        result = analyze_pair(symbol_a, symbol_b, df_a, df_b)

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
    Formatea una señal para imprimir o enviar por Telegram.
    """
    return (
        f"{signal['symbol_a']}/{signal['symbol_b']} | "
        f"Direccion: {signal['direction']} | "
        f"Operativa par: {signal['action']} | "
        f"Precio actual: {signal['price_a']:.2f}/{signal['price_b']:.2f} | "
        f"Apertura: {signal['price_a']:.2f}/{signal['price_b']:.2f} | "
        f"Cierre: {signal['target_price_a']:.2f} | "
        f"Stop Loss: {signal['stop_price_a']:.2f} | "
        f"ZScore: {signal['zscore']:.2f} | "
        f"Corr: {signal['correlation']:.2f} | "
        f"Hedge: {signal['hedge_ratio']:.2f} | "
        f"ZScore objetivo: {signal['target_zscore']:.2f} | "
        f"ZScore stop: {signal['stop_zscore']:.2f} | "
        f"Salida teorica: ZScore cerca de ±{signal['exit_zscore']} | "
        f"Score: {signal['score']:.2f}"
    )


if __name__ == "__main__":
    results = find_pairs_trading_signals()
    output_path, output_count = write_results_to_txt("PairsTrading", results, format_signal)
    print(f"TXT actualizado: {output_path} ({output_count})")

    if not results:
        print("No hay señales Pairs Trading con los filtros actuales.")
    else:
        print("Señales Pairs Trading:")
        for signal in results:
            print(format_signal(signal))
