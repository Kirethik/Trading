"""Factor / allocation challengers: dual momentum and TAA rotation."""
from __future__ import annotations

import numpy as np

from ..data import features as F
from .base import Strategy, StrategyContext, equal_weights


class DualMomentum(Strategy):
    """Antonacci-style GEM: relative momentum picks the strongest risk asset,
    absolute momentum decides risk-on vs defensive (bonds/cash)."""

    name = "dual-momentum"

    def __init__(self, risk_assets: list[str] | None = None, defensive: str = "TLT", lookback: int = 252):
        self.risk_assets = risk_assets or ["SPY", "QQQ", "EFA"]
        self.defensive = defensive
        self.lookback = lookback

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        moms = {}
        for s in self.risk_assets:
            c = ctx.closes.get(s)
            if c is None or len(c.dropna()) < self.lookback + 1:
                continue
            m = F.momentum(c.dropna(), self.lookback).iloc[-1]
            if np.isfinite(m):
                moms[s] = m
        if not moms:
            return {}
        best = max(moms, key=moms.get)
        if moms[best] > 0:  # absolute momentum gate
            return {best: 1.0}
        if self.defensive in ctx.closes.columns:
            d = ctx.closes[self.defensive].dropna()
            if len(d) >= self.lookback + 1 and F.momentum(d, self.lookback).iloc[-1] > 0:
                return {self.defensive: 1.0}
        return {}  # cash


class TAARotation(Strategy):
    """Tactical rotation: blended 1/3/6/12-month momentum across an ETF set,
    hold the top-N that are above their 200-SMA with positive blend."""

    name = "taa-rotation"

    def __init__(self, top_n: int = 3, trend_window: int = 200):
        self.top_n, self.trend_window = top_n, trend_window
        self.lookbacks = (21, 63, 126, 252)

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        scores = {}
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < max(self.lookbacks) + 1 or len(c) < self.trend_window:
                continue
            blend = np.nanmean([F.momentum(c, lb).iloc[-1] for lb in self.lookbacks])
            above_trend = c.iloc[-1] > F.sma(c, self.trend_window).iloc[-1]
            if np.isfinite(blend) and blend > 0 and above_trend:
                scores[s] = blend
        top = sorted(scores, key=scores.get, reverse=True)[: self.top_n]
        return equal_weights(top, gross=len(top) / self.top_n)
