"""Built-in benchmarks: buy-and-hold, simple trend filter, and
random-allocation-with-the-same-risk-rules (many seeds, mean/std).

The random benchmark answers the uncomfortable question: is the edge the
regime model, or just the risk layer? It runs through the full engine, so it
gets the identical stops, breakers, caps, slippage and fill delay.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from ..config import get_config
from ..data import features as F
from ..strategy.base import Strategy, StrategyContext
from .engine import WalkForwardEngine
from .metrics import compute_metrics


def buy_and_hold(closes: pd.DataFrame, symbol: str, start_ts, capital: float) -> pd.Series:
    c = closes[symbol].dropna()
    c = c[c.index >= start_ts]
    return (c / c.iloc[0] * capital).rename("equity")


def trend_filter_equity(closes: pd.DataFrame, symbol: str, start_ts, capital: float,
                        window: int = 200, slippage_bps: float = 5.0) -> pd.Series:
    """Long when above the SMA, cash otherwise; signal acts next bar."""
    c = closes[symbol].dropna()
    pos = (c > F.sma(c, window)).astype(float).shift(1).fillna(0.0)
    rets = c.pct_change().fillna(0.0) * pos
    rets -= pos.diff().abs().fillna(0.0) * (slippage_bps / 10_000.0)
    eq = (1.0 + rets[rets.index >= start_ts]).cumprod()
    return (eq / eq.iloc[0] * capital).rename("equity")


class RandomAllocation(Strategy):
    """Random long-only weights, deterministic per (seed, bar) so truncating
    history never changes earlier draws."""

    name = "random-allocation"

    def __init__(self, seed: int):
        self.seed = seed
        self.name = f"random-allocation-{seed}"

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        key = f"{self.seed}:{ctx.now.isoformat()}".encode()
        rng = np.random.default_rng(int.from_bytes(hashlib.md5(key).digest()[:8], "big"))
        picks = [s for s in ctx.symbols if rng.random() < 0.5]
        if not picks:
            return {}
        raw = rng.dirichlet(np.ones(len(picks)))
        gross = rng.uniform(0.3, 1.3)
        return {s: float(w) * gross for s, w in zip(picks, raw)}


def run_benchmarks(
    bars: dict[str, pd.DataFrame],
    oracle: pd.DataFrame,
    symbols: list[str],
    n_random_seeds: int | None = None,
) -> dict:
    cfg = get_config()
    capital = cfg.backtest.initial_capital
    proxy = cfg.universe.market_proxy
    start_ts = oracle.index[0]
    closes = pd.DataFrame({s: d["close"] for s, d in bars.items()}).sort_index().ffill(limit=5)

    out: dict[str, dict] = {
        "buy_and_hold": compute_metrics(buy_and_hold(closes, proxy, start_ts, capital)),
        "trend_filter": compute_metrics(trend_filter_equity(closes, proxy, start_ts, capital)),
    }

    seeds = n_random_seeds if n_random_seeds is not None else cfg.backtest.random_benchmark_seeds
    rand_metrics = []
    for seed in range(seeds):
        engine = WalkForwardEngine(bars, RandomAllocation(seed), symbols, regime_oracle=oracle)
        rand_metrics.append(engine.run().metrics)
    if rand_metrics:
        keys = ("sharpe", "cagr", "max_drawdown", "total_return")
        out["random_same_risk_rules"] = {
            "seeds": seeds,
            **{f"{k}_mean": float(np.mean([m[k] for m in rand_metrics])) for k in keys},
            **{f"{k}_std": float(np.std([m[k] for m in rand_metrics])) for k in keys},
        }
    return out
