from strategy_rules.acumulacion import AcumulacionStrategy
from strategy_rules.acumula_metales import AcumulaMetalesStrategy
from strategy_rules.breakout import BreakoutStrategy
from strategy_rules.dividend_growth import DividendGrowthStrategy
from strategy_rules.extension_reversal import ExtensionReversalStrategy
from strategy_rules.entrada_dinero_direccional import EntradaDineroDireccionalStrategy
from strategy_rules.follow_the_money import FollowTheMoneyStrategy
from strategy_rules.gap_and_go import GapAndGoStrategy
from strategy_rules.mean_reversion import MeanReversionStrategy
from strategy_rules.momentum import MomentumStrategy
from strategy_rules.momentum_intradia import MomentumIntradiaStrategy
from strategy_rules.opening_range_breakout import OpeningRangeBreakoutStrategy
from strategy_rules.quality_investing import QualityInvestingStrategy
from strategy_rules.scalping_pullbacks import ScalpingPullbacksStrategy
from strategy_rules.sector_rotation import SectorRotationStrategy
from strategy_rules.swing_trading import SwingTradingStrategy
from strategy_rules.trend_following import TrendFollowingStrategy
from strategy_rules.value_trading import ValueTradingStrategy
from strategy_rules.vwap_reversion import VwapReversionStrategy


STRATEGY_REGISTRY = {
    "momentum": MomentumStrategy(),
    "swing_trading": SwingTradingStrategy(),
    "breakout": BreakoutStrategy(),
    "mean_reversion": MeanReversionStrategy(),
    "value_trading": ValueTradingStrategy(),
    "dividend_growth": DividendGrowthStrategy(),
    "trend_following": TrendFollowingStrategy(),
    "sector_rotation": SectorRotationStrategy(),
    "quality_investing": QualityInvestingStrategy(),
    "opening_range_breakout": OpeningRangeBreakoutStrategy(),
    "vwap_reversion": VwapReversionStrategy(),
    "momentum_intradia": MomentumIntradiaStrategy(),
    "scalping_pullbacks": ScalpingPullbacksStrategy(),
    "gap_and_go": GapAndGoStrategy(),
    "follow_the_money": FollowTheMoneyStrategy(),
    "entrada_dinero_direccional": EntradaDineroDireccionalStrategy(),
    "acumula_metales": AcumulaMetalesStrategy(),
    "acumulacion": AcumulacionStrategy(),
    "extension_reversal": ExtensionReversalStrategy(),
}
