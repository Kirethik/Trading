"""Alerts on events that matter, rate-limited, multi-channel.

Channels: console + alert log always; webhook if configured.
"""
from __future__ import annotations

import time
from enum import Enum

import requests

from ..config import get_config
from .log_setup import get_logger


class AlertKind(str, Enum):
    REGIME_CHANGE = "regime_change"
    CIRCUIT_BREAKER = "circuit_breaker"
    LARGE_PNL_SWING = "large_pnl_swing"
    DATA_LOSS = "data_loss"
    API_LOSS = "api_loss"
    MODEL_RETRAIN = "model_retrain"
    FLICKER = "flicker"
    ORDER_REJECTED = "order_rejected"
    SYSTEM = "system"


class AlertManager:
    def __init__(self, rate_limit_seconds: int | None = None, webhook_url: str | None = None):
        cfg = get_config()
        self.rate_limit = (
            rate_limit_seconds
            if rate_limit_seconds is not None
            else cfg.monitoring.alert_rate_limit_seconds
        )
        self.webhook_url = webhook_url if webhook_url is not None else cfg.monitoring.webhook_url
        self._last_sent: dict[str, float] = {}
        self.log = get_logger("alerts")
        self.recent: list[dict] = []  # ring buffer for the dashboard

    def send(self, kind: AlertKind, message: str, ctx: dict | None = None, dedupe_key: str | None = None) -> bool:
        """Returns True if the alert was emitted (False if rate-limited)."""
        key = dedupe_key or f"{kind.value}:{message[:60]}"
        now = time.monotonic()
        if now - self._last_sent.get(key, -1e12) < self.rate_limit:
            return False
        self._last_sent[key] = now

        record = {"kind": kind.value, "message": message, "ctx": ctx or {}, "ts": time.time()}
        self.recent.append(record)
        del self.recent[:-50]

        self.log.warning("[%s] %s", kind.value.upper(), message, extra={"ctx": ctx or {}})
        print(f"  ALERT [{kind.value}] {message}")
        if self.webhook_url:
            try:
                requests.post(self.webhook_url, json=record, timeout=5)
            except Exception as exc:  # alerting must never crash trading
                self.log.error("webhook delivery failed: %s", exc)
        return True
