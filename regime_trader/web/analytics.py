"""Post-backtest analytics helpers: time-series decompositions for the UI.

Everything here takes a BacktestResult (or its equity/trades sub-series) and
returns plain Python lists/dicts ready for JSON serialisation to the frontend.
No I/O, no randomness, no external calls — pure pandas transform.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..govern.metrics_ext import drawdown_series as _dd_series
from ..backtest.metrics import compute_metrics


def _ts_ms(idx) -> list[int]:
    return [int(pd.Timestamp(ts).timestamp() * 1000) for ts in idx]


def monthly_returns(equity: pd.Series) -> list[dict]:
    """Monthly PnL as [{year, month, return}] — powers the calendar heatmap."""
    if len(equity) < 2:
        return []
    monthly = equity.resample("ME").last().dropna()
    if len(monthly) < 2:
        return []
    pct = monthly.pct_change().dropna()
    return [
        {"year": int(ts.year), "month": int(ts.month), "return": float(v)}
        for ts, v in pct.items()
        if math.isfinite(v)
    ]


def yearly_returns(equity: pd.Series) -> list[dict]:
    """Annual PnL as [{year, return}] — powers the yearly bar chart."""
    if len(equity) < 2:
        return []
    yearly = equity.resample("YE").last().dropna()
    if len(yearly) < 2:
        return []
    pct = yearly.pct_change().dropna()
    return [
        {"year": int(ts.year), "return": float(v)}
        for ts, v in pct.items()
        if math.isfinite(v)
    ]


def rolling_sharpe(equity: pd.Series, window: int = 90, ppy: int = 252) -> list[dict]:
    """Rolling Sharpe ratio as [{ts_ms, value}]."""
    if len(equity) < window + 2:
        return []
    rets = equity.pct_change().dropna()
    ann = math.sqrt(ppy)
    roll_mean = rets.rolling(window).mean()
    roll_std = rets.rolling(window).std()
    sharpe = (roll_mean / roll_std * ann).dropna()
    ts = _ts_ms(sharpe.index)
    return [{"ts": t, "value": float(v)} for t, v in zip(ts, sharpe.values) if math.isfinite(v)]


def rolling_vol(equity: pd.Series, window: int = 21, ppy: int = 252) -> list[dict]:
    """Rolling annualised volatility as [{ts_ms, value}]."""
    if len(equity) < window + 2:
        return []
    rets = equity.pct_change().dropna()
    ann = math.sqrt(ppy)
    vol = (rets.rolling(window).std() * ann).dropna()
    ts = _ts_ms(vol.index)
    return [{"ts": t, "value": float(v)} for t, v in zip(ts, vol.values) if math.isfinite(v)]


def drawdown_series_json(equity: pd.Series) -> list[list]:
    """Drawdown as [[ts_ms, value]] — ready for the area chart."""
    if len(equity) < 2:
        return []
    dd = _dd_series(equity).dropna()
    ts = _ts_ms(dd.index)
    return [[t, float(v)] for t, v in zip(ts, dd.values) if math.isfinite(v)]


def equity_series_json(equity: pd.Series, initial_capital: float = 1.0) -> list[list]:
    """Equity curve as [[ts_ms, value]] scaled to initial_capital."""
    if len(equity) < 2:
        return []
    base = float(equity.iloc[0])
    if base == 0:
        return []
    scale = initial_capital / base
    ts = _ts_ms(equity.index)
    return [[t, float(v) * scale] for t, v in zip(ts, equity.values) if math.isfinite(v)]


def price_series_json(bars: pd.DataFrame) -> list[dict]:
    """OHLCV as [{time, open, high, low, close, volume}] for the price chart."""
    recs = []
    for ts, row in bars.iterrows():
        recs.append({
            "time": int(pd.Timestamp(ts).timestamp() * 1000),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        })
    return recs


def extract_signals(trades: pd.DataFrame) -> list[dict]:
    """Buy/sell markers for the price chart: [{ts_ms, price, side, reason}].

    Handles both round-trip ledgers (entry_ts/exit_ts columns) and fill ledgers
    (ts column with side column). Falls back gracefully when columns are missing.
    """
    if trades is None or len(trades) == 0:
        return []

    out = []
    cols = set(trades.columns)

    if "entry_ts" in cols and "exit_ts" in cols:
        for _, row in trades.iterrows():
            ep = row.get("entry_price") or row.get("fill_price")
            xp = row.get("exit_price") or row.get("fill_price")
            reason = str(row.get("reason", "exit"))
            if pd.notna(row.get("entry_ts")) and ep and math.isfinite(float(ep)):
                out.append({
                    "ts": int(pd.Timestamp(row["entry_ts"]).timestamp() * 1000),
                    "price": float(ep), "side": "buy", "reason": "entry",
                })
            if pd.notna(row.get("exit_ts")) and xp and math.isfinite(float(xp)):
                out.append({
                    "ts": int(pd.Timestamp(row["exit_ts"]).timestamp() * 1000),
                    "price": float(xp), "side": "sell", "reason": reason,
                })
    elif "ts" in cols:
        for _, row in trades.iterrows():
            side = str(row.get("side", "buy")).lower()
            price = row.get("fill_price") or row.get("price")
            if price is None or not math.isfinite(float(price)):
                continue
            out.append({
                "ts": int(pd.Timestamp(row["ts"]).timestamp() * 1000),
                "price": float(price), "side": side,
                "reason": str(row.get("reason", side)),
            })

    return sorted(out, key=lambda x: x["ts"])


def per_regime_list(per_regime_df: pd.DataFrame) -> list[dict]:
    """Convert per_regime DataFrame to JSON-serialisable list."""
    if per_regime_df is None or per_regime_df.empty:
        return []
    out = []
    for _, row in per_regime_df.iterrows():
        rec = {}
        for k, v in row.items():
            if pd.isna(v):
                continue
            if isinstance(v, (float, np.floating)):
                rec[k] = float(v)
            elif isinstance(v, (int, np.integer)):
                rec[k] = int(v)
            else:
                rec[k] = str(v)
        out.append(rec)
    return out


def round_trips_json(trades: pd.DataFrame, last_ts_ms: int) -> list[dict]:
    """Pair buy/sell fills into round-trip trade regions for the chart overlay.

    Each entry carries entry_ts, exit_ts (both ms-epoch), prices, holding_days,
    return_pct, and is_open flag. Open positions use last_ts_ms as exit_ts so
    the chart can shade right up to the final candle and label it "Open".
    """
    if trades is None or len(trades) == 0:
        return []
    out: list[dict] = []
    for symbol in trades["symbol"].unique():
        sym = trades[trades["symbol"] == symbol].sort_values("ts")
        open_entry: dict | None = None
        for _, row in sym.iterrows():
            ts_ms = int(pd.Timestamp(row["ts"]).timestamp() * 1000)
            side = str(row.get("side", "buy")).lower()
            price = float(row.get("price", 0) or 0)
            if side == "buy" and open_entry is None:
                open_entry = {"ts_ms": ts_ms, "price": price}
            elif side in ("sell", "stop") and open_entry is not None:
                ep = open_entry["price"]
                ret = (price / ep - 1) if ep > 0 else 0.0
                days = (ts_ms - open_entry["ts_ms"]) / 86_400_000.0
                out.append({
                    "symbol": str(symbol),
                    "entry_ts": open_entry["ts_ms"],
                    "entry_price": round(ep, 6),
                    "exit_ts": ts_ms,
                    "exit_price": round(price, 6),
                    "holding_days": round(days, 1),
                    "return_pct": round(ret * 100, 2),
                    "is_open": False,
                    "profitable": bool(ret > 0),
                })
                open_entry = None
        if open_entry is not None:
            days = (last_ts_ms - open_entry["ts_ms"]) / 86_400_000.0
            out.append({
                "symbol": str(symbol),
                "entry_ts": open_entry["ts_ms"],
                "entry_price": round(open_entry["price"], 6),
                "exit_ts": last_ts_ms,
                "exit_price": None,
                "holding_days": round(days, 1),
                "return_pct": None,
                "is_open": True,
                "profitable": None,
            })
    return sorted(out, key=lambda x: x["entry_ts"])


def assemble_payload(
    result,                     # BacktestResult
    bars: dict,                 # {symbol: DataFrame}
    symbol: str,
    timeframe: str,
    ppy: int,
    initial_capital: float = 10_000.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Build the full JSON payload the frontend expects from a BacktestResult.

    Slices equity/trades to [start_date, end_date] when provided, then runs
    all analytics helpers and assembles the canonical response shape.
    """
    equity = result.equity.copy()
    trades = result.trades

    if start_date:
        lo = pd.Timestamp(start_date, tz="UTC")
        equity = equity[equity.index >= lo]
        if trades is not None and len(trades) and "ts" in trades.columns:
            mask = pd.to_datetime(trades["ts"], utc=True) >= lo
            trades = trades[mask.values]
    if end_date:
        hi = pd.Timestamp(end_date, tz="UTC")
        equity = equity[equity.index <= hi]
        if trades is not None and len(trades) and "ts" in trades.columns:
            mask = pd.to_datetime(trades["ts"], utc=True) <= hi
            trades = trades[mask.values]

    closed_trades = (trades if (trades is not None and len(trades) > 0) else None)
    metrics = compute_metrics(equity, closed_trades, periods_per_year=ppy)

    price_bars = bars.get(symbol, pd.DataFrame())
    if not price_bars.empty and start_date:
        price_bars = price_bars[price_bars.index >= pd.Timestamp(start_date, tz="UTC")]
    if not price_bars.empty and end_date:
        price_bars = price_bars[price_bars.index <= pd.Timestamp(end_date, tz="UTC")]

    roll_window = max(10, min(90, len(equity) // 10))
    vol_window = max(5, min(21, len(equity) // 20))

    _trades_df = trades if trades is not None else pd.DataFrame()
    last_ts_ms = int(equity.index[-1].timestamp() * 1000) if len(equity) > 0 else 0
    return {
        "metrics": metrics,
        "equity": equity_series_json(equity, initial_capital),
        "drawdown": drawdown_series_json(equity),
        "price": price_series_json(price_bars),
        "signals": extract_signals(_trades_df),
        "trades": round_trips_json(_trades_df, last_ts_ms),
        "monthly": monthly_returns(equity),
        "yearly": yearly_returns(equity),
        "rolling_sharpe": rolling_sharpe(equity, window=roll_window, ppy=ppy),
        "rolling_vol": rolling_vol(equity, window=vol_window, ppy=ppy),
        "per_regime": per_regime_list(result.per_regime),
    }
