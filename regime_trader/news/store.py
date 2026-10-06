"""Point-in-time news store (SQLite, stdlib).

Every article keeps two clocks: `published_at` (the source's own timestamp)
and `seen_at` (when we fetched it). A question asked "as of t" only ever sees
items that were both published and seen by t — so a back-test or a replayed
decision can never use news from its future. Sources whose publish dates are
unreliable (Exa web search) count only by `seen_at`.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import get_config

SEEN_ONLY_SOURCES = ("exa",)     # publish dates not trusted for point-in-time
POINT_IN_TIME_SOURCES = ("alpaca", "sec")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, symbols TEXT NOT NULL,
    published_at TEXT, seen_at TEXT NOT NULL, headline TEXT NOT NULL,
    summary TEXT, url TEXT, body TEXT, publisher TEXT
);
CREATE INDEX IF NOT EXISTS articles_seen ON articles (seen_at);
CREATE TABLE IF NOT EXISTS article_symbols (article_id TEXT, symbol TEXT, PRIMARY KEY (article_id, symbol));
CREATE INDEX IF NOT EXISTS article_symbols_symbol ON article_symbols (symbol);
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, as_of TEXT NOT NULL,
    sentiment REAL NOT NULL, confidence TEXT NOT NULL, impact TEXT NOT NULL, why TEXT,
    article_ids TEXT NOT NULL, model TEXT
);
CREATE INDEX IF NOT EXISTS assessments_symbol ON assessments (symbol, as_of);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
    kind TEXT NOT NULL, reason TEXT, assessment_id INTEGER, result TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS actions_symbol ON actions (symbol, ts);
"""


