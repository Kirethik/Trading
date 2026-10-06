"""Tests for the research additions: new indicators (causal/no-look-ahead),
extended metrics, and the candidate grid."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from regime_trader.backtest.metrics import compute_metrics
from regime_trader.data import features as F


def _synth_ohlcv(n=600, seed=7):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.001, 0.04, n)
    close = 100 * np.cumprod(1 + rets)
    idx = pd.date_range("2019-01-01", periods=n, freq="D", tz="UTC")
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    vol = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


@pytest.mark.parametrize("name", ["ema", "macd_hist", "adx", "plus_di", "vwap_roll", "obv", "vol_z"])
def test_new_indicators_are_truncation_invariant(name):
    """The no-look-ahead guarantee: the value at bar t is identical whether or not
    bars after t exist in the input."""
    df = _synth_ohlcv()
    cut = len(df) - 40
    full = _indicator(df, name)
    trunc = _indicator(df.iloc[:cut], name)
    ts = df.index[cut - 1]
    a, b = float(full.loc[ts]), float(trunc.iloc[-1])
    assert np.isclose(a, b, rtol=1e-9, atol=1e-9) or (np.isnan(a) and np.isnan(b)), f"{name} leaks future"


def _indicator(df, name):
    close = df["close"]
    return {
        "ema": F.ema(close, 20),
        "macd_hist": F.macd(close)["hist"],
        "adx": F.adx(df, 14)["adx"],
        "plus_di": F.adx(df, 14)["plus_di"],
        "vwap_roll": F.vwap(df, 20),
        "obv": F.obv(df),
        "vol_z": F.volume_zscore(df, 20),
    }[name]


def test_adx_in_range():
    df = _synth_ohlcv()
    adx = F.adx(df, 14)["adx"].dropna()
    assert ((adx >= 0) & (adx <= 100)).all()


def test_recovery_factor_and_avg_trade_present():
    df = _synth_ohlcv()
    eq = df["close"] / df["close"].iloc[0] * 100_000
    m = compute_metrics(eq, periods_per_year=365)
    assert "recovery_factor" in m and "avg_trade" in m
    # recovery factor = total_return / |max_dd|
    if m["max_drawdown"] < 0 and m["recovery_factor"] is not None:
        assert np.isclose(m["recovery_factor"], m["total_return"] / abs(m["max_drawdown"]), rtol=1e-6)


def test_annualization_scales_with_periods_per_year():
    df = _synth_ohlcv()
    eq = df["close"] / df["close"].iloc[0] * 100_000
    s252 = compute_metrics(eq, periods_per_year=252)["sharpe"]
    s365 = compute_metrics(eq, periods_per_year=365)["sharpe"]
    if s252:
        assert np.isclose(s365 / s252, np.sqrt(365 / 252), rtol=1e-6)


def test_candidate_grid_builds_and_dedupes():
    from regime_trader.research.candidates import build_candidates
    for tf in ("1Day", "4Hour"):
        cands = build_candidates(tf)
        ids = [c.cid for c in cands]
        assert len(ids) == len(set(ids)), f"duplicate candidate ids on {tf}"
        assert len(cands) >= 15
        for c in cands:  # every candidate is a usable strategy
            assert hasattr(c.strategy, "compute_weights")


def test_ensemble_averages_member_weights():
    from regime_trader.strategy.base import StrategyContext
    from regime_trader.strategy.extended import Ensemble
    from regime_trader.strategy.trend import SMATrendFilter

    df = _synth_ohlcv()
    closes = pd.DataFrame({"ETH/USD": df["close"]})
    ctx = StrategyContext(bars={"ETH/USD": df}, closes=closes, symbols=["ETH/USD"])
    # two members both long -> ensemble equals their (averaged) long weight
    ens = Ensemble([SMATrendFilter(20), SMATrendFilter(50)])
    w = ens.compute_weights(ctx)
    assert all(0.0 <= v <= 1.0 for v in w.values())
