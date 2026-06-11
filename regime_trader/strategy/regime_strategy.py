"""The incumbent: volatility-regime-scaled allocation.

Low vol  -> fully invested with modest leverage (calm markets trend up).
Mid vol  -> invested while the trend holds; cut back when it breaks.
High vol -> reduced but never flat, never short (catch V-shaped rebounds).
Uncertainty/flicker -> halve sizes, leverage forced to neutral.
The edge being harvested is avoiding large drawdowns, not calling tops.

Regime-count agnostic: everything keys off continuous vol_rank in [0, 1],
so it works identically whether BIC picked 3 states or 7.
"""
from __future__ import annotations

import numpy as np

from ..config import get_config
from ..data import features as F
from .base import Strategy, StrategyContext, equal_weights, inverse_vol_weights


class RegimeStrategy(Strategy):
    name = "regime-hmm"

    def __init__(self):
        cfg = get_config().allocation
        self.curve = sorted((float(x), float(y)) for x, y in cfg.exposure_curve)
        self.trend_sma = cfg.trend_sma
        self.trend_break_scale = cfg.trend_break_scale
        self.transition_scale = cfg.transition_scale
        self.uncertainty_scale = cfg.uncertainty_scale
        self.uncertainty_max_lev = cfg.uncertainty_max_leverage
        self.weighting = cfg.weighting
        self.low_confidence = float(get_config().allocation.get("low_confidence", 0.45))

    def gross_exposure(self, ctx: StrategyContext) -> float:
        regime, stab = ctx.regime, ctx.stability
        if regime is None:
            return 0.5  # model unavailable -> conservative half size

        xs = [p[0] for p in self.curve]
        ys = [p[1] for p in self.curve]
        gross = float(np.interp(regime.vol_rank, xs, ys))

        # mid-vol trend gate: stay invested only while the trend is intact
        if 0.25 <= regime.vol_rank <= 0.75:
            proxy = ctx.closes.get(ctx.market_proxy)
            if proxy is not None:
                proxy = proxy.dropna()
                if len(proxy) >= self.trend_sma and proxy.iloc[-1] < F.sma(proxy, self.trend_sma).iloc[-1]:
                    gross *= self.trend_break_scale

        uncertain = (stab is not None and stab.uncertainty_mode) or regime.confidence < self.low_confidence
        if stab is not None and stab.in_transition:
            gross *= self.transition_scale
        if uncertain:
            gross *= self.uncertainty_scale
            gross = min(gross, self.uncertainty_max_lev)
        return max(0.0, gross)

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        gross = self.gross_exposure(ctx)
        if gross <= 0:
            return {}
        if self.weighting == "inverse_vol":
            base = inverse_vol_weights(ctx.closes, ctx.symbols)
        else:
            base = equal_weights(ctx.symbols)
        return {s: w * gross for s, w in base.items()}
