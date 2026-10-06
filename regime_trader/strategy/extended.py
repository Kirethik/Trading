"""Extended trend / breakout / multi-factor challengers for ETH research.

New families built on the causal indicators added to data.features: EMA and MACD
trend, ADX-gated trend, Keltner (ATR) channel breakout, and a multi-factor blend.
All long-only or long/flat — negative weights are clipped by target_weights, and
the risk layer sizes every entry by its ATR stop. These work on any symbol list;
on a single-asset universe (ETH/USD) a "long" signal yields a unit target that
the risk manager then caps and stop-sizes.
"""
from __future__ import annotations

import numpy as np

from ..data import features as F
from .base import Strategy, StrategyContext, in_position_last_signal, inverse_vol_weights


def _breadth_weights(on: list[str], n_symbols: int) -> dict[str, float]:
    """Gross proportional to breadth of signals; unit weight on a single asset."""
    return {s: 1.0 / max(1, n_symbols) for s in on}


class EMACrossover(Strategy):
    """Long while the fast EMA is above the slow EMA; flat otherwise. The classic
    dual-EMA trend follower — fewer whipsaws than price-vs-SMA in fast markets."""

    name = "ema-cross"

    def __init__(self, fast: int = 20, slow: int = 50):
        self.fast, self.slow = fast, slow

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        on = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.slow + 1:
                continue
            if F.ema(c, self.fast).iloc[-1] > F.ema(c, self.slow).iloc[-1]:
                on.append(s)
        return _breadth_weights(on, len(ctx.symbols))


class MACDTrend(Strategy):
    """Long while the MACD histogram is positive (MACD above signal) AND price is
    above its long SMA — momentum confirmation with a trend filter to cut chop."""

    name = "macd-trend"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, trend_window: int = 100):
        self.fast, self.slow, self.signal, self.trend_window = fast, slow, signal, trend_window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        on = []
        need = max(self.slow + self.signal, self.trend_window) + 1
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < need:
                continue
            hist = F.macd(c, self.fast, self.slow, self.signal)["hist"].iloc[-1]
            trend_ok = c.iloc[-1] > F.sma(c, self.trend_window).iloc[-1]
            if np.isfinite(hist) and hist > 0 and trend_ok:
                on.append(s)
        return _breadth_weights(on, len(ctx.symbols))


class ADXTrend(Strategy):
    """Trend-strength-gated long: hold only when ADX confirms a real trend, the
    +DI/-DI cross is bullish, and price is above its SMA. ADX is the chop filter
    that keeps a trend follower out of range-bound, mean-reverting markets."""

    name = "adx-trend"

    def __init__(self, trend_window: int = 100, adx_window: int = 14, adx_min: float = 20.0):
        self.trend_window, self.adx_window, self.adx_min = trend_window, adx_window, adx_min

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        on = []
        need = max(self.trend_window, self.adx_window * 3) + 1
        for s in ctx.symbols:
            df = ctx.bars.get(s)
            if df is None or len(df) < need:
                continue
            a = F.adx(df, self.adx_window)
            adx_v = a["adx"].iloc[-1]
            up = a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
            trend_ok = df["close"].iloc[-1] > F.sma(df["close"], self.trend_window).iloc[-1]
            if np.isfinite(adx_v) and adx_v >= self.adx_min and up and trend_ok:
                on.append(s)
        return _breadth_weights(on, len(ctx.symbols))


class KeltnerBreakout(Strategy):
    """Keltner channel breakout: enter long when close breaks above EMA + mult*ATR,
    exit when close falls back below the EMA midline. Volatility-adaptive cousin of
    Donchian (ATR bands flex with vol). Stateless: most recent signal wins."""

    name = "keltner-breakout"

    def __init__(self, ema_window: int = 20, atr_window: int = 14, mult: float = 2.0):
        self.ema_window, self.atr_window, self.mult = ema_window, atr_window, mult

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        on = []
        need = self.ema_window + self.atr_window + 2
        for s in ctx.symbols:
            df = ctx.bars.get(s)
            if df is None or len(df) < need:
                continue
            mid = F.ema(df["close"], self.ema_window)
            upper = mid + self.mult * F.atr(df, self.atr_window)
            entry = df["close"] > upper
            exit_ = df["close"] < mid
            if in_position_last_signal(entry, exit_):
                on.append(s)
        return _breadth_weights(on, len(ctx.symbols))


