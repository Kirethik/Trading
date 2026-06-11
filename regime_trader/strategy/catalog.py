"""Registry of every head-to-head strategy for the §5 bracket."""
from __future__ import annotations

from ..config import get_config
from .allocation import DualMomentum, TAARotation
from .base import Strategy
from .breadth import BreadthOverlay
from .meanrev import BollingerReversion, RSI2Pullback
from .regime_strategy import RegimeStrategy
from .trend import CrossSectionalMomentum, DonchianBreakout, SMATrendFilter, TimeSeriesMomentum
from .vol import VolTargeting


def build_challengers() -> list[Strategy]:
    """Everything that competes in the bracket, incumbent included."""
    return [
        RegimeStrategy(),                       # the incumbent
        TimeSeriesMomentum(),
        CrossSectionalMomentum(),
        SMATrendFilter(),
        DonchianBreakout(),
        RSI2Pullback(),
        BollingerReversion(),
        VolTargeting(),
        DualMomentum(),
        TAARotation(),
        BreadthOverlay(SMATrendFilter()),       # optional breadth risk filter
    ]


def strategy_universe(strategy: Strategy) -> list[str]:
    """Which symbols a strategy trades (TAA/dual-momentum use the ETF set)."""
    cfg = get_config().universe
    if strategy.name in ("taa-rotation", "dual-momentum"):
        return list(cfg.etf_rotation)
    return list(cfg.equities)


def all_required_symbols() -> list[str]:
    cfg = get_config().universe
    return sorted(set(cfg.equities) | set(cfg.etf_rotation) | {cfg.market_proxy})
