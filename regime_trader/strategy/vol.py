"""Volatility-aware challenger: volatility targeting / naive risk parity."""
from __future__ import annotations

import numpy as np

from ..data import features as F
from .base import Strategy, StrategyContext


class VolTargeting(Strategy):
    """Each sleeve sized so its risk contribution targets `target_vol`
    portfolio-wide (naive risk parity, ignoring correlations). Gross capped."""

    name = "vol-target"

    def __init__(self, target_vol: float = 0.10, vol_window: int = 21, max_gross: float = 1.3):
        self.target_vol, self.vol_window, self.max_gross = target_vol, vol_window, max_gross

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        weights = {}
        n = len(ctx.symbols)
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.vol_window + 2:
                continue
            v = F.realized_vol(c, self.vol_window).iloc[-1]
            if np.isfinite(v) and v > 0:
                weights[s] = (self.target_vol / n) / v
        gross = sum(weights.values())
        if gross > self.max_gross:
            scale = self.max_gross / gross
            weights = {s: w * scale for s, w in weights.items()}
        return weights
