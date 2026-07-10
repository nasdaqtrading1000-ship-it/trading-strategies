from pathlib import Path
import sqlite3


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "strategies.db"
SIGNALS_DIR = BASE_DIR / "Estrategias" / "salidas_txt"


STRATEGIES = [
    {
        "name": "Momentum",
        "description": "Compra activos con fuerza relativa alta, tendencia alcista y buen comportamiento frente al mercado. Ordena candidatos por score para priorizar los mas fuertes.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / swing",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_momentum",
        "signals_txt_name": "Momentum.txt",
    },
    {
        "name": "Swing Trading",
        "description": "Busca entradas de varios dias en activos con tendencia sana, retrocesos controlados y confirmacion tecnica para operaciones de corto-medio plazo.",
        "risk_level": "Medio",
        "signal_frequency": "Varias senales por semana",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_swing_trading",
        "signals_txt_name": "SwingTrading.txt",
    },
    {
        "name": "BreaKout",
        "description": "Detecta rupturas de resistencia con aumento de volumen, expansion de rango y precio cerca de maximos relevantes.",
        "risk_level": "Alto",
        "signal_frequency": "Segun rupturas de mercado",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_breakout",
        "signals_txt_name": "BreaKout.txt",
    },
    {
        "name": "Mean Reversion",
        "description": "Busca activos sobrevendidos o alejados de su media que puedan volver a niveles normales, filtrando por liquidez y confirmaciones.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_mean_reversion",
        "signals_txt_name": "Mean_Reversion.txt",
    },
    {
        "name": "Value Trading",
        "description": "Filtra companias con valoracion atractiva, fundamentales razonables y descuento relativo frente a sus metricas.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_value_trading",
        "signals_txt_name": "ValueTrading.txt",
    },
    {
        "name": "Dividend Growth",
        "description": "Selecciona companias con crecimiento de dividendos, estabilidad financiera y perfil defensivo para inversion de largo plazo.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_dividend_growth",
        "signals_txt_name": "DividenGrowth.txt",
    },
    {
        "name": "Trend Following",
        "description": "Sigue tendencias establecidas mediante medias, momentum y confirmacion de precio para mantenerse en activos fuertes.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_trend_following",
        "signals_txt_name": "TrendFollowing.txt",
    },
    {
        "name": "Pairs Trading",
        "description": "Analiza pares correlacionados y busca desviaciones estadisticas para operar convergencia entre dos activos.",
        "risk_level": "Medio",
        "signal_frequency": "Variable",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_pairs_trading",
        "signals_txt_name": "PairsTrading.txt",
    },
    {
        "name": "Sector Rotation",
        "description": "Compara fuerza relativa por sectores y propone activos lideres dentro de los sectores con mejor comportamiento.",
        "risk_level": "Medio",
        "signal_frequency": "Semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_sector_rotation",
        "signals_txt_name": "SectorRotation.txt",
    },
    {
        "name": "Quality Investing",
        "description": "Busca empresas de calidad con buenos margenes, crecimiento, estabilidad financiera y comportamiento tecnico aceptable.",
        "risk_level": "Bajo",
        "signal_frequency": "Baja / semanal",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_quality_investing",
        "signals_txt_name": "QualityInvesting.txt",
    },
    {
        "name": "Opening Range BreaKout",
        "description": "Estrategia intradia que espera la ruptura del rango inicial de la sesion con volumen y direccion clara.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_opening_range_breakout",
        "signals_txt_name": "OpeningRangeBreaKout.txt",
    },
    {
        "name": "VWAP Reversion",
        "description": "Busca reversiones intradia hacia VWAP cuando el precio se aleja demasiado y aparecen senales de agotamiento.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_vwap_reversion",
        "signals_txt_name": "VWAP_Reversion.txt",
    },
    {
        "name": "Momentum Intradia",
        "description": "Detecta movimientos fuertes dentro de la sesion usando momentum reciente, VWAP, volumen relativo y gestion de riesgo.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_momentum_intradia",
        "signals_txt_name": "MomentumIntradia.txt",
    },
    {
        "name": "Scalping The PullBacks",
        "description": "Busca pequenos retrocesos dentro de una tendencia intradia para entrar a favor del movimiento principal.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / frecuente",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_scalping_pullbacks",
        "signals_txt_name": "ScalpingThePullBacKs.txt",
    },
    {
        "name": "Gap and Go",
        "description": "Detecta activos que abren con gap relevante y continuan en la direccion del impulso tras romper el rango inicial.",
        "risk_level": "Alto",
        "signal_frequency": "Intradia / apertura",
        "historical_return": "Pendiente de backtest",
        "telegram_url": "https://t.me/tu_canal_gap_and_go",
        "signals_txt_name": "Gap_and_Go.txt",
    },
    {
        "name": "Entrada Dinero Direccional",
        "description": "Busca activos liquidos con entrada fuerte de dinero frente a 120 dias y direccion alcista: precio sobre SMA20, SMA20 sobre SMA50 y rentabilidad 5D positiva.",
        "risk_level": "Medio",
        "signal_frequency": "Diaria / rotacion",
        "historical_return": "Pendiente de seguimiento",
        "telegram_url": "https://t.me/tu_canal_entrada_dinero_direccional",
        "signals_txt_name": "Entrada_Dinero_Direccional.txt",
    },
]


def is_custom_telegram(url):
    if not url:
        return False
    generic_bits = ("tu_canal", "crypto", "swing")
    return not any(bit in url for bit in generic_bits)


def main():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    for strategy in STRATEGIES:
        (SIGNALS_DIR / strategy["signals_txt_name"]).touch(exist_ok=True)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    existing_rows = connection.execute("SELECT * FROM strategies").fetchall()
    existing_by_name = {row["name"]: row for row in existing_rows}
    wanted_names = {strategy["name"] for strategy in STRATEGIES}

    for strategy in STRATEGIES:
        existing = existing_by_name.get(strategy["name"])
        telegram_url = strategy["telegram_url"]
        if existing and is_custom_telegram(existing["telegram_url"]):
            telegram_url = existing["telegram_url"]

        payload = {
            **strategy,
            "telegram_url": telegram_url,
            "is_active": 1,
        }

        if existing:
            connection.execute(
                """
                UPDATE strategies
                SET description = :description,
                    risk_level = :risk_level,
                    signal_frequency = :signal_frequency,
                    historical_return = :historical_return,
                    telegram_url = :telegram_url,
                    signals_txt_name = :signals_txt_name,
                    is_active = :is_active
                WHERE name = :name
                """,
                payload,
            )
        else:
            connection.execute(
                """
                INSERT INTO strategies
                (name, description, risk_level, signal_frequency,
                 historical_return, telegram_url, signals_txt_name, is_active)
                VALUES (:name, :description, :risk_level, :signal_frequency,
                        :historical_return, :telegram_url, :signals_txt_name, :is_active)
                """,
                payload,
            )

    connection.execute(
        "DELETE FROM strategies WHERE name NOT IN ({})".format(
            ",".join("?" for _ in wanted_names)
        ),
        tuple(wanted_names),
    )
    connection.commit()
    connection.close()
    print(f"Estrategias cargadas: {len(STRATEGIES)}")


if __name__ == "__main__":
    main()
