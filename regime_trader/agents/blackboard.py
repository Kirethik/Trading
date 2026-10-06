"""Shared blackboard: how agents leave findings for each other.

A tiny append-only topic log in SQLite (stdlib; safe across the cron jobs
and the supervisor process). Payloads are JSON. Rows are data, never
instructions — readers must treat them as untrusted input.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..config import get_config
from ..utils import utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    topic   TEXT NOT NULL,
    author  TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_topic_id ON posts (topic, id);
"""


class Blackboard:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(_SCHEMA)

    @classmethod
    def from_config(cls) -> "Blackboard":
        cfg = get_config()
        return cls(cfg.resolve_path(cfg.agents.blackboard_db))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def post(self, topic: str, author: str, payload: dict) -> int:
        with closing(self._connect()) as db, db:
            cur = db.execute(
                "INSERT INTO posts (ts, topic, author, payload) VALUES (?, ?, ?, ?)",
                (utc_now_iso(), topic, author, json.dumps(payload, default=str)),
            )
            return int(cur.lastrowid)

    def read(self, topic: str, since_id: int = 0, limit: int = 100) -> list[dict]:
        """The next `limit` posts on `topic` after `since_id`, oldest first —
        a poller that advances since_id to the last id seen never skips rows."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, ts, author, payload FROM posts WHERE topic = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (topic, int(since_id), int(limit)),
            ).fetchall()
        return [{"id": i, "ts": ts, "author": a, "payload": json.loads(p)} for i, ts, a, p in rows]

    def latest(self, topic: str, limit: int = 20) -> list[dict]:
        """The newest `limit` posts on `topic`, oldest first."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, ts, author, payload FROM posts WHERE topic = ? ORDER BY id DESC LIMIT ?",
                (topic, int(limit)),
            ).fetchall()
        return [{"id": i, "ts": ts, "author": a, "payload": json.loads(p)}
                for i, ts, a, p in reversed(rows)]
