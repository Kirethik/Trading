"""News source adapters. Each returns normalized `Article`s, retries with
backoff, respects a per-source daily call cap, and fails in isolation — one
source being down never stops the others.

  alpaca     company + crypto news (Benzinga), time-stamped     [existing Alpaca keys]
  sec        SEC EDGAR filings (8-K events, Form 4 insider, ..)  [SEC_USER_AGENT, no key]
  exa        web news search, used to corroborate               [EXA_API_KEY]
  firecrawl  full article text for items that lack it           [FIRECRAWL_API_KEY]
  feargreed  crypto market mood index                           [no key]

Request shapes verified against the providers' docs on 2026-10-06.
"""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

import requests

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import atomic_write_json, load_json, utc_now
from .store import Article

log = get_logger("agents")

TIMEOUT = 20
EXA_URL = "https://api.exa.ai/search"
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FEARGREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"

# common 8-K item codes, in plain English
SEC_8K_ITEMS = {
    "1.01": "signed a major agreement", "1.02": "ended a major agreement", "1.03": "bankruptcy",
    "1.05": "a cybersecurity incident", "2.01": "completed an acquisition or sale",
    "2.02": "quarterly results", "2.03": "took on significant debt", "2.05": "restructuring costs",
    "2.06": "an impairment (write-down)", "3.01": "an exchange listing problem",
    "4.02": "earlier financials can no longer be relied on", "5.01": "a change in control",
    "5.02": "a change of directors or executives", "5.07": "shareholder vote results",
    "7.01": "a public disclosure", "8.01": "other material events",
}
SEC_FORMS = {"4": "an insider trade report (Form 4)", "10-Q": "its quarterly report (10-Q)",
             "10-K": "its annual report (10-K)", "8-K": "a major-event report (8-K)"}


class SourceUnavailable(RuntimeError):
    """Source disabled, missing credentials, or over its daily call cap."""


# ------------------------------------------------------------- call budget
class CallBudget:
    """Per-source daily call counter (paid APIs: Exa, Firecrawl)."""

    def __init__(self, path, caps: dict[str, int]):
        self.path, self.caps = path, caps

    @classmethod
    def from_config(cls) -> "CallBudget":
        cfg = get_config()
        srcs = cfg.news.sources.as_dict()
        return cls(cfg.resolve_path(cfg.state_dir) / "news_calls.json",
                   {k: int(v.get("max_calls_per_day", 10**9)) for k, v in srcs.items()})

    def take(self, source: str, n: int = 1) -> None:
        today = utc_now().date().isoformat()
        state = load_json(self.path, default=None) or {}
        counts = state.get("counts", {}) if state.get("date") == today else {}
        used = int(counts.get(source, 0))
        if used + n > self.caps.get(source, 10**9):
            raise SourceUnavailable(f"{source}: daily cap of {self.caps[source]} calls reached")
        atomic_write_json(self.path, {"date": today, "counts": {**counts, source: used + n}})


def retry(fn: Callable, attempts: int = 4, base: float = 1.5, sleep: Callable = time.sleep):
    """Retry transient failures (resets, timeouts, 429, 5xx) with backoff."""
    for i in range(attempts):
        try:
            return fn()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
        except (requests.ConnectionError, requests.Timeout, ConnectionError):
            if i == attempts - 1:
                raise
        sleep(base * (2 ** i))
    return None


def _text(html_or_text: str | None, limit: int = 4000) -> str:
    plain = re.sub(r"<[^>]+>", " ", html_or_text or "")
    return re.sub(r"\s+", " ", html.unescape(plain)).strip()[:limit]


def news_symbol(symbol: str) -> str:
    """Universe symbol -> Alpaca news symbol (crypto: ETH/USD -> ETHUSD)."""
    return symbol.replace("/", "")


# ------------------------------------------------------------------ alpaca
def alpaca_news(symbols: list[str], start: datetime, end: datetime, *, client=None,
                limit: int = 50, now: datetime | None = None) -> list[Article]:
    from alpaca.data.requests import NewsRequest

    if client is None:
        from alpaca.data.historical.news import NewsClient
        cfg = get_config()
        client = NewsClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    back = {news_symbol(s): s for s in symbols}
    req = NewsRequest(symbols=",".join(back), start=start, end=end, limit=limit,
                      include_content=True, sort="desc")
    res = retry(lambda: client.get_news(req))
    items = res.data.get("news", []) if hasattr(res, "data") else getattr(res, "news", [])
    seen = now or utc_now()
    out = []
    for n in items:
        syms = tuple(back[s] for s in (n.symbols or []) if s in back)
        if not syms:
            continue
        out.append(Article(source="alpaca", source_id=str(n.id), symbols=syms, published_at=n.created_at,
                           seen_at=seen, headline=n.headline or "", summary=_text(n.summary, 1000),
                           url=n.url or "", body=_text(n.content),
                           publisher=canonical_publisher(n.source or "alpaca")))
    return out


# --------------------------------------------------------------------- sec
def _sec_headers() -> dict:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise SourceUnavailable("sec: SEC_USER_AGENT is not set in .env (name + email; no key needed)")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def sec_ticker_map(session=None) -> dict[str, int]:
    """Ticker -> CIK, cached for a week in data_cache/."""
    cfg = get_config()
    path = cfg.resolve_path(cfg.data.cache_dir) / "sec_company_tickers.json"
    cached = load_json(path, default=None)
    if cached and datetime.fromisoformat(cached["fetched"]) > utc_now() - timedelta(days=7):
        return cached["map"]
    s = session or requests
    resp = retry(lambda: _ok(s.get(SEC_TICKERS_URL, headers=_sec_headers(), timeout=TIMEOUT)))
    mapping = {row["ticker"].upper(): int(row["cik_str"]) for row in resp.json().values()}
    atomic_write_json(path, {"fetched": utc_now().isoformat(), "map": mapping})
    return mapping


