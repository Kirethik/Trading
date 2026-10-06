"""Deep-history crypto data from free, keyless public APIs.

Alpaca's crypto history only reaches ~2021 and has no native 4h bar, which is
too shallow to see multiple ETH market cycles. This module pulls long history
straight from exchange REST endpoints and writes it into the SAME CSV cache the
`DataProvider` reads (`data_cache/ETH-USD_1Day.csv`, `..._4Hour.csv`), so the
backtest/research path consumes it transparently with no Alpaca call.

Sources (in preference order):
- Binance spot (`api.binance.com`, ETHUSDT) — primary. Native 1d AND 4h bars
  from 2017-08-17; one venue keeps the daily and 4h series mutually consistent.
- Binance.US (`api.binance.us`, ETHUSD) — geo fallback if `.com` is blocked.
- Coinbase Exchange (`ETH-USD`) — true-USD daily back to 2016-05; history
  extender / cross-check. No native 4h (would need resampling), so 4h stays Binance.

Perp funding rate (full history) and open interest (short window only — a free
Binance limitation we surface honestly) feed the research-only long/short study.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd

from ..monitor.log_setup import get_logger
from .provider import BAR_COLUMNS, _cache_path

log = get_logger("main")

# our timeframe label -> Binance kline interval
BINANCE_INTERVAL = {"1Day": "1d", "4Hour": "4h", "1Hour": "1h", "15Min": "15m", "5Min": "5m"}
# our timeframe label -> Coinbase granularity (seconds). No 4h on Coinbase.
COINBASE_GRANULARITY = {"1Day": 86400, "1Hour": 3600, "15Min": 900, "5Min": 300}

_HEADERS = {"User-Agent": "regime-trader/research"}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _get(url: str, params: dict, timeout: int = 30, retries: int = 5) -> object:
    """GET JSON with backoff on rate-limit (429/418) and transient errors.

    Uses stdlib urllib (not requests) so a broken REQUESTS_CA_BUNDLE env var in
    the host can't break TLS — urllib uses Python's default SSL trust store.
    """
    full = f"{url}?{urllib.parse.urlencode(params)}"
    delay = 1.0
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(full, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 418):  # rate limited / temp ban
                wait = float(exc.headers.get("Retry-After", delay)) if exc.headers else delay
                log.warning("rate limited (%s); sleeping %.1fs", exc.code, wait)
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last_exc})")


# --------------------------------------------------------------------- Binance
def fetch_binance_klines(
    timeframe: str = "1Day",
    binance_symbol: str = "ETHUSDT",
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    base_url: str = "https://api.binance.com",
) -> pd.DataFrame:
    """Paginated OHLCV from Binance spot. Returns the standard BAR_COLUMNS frame
    indexed by UTC bar-open time, with the still-forming final bar dropped."""
    interval = BINANCE_INTERVAL[timeframe]
    start_ms = 0 if start is None else int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = _now_ms() if end is None else int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)

    rows: list[list] = []
    cursor = start_ms
    while True:
        batch = _get(
            f"{base_url}/api/v3/klines",
            {"symbol": binance_symbol, "interval": interval, "startTime": cursor, "limit": 1000},
        )
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1][0]
        if len(batch) < 1000 or last_open >= end_ms:
            break
        cursor = last_open + 1
        time.sleep(0.25)  # be polite; ~well under Binance weight limits

    if not rows:
        raise RuntimeError(f"no Binance bars for {binance_symbol} {interval}")

    # kline row: [openTime, o, h, l, c, v, closeTime, ...]
    now = _now_ms()
    recs = []
    for k in rows:
        if k[6] > now:  # closeTime in the future -> bar still forming, skip
            continue
        recs.append((k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
    df = pd.DataFrame(recs, columns=["ts", *BAR_COLUMNS])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "timestamp"
    return df[(df.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))]


# -------------------------------------------------------------------- Coinbase
def fetch_coinbase_candles(
    timeframe: str = "1Day",
    product: str = "ETH-USD",
    start: str | datetime | None = "2016-05-01",
    end: str | datetime | None = None,
) -> pd.DataFrame:
    """Paginated OHLCV from Coinbase Exchange (max 300 candles/request).

    candle = [time(s), low, high, open, close, volume]. True-USD; longest daily
    history. Used to extend the daily series before Binance's 2017-08 inception.
    """
    gran = COINBASE_GRANULARITY[timeframe]
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp(datetime.now(timezone.utc))
    span = pd.Timedelta(seconds=gran * 300)

    recs: list[tuple] = []
    win_start = start_ts
    while win_start < end_ts:
        win_end = min(win_start + span, end_ts)
        batch = _get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            {"granularity": gran, "start": win_start.isoformat(), "end": win_end.isoformat()},
        )
        for c in batch or []:
            recs.append((c[0] * 1000, float(c[3]), float(c[2]), float(c[1]), float(c[4]), float(c[5])))
        win_start = win_end
        time.sleep(0.2)

    if not recs:
        raise RuntimeError(f"no Coinbase candles for {product}")
    df = pd.DataFrame(recs, columns=["ts", *BAR_COLUMNS])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "timestamp"
    return df


# ---------------------------------------------------------- perp funding / OI
def fetch_binance_funding(
    binance_symbol: str = "ETHUSDT", start: str | datetime | None = None
) -> pd.DataFrame:
    """Full perp funding-rate history (8-hourly). Columns: funding_rate, mark_price.

    NOTE: Binance returns the most-recent batch when startTime is 0/missing, so
    we anchor at perp inception (2019-09) and page forward explicitly.
    """
    start_ms = int(pd.Timestamp(start or "2019-09-01", tz="UTC").timestamp() * 1000)
    out: list[tuple] = []
    cursor = start_ms
    while True:
        batch = _get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            {"symbol": binance_symbol, "startTime": cursor, "limit": 1000},
        )
        if not batch:
            break
        for f in batch:
            out.append((f["fundingTime"], float(f["fundingRate"]), float(f.get("markPrice") or "nan")))
        last = batch[-1]["fundingTime"]
        if len(batch) < 1000:
            break
        cursor = last + 1
        time.sleep(0.25)
    if not out:
        raise RuntimeError("no funding data")
    df = pd.DataFrame(out, columns=["ts", "funding_rate", "mark_price"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts").sort_index()[~df.set_index("ts").index.duplicated(keep="last")]


def fetch_binance_open_interest(
    binance_symbol: str = "ETHUSDT", period: str = "4h"
) -> pd.DataFrame:
    """Open-interest history. NOTE: Binance only serves ~last 30 days free, so
    this is a short window — the research treats OI as illustrative, not a deep
    backtest feature. Columns: open_interest, oi_value."""
    batch = _get(
        "https://fapi.binance.com/futures/data/openInterestHist",
        {"symbol": binance_symbol, "period": period, "limit": 500},
    )
    if not batch:
        raise RuntimeError("no open-interest data")
    df = pd.DataFrame(
        [(o["timestamp"], float(o["sumOpenInterest"]), float(o["sumOpenInterestValue"])) for o in batch],
        columns=["ts", "open_interest", "oi_value"],
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts").sort_index()


# ------------------------------------------------------------------- cache I/O
def _validate_bars(df: pd.DataFrame, timeframe: str) -> dict:
    """Sanity report: row count, span, and the worst inter-bar gap (in bars)."""
    expected = {"1Day": pd.Timedelta(days=1), "4Hour": pd.Timedelta(hours=4),
                "1Hour": pd.Timedelta(hours=1), "15Min": pd.Timedelta(minutes=15),
                "5Min": pd.Timedelta(minutes=5)}.get(timeframe, pd.Timedelta(days=1))
    deltas = df.index.to_series().diff().dropna()
    max_gap = (deltas.max() / expected) if len(deltas) else 0
    return {
        "rows": int(len(df)),
        "start": str(df.index[0].date()) if len(df) else None,
        "end": str(df.index[-1].date()) if len(df) else None,
        "max_gap_bars": round(float(max_gap), 1),
    }


def update_cache(
    symbol: str = "ETH/USD",
    timeframe: str = "1Day",
    source: str = "binance",
    start: str | None = None,
) -> dict:
    """Fetch `symbol`/`timeframe` from `source` and write the provider cache CSV.

    Returns a small validation dict (rows / span / max gap). `source` is one of
    'binance', 'binanceus', 'coinbase'.
    """
    if source == "binance":
        df = fetch_binance_klines(timeframe, "ETHUSDT", start=start, base_url="https://api.binance.com")
    elif source == "binanceus":
        df = fetch_binance_klines(timeframe, "ETHUSD", start=start, base_url="https://api.binance.us")
    elif source == "coinbase":
        df = fetch_coinbase_candles(timeframe, "ETH-USD", start=start or "2016-05-01")
    else:
        raise ValueError(f"unknown source: {source}")

    path = _cache_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[BAR_COLUMNS].to_csv(path)
    report = _validate_bars(df, timeframe)
    log.info("cached %s %s from %s: %s", symbol, timeframe, source, report)
    return {"path": str(path), "source": source, **report}


def update_funding_cache(symbol: str = "ETH/USD") -> dict:
    safe = symbol.replace("/", "-")
    df = fetch_binance_funding("ETHUSDT")
    path = _cache_path(symbol, "funding").with_name(f"{safe}_funding.csv")
    df.to_csv(path)
    return {"path": str(path), "rows": int(len(df)), "start": str(df.index[0].date()),
            "end": str(df.index[-1].date())}


def update_oi_cache(symbol: str = "ETH/USD", period: str = "4h") -> dict:
    safe = symbol.replace("/", "-")
    df = fetch_binance_open_interest("ETHUSDT", period)
    path = _cache_path(symbol, "oi").with_name(f"{safe}_oi.csv")
    df.to_csv(path)
    return {"path": str(path), "rows": int(len(df)), "start": str(df.index[0].date()),
            "end": str(df.index[-1].date()), "note": "Binance free OI history ~30d only"}


# --------------------------------------------------------- any-symbol helpers
# Maps our internal symbol names (slash-separated) to Binance spot tickers.
SYMBOL_TO_BINANCE: dict[str, str] = {
    "ETH/USD": "ETHUSDT",
    "BTC/USD": "BTCUSDT",
    "SOL/USD": "SOLUSDT",
    "ADA/USD": "ADAUSDT",
    "DOGE/USD": "DOGEUSDT",
    "AVAX/USD": "AVAXUSDT",
    "LINK/USD": "LINKUSDT",
    "LTC/USD": "LTCUSDT",
}


def write_cache(symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    """Write a pre-fetched DataFrame into the DataProvider CSV cache."""
    path = _cache_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[BAR_COLUMNS].to_csv(path)
    log.info("wrote cache %s %s (%d rows)", symbol, timeframe, len(df))


def fetch_any_crypto(
    symbol: str,
    timeframe: str = "1Day",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV for any supported crypto symbol from Binance and update cache.

    Raises ValueError for unknown symbols. Returned DataFrame uses BAR_COLUMNS
    indexed by UTC timestamp — identical to what DataProvider.get_bars() returns.
    """
    binance_sym = SYMBOL_TO_BINANCE.get(symbol)
    if binance_sym is None:
        raise ValueError(f"No Binance mapping for {symbol}. Known: {list(SYMBOL_TO_BINANCE)}")
    df = fetch_binance_klines(timeframe=timeframe, binance_symbol=binance_sym,
                               start=start, end=end)
    write_cache(symbol, timeframe, df)
    return df
