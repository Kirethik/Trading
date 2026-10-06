"""Extended risk metrics the spec requires beyond backtest/metrics.py.

Wraps `compute_metrics` (Sharpe, Sortino, Calmar, CAGR, max DD, win rate,
profit factor, ...) and adds: recovery factor, expectancy, exposure, average
drawdown, time underwater, recovery time, and a single-path probability-of-ruin
proxy (the Monte Carlo engine in montecarlo.py produces the distributional one).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.metrics import TRADING_DAYS, compute_metrics


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def average_drawdown(equity: pd.Series) -> float:
    """Mean depth of the distinct underwater episodes (0 if never underwater)."""
    dd = drawdown_series(equity)
    episodes, cur_min, underwater = [], 0.0, False
    for d in dd:
        if d < 0:
            underwater = True
            cur_min = min(cur_min, d)
        elif underwater:               # surfaced -> close the episode
            episodes.append(cur_min)
            cur_min, underwater = 0.0, False
    if underwater:
        episodes.append(cur_min)
    return float(np.mean(episodes)) if episodes else 0.0


def time_underwater(equity: pd.Series) -> float:
    """Fraction of bars spent below the prior peak."""
    dd = drawdown_series(equity)
    return float((dd < 0).mean()) if len(dd) else 0.0


def recovery_time_bars(equity: pd.Series) -> int:
    """Bars from the deepest drawdown's trough back to a new high.

    Returns -1 if the curve never recovered from its worst trough.
    """
    dd = drawdown_series(equity)
    if dd.empty or dd.min() >= 0:
        return 0
    trough_pos = int(np.argmin(dd.to_numpy()))
    peak_at_trough = equity.iloc[: trough_pos + 1].max()
    after = equity.iloc[trough_pos:]
    recovered = np.where(after.to_numpy() >= peak_at_trough)[0]
    return int(recovered[0]) if len(recovered) else -1


def expectancy(trades: pd.DataFrame | None) -> float:
    """Average realized P&L per closed round trip (currency units)."""
    if trades is None or len(trades) == 0 or "realized_pnl" not in trades.columns:
        return 0.0
    closed = trades.dropna(subset=["realized_pnl"])
    closed = closed[closed["realized_pnl"] != 0]
    return float(closed["realized_pnl"].mean()) if len(closed) else 0.0


def exposure(weights: pd.DataFrame | None) -> float:
    """Average fraction of bars with any capital deployed (gross > 0)."""
    if weights is None or len(weights) == 0:
        return 0.0
    gross = weights.sum(axis=1)
    return float((gross > 1e-9).mean())


def single_path_ruin(equity: pd.Series, ruin_drawdown: float = -0.50) -> bool:
    """Did the realized path itself ever breach the ruin drawdown?"""
    return bool(drawdown_series(equity).min() <= ruin_drawdown)


def extended_metrics(
    equity: pd.Series,
    trades: pd.DataFrame | None = None,
    weights: pd.DataFrame | None = None,
    ruin_drawdown: float = -0.50,
) -> dict:
    """compute_metrics(...) plus the additional spec-required fields."""
    m = compute_metrics(equity, trades)
    if "error" in m:
        return m
    cagr, max_dd = m["cagr"], m["max_drawdown"]
    total_return = m["total_return"]
    m.update(
        {
            "recovery_factor": float(total_return / abs(max_dd)) if max_dd < 0 else float("inf"),
            "expectancy": expectancy(trades),
            "exposure": exposure(weights),
            "average_drawdown": average_drawdown(equity),
            "time_underwater": time_underwater(equity),
            "recovery_time_bars": recovery_time_bars(equity),
            "single_path_ruin": single_path_ruin(equity, ruin_drawdown),
        }
    )
    return m
