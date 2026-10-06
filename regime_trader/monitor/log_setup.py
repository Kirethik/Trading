"""Structured rotating logs: main / trades / alerts / regime.

Every record can carry structured context (regime, equity, positions, pnl)
via the `extra={"ctx": {...}}` convention; it is appended as compact JSON.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
CHANNELS = ("main", "trades", "alerts", "regime", "governance", "agents")


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "ctx", None)
        if ctx:
            base += " | " + json.dumps(ctx, default=str, separators=(",", ":"))
        return base


def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
    """Idempotent. Creates one rotating file per channel plus console output."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = ContextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    for channel in CHANNELS:
        logger = logging.getLogger(f"rt.{channel}")
        logger.setLevel(level)
        logger.propagate = False
        handler = RotatingFileHandler(
            log_dir / f"{channel}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        if channel == "main":
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            console.setLevel(logging.INFO)
            logger.addHandler(console)
    _CONFIGURED = True


def get_logger(channel: str = "main") -> logging.Logger:
    if channel not in CHANNELS:
        channel = "main"
    return logging.getLogger(f"rt.{channel}")
