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


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average. Causal: adjust=False so each point uses only
    past bars; min_periods=span suppresses an under-warmed early value."""
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line, histogram — all from backward-only EMAs."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line})


def adx(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    """Wilder's ADX with directional indicators (+DI/-DI). ADX in [0,100] gauges
    trend STRENGTH (not direction); the DIs give direction. All Wilder-smoothed
    (backward-only EWM), matching atr()/rsi()."""
    high, low, close = df["high"], df["low"], df["close"]
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr_w = tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_line = dx.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    return pd.DataFrame({"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di})


def vwap(df: pd.DataFrame, window: int | None = None) -> pd.Series:
    """Volume-weighted average price on typical price ((H+L+C)/3). Rolling over
    `window` bars, or cumulative (anchored at series start) when window is None."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    if window is None:
        return pv.cumsum() / df["volume"].cumsum().replace(0.0, np.nan)
    return pv.rolling(window).sum() / df["volume"].rolling(window).sum().replace(0.0, np.nan)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-balance volume: cumulative volume signed by close-to-close direction."""
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def volume_zscore(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling z-score of volume — a causal volume-spike / participation gauge."""
    v = df["volume"]
    return (v - v.rolling(window).mean()) / v.rolling(window).std().replace(0.0, np.nan)


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
