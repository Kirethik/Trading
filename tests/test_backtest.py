"""Engine realism + determinism, metrics sanity, stress injectors."""
from __future__ import annotations

import numpy as np
import pandas as pd

from regime_trader.backtest.engine import WalkForwardEngine
from regime_trader.backtest.metrics import compute_metrics
from regime_trader.backtest.stress import inject_crash, inject_gaps, scramble_oracle
from regime_trader.strategy.trend import SMATrendFilter, TimeSeriesMomentum

from conftest import make_synthetic_bars


def run_engine(universe, strat=None, start_bar: int = 300):
    strat = strat or SMATrendFilter(100)
    start = list(universe.values())[0].index[start_bar]
    return WalkForwardEngine(universe, strat, list(universe), start_ts=start).run()


class TestEngine:
    def test_deterministic(self, synthetic_universe):
        a = run_engine(synthetic_universe)
        b = run_engine(synthetic_universe)
        assert a.equity.equals(b.equity)
        assert len(a.trades) == len(b.trades)

    def test_equity_finite_and_positive(self, synthetic_universe):
        res = run_engine(synthetic_universe)
        assert np.isfinite(res.equity.to_numpy()).all()
        assert (res.equity > 0).all()

    def test_fills_are_next_bar(self, synthetic_universe):
        """No trade may print on the signal bar: first fill is after start."""
        res = run_engine(synthetic_universe)
        assert len(res.trades) > 0
        first_fill = pd.Timestamp(res.trades["ts"].iloc[0])
        assert first_fill > res.equity.index[0]

    def test_buys_never_exceed_caps(self, synthetic_universe):
        res = run_engine(synthetic_universe, TimeSeriesMomentum(63, 0))
        gross = res.weights.sum(axis=1)
        assert (gross <= 1.5 + 1e-6).all()
        assert (res.weights >= -1e-12).all().all(), "short position detected"

    def test_stop_exits_logged_with_reason(self, synthetic_universe):
        crashed = inject_crash(synthetic_universe, at_frac=0.6, days=5, daily_drop=0.10)
        res = run_engine(crashed)
        if len(res.trades):
            reasons = set(res.trades["reason"])
            assert reasons <= {"rebalance", "stop"}

    def test_decisions_logged_with_reasons(self, synthetic_universe):
        res = run_engine(synthetic_universe)
        assert res.decisions, "risk layer saw no intents — engine wiring broken"
        assert all(d["reasons"] for d in res.decisions)


class TestMetrics:
    def test_flat_equity_zero_return(self):
        idx = pd.bdate_range("2020-01-01", periods=300, tz="UTC")
        m = compute_metrics(pd.Series(100_000.0, index=idx))
        assert abs(m["total_return"]) < 1e-12
        assert m["max_drawdown"] == 0.0

    def test_known_drawdown(self):
        idx = pd.bdate_range("2020-01-01", periods=4, tz="UTC")
        m = compute_metrics(pd.Series([100.0, 120.0, 90.0, 110.0], index=idx))
        assert abs(m["max_drawdown"] - (90.0 / 120.0 - 1.0)) < 1e-12


class TestStressInjectors:
    def test_crash_reduces_prices(self, synthetic_universe):
        crashed = inject_crash(synthetic_universe)
        for s in synthetic_universe:
            assert crashed[s]["close"].iloc[-1] < synthetic_universe[s]["close"].iloc[-1]
            assert len(crashed[s]) == len(synthetic_universe[s])

    def test_gaps_inserted(self, synthetic_universe):
        gapped = inject_gaps(synthetic_universe)
        s = "SPY"
        ratio = gapped[s]["open"] / gapped[s]["close"].shift(1)
        base = synthetic_universe[s]["open"] / synthetic_universe[s]["close"].shift(1)
        assert ratio.min() < base.min() - 0.02, "no gap visibly injected"

    def test_scramble_preserves_index(self):
        idx = pd.bdate_range("2020-01-01", periods=50, tz="UTC")
        oracle = pd.DataFrame({
            "state": range(50), "prob": 0.9, "confidence": 0.9, "vol_rank": 0.5,
            "expected_vol": 0.2, "expected_ret": 0.1, "n_states": 3,
            "label": ["x"] * 50, "refit_id": 0,
        }, index=idx)
        scrambled = scramble_oracle(oracle)
        assert scrambled.index.equals(oracle.index)
        assert sorted(scrambled["state"]) == sorted(oracle["state"])
        assert (scrambled["state"] != oracle["state"]).any()
