"""Strategy interface shared by backtest and live trading.

A strategy receives a StrategyContext whose data is already sliced up to the
current bar (inclusive) by the caller — strategies physically cannot see the
future. It returns target portfolio weights (fraction of equity per symbol).

All strategies are long-only or long/flat: negative weights are clipped.
Leverage and exposure limits are the risk layer's job, not the strategy's.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..data import features as F
from ..regime.hmm import RegimeView
from ..regime.stability import StabilityState


@dataclass
class StrategyContext:
    bars: dict[str, pd.DataFrame]          # symbol -> OHLCV up to and including now
    closes: pd.DataFrame                   # aligned closes up to and including now
    symbols: list[str]                     # tradable universe for this strategy
    market_proxy: str = "SPY"
    regime: Optional[RegimeView] = None    # None for regime-blind challengers
    stability: Optional[StabilityState] = None
    extras: dict = field(default_factory=dict)

    @property
    def now(self) -> pd.Timestamp:
        return self.closes.index[-1]


class Strategy(ABC):
    name: str = "base"
    sizing_mode: str = "risk"           # "risk" | "vol_target"
    target_vol: float = 0.0             # annualised vol target (0 = inactive)
    stop_atr_mult: float | None = None  # None = use RiskManager global default

    @abstractmethod
    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        ...

    def target_weights(self, ctx: StrategyContext) -> dict[str, float]:
        """Public entry: enforces the always-long, never-short invariant."""
        weights = self.compute_weights(ctx)
        return {s: max(0.0, float(w)) for s, w in weights.items() if np.isfinite(w) and w > 0}


def inverse_vol_weights(closes: pd.DataFrame, symbols: list[str], window: int = 21) -> dict[str, float]:
    """Risk-balanced weights summing to 1 across symbols with enough history."""
    vols = {}
    for s in symbols:
        if s not in closes.columns:
            continue
        v = F.realized_vol(closes[s].dropna(), window).iloc[-1] if len(closes[s].dropna()) > window else np.nan
        if np.isfinite(v) and v > 0:
            vols[s] = v
    if not vols:
        return {}
    inv = {s: 1.0 / v for s, v in vols.items()}
    total = sum(inv.values())
    return {s: w / total for s, w in inv.items()}


def equal_weights(symbols: list[str], gross: float = 1.0) -> dict[str, float]:
    if not symbols:
        return {}
    w = gross / len(symbols)
    return {s: w for s in symbols}


def in_position_last_signal(entry: pd.Series, exit_: pd.Series) -> bool:
    """Stateless long/flat logic: are we long now, given boolean entry/exit
    signal series? The most recent signal (entry vs exit) wins."""
    e = entry[entry.fillna(False)].index
    x = exit_[exit_.fillna(False)].index
    if len(e) == 0:
        return False
    if len(x) == 0:
        return True
    return e[-1] > x[-1]
