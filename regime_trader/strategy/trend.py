"""Trend / momentum challengers — all long-only or long/flat."""
from __future__ import annotations

import numpy as np

from ..data import features as F
from .base import (
    Strategy,
    StrategyContext,
    equal_weights,
    in_position_last_signal,
    inverse_vol_weights,
)


class TimeSeriesMomentum(Strategy):
    """12-1 absolute momentum: long each symbol whose own past year (skipping
    the most recent month) is positive. Gross scales with breadth of trends."""

    name = "tsmom-12-1"

    def __init__(self, lookback: int = 252, skip: int = 21):
        self.lookback, self.skip = lookback, skip

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        winners = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None or len(c.dropna()) < self.lookback + 1:
                continue
            m = F.momentum(c.dropna(), self.lookback, self.skip).iloc[-1]
            if np.isfinite(m) and m > 0:
                winners.append(s)
        if not winners:
            return {}
        iv = inverse_vol_weights(ctx.closes, winners)
        gross = len(winners) / max(1, len(ctx.symbols))  # natural de-risking
        return {s: w * gross for s, w in iv.items()}


class CrossSectionalMomentum(Strategy):
    """Long the top-k symbols by 12-1 momentum, provided momentum is positive."""

    name = "xs-momentum"

    def __init__(self, top_k: int = 3, lookback: int = 252, skip: int = 21):
        self.top_k, self.lookback, self.skip = top_k, lookback, skip

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        scores = {}
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None or len(c.dropna()) < self.lookback + 1:
                continue
            m = F.momentum(c.dropna(), self.lookback, self.skip).iloc[-1]
            if np.isfinite(m) and m > 0:
                scores[s] = m
        top = sorted(scores, key=scores.get, reverse=True)[: self.top_k]
        return equal_weights(top, gross=len(top) / self.top_k)


class SMATrendFilter(Strategy):
    """Dual moving-average / 200-SMA filter: hold what's above its long SMA."""

    name = "sma200-trend"

    def __init__(self, window: int = 200):
        self.window = window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        holds = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) >= self.window and c.iloc[-1] > F.sma(c, self.window).iloc[-1]:
                holds.append(s)
        return equal_weights(holds, gross=len(holds) / max(1, len(ctx.symbols)))


class DonchianBreakout(Strategy):
    """Turtle-style: enter on a 55-bar high breakout, exit on a 20-bar low."""

    name = "donchian-55-20"

    def __init__(self, entry_window: int = 55, exit_window: int = 20):
        self.entry_window, self.exit_window = entry_window, exit_window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        open_pos = []
        for s in ctx.symbols:
            df = ctx.bars.get(s)
            if df is None or len(df) < self.entry_window + 2:
                continue
            ch = F.donchian(df, self.entry_window, self.exit_window)
            entry = df["close"] > ch["entry_high"]
            exit_ = df["close"] < ch["exit_low"]
            if in_position_last_signal(entry, exit_):
                open_pos.append(s)
        # fixed slice per open position keeps gross proportional to breadth
        return {s: 1.0 / max(1, len(ctx.symbols)) for s in open_pos}
