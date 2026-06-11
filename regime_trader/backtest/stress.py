"""Stress testing: the system must survive even when the model is wrong.

Three injected catastrophes, all run through the full engine + risk layer:
- crash: consecutive heavy down days with widened ranges
- gaps: surprise overnight gap-downs (tests stop handling through gaps)
- scrambled regimes: the oracle's vol ranks are randomly shuffled — the
  model is deliberately, maximally wrong, and the risk layer must contain it.

Survival = run completes, equity stays positive, drawdown bounded.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..strategy.base import Strategy
from .engine import WalkForwardEngine

log = get_logger("main")

MAX_TOLERATED_DD = -0.50  # beyond this the risk layer failed its one job


def inject_crash(bars: dict[str, pd.DataFrame], at_frac: float = 0.6,
                 days: int = 4, daily_drop: float = 0.07) -> dict[str, pd.DataFrame]:
    """Multiply prices by a cumulative crash starting ~at_frac through history."""
    out = {}
    for sym, df in bars.items():
        df = df.copy()
        start = int(len(df) * at_frac)
        factor = 1.0
        for d in range(days):
            if start + d >= len(df):
                break
            factor *= 1.0 - daily_drop
            i = df.index[start + d]
            df.loc[i:, ["open", "high", "low", "close"]] *= (1.0 - daily_drop)
            # widen the crash bars' intrabar range to simulate panic
            df.loc[i, "low"] = df.loc[i, "low"] * 0.97
        out[sym] = df
    return out


def inject_gaps(bars: dict[str, pd.DataFrame], n_gaps: int = 6, gap: float = 0.08,
                seed: int = 7) -> dict[str, pd.DataFrame]:
    """Random overnight gap-downs: the open (and low) print far below the
    prior close, exactly where resting stops get gapped through."""
    rng = np.random.default_rng(seed)
    out = {}
    for sym, df in bars.items():
        df = df.copy()
        if len(df) < 300:
            out[sym] = df
            continue
        spots = rng.choice(np.arange(250, len(df)), size=min(n_gaps, len(df) - 250), replace=False)
        for j in sorted(spots):
            i = df.index[j]
            df.loc[i:, ["open", "high", "low", "close"]] *= (1.0 - gap)
            df.loc[i, "open"] = df.loc[i, "open"] * (1.0 - 0.02)  # open prints extra-low
            df.loc[i, "low"] = min(df.loc[i, "low"], df.loc[i, "open"])
        out[sym] = df
    return out


def scramble_oracle(oracle: pd.DataFrame, seed: int = 13) -> pd.DataFrame:
    """Shuffle regime assignments in time — model output becomes noise."""
    rng = np.random.default_rng(seed)
    scrambled = oracle.copy()
    perm = rng.permutation(len(scrambled))
    for col in ["state", "prob", "confidence", "vol_rank", "expected_vol", "expected_ret", "label"]:
        scrambled[col] = scrambled[col].to_numpy()[perm]
    return scrambled


def run_stress_suite(
    strategy: Strategy,
    bars: dict[str, pd.DataFrame],
    oracle: pd.DataFrame,
    symbols: list[str],
) -> dict:
    """Run every scenario; return per-scenario metrics + pass/fail verdicts."""
    scenarios = {
        "baseline": (bars, oracle),
        "injected_crash": (inject_crash(bars), oracle),
        "overnight_gaps": (inject_gaps(bars), oracle),
        "scrambled_regimes": (bars, scramble_oracle(oracle)),
    }
    report: dict[str, dict] = {}
    for name, (b, o) in scenarios.items():
        try:
            res = WalkForwardEngine(b, strategy, symbols, regime_oracle=o).run()
            m = res.metrics
            breaker_rejections = sum(
                1 for d in res.decisions
                if not d["approved"] and "circuit breaker" in (d["reasons"][0] if d["reasons"] else "")
            )
            survived = (
                m.get("max_drawdown", -1.0) >= MAX_TOLERATED_DD
                and res.equity.iloc[-1] > 0
            )
            report[name] = {
                "survived": bool(survived),
                "max_drawdown": m.get("max_drawdown"),
                "total_return": m.get("total_return"),
                "sharpe": m.get("sharpe"),
                "breaker_rejections": breaker_rejections,
            }
        except Exception as exc:
            report[name] = {"survived": False, "error": str(exc)}
        log.info("stress scenario %s: %s", name, report[name])
    report["passed"] = all(v.get("survived") for k, v in report.items() if isinstance(v, dict))
    return report
