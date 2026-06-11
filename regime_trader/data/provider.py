"""Historical market data via Alpaca, with local caching and gap handling.

- Stocks and crypto, daily bars by default (split/dividend adjusted for stocks).
- CSV cache per symbol under data_cache/; offline fallback to cache on API error.
- SIP feed by default, automatic IEX fallback if the account lacks SIP permission.
- Weekend/holiday/halt gaps: bars are kept on their native trading calendar;
  alignment across symbols is explicit via `aligned_closes` (bounded ffill).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..config import get_config
from ..monitor.log_setup import get_logger

log = get_logger("main")

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def _cache_path(symbol: str, timeframe: str) -> Path:
    cfg = get_config()
    safe = symbol.replace("/", "-")
    return cfg.resolve_path(cfg.data.cache_dir) / f"{safe}_{timeframe}.csv"


class DataProvider:
    """Fetch + cache daily bars for stocks and crypto."""

    def __init__(self, allow_network: bool = True):
        self.cfg = get_config()
        self.allow_network = allow_network
        self._stock_client = None
        self._crypto_client = None

    # --- clients are created lazily so cache-only use needs no credentials ---
    def _stocks(self):
        if self._stock_client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._stock_client = StockHistoricalDataClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key
            )
        return self._stock_client

    def _crypto(self):
        if self._crypto_client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient

            self._crypto_client = CryptoHistoricalDataClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key
            )
        return self._crypto_client

    def get_bars(
        self,
        symbol: str,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        timeframe: str = "1Day",
        refresh: bool = False,
    ) -> pd.DataFrame:
        """Daily OHLCV indexed by UTC timestamp. Cache-first."""
        start = pd.Timestamp(start or self.cfg.data.history_start, tz="UTC")
        # SIP free tier forbids the most recent 15 minutes — stay clear of it.
        end_limit = datetime.now(timezone.utc) - timedelta(minutes=16)
        end = min(pd.Timestamp(end, tz="UTC") if end else pd.Timestamp(end_limit), pd.Timestamp(end_limit))

        path = _cache_path(symbol, timeframe)
        cached = self._read_cache(path)
        if cached is not None and not refresh:
            covers_start = cached.index[0] <= start + pd.Timedelta(days=7)
            covers_end = cached.index[-1] >= end - pd.Timedelta(days=4)
            if covers_start and covers_end:
                return cached.loc[(cached.index >= start) & (cached.index <= end)]

        if not self.allow_network:
            if cached is not None:
                return cached.loc[(cached.index >= start) & (cached.index <= end)]
            raise RuntimeError(f"no cached data for {symbol} and network disabled")

        try:
            df = self._fetch(symbol, start, end, timeframe)
        except Exception as exc:
            if cached is not None:
                log.warning("fetch failed for %s (%s); using stale cache", symbol, exc)
                return cached.loc[(cached.index >= start) & (cached.index <= end)]
            raise
        df.to_csv(path)
        return df.loc[(df.index >= start) & (df.index <= end)]

    def get_universe_bars(
        self, symbols: list[str], start=None, end=None, timeframe: str = "1Day", refresh: bool = False
    ) -> dict[str, pd.DataFrame]:
        out = {}
        for sym in symbols:
            try:
                out[sym] = self.get_bars(sym, start, end, timeframe, refresh)
            except Exception as exc:
                log.error("could not load %s: %s", sym, exc)
        return out

    # ------------------------------------------------------------------
    def _read_cache(self, path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.DatetimeIndex(df.index, tz="UTC") if df.index.tz is None else df.index.tz_convert("UTC")
            return df[BAR_COLUMNS].astype(float)
        except Exception as exc:
            log.warning("corrupt cache %s (%s); refetching", path, exc)
            return None

    def _fetch(self, symbol: str, start, end, timeframe: str) -> pd.DataFrame:
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1Day": TimeFrame.Day,
            "1Hour": TimeFrame.Hour,
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        }
        tf = tf_map.get(timeframe, TimeFrame.Day)

        if is_crypto(symbol):
            from alpaca.data.requests import CryptoBarsRequest

            req = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf, start=start.to_pydatetime(), end=end.to_pydatetime()
            )
            bars = self._crypto().get_crypto_bars(req)
            return self._tidy(bars.df, symbol)

        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest

        feeds = [self.cfg.data.feed, "iex"] if self.cfg.data.feed != "iex" else ["iex"]
        last_exc: Exception | None = None
        for feed in feeds:
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start.to_pydatetime(),
                    end=end.to_pydatetime(),
                    adjustment=Adjustment.ALL,
                    feed=DataFeed(feed),
                )
                bars = self._stocks().get_stock_bars(req)
                return self._tidy(bars.df, symbol)
            except Exception as exc:
                last_exc = exc
                log.warning("feed %s failed for %s: %s", feed, symbol, exc)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _tidy(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if df.empty:
            raise RuntimeError(f"no bars returned for {symbol}")
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df[BAR_COLUMNS].astype(float).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        idx = pd.DatetimeIndex(df.index)
        df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return df


def aligned_closes(bars: dict[str, pd.DataFrame], max_ffill: int = 5) -> pd.DataFrame:
    """Close prices aligned on the union calendar.

    Halt/holiday gaps are forward-filled at most `max_ffill` bars so a
    long-dead series cannot silently pretend to have prices.
    """
    closes = pd.DataFrame({sym: df["close"] for sym, df in bars.items()})
    return closes.sort_index().ffill(limit=max_ffill)
