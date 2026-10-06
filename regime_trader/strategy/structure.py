"""Mechanical price-action: market structure + supply/demand pullback.

A faithful, *testable* approximation of the popular "market structure + supply &
demand" discretionary method (e.g. TradingLab's "The Only Strategy You'll Ever
Need"). The discretionary original is drawn by eye and therefore cannot be
honestly backtested; this codifies its falsifiable skeleton so it can:

  1. Market structure  — trend from CONFIRMED swing highs/lows. A swing high at
     bar i is a local max over +/-k bars; it is only *known* k bars later, so the
     signal is strictly causal (no peeking at the future). Uptrend = higher highs
     AND higher lows; downtrend = lower highs AND lower lows.
  2. Demand zone       — in an uptrend, the most recent confirmed swing low is the
     demand zone (support); the most recent confirmed swing high is the target.
  3. Pullback entry    — go long when price pulls back INTO the demand zone (within
     zone_tol above support) and the reward:risk to the prior swing high clears
     min_rr. Stop sits stop_buf below the zone; exit on stop, target, or a trend
     flip to down. Long/flat only, stateless via last-signal logic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, StrategyContext, in_position_last_signal


def _swing_indices(highs: np.ndarray, lows: np.ndarray, k: int) -> tuple[list[int], list[int]]:
    """Fractal swing points: index i (k <= i <= n-1-k) is a swing high if highs[i]
    is the max of [i-k, i+k], a swing low if lows[i] is the min. Confirmed only
    once the k trailing bars exist, so callers treat a swing as 'known' at i+k."""
    n = len(highs)
    sh, sl = [], []
    for i in range(k, n - k):
        if highs[i] == highs[i - k : i + k + 1].max():
            sh.append(i)
        if lows[i] == lows[i - k : i + k + 1].min():
            sl.append(i)
    return sh, sl


def _last_prev_timeline(idx_list: list[int], prices: np.ndarray, k: int, n: int) -> tuple[np.ndarray, np.ndarray]:
    """For each bar t, the most-recent and second-most-recent swing price KNOWN by
    t (a swing at index si becomes known at si+k). O(n) one-pass merge."""
    last = np.full(n, np.nan)
    prev = np.full(n, np.nan)
    events = [(si + k, float(prices[si])) for si in idx_list]  # (known_at_bar, price)
    cur_last = np.nan
    cur_prev = np.nan
    ei = 0
    for t in range(n):
        while ei < len(events) and events[ei][0] <= t:
            cur_prev = cur_last
            cur_last = events[ei][1]
            ei += 1
        last[t] = cur_last
        prev[t] = cur_prev
    return last, prev


class StructureZonePullback(Strategy):
    """Trend (market structure) + demand-zone pullback entry with an R:R filter."""

    name = "structure-zone-pullback"

    def __init__(
        self,
        swing_k: int = 3,
        zone_tol: float = 0.03,
        stop_buf: float = 0.01,
        min_rr: float = 2.5,
    ):
        self.swing_k = swing_k        # fractal half-width for swing detection
        self.zone_tol = zone_tol      # how close above support still counts as "in the zone"
        self.stop_buf = stop_buf      # stop placed this fraction below the demand zone
        self.min_rr = min_rr          # minimum reward:risk to the prior swing high

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in ctx.symbols:
            df = ctx.bars.get(s)
            if df is None or len(df) < self.swing_k * 2 + 5:
                continue
            if self._is_long(df):
                out[s] = 1.0 / max(1, len(ctx.symbols))
        return out

    def _is_long(self, df: pd.DataFrame) -> bool:
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        n = len(close)
        k = self.swing_k

        sh_idx, sl_idx = _swing_indices(highs, lows, k)
        if len(sh_idx) < 2 or len(sl_idx) < 2:
            return False

        last_sh, prev_sh = _last_prev_timeline(sh_idx, highs, k, n)
        last_sl, prev_sl = _last_prev_timeline(sl_idx, lows, k, n)

        uptrend = (last_sh > prev_sh) & (last_sl > prev_sl)
        downtrend = (last_sh < prev_sh) & (last_sl < prev_sl)

        support = last_sl                       # demand zone (most recent swing low)
        resistance = last_sh                    # target (most recent swing high)
        stop = support * (1.0 - self.stop_buf)
        with np.errstate(invalid="ignore", divide="ignore"):
            rr = (resistance - close) / np.maximum(close - stop, 1e-9)
            at_zone = (close <= support * (1.0 + self.zone_tol)) & (close > stop)
            entry_arr = uptrend & at_zone & (rr >= self.min_rr)
            exit_arr = (close < stop) | (close >= resistance) | downtrend

        entry = pd.Series(np.nan_to_num(entry_arr, nan=0.0).astype(bool), index=df.index)
        exit_ = pd.Series(np.nan_to_num(exit_arr, nan=0.0).astype(bool), index=df.index)
        return in_position_last_signal(entry, exit_)
