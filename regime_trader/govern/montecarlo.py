"""Monte Carlo robustness: how fragile is a backtest's equity curve?

Three resampling families (spec §3), each rebuilding many synthetic equity
paths and measuring how often the strategy is ruined and how its outcomes are
distributed:

- return     : i.i.d. resample of daily returns (destroys ordering)
- bootstrap  : block resample of daily returns (preserves short autocorrelation)
- trade      : resample the sequence of realized trade P&L (path of bets)

Deterministic: the caller passes a seed (varied per strategy by name hash), so
re-running yields identical distributions. No wall clock, no global RNG.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.metrics import TRADING_DAYS

PCTILES = [1, 5, 25, 50, 75, 95, 99]


def _path_stats(returns: np.ndarray, ruin_drawdown: float) -> tuple[float, float, float, bool]:
    """(total_return, max_drawdown, sharpe, ruined) for one synthetic path."""
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    max_dd = float(dd.min())
    total = float(equity[-1] - 1.0)
    std = returns.std()
    sharpe = float(returns.mean() / std * np.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    return total, max_dd, sharpe, bool(max_dd <= ruin_drawdown)


def _resample_returns(rng, rets: np.ndarray, method: str, block: int) -> np.ndarray:
    n = len(rets)
    if method == "return":
        return rng.choice(rets, size=n, replace=True)
    if method == "bootstrap":
        out, b = [], max(1, block)
        while len(out) < n:
            start = rng.integers(0, max(1, n - b))
            out.extend(rets[start : start + b])
        return np.asarray(out[:n])
    raise ValueError(method)


def _trade_paths(rng, trade_pnl: np.ndarray, capital: float, n_sims: int,
                 ruin_drawdown: float) -> list[tuple]:
    """Resample the ORDER of realized trade P&L and walk the account."""
    stats = []
    for _ in range(n_sims):
        seq = rng.choice(trade_pnl, size=len(trade_pnl), replace=True)
        equity = capital + np.cumsum(seq)
        equity = np.maximum(equity, 1e-9)
        peak = np.maximum.accumulate(equity)
        dd = equity / peak - 1.0
        stats.append((float(equity[-1] / capital - 1.0), float(dd.min()),
                      0.0, bool(dd.min() <= ruin_drawdown)))
    return stats


def monte_carlo(
    equity: pd.Series,
    trades: pd.DataFrame | None = None,
    n_sims: int = 10000,
    methods: tuple[str, ...] = ("trade", "return", "bootstrap"),
    block: int = 5,
    ruin_drawdown: float = -0.50,
    seed: int = 42,
    initial_capital: float = 100000.0,
) -> dict:
    """Returns probability of ruin, plus return/drawdown/Sharpe distributions.

    n_sims is split across the requested methods. Robust to short curves
    (returns a structured 'insufficient' result rather than raising).
    """
    rets = equity.pct_change().dropna().to_numpy()
    if len(rets) < 20:
        return {"error": "equity too short for Monte Carlo", "bars": int(len(rets))}

    rng = np.random.default_rng(seed)
    per = max(1, n_sims // max(1, len(methods)))
    all_stats: list[tuple] = []
    by_method: dict[str, dict] = {}

    for method in methods:
        if method == "trade":
            pnl = _method_trade_pnl(trades)
            if pnl is None:
                # no usable trade ledger: fall back to i.i.d. return resampling
                stats = [_path_stats(_resample_returns(rng, rets, "return", block), ruin_drawdown)
                         for _ in range(per)]
            else:
                stats = _trade_paths(rng, pnl, initial_capital, per, ruin_drawdown)
        else:
            stats = [_path_stats(_resample_returns(rng, rets, method, block), ruin_drawdown)
                     for _ in range(per)]
        all_stats.extend(stats)
        by_method[method] = _summarize(stats, ruin_drawdown)

    summary = _summarize(all_stats, ruin_drawdown)
    summary["by_method"] = by_method
    summary["n_sims"] = len(all_stats)
    return summary


def _method_trade_pnl(trades: pd.DataFrame | None) -> np.ndarray | None:
    if trades is None or len(trades) == 0 or "realized_pnl" not in trades.columns:
        return None
    pnl = trades.dropna(subset=["realized_pnl"])
    pnl = pnl[pnl["realized_pnl"] != 0]["realized_pnl"].to_numpy()
    return pnl if len(pnl) >= 10 else None


def _summarize(stats: list[tuple], ruin_drawdown: float) -> dict:
    if not stats:
        return {"prob_of_ruin": 0.0, "n_sims": 0}
    totals = np.array([s[0] for s in stats])
    dds = np.array([s[1] for s in stats])
    sharpes = np.array([s[2] for s in stats])
    ruined = np.array([s[3] for s in stats])
    return {
        "prob_of_ruin": float(ruined.mean()),
        "ruin_drawdown": ruin_drawdown,
        "return_distribution": {f"p{p}": float(np.percentile(totals, p)) for p in PCTILES},
        "drawdown_distribution": {f"p{p}": float(np.percentile(dds, p)) for p in PCTILES},
        "worst_case_return_p1": float(np.percentile(totals, 1)),
        "worst_case_drawdown_p1": float(np.percentile(dds, 1)),
        "expected_return_mean": float(totals.mean()),
        "sharpe_mean": float(sharpes.mean()),
        "sharpe_std": float(sharpes.std()),
        "prob_profit": float((totals > 0).mean()),
    }
