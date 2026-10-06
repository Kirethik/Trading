"""Redis-backed result cache for deterministic backtests.

When Redis is unavailable operations silently degrade to no-ops so the API
works without the cache layer.  Identical requests return from cache instantly;
first run is ~3-10 s depending on depth.

Key schema:
  bt:{hash32}   gzip-compressed JSON backtest payload  TTL 30 d
  job:{id}      RQ job status dict                      TTL 1 d
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os

_redis_client = None
_redis_ok: bool | None = None   # None = untested

BTL_SECS = 60 * 60 * 24 * 30   # 30 days
JOB_SECS = 60 * 60 * 24         # 1 day


def _client():
    global _redis_client, _redis_ok
    if _redis_ok is False:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        c = redis.from_url(url, decode_responses=False, socket_connect_timeout=2)
        c.ping()
        _redis_client = c
        _redis_ok = True
        return c
    except Exception:
        _redis_ok = False
        return None


def make_hash(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def cache_get(h: str) -> dict | None:
    r = _client()
    if r is None:
        return None
    try:
        raw = r.get(f"bt:{h}")
        return json.loads(gzip.decompress(raw).decode()) if raw else None
    except Exception:
        return None


def cache_set(h: str, payload: dict, ttl: int = BTL_SECS) -> bool:
    r = _client()
    if r is None:
        return False
    try:
        blob = gzip.compress(json.dumps(payload, default=str).encode(), compresslevel=6)
        r.setex(f"bt:{h}", ttl, blob)
        return True
    except Exception:
        return False


def cache_delete(h: str) -> bool:
    r = _client()
    if r is None:
        return False
    try:
        r.delete(f"bt:{h}")
        return True
    except Exception:
        return False


def job_set(job_id: str, payload: dict) -> None:
    r = _client()
    if r is None:
        return
    try:
        r.setex(f"job:{job_id}", JOB_SECS, json.dumps(payload, default=str).encode())
    except Exception:
        pass


def job_get(job_id: str) -> dict | None:
    r = _client()
    if r is None:
        return None
    try:
        raw = r.get(f"job:{job_id}")
        return json.loads(raw.decode()) if raw else None
    except Exception:
        return None


def is_available() -> bool:
    return _client() is not None
