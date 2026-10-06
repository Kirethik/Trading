"""THE test: zero look-ahead bias, proven by truncation invariance.

If the regime at time T (or the equity at time T) changes when future data is
appended, something is smoothing backward and the whole system is invalid.
"""
from __future__ import annotations

import numpy as np
import pytest

from regime_trader.data import features as F
from regime_trader.regime.hmm import RegimeModel
from regime_trader.backtest.engine import WalkForwardEngine
from regime_trader.strategy.trend import SMATrendFilter

from conftest import make_synthetic_bars


@pytest.fixture(scope="module")
def fitted_model_and_features():
    bars = make_synthetic_bars(n=900, seed=42)
    X = F.regime_features(bars, vol_window=21)
    model = RegimeModel(n_states_min=3, n_states_max=3, random_seed=7).fit(X.iloc[:600])
    return model, X


class TestFilteredInferenceIsCausal:
    def test_regime_at_t_identical_with_and_without_future(self, fitted_model_and_features):
        """Filtered probabilities at every t <= 700 must be bit-identical
        whether the series ends at 700 or at 880."""
        model, X = fitted_model_and_features
        probs_short = model.filtered_probabilities(X.iloc[:700])
        probs_long = model.filtered_probabilities(X)
        assert np.allclose(probs_short, probs_long[:700], rtol=0, atol=1e-12), (
            "filtered inference saw the future"
        )

    def test_single_bar_extension_does_not_rewrite_history(self, fitted_model_and_features):
        model, X = fitted_model_and_features
        a = model.filtered_probabilities(X.iloc[:750])
        b = model.filtered_probabilities(X.iloc[:751])
        assert np.allclose(a, b[:750], rtol=0, atol=1e-12)

    def test_views_match_filtered_argmax(self, fitted_model_and_features):
        model, X = fitted_model_and_features
        probs = model.filtered_probabilities(X.iloc[:100])
        views = model.views(X.iloc[:100])
        assert [v.state for v in views] == [int(p.argmax()) for p in probs]


class TestFeatureCausality:
    def test_features_truncation_invariant(self):
        bars = make_synthetic_bars(n=500, seed=3)
        full = F.regime_features(bars, vol_window=21)
        part = F.regime_features(bars.iloc[:400], vol_window=21)
        overlap = part.index
        assert np.allclose(full.loc[overlap].to_numpy(), part.to_numpy(), atol=1e-12)

    def test_indicators_truncation_invariant(self):
        bars = make_synthetic_bars(n=400, seed=5)
        c = bars["close"]
        for fn in (lambda s: F.sma(s, 50), lambda s: F.rsi(s, 2),
                   lambda s: F.realized_vol(s, 21), lambda s: F.momentum(s, 100, 10)):
            full = fn(c)
            part = fn(c.iloc[:300])
            assert np.allclose(full.iloc[:300].fillna(-9).to_numpy(),
                               part.fillna(-9).to_numpy(), atol=1e-10)
        a_full = F.atr(bars, 14)
        a_part = F.atr(bars.iloc[:300], 14)
        assert np.allclose(a_full.iloc[:300].fillna(-9).to_numpy(),
                           a_part.fillna(-9).to_numpy(), atol=1e-10)


class TestBacktestTruncationInvariance:
    def test_equity_prefix_identical_across_end_dates(self, synthetic_universe):
        """Backtests must be identical across different end dates (§11)."""
        strat = SMATrendFilter(100)
        symbols = list(synthetic_universe)
        start = list(synthetic_universe.values())[0].index[300]

        full = WalkForwardEngine(synthetic_universe, strat, symbols, start_ts=start).run()
        cut = {s: d.iloc[:-60] for s, d in synthetic_universe.items()}
        part = WalkForwardEngine(cut, strat, symbols, start_ts=start).run()

        overlap = part.equity.index
        assert len(overlap) > 100
        assert np.allclose(full.equity.loc[overlap].to_numpy(), part.equity.to_numpy(),
                           rtol=1e-12), "future bars changed past equity"
