"""Research-only long/short + funding study (production stays long-only spot).

A small vectorized simulator that answers two questions the brief asks about the
instrument choice:

  1. Does adding SHORTS (capturing ETH bear markets) beat long-or-flat spot,
     once realistic perpetual FUNDING costs are paid?
  2. Do funding rate / open interest carry any usable signal?

It deliberately does NOT touch the production engine (which is long-only spot by
design). Weights are signed (-1..+1), costs are charged on turnover, and funding
is charged to the perp position each day. Metrics use the same crypto
annualization (365) as the rest of the research for apples-to-apples comparison.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.metrics import compute_metrics
from ..config import get_config
from ..data import features as F
from ..data.provider import DataProvider

PPY = 365


def load_daily() -> pd.DataFrame:
    return DataProvider(allow_network=False).get_bars("ETH/USD", timeframe="1Day")


def load_funding_daily(index: pd.DatetimeIndex) -> pd.Series:
    """8-hourly perp funding summed to a daily rate, aligned to the bar calendar.
    Zero before perp inception (2019-11) so the pre-perp era is simply un-funded."""
    cfg = get_config()
    path = cfg.resolve_path(cfg.research.longshort.funding_file)
    if not path.exists():
        return pd.Series(0.0, index=index)
    f = pd.read_csv(path, index_col=0)["funding_rate"]
    f.index = pd.to_datetime(f.index, utc=True, format="ISO8601")
    daily = f.resample("1D").sum()
    return daily.reindex(index).fillna(0.0)


def _signal_trend(close: pd.Series, window: int = 100, allow_short: bool = True) -> pd.Series:
    """Canonical trend signal: +1 above SMA, (-1 or 0) below."""
    above = close > F.sma(close, window)
    return pd.Series(np.where(above, 1.0, -1.0 if allow_short else 0.0), index=close.index)


def simulate(close: pd.Series, weight: pd.Series, cost_bps: float = 20.0,
             funding: pd.Series | None = None) -> tuple[pd.Series, dict]:
    """Vectorized signed-weight backtest. Signal at t is applied to t+1 return
    (no look-ahead). Returns (equity, metrics)."""
    ret = close.pct_change().fillna(0.0)
    w = weight.shift(1).fillna(0.0)                       # act on next bar
    turnover = (w - w.shift(1).fillna(0.0)).abs()
    cost = turnover * cost_bps / 10_000.0
    fund = (w * funding.reindex(close.index).fillna(0.0)) if funding is not None else 0.0
    strat = w * ret - cost - fund                        # funding: long pays when rate>0
    equity = 100_000.0 * (1.0 + strat).cumprod()
    return equity, compute_metrics(equity, None, periods_per_year=PPY)


def _cycle_returns(close, weight, cost_bps, funding, cycles) -> dict:
    out = {}
    for name, lo, hi in cycles:
        m = (close.index >= pd.Timestamp(lo, tz="UTC")) & (close.index <= pd.Timestamp(hi, tz="UTC"))
        if m.sum() < 10:
            continue
        eq, met = simulate(close[m], weight[m], cost_bps, funding[m] if funding is not None else None)
        out[name] = {k: round(float(met.get(k, 0.0)), 3) for k in ("cagr", "sharpe", "max_drawdown")}
    return out


def funding_signal_analysis(close: pd.Series, funding_daily: pd.Series) -> dict:
    """Is funding a crowding signal? Correlate the funding rate with FORWARD
    returns — persistently high funding (crowded longs) often precedes weakness."""
    fwd = close.pct_change().shift(-1)          # next-day return
    fund = funding_daily.reindex(close.index).fillna(0.0)
    valid = fund.replace(0.0, np.nan).dropna().index
    if len(valid) < 100:
        return {"note": "insufficient funding history"}
    f, r = fund.loc[valid], fwd.loc[valid]
    hi = f > f.quantile(0.8)                    # top-quintile funding days
    lo = f < f.quantile(0.2)
    return {
        "corr_funding_vs_fwd_ret": round(float(f.corr(r)), 4),
        "mean_fwd_ret_high_funding": round(float(r[hi].mean()), 5),
        "mean_fwd_ret_low_funding": round(float(r[lo].mean()), 5),
        "avg_daily_funding_rate": round(float(f.mean()), 6),
        "annualized_funding_drag_for_perp_long": round(float(f.mean() * 365), 4),
        "funding_history_days": int(len(valid)),
    }


def run_longshort_study(out_dir: str | None = None) -> dict:
    cfg = get_config()
    out = Path(out_dir or cfg.research.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    df = load_daily()
    close = df["close"]
    funding = load_funding_daily(close.index)
    cost = float(cfg.research.commission_bps) + float(cfg.research.spread_bps) + float(cfg.research.base_slippage_bps)

    cycles = [("2018-bear", "2018-01-01", "2018-12-31"), ("2021-bull", "2021-01-01", "2021-12-31"),
              ("2022-bear", "2022-01-01", "2022-12-31"), ("2023-26", "2023-01-01", "2026-12-31")]

    variants = {}
    # 1) long-only spot (no funding) — the production model
    w_lo = _signal_trend(close, 100, allow_short=False)
    eq, m = simulate(close, w_lo, cost, funding=None)
    variants["long_only_spot"] = {"metrics": {k: round(float(m.get(k, 0.0)), 3)
                                  for k in ("cagr", "sharpe", "calmar", "max_drawdown", "recovery_factor")},
                                  "per_cycle": _cycle_returns(close, w_lo, cost, None, cycles)}
    # 2) long/short perp WITH funding
    w_ls = _signal_trend(close, 100, allow_short=True)
    eq, m = simulate(close, w_ls, cost, funding=funding)
    variants["long_short_perp_funded"] = {"metrics": {k: round(float(m.get(k, 0.0)), 3)
                                          for k in ("cagr", "sharpe", "calmar", "max_drawdown", "recovery_factor")},
                                          "per_cycle": _cycle_returns(close, w_ls, cost, funding, cycles)}
    # 3) long/short perp WITHOUT funding (to isolate funding drag)
    eq, m = simulate(close, w_ls, cost, funding=None)
    variants["long_short_perp_nofunding"] = {"metrics": {k: round(float(m.get(k, 0.0)), 3)
                                             for k in ("cagr", "sharpe", "calmar", "max_drawdown", "recovery_factor")}}

    result = {
        "cost_bps_per_side": cost,
        "signal": "price vs 100-bar SMA (canonical trend)",
        "variants": variants,
        "funding_signal": funding_signal_analysis(close, funding),
    }
    with open(out / "longshort_study.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    return result