def sec_filings(tickers: list[str], since: datetime, *, forms: tuple[str, ...] = ("8-K", "4", "10-Q", "10-K"),
                session=None, ticker_map: dict | None = None, now: datetime | None = None,
                sleep: Callable = time.sleep) -> list[Article]:
    headers = _sec_headers()
    s = session or requests
    cik_of = ticker_map if ticker_map is not None else sec_ticker_map(s)
    seen = now or utc_now()
    out = []
    for t in tickers:
        cik = cik_of.get(t.upper())
        if cik is None:
            continue
        sleep(0.15)  # stay well under the SEC's 10 requests/second
        data = retry(lambda: _ok(s.get(SEC_SUBMISSIONS_URL.format(cik=cik), headers=headers,
                                       timeout=TIMEOUT))).json()
        recent = (data.get("filings") or {}).get("recent") or {}
        name = data.get("name", t)
        for i, form in enumerate(recent.get("form", [])):
            if form not in forms:
                continue
            accepted = _parse_ts(recent["acceptanceDateTime"][i])
            if accepted is None or accepted <= since:
                continue
            acc = recent["accessionNumber"][i]
            items = [c.strip() for c in (recent.get("items", [""] * (i + 1))[i] or "").split(",") if c.strip()]
            what = ", ".join(SEC_8K_ITEMS[c] for c in items if c in SEC_8K_ITEMS)
            headline = f"{name} filed {SEC_FORMS.get(form, form)}" + (f": {what}" if what else "")
            out.append(Article(
                source="sec", source_id=acc, symbols=(t,), published_at=accepted, seen_at=seen,
                headline=headline, summary=recent.get("primaryDocDescription", [""] * (i + 1))[i] or "",
                url=SEC_DOC_URL.format(cik=cik, acc=acc.replace("-", ""), doc=recent["primaryDocument"][i]),
                publisher="sec"))
    return out


# --------------------------------------------------------------------- exa
def exa_news(symbol: str, query: str, since: datetime, *, api_key: str | None = None, session=None,
             n: int = 5, now: datetime | None = None) -> list[Article]:
    key = api_key or get_config().exa_api_key
    if not key:
        raise SourceUnavailable("exa: EXA_API_KEY is not set")
    body = {"query": query, "type": "auto", "category": "news", "numResults": int(n),
            "startPublishedDate": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "contents": {"text": {"maxCharacters": 3000}, "highlights": {"maxCharacters": 400},
                         "summary": {"query": f"What does this mean for {symbol}'s stock price?"}}}
    s = session or requests
    data = retry(lambda: _ok(s.post(EXA_URL, json=body, headers={"x-api-key": key}, timeout=TIMEOUT))).json()
    seen = now or utc_now()
    out = []
    for r in data.get("results", []):
        url = r.get("url") or ""
        highlights = " … ".join(r.get("highlights") or [])
        out.append(Article(source="exa", source_id=str(r.get("id") or url), symbols=(symbol,),
                           published_at=_parse_ts(r.get("publishedDate")), seen_at=seen,
                           headline=r.get("title") or url, summary=_text(r.get("summary") or highlights, 1000),
                           url=url, body=_text(r.get("text")), publisher=_domain(url)))
    return out


# --------------------------------------------------------------- firecrawl
def firecrawl_text(url: str, *, api_key: str | None = None, session=None) -> str:
    key = api_key or get_config().firecrawl_api_key
    if not key:
        raise SourceUnavailable("firecrawl: FIRECRAWL_API_KEY is not set")
    if urlparse(url).scheme not in ("http", "https"):
        return ""
    s = session or requests
    data = retry(lambda: _ok(s.post(
        FIRECRAWL_URL, json={"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 30000},
        headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT + 30))).json()
    return _text((data.get("data") or {}).get("markdown", ""), 6000) if data.get("success") else ""


# --------------------------------------------------------------- feargreed
def fear_greed(symbols: tuple[str, ...], *, session=None, now: datetime | None = None) -> list[Article]:
    s = session or requests
    data = retry(lambda: _ok(s.get(FEARGREED_URL, timeout=TIMEOUT))).json()
    rows = data.get("data") or []
    if not rows:
        return []
    r = rows[0]
    ts = datetime.fromtimestamp(int(r["timestamp"]), tz=timezone.utc)
    return [Article(source="feargreed", source_id=r["timestamp"], symbols=symbols, published_at=ts,
                    seen_at=now or utc_now(),
                    headline=f"Crypto Fear & Greed index: {r['value']}/100 ({r['value_classification']})",
                    summary="Daily crypto market mood (0 = extreme fear, 100 = extreme greed). Source: alternative.me",
                    url="https://alternative.me/crypto/fear-and-greed-index/", publisher="alternative.me")]


# ----------------------------------------------------------------- helpers
def _ok(resp):
    resp.raise_for_status()
    return resp


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_TWO_PART_TLDS = {"co.uk", "com.au", "co.in", "co.jp", "com.br"}


def canonical_publisher(name_or_url: str) -> str:
    """'https://www.reuters.com/x' / 'reuters.com' / 'Reuters' -> 'reuters', so
    one outlet syndicated through two feeds never counts as two sources."""
    raw = (name_or_url or "").lower().strip()
    host = urlparse(raw).netloc if "://" in raw else raw
    host = host.split(":")[0]
    parts = [p for p in host.split(".") if p and p != "www"]
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else "web"


def _domain(url: str) -> str:
    return canonical_publisher(url)
