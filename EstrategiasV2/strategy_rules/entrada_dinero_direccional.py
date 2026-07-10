from __future__ import annotations

from models import StrategySignal, TickerData
from strategy_rules.base import StrategyRule, metric
from strategy_rules.common import base_metrics, pct_stop, pct_target


EXCLUDED_SYMBOLS = {
    "TQQQ",
    "SQQQ",
    "NVDL",
    "NVD",
    "TSLL",
    "TSLG",
    "TSLQ",
    "AMDL",
    "MSFU",
    "AMZU",
    "AMZD",
    "GGLL",
    "PLTU",
    "METU",
    "OKLL",
    "XNDU",
    "MUU",
    "CONL",
    "TSDD",
    "IBIT",
    "ETHA",
    "MSTR",
    "COIN",
    "MARA",
    "RIOT",
    "CLSK",
    "HUT",
    "WULF",
    "CIFR",
    "CORZ",
    "IREN",
    "BTDR",
    "HIVE",
    "GLXY",
    "QQQI",
    "QYLD",
    "JEPQ",
    "UFO",
    "QTUM",
    "AIQ",
    "GRID",
    "PDBC",
    "SHY",
    "VGSH",
    "VBIL",
    "VTIP",
    "ALAB",
    "NBIS",
    "CRWV",
    "PLTD",
    "USAR",
    "MDLN",
    "VISN",
    "XOVR",
}


class EntradaDineroDireccionalStrategy(StrategyRule):
    key = "entrada_dinero_direccional"
    name = "Entrada Dinero Direccional"

    def analyze(self, ticker: TickerData, config: dict) -> StrategySignal | None:
        symbol = ticker.symbol.upper()
        if symbol in EXCLUDED_SYMBOLS:
            return None

        price = metric(ticker, "price")
        avg_dollar_volume_20d = metric(ticker, "avg_dollar_volume_20d")
        money_in_ratio = metric(ticker, "dollar_volume_ma5_vs_ma120")
        sma20 = metric(ticker, "daily_sma20")
        sma50 = metric(ticker, "daily_sma50")
        return_5d = metric(ticker, "daily_return_5d_pct")
        liquidity_rank = metric(ticker, "avg_dollar_volume_20d_rank")
        money_rank = metric(ticker, "dollar_volume_ma5_vs_ma120_rank")

        required = [price, avg_dollar_volume_20d, money_in_ratio, sma20, sma50, return_5d, liquidity_rank, money_rank]
        if any(value is None for value in required):
            return None

        min_price = float(config.get("entrada_dinero_min_price", 2))
        min_dollar_volume = float(config.get("entrada_dinero_min_dollar_volume_20d", 5_000_000))
        top_liquidity = int(config.get("entrada_dinero_top_liquidity", 100))
        top_money = int(config.get("entrada_dinero_top_money", 20))
        top_final = int(config.get("entrada_dinero_top_final", 10))

        if price <= min_price:
            return None
        if avg_dollar_volume_20d < min_dollar_volume:
            return None
        if not (price > sma20 > sma50):
            return None
        if return_5d <= 0:
            return None
        if liquidity_rank > top_liquidity:
            return None
        if money_rank > min(top_money, top_final):
            return None

        score = round((top_final + 1 - min(money_rank, top_final)) * 10 + money_in_ratio * 5, 2)
        reason = (
            "Entrada de dinero con direccion alcista: liquidez alta, ratio 5D/120D destacado, "
            "precio sobre SMA20, SMA20 sobre SMA50 y rentabilidad 5D positiva."
        )
        metrics = base_metrics(
            ticker,
            "avg_dollar_volume_20d",
            "avg_dollar_volume_120d",
            "dollar_volume_ma5_vs_ma120",
            "avg_dollar_volume_20d_rank",
            "dollar_volume_ma5_vs_ma120_rank",
            "daily_sma20",
            "daily_sma50",
            "daily_return_5d_pct",
        )
        return StrategySignal(
            self.name,
            symbol,
            "LONG",
            price,
            pct_target(price, float(config.get("entrada_dinero_target_pct", 10))),
            pct_stop(price, float(config.get("entrada_dinero_stop_pct", 8))),
            score,
            reason,
            metrics,
        )
