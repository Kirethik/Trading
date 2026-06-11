"""Performance metrics: the full picture plus the granular proofs.

Attribution convention: the return earned over bar t is explained by the
regime/confidence known at t-1 (when the allocation was decided) — grouping
anything by same-bar regime would itself be a small look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def compute_metrics(equity: pd.Series, trades: pd.DataFrame | None = None) -> dict:
    equity = equity.dropna()
    if len(equity) < 3:
        return {"error": "equity curve too short"}
    rets = equity.pct_change().dropna()
    years = len(rets) / TRADING_DAYS
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / max(years, 1e-9)) - 1.0)

    std = float(rets.std())
    sharpe = float(rets.mean() / std * np.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    downside = float(rets[rets < 0].std())
    sortino = float(rets.mean() / downside * np.sqrt(TRADING_DAYS)) if downside > 0 else float("inf")

    peak = equity.cummax()
    dd = equity / peak - 1.0
    max_dd = float(dd.min())
    underwater = dd < 0
    dd_dur = max_dur = 0
    for flag in underwater:
        dd_dur = dd_dur + 1 if flag else 0
        max_dur = max(max_dur, dd_dur)
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else float("inf")

    out = {
        "total_return": total,
        "cagr": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown": max_dd,
        "max_drawdown_duration_bars": int(max_dur),
        "ann_vol": std * float(np.sqrt(TRADING_DAYS)),
        "bars": int(len(equity)),
    }

    if trades is not None and len(trades) > 0 and "realized_pnl" in trades.columns:
        closed = trades.dropna(subset=["realized_pnl"])
        closed = closed[closed["realized_pnl"] != 0]
        if len(closed) > 0:
            wins = closed[closed["realized_pnl"] > 0]["realized_pnl"].sum()
            losses = abs(closed[closed["realized_pnl"] < 0]["realized_pnl"].sum())
            out["win_rate"] = float((closed["realized_pnl"] > 0).mean())
            out["profit_factor"] = float(wins / losses) if losses > 0 else float("inf")
            out["n_round_trips"] = int(len(closed))
        out["n_fills"] = int(len(trades))
    else:  # fallback: daily-return based
        out["win_rate"] = float((rets > 0).mean())
        wins, losses = rets[rets > 0].sum(), abs(rets[rets < 0].sum())
        out["profit_factor"] = float(wins / losses) if losses > 0 else float("inf")
        out["n_fills"] = 0
    return out


def per_regime_breakdown(equity: pd.Series, regime_history: pd.DataFrame) -> pd.DataFrame:
    """Strategy returns bucketed by the regime active when each bet was placed.
    Proves each environment behaves as intended (calm earns, turbulent protects)."""
    rets = equity.pct_change().dropna()
    reg = regime_history.reindex(equity.index).shift(1)  # decision-time regime
    df = pd.DataFrame({"ret": rets}).join(reg[["label", "vol_rank"]]).dropna()
    if df.empty:
        return pd.DataFrame()
    rows = []
    for label, grp in df.groupby("label"):
        std = grp["ret"].std()
        rows.append(
            {
                "regime": label,
                "bars": len(grp),
                "ann_return": grp["ret"].mean() * TRADING_DAYS,
                "ann_vol": std * np.sqrt(TRADING_DAYS) if std == std else np.nan,
                "sharpe": grp["ret"].mean() / std * np.sqrt(TRADING_DAYS) if std and std > 0 else 0.0,
                "mean_vol_rank": grp["vol_rank"].mean(),
            }
        )
    return pd.DataFrame(rows).sort_values("mean_vol_rank").reset_index(drop=True)


def confidence_buckets(equity: pd.Series, regime_history: pd.DataFrame, n_buckets: int = 4) -> pd.DataFrame:
    """Performance by model-confidence quartile at decision time — higher
    confidence should earn better risk-adjusted returns if the model is real."""
    rets = equity.pct_change().dropna()
    conf = regime_history.reindex(equity.index)["confidence"].shift(1)
    df = pd.DataFrame({"ret": rets, "confidence": conf}).dropna()
    if len(df) < n_buckets * 5:
        return pd.DataFrame()
    try:
        df["bucket"] = pd.qcut(df["confidence"], n_buckets, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    rows = []
    for bucket, grp in df.groupby("bucket", observed=True):
        std = grp["ret"].std()
        rows.append(
            {
                "confidence_bucket": str(bucket),
                "mean_confidence": grp["confidence"].mean(),
                "bars": len(grp),
                "ann_return": grp["ret"].mean() * TRADING_DAYS,
                "sharpe": grp["ret"].mean() / std * np.sqrt(TRADING_DAYS) if std and std > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)
