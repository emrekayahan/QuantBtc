"""K2 Strategy Set v2 registry (all new strategies default to shadow; legacy suspended)."""
from engine.strategies.base import STRATEGY_INITIAL_STATUS, StrategySignal
from engine.strategies.bear_breakdown_short import score as score_bear_breakdown_short
from engine.strategies.range_fade import score as score_range_fade
from engine.strategies.trend_pullback import score as score_trend_pullback
from engine.strategies.vol_breakout import score as score_vol_breakout

STRATEGY_SCORERS = {
    "trend_pullback": score_trend_pullback,
    "vol_breakout": score_vol_breakout,
    "range_fade": score_range_fade,
    "bear_breakdown_short": score_bear_breakdown_short,
}

__all__ = ["STRATEGY_INITIAL_STATUS", "STRATEGY_SCORERS", "StrategySignal"]
