"""SMATrendFilter fast-path equivalence.

The strategy now compares price to the mean of the last `window` closes instead
of rolling F.sma over the whole series each bar (an O(n)->O(window) hot-path win
for the interactive backtest). This must be value- AND decision-identical to the
previous implementation, so backtest metrics are unchanged — the always-exact
guarantee. The previous implementation is kept here as the reference oracle.
"""
from __future__ import annotations

import numpy as np

from regime_trader.backtest.engine import WalkForwardEngine
from regime_trader.data import features as F
from regime_trader.strategy.base import StrategyContext, equal_weights
from regime_trader.strategy.trend import SMATrendFilter

from conftest import make_synthetic_bars


class _OldSMATrend(SMATrendFilter):
    """The pre-optimization implementation (rolls F.sma over the full series)."""

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


def _universe():
    syms = {"SPY": 0, "QQQ": 1, "AAPL": 2, "MSFT": 3, "TLT": 4}
    return {s: make_synthetic_bars(seed=k) for s, k in syms.items()}


def _run(strat, universe):
    start = list(universe.values())[0].index[300]
    return WalkForwardEngine(universe, strat, list(universe), start_ts=start).run()


def test_sma_tail_mean_matches_full_roll():
    """Value + decision equivalence at every endpoint, across windows."""
    c = make_synthetic_bars(seed=3)["close"]
    for window in (50, 150, 250):
        full = F.sma(c, window)
        for i in range(window, len(c)):
            tail = c.iloc[i - window + 1 : i + 1].mean()   # what the strategy now uses
            old = full.iloc[i]                              # F.sma(...).iloc[-1] at bar i
            assert np.isclose(tail, old, rtol=1e-12, atol=1e-9)
            assert (c.iloc[i] > tail) == (c.iloc[i] > old), "trend decision flipped"


def test_sma_fastpath_backtest_identical():
    """Full engine equity + trade count unchanged vs the previous implementation."""
    for window in (100, 150, 200):
        uni = _universe()
        new = _run(SMATrendFilter(window), uni)
        old = _run(_OldSMATrend(window), uni)
        assert new.equity.equals(old.equity) or np.allclose(
            new.equity.to_numpy(), old.equity.to_numpy(), rtol=0, atol=1e-9
        ), f"equity diverged at window={window}"
        assert len(new.trades) == len(old.trades)
