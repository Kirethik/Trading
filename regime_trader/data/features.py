"""Causal feature engineering. Every indicator looks strictly backward.

The no-look-ahead guarantee starts here: nothing in this module may use
centered windows, future shifts, or full-sample statistics.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def realized_vol(close: pd.Series, window: int = 21, annualize: bool = True) -> pd.Series:
    rv = log_returns(close).rolling(window).std()
    return rv * np.sqrt(TRADING_DAYS) if annualize else rv


def intraday_range(df: pd.DataFrame) -> pd.Series:
    """Normalized high-low range — a same-bar dispersion proxy."""
    return (df["high"] - df["low"]) / df["close"]


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def rsi(close: pd.Series, window: int = 2) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(loss.notna(), np.nan)


def bollinger(close: pd.Series, window: int = 20, n_std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(window).mean()
    sd = close.rolling(window).std()
    return pd.DataFrame({"mid": mid, "upper": mid + n_std * sd, "lower": mid - n_std * sd})


def donchian(df: pd.DataFrame, entry_window: int = 55, exit_window: int = 20) -> pd.DataFrame:
    """Channels shifted by one bar: today's breakout compares to PRIOR highs/lows."""
    return pd.DataFrame(
        {
            "entry_high": df["high"].rolling(entry_window).max().shift(1),
            "exit_low": df["low"].rolling(exit_window).min().shift(1),
        }
    )


def momentum(close: pd.Series, lookback: int, skip: int = 0) -> pd.Series:
    """Total return over [t-lookback, t-skip] — e.g. 12-1 momentum: (252, 21)."""
    return close.shift(skip) / close.shift(lookback) - 1.0


def regime_features(market_df: pd.DataFrame, vol_window: int = 21) -> pd.DataFrame:
    """Feature matrix for the volatility-regime HMM (market proxy bars in, X out).

    Columns: log_return, log_realized_vol, intraday_range. Rows with any NaN
    (warm-up) are dropped, so X.index marks bars with fully-formed features.
    """
    feats = pd.DataFrame(
        {
            "log_return": log_returns(market_df["close"]),
            "log_realized_vol": np.log(realized_vol(market_df["close"], vol_window).clip(lower=1e-8)),
            "intraday_range": intraday_range(market_df),
        }
    )
    return feats.dropna()
