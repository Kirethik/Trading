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
    """Dual moving-average / 200-SMA filter: hold what's above its long SMA.

    sizing_mode="risk" (default): equal-weight long/flat — legacy fast path.
    sizing_mode="vol_target": band hysteresis entry/exit + per-symbol
      volatility-target sizing so the portfolio deploys meaningful capital.
    """

    name = "sma200-trend"

    def __init__(
        self,
        window: int = 200,
        band: float = 0.0,
        target_vol: float = 0.0,
        vol_window: int = 21,
        sizing_mode: str = "risk",
        stop_atr_mult: float | None = None,
    ):
        self.window = window
        self.band = band
        self.target_vol = target_vol
        self.vol_window = vol_window
        self.sizing_mode = sizing_mode
        self.stop_atr_mult = stop_atr_mult

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        if self.sizing_mode == "vol_target":
            return self._vol_target_weights(ctx)
        # --- legacy risk path (default) ---
        holds = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            # mean of the last `window` closes == F.sma(c, window).iloc[-1] (the
            # guard guarantees a full window), but O(window) instead of rolling
            # the whole series every bar — the interactive backtest hot path.
            if len(c) >= self.window and c.iloc[-1] > c.iloc[-self.window:].mean():
                holds.append(s)
        return equal_weights(holds, gross=len(holds) / max(1, len(ctx.symbols)))

    def _vol_target_weights(self, ctx: StrategyContext) -> dict[str, float]:
        """Band-hysteresis entry/exit + per-symbol vol-target sizing."""
        out: dict[str, float] = {}
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.window:
                continue
            sma = c.iloc[-self.window:].mean()
            entry_sig = c > sma * (1.0 + self.band)
            exit_sig = c < sma * (1.0 - self.band)
            if not in_position_last_signal(entry_sig, exit_sig):
                continue
            rv = F.realized_vol(c, self.vol_window, annualize=True).iloc[-1]
            if not (np.isfinite(rv) and rv > 0):
                continue
            tv = self.target_vol if self.target_vol > 0 else 0.15
            out[s] = tv / rv
        return out


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
