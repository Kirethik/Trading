"""Password gate: a stdlib-`hmac`-signed session cookie (no extra dependency).

The cookie value is ``<expiry>.<sig>`` where ``sig = HMAC-SHA256(secret,
"v1|<expiry>")``. The password and the signing secret are read from the
environment (names configured under ``web:`` in config.yaml) — never from the
repo. ``.env`` (gitignored) holds the real values on EC2.
"""
from __future__ import annotations

import hmac
import os
import time
from hashlib import sha256

from fastapi import Request

from ..config import get_config

SESSION_COOKIE = "rt_session"
# Used only when DASHBOARD_SECRET is unset (local dev). EC2 sets a real secret.
_FALLBACK_SECRET = "regime-trader-dev-secret-set-DASHBOARD_SECRET-in-.env"


def _web_cfg():
    try:
        return get_config().get("web", None)
    except Exception:
        return None


def _env_for(key: str, default: str) -> str:
    cfg = _web_cfg()
    return (cfg.get(key, default) if cfg else default)


def _password() -> str | None:
    return os.environ.get(_env_for("password_env", "DASHBOARD_PASSWORD"))


def _secret() -> bytes:
    return (os.environ.get(_env_for("session_secret_env", "DASHBOARD_SECRET"))
            or _FALLBACK_SECRET).encode()


def _session_seconds() -> int:
    cfg = _web_cfg()
    hours = float(cfg.get("session_hours", 12)) if cfg else 12.0
    return int(hours * 3600)


def password_configured() -> bool:
    return bool(_password())


def verify_password(candidate: str | None) -> bool:
    real = _password()
    if not real:
        return False
    return hmac.compare_digest(candidate or "", real)


def _sign(expiry: int) -> str:
    return hmac.new(_secret(), f"v1|{expiry}".encode(), sha256).hexdigest()


def issue_cookie() -> str:
    expiry = int(time.time()) + _session_seconds()
    return f"{expiry}.{_sign(expiry)}"


def valid_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp_s, sig = value.split(".", 1)
    try:
        expiry = int(exp_s)
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(expiry))


def is_authed(request: Request) -> bool:
    return valid_cookie(request.cookies.get(SESSION_COOKIE))
