from __future__ import annotations

from models import StrategySignal, TickerData
from strategy_rules.acumulacion import accumulation_signal
from strategy_rules.base import StrategyRule


class AcumulaMetalesStrategy(StrategyRule):
    key = "acumula_metales"
    name = "Acumula Metales"
    metals = {
        "GLD", "SLV", "IAU", "GDX", "GDXJ", "SIL", "SILJ", "PPLT", "PALL", "COPX",
        "NEM", "GOLD", "AEM", "WPM", "FNV", "RGLD", "PAAS", "AG", "HL", "CDE",
        "EXK", "FSM", "FCX", "SCCO", "PICK", "XME",
    }

    def analyze(self, ticker: TickerData, config: dict) -> StrategySignal | None:
        allowed = set(config.get("metals_symbols", [])) or self.metals
        if ticker.symbol not in allowed:
            return None
        rsi_limit = float(config.get("metals_rsi_max", 40))
        return accumulation_signal(
            self.name,
            ticker,
            config,
            f"Metales por debajo de SMA180 diaria y SMA120 semanal con RSI14 menor que {rsi_limit:g}.",
            rsi_limit=rsi_limit,
        )
