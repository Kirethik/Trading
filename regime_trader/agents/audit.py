"""Append-only JSONL audit trail of every agent call, tool call and order.

One rotated backup (`<file>.1`) keeps disk bounded on the EC2 box.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections import deque
from pathlib import Path

from ..config import get_config
from ..utils import utc_now_iso


def payload_hash(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: str | Path, max_bytes: int):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)

    @classmethod
    def from_config(cls) -> "AuditLog":
        a = get_config().agents
        return cls(get_config().resolve_path(a.audit_file), a.audit_max_bytes)

    def write(self, record: dict) -> None:
        line = json.dumps({"ts": utc_now_iso(), **record}, default=str) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # lock a sidecar file, not the log itself: rotation renames the log, and
        # a writer queued on the old inode would otherwise append into `.1`
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
                if size and size + len(line) > self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def tail(self, n: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = deque(fh, maxlen=n)
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn line from a crash — skip, never fail the reader
        return out
