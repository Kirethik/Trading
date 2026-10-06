"""Shared fixtures: deterministic synthetic markets, isolated risk/state paths.

No test touches the network or the live state/ directory.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from regime_trader.config import get_config
from regime_trader.risk.manager import PortfolioState, Position, RiskManager


def make_synthetic_bars(n: int = 900, seed: int = 0, start: str = "2019-01-02",
                        drift: float = 0.0004) -> pd.DataFrame:
    """GBM with alternating calm/turbulent volatility blocks of 150 bars —
    exactly the structure a volatility-regime HMM should find."""
    rng = np.random.default_rng(seed)
    vols = np.where((np.arange(n) // 150) % 2 == 0, 0.008, 0.028)
    rets = rng.normal(drift, 1.0, n) * vols
    close = 100.0 * np.exp(np.cumsum(rets))
    gap = rng.normal(0, 0.2, n) * vols
    opn = np.empty(n)
    opn[0] = close[0]
    opn[1:] = close[:-1] * np.exp(gap[1:])
    span = np.abs(rng.normal(0, 0.6, n)) * vols
    high = np.maximum(opn, close) * np.exp(span)
    low = np.minimum(opn, close) * np.exp(-span)
    idx = pd.bdate_range(start, periods=n, tz="UTC")
    return pd.DataFrame(
        {"open": opn, "high": high, "low": low, "close": close, "volume": 1e6},
        index=idx,
    )


@pytest.fixture(scope="session")
def cfg():
    return get_config()


@pytest.fixture(scope="session")
def synthetic_universe() -> dict[str, pd.DataFrame]:
    syms = {"SPY": 0, "QQQ": 1, "AAPL": 2, "MSFT": 3, "TLT": 4}
    return {s: make_synthetic_bars(seed=k) for s, k in syms.items()}


@pytest.fixture()
def risk(tmp_path) -> RiskManager:
    return RiskManager(lock_file=tmp_path / "breaker.lock")


@pytest.fixture()
def healthy_portfolio() -> PortfolioState:
    return PortfolioState(
        equity=100_000.0, cash=100_000.0, positions={},
        peak_equity=100_000.0, day_start_equity=100_000.0,
        week_start_equity=100_000.0, trades_today=0,
        prices={"SPY": 500.0, "QQQ": 400.0, "AAPL": 200.0, "MSFT": 420.0},
    )


def position(symbol: str, qty: float, entry: float, stop: float, sector: str = "tech") -> Position:
    return Position(symbol=symbol, qty=qty, entry_price=entry, stop_price=stop, sector=sector)
