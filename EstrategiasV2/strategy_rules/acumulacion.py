from __future__ import annotations

from models import StrategySignal, TickerData
from strategy_rules.base import StrategyRule, metric
from strategy_rules.common import base_metrics, liquid


class AcumulacionStrategy(StrategyRule):
    key = "acumulacion"
    name = "Acumulacion"

    def analyze(self, ticker: TickerData, config: dict) -> StrategySignal | None:
        rsi_limit = float(config.get("accumulation_rsi_max", 30))
        return accumulation_signal(self.name, ticker, config, "Acumulacion de activo castigado bajo medias largas y RSI14 bajo.", rsi_limit=rsi_limit)


def accumulation_signal(name: str, ticker: TickerData, config: dict, reason: str, rsi_limit: float = 30) -> StrategySignal | None:
    price = metric(ticker, "price")
    daily_sma180 = metric(ticker, "daily_sma180")
    weekly_sma120 = metric(ticker, "weekly_sma120")
    rsi14 = metric(ticker, "daily_rsi14")
    if None in [price, daily_sma180, weekly_sma120, rsi14] or not liquid(ticker, config):
        return None
    if not (price < daily_sma180 and price < weekly_sma120 and rsi14 < rsi_limit):
        return None
    score = (rsi_limit - rsi14) * 2 + abs(metric(ticker, "distance_daily_sma180_pct") or 0)
    return StrategySignal(name, ticker.symbol, "LONG", price, None, None, round(score, 2), reason, base_metrics(ticker, "daily_sma180", "weekly_sma120", "daily_rsi14", "distance_daily_sma180_pct", "distance_weekly_sma120_pct"))