class MultiFactor(Strategy):
    """Multi-factor blend: score each symbol by agreement across three orthogonal
    long-signals — trend (price > SMA), absolute momentum (12-1 > 0), and MACD
    histogram > 0. Target weight is proportional to the fraction agreeing, so the
    book leans in only when factors confirm each other. Optional soft vol-targeting."""

    name = "multi-factor"

    def __init__(self, trend_window: int = 100, mom_lookback: int = 90, mom_skip: int = 7,
                 target_vol: float = 0.0, vol_window: int = 20):
        self.trend_window, self.mom_lookback, self.mom_skip = trend_window, mom_lookback, mom_skip
        self.target_vol, self.vol_window = target_vol, vol_window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        out: dict[str, float] = {}
        need = max(self.trend_window, self.mom_lookback) + 1
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < need:
                continue
            f_trend = c.iloc[-1] > F.sma(c, self.trend_window).iloc[-1]
            f_mom = F.momentum(c, self.mom_lookback, self.mom_skip).iloc[-1] > 0
            f_macd = F.macd(c)["hist"].iloc[-1] > 0
            score = (int(bool(f_trend)) + int(bool(f_mom)) + int(bool(f_macd))) / 3.0
            if score <= 0:
                continue
            w = score / max(1, len(ctx.symbols))
            if self.target_vol > 0:
                rv = F.realized_vol(c, self.vol_window, annualize=True).iloc[-1]
                if np.isfinite(rv) and rv > 0:
                    w *= min(2.0, self.target_vol / rv)  # soft scale, capped
            out[s] = w
        return out


class Ensemble(Strategy):
    """Blend of diversified member strategies — the variance-reduction lever.

    Each member votes a long-only target weight; the ensemble holds the average.
    On a single asset this turns N all-or-nothing signals into a smooth 0..1
    conviction (fraction of members long), which cuts whipsaw and, when members
    are weakly correlated (trend + breakout + mean-reversion), lifts Sharpe above
    any single member. Optional volatility targeting scales the blended conviction
    toward a target annual vol so exposure (and cost efficiency) rises in calm tape."""

    name = "ensemble"

    def __init__(self, members: list[Strategy], target_vol: float = 0.0,
                 vol_window: int = 20, max_scale: float = 3.0):
        self.members = members
        self.target_vol, self.vol_window, self.max_scale = target_vol, vol_window, max_scale

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        agg: dict[str, float] = {}
        for m in self.members:
            for s, val in m.target_weights(ctx).items():
                agg[s] = agg.get(s, 0.0) + val
        n = max(1, len(self.members))
        out = {s: v / n for s, v in agg.items()}
        if self.target_vol > 0:
            for s in list(out):
                c = ctx.closes.get(s)
                if c is None:
                    continue
                rv = F.realized_vol(c.dropna(), self.vol_window, annualize=True).iloc[-1]
                if np.isfinite(rv) and rv > 0:
                    out[s] *= min(self.max_scale, self.target_vol / rv)
        return out


class RegimeGated(Strategy):
    """Wrap any strategy and stand aside in hostile regimes. Uses the causal HMM
    view on the context: when the regime's volatility rank is above `max_vol_rank`
    (turbulent) or model confidence is below `min_confidence`, hold cash. This is
    the regime-conditioning lever — it removes the worst whipsaw/bear chop that a
    regime-blind trend follower otherwise eats, raising risk-adjusted return."""

    name = "regime-gated"

    def __init__(self, base: Strategy, max_vol_rank: float = 0.66, min_confidence: float = 0.0):
        self.base = base
        self.max_vol_rank, self.min_confidence = max_vol_rank, min_confidence
        # Forward vol-target sizing attrs so the engine can read them from either layer
        self.sizing_mode = getattr(base, "sizing_mode", "risk")
        self.target_vol = getattr(base, "target_vol", 0.0)
        self.stop_atr_mult = getattr(base, "stop_atr_mult", None)

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        view = ctx.regime
        if view is not None:
            if view.vol_rank > self.max_vol_rank or view.confidence < self.min_confidence:
                return {}
        return self.base.compute_weights(ctx)