def iso(dt: datetime | None) -> str | None:
    """Normalized UTC ISO text: lexicographic order == time order."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


@dataclass(frozen=True)
class Article:
    source: str                    # alpaca | sec | exa | firecrawl | feargreed
    source_id: str
    symbols: tuple[str, ...]
    published_at: datetime | None
    seen_at: datetime
    headline: str
    summary: str = ""
    url: str = ""
    body: str = ""
    publisher: str = ""            # independent outlet (benzinga, sec, reuters.com ...)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"


class NewsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.executescript(_SCHEMA)

    @classmethod
    def from_config(cls) -> "NewsStore":
        cfg = get_config()
        news = cfg.get("news")
        return cls(cfg.resolve_path((news.get("db") if news is not None else None) or "state/news.db"))

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------ articles
    def add_articles(self, articles: list[Article]) -> int:
        new = 0
        with closing(self._db()) as db, db:
            for a in articles:
                cur = db.execute(
                    "INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (a.id, a.source, json.dumps(list(a.symbols)), iso(a.published_at), iso(a.seen_at),
                     a.headline[:500], a.summary[:2000], a.url[:1000], a.body[:6000],
                     (a.publisher or a.source)[:100]))
                if cur.rowcount:
                    new += 1
                    db.executemany("INSERT OR IGNORE INTO article_symbols VALUES (?,?)",
                                   [(a.id, s) for s in a.symbols])
        return new

    def articles(self, symbol: str | None = None, as_of: datetime | None = None,
                 since: datetime | None = None, limit: int = 50) -> list[dict]:
        """Newest first. With `as_of`, strictly point-in-time."""
        sql = "SELECT a.* FROM articles a"
        where, args = [], []
        if symbol:
            sql += " JOIN article_symbols s ON s.article_id = a.id"
            where.append("s.symbol = ?")
            args.append(symbol)
        if as_of is not None:
            where.append("a.seen_at <= ? AND (a.source IN (%s) OR a.published_at IS NULL OR a.published_at <= ?)"
                         % ",".join("?" * len(SEEN_ONLY_SOURCES)))
            args += [iso(as_of), *SEEN_ONLY_SOURCES, iso(as_of)]
        if since is not None:
            where.append("a.seen_at > ?")
            args.append(iso(since))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(a.published_at, a.seen_at) DESC LIMIT ?"
        args.append(int(limit))
        with closing(self._db()) as db:
            return [_article_row(r) for r in db.execute(sql, args).fetchall()]

    def fill_body(self, article_id: str, body: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("UPDATE articles SET body = ? WHERE id = ? AND (body IS NULL OR body = '')",
                       (body[:6000], article_id))

    def symbols_seen_since(self, since: datetime | None) -> list[str]:
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT DISTINCT s.symbol FROM article_symbols s JOIN articles a ON a.id = s.article_id "
                "WHERE a.seen_at > ? ORDER BY s.symbol", (iso(since) or "",)).fetchall()
        return [r[0] for r in rows]

    # --------------------------------------------------------- assessments
    def add_assessment(self, a: dict) -> int:
        with closing(self._db()) as db, db:
            cur = db.execute(
                "INSERT INTO assessments (symbol, as_of, sentiment, confidence, impact, why, article_ids, model) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a["symbol"], iso(a["as_of"]), float(a["sentiment"]), a["confidence"], a["impact"],
                 str(a.get("why", ""))[:600], json.dumps(list(a.get("article_ids", []))), a.get("model")))
            return int(cur.lastrowid)

    def latest_assessment(self, symbol: str, as_of: datetime | None = None) -> dict | None:
        sql, args = "SELECT * FROM assessments WHERE symbol = ?", [symbol]
        if as_of is not None:
            sql += " AND as_of <= ?"
            args.append(iso(as_of))
        with closing(self._db()) as db:
            row = db.execute(sql + " ORDER BY as_of DESC, id DESC LIMIT 1", args).fetchone()
        return _assessment_row(row) if row else None

    def assessments(self, limit: int = 50, symbol: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM assessments", []
        if symbol:
            sql += " WHERE symbol = ?"
            args.append(symbol)
        with closing(self._db()) as db:
            rows = db.execute(sql + " ORDER BY as_of DESC, id DESC LIMIT ?", [*args, int(limit)]).fetchall()
        return [_assessment_row(r) for r in rows]

    # ------------------------------------------------------------- actions
    def add_action(self, a: dict) -> int:
        with closing(self._db()) as db, db:
            cur = db.execute(
                "INSERT INTO actions (ts, symbol, kind, reason, assessment_id, result, details) VALUES (?,?,?,?,?,?,?)",
                (iso(a["ts"]), a["symbol"], a["kind"], str(a.get("reason", ""))[:1000],
                 a.get("assessment_id"), a.get("result"), json.dumps(a.get("details") or {}, default=str)))
            return int(cur.lastrowid)

    def actions(self, limit: int = 50, symbol: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM actions", []
        if symbol:
            sql += " WHERE symbol = ?"
            args.append(symbol)
        with closing(self._db()) as db:
            rows = db.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", [*args, int(limit)]).fetchall()
        return [_action_row(r) for r in rows]

    def open_cautions(self, as_of: datetime, hours: float) -> dict[str, dict]:
        lo = iso(as_of - timedelta(hours=hours))
        with closing(self._db()) as db:
            rows = db.execute("SELECT * FROM actions WHERE kind = 'caution' AND ts > ? AND ts <= ? ORDER BY ts",
                              (lo, iso(as_of))).fetchall()
        return {r["symbol"]: _action_row(r) for r in rows}

    def news_holdings(self) -> dict[str, dict]:
        """Symbols bought on news (submitted) and not yet exited."""
        out: dict[str, dict] = {}
        with closing(self._db()) as db:
            rows = db.execute("SELECT * FROM actions WHERE kind IN ('buy','exit') AND result = 'submitted' "
                              "ORDER BY ts, id").fetchall()
        for r in rows:
            if r["kind"] == "buy":
                out[r["symbol"]] = {"opened": parse(r["ts"]),
                                    "weight": float(json.loads(r["details"] or "{}").get("weight", 0.0))}
            else:
                out.pop(r["symbol"], None)
        return out

    def buys_on(self, day: str) -> int:
        with closing(self._db()) as db:
            return int(db.execute("SELECT COUNT(*) FROM actions WHERE kind = 'buy' AND result = 'submitted' "
                                  "AND substr(ts, 1, 10) = ?", (day,)).fetchone()[0])


def _article_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "source": r["source"], "symbols": json.loads(r["symbols"]),
            "published_at": r["published_at"], "seen_at": r["seen_at"], "headline": r["headline"],
            "summary": r["summary"] or "", "url": r["url"] or "", "body": r["body"] or "",
            "publisher": r["publisher"] or r["source"]}


def _assessment_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "symbol": r["symbol"], "as_of": r["as_of"], "sentiment": r["sentiment"],
            "confidence": r["confidence"], "impact": r["impact"], "why": r["why"] or "",
            "article_ids": json.loads(r["article_ids"]), "model": r["model"]}


def _action_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "ts": r["ts"], "symbol": r["symbol"], "kind": r["kind"], "reason": r["reason"] or "",
            "assessment_id": r["assessment_id"], "result": r["result"],
            "details": json.loads(r["details"] or "{}")}
