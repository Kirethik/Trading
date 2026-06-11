"""Market-breadth overlay [optional challenger]: a risk filter, not a signal.

Wraps any base strategy and scales its weights down when participation is
narrow (few universe members above their 200-SMA) — breadth deterioration
tends to lead volatility-regime transitions.
"""
from __future__ import annotations

from ..data import features as F
from .base import Strategy, StrategyContext


class BreadthOverlay(Strategy):
    def __init__(self, base: Strategy, trend_window: int = 200,
                 weak_breadth: float = 0.40, weak_scale: float = 0.5,
                 critical_breadth: float = 0.25, critical_scale: float = 0.25):
        self.base = base
        self.trend_window = trend_window
        self.weak_breadth, self.weak_scale = weak_breadth, weak_scale
        self.critical_breadth, self.critical_scale = critical_breadth, critical_scale
        self.name = f"{base.name}+breadth"

    def breadth(self, ctx: StrategyContext) -> float:
        above = total = 0
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.trend_window:
                continue
            total += 1
            if c.iloc[-1] > F.sma(c, self.trend_window).iloc[-1]:
                above += 1
        return above / total if total else 1.0

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        weights = self.base.target_weights(ctx)
        b = self.breadth(ctx)
        if b < self.critical_breadth:
            scale = self.critical_scale
        elif b < self.weak_breadth:
            scale = self.weak_scale
        else:
            scale = 1.0
        return {s: w * scale for s, w in weights.items()}
