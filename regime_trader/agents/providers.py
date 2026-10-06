"""Free-tier model routing: each agent has an ordered chain of
provider:model entries; a call walks the chain until one answers.

  gemini      via Google ADK (existing path)                    [GOOGLE_API_KEY]
  groq        OpenAI-compatible, ~1000 req/day/model free, no retention [GROQ_API_KEY]
  openrouter  OpenAI-compatible, ':free' models, 50 req/day       [OPENROUTER_API_KEY]
  mistral     OpenAI-compatible (disabled: no free plan)           [MISTRAL_API_KEY]

Every provider keeps a per-model daily request counter (our own cap, at or
under the provider's free allowance). A 429 rests that model until its
retry-after (or the next UTC day for daily quotas); a 401/403 rests it for
the day (bad key). Nothing here ever exceeds a free tier on purpose, and it
never rotates keys or accounts to get around a limit.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import atomic_write_json, load_json, utc_now

log = get_logger("agents")

_DAILY_QUOTA_WORDS = ("per day", "perday", "daily", "rpd", "requests per day", "free_tier_requests")


class AllProvidersExhausted(RuntimeError):
    """No provider in the chain could answer (quota, key, outage)."""


@dataclass(frozen=True)
class Route:
    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_route(text: str) -> Route:
    provider, _, model = text.partition(":")
    return Route(provider.strip(), model.strip())


# ------------------------------------------------------------------ usage
class Usage:
    """Per provider:model daily request counts + rest-until times."""

    def __init__(self, path):
        self.path = path

    @classmethod
    def from_config(cls) -> "Usage":
        cfg = get_config()
        return cls(cfg.resolve_path(cfg.state_dir) / "llm_usage.json")

    def _state(self) -> dict:
        today = utc_now().date().isoformat()
        try:
            s = load_json(self.path, default=None) or {}
        except (OSError, ValueError):
            s = {}
        if s.get("date") != today:
            s = {"date": today, "counts": {}, "rest": {k: v for k, v in (s.get("rest") or {}).items()
                                                       if _ts(v) and _ts(v) > utc_now()}}
        return s

    def available(self, route: Route, cap: int) -> bool:
        s = self._state()
        until = _ts((s.get("rest") or {}).get(route.label))
        if until and until > utc_now():
            return False
        return int((s.get("counts") or {}).get(route.label, 0)) < cap

    def count(self, route: Route) -> None:
        s = self._state()
        counts = dict(s.get("counts") or {})
        counts[route.label] = int(counts.get(route.label, 0)) + 1
        self._write({**s, "counts": counts})

    def rest(self, route: Route, until: datetime, why: str) -> None:
        s = self._state()
        log.warning("resting %s until %s (%s)", route.label, until.isoformat(timespec="minutes"), why)
        self._write({**s, "rest": {**(s.get("rest") or {}), route.label: until.isoformat(timespec="seconds")}})

    def snapshot(self) -> dict:
        return self._state()

    def _write(self, s: dict) -> None:
        try:
            atomic_write_json(self.path, s)
        except OSError as exc:
            log.warning("could not save llm usage: %s", exc)


def _ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _tomorrow() -> datetime:
    now = utc_now()
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1, minutes=5)


# ------------------------------------------------------- openai-compatible
class ProviderError(RuntimeError):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"{status}: {message[:200]}")
        self.status, self.retry_after, self.message = status, retry_after, message


def openai_compatible_call(base_url: str, api_key: str, model: str, system: str, user: str,
                           output_model: Any, *, timeout: float, temperature: float,
                           session=None, extra_headers: dict | None = None) -> tuple[str, int, int]:
    """One chat completion in JSON mode. The schema goes in the prompt (JSON
    mode is supported everywhere; strict schemas aren't), and the reply is
    validated by the runtime's pydantic model afterwards."""
    if output_model is not None:
        user = (f"{user}\n\nRespond with ONE JSON object only, matching this JSON schema:\n"
                f"{json.dumps(output_model.model_json_schema())}")
    body = {"model": model, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **(extra_headers or {})}
    s = session or requests
    try:
        resp = s.post(base_url.rstrip("/") + "/chat/completions", json=body, headers=headers, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise ProviderError(0, f"network: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        retry = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
        try:
            retry_s = float(retry) if retry is not None else None
        except ValueError:
            retry_s = None
        raise ProviderError(resp.status_code, _error_text(resp), retry_s)
    data = resp.json()
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def _error_text(resp) -> str:
    try:
        err = resp.json().get("error")
        return str(err.get("message") if isinstance(err, dict) else err)
    except Exception:
        return getattr(resp, "text", "")[:300]


# ------------------------------------------------------------------ chain
def _providers_cfg() -> dict:
    p = get_config().agents.get("providers")
    return p.as_dict() if p is not None else {}


def routes_for(agent: str) -> list[Route]:
    r = get_config().agents.get("routes")
    chain = (r.as_dict() if r is not None else {}).get(agent) or []
    return [parse_route(x) for x in chain]


class Chain:
    """Walks one agent's provider chain. Injectable pieces for tests."""

    def __init__(self, routes: list[Route], *, providers: dict | None = None, usage: Usage | None = None,
                 gemini_call: Callable | None = None, http_call: Callable = openai_compatible_call,
                 env: dict | None = None, clock: Callable[[], datetime] = utc_now):
        self.routes = routes
        self.providers = providers if providers is not None else _providers_cfg()
        self.usage = usage or Usage.from_config()
        self.gemini_call = gemini_call
        self.http_call = http_call
        self.env = env if env is not None else os.environ
        self.clock = clock
        self.last_route: Route | None = None
        self.tried: list[str] = []

    def _key(self, cfg: dict) -> str | None:
        return (self.env.get(cfg.get("key_env", "")) or "").strip() or None

    async def __call__(self, spec, system: str, user: str, output_model: Any, usage_tally: dict | None = None):
        tally = usage_tally if usage_tally is not None else {"in": 0, "out": 0}
        temperature = float(get_config().agents.get("temperature", 0.2))
        for route in self.routes:
            cfg = self.providers.get(route.provider) or {}
            if not cfg.get("enabled", True):
                self.tried.append(f"{route.label}: off")
                continue
            key = self._key(cfg)
            if not key:
                self.tried.append(f"{route.label}: no key")
                continue
            cap = int((cfg.get("daily_requests") or {}).get(route.model, cfg.get("default_daily_requests", 50)))
            if not self.usage.available(route, cap):
                self.tried.append(f"{route.label}: resting/at cap")
                continue
            self.usage.count(route)
            try:
                if cfg.get("kind") == "adk":
                    text, tin, tout = await self.gemini_call(spec, system, user, output_model, tally, route.model)
                else:
                    text, tin, tout = await _to_thread(
                        self.http_call, cfg["base_url"], key, route.model, system, user, output_model,
                        timeout=float(cfg.get("timeout_seconds", 60)), temperature=temperature,
                        extra_headers=cfg.get("headers"))
            except ProviderError as exc:
                self._handle(route, exc.status, exc.message, exc.retry_after)
                continue
            except Exception as exc:  # ADK path errors (quota text, 5xx) — classify by message
                status = 429 if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc) else 0
                self._handle(route, status, str(exc), None)
                continue
            if not text.strip():
                self.tried.append(f"{route.label}: empty reply")
                continue
            self.last_route = route
            tally["in"] += tin
            tally["out"] += tout
            return text, tin, tout
        raise AllProvidersExhausted("; ".join(self.tried) or "no routes configured")

    def _handle(self, route: Route, status: int, message: str, retry_after: float | None) -> None:
        self.tried.append(f"{route.label}: {status or 'error'}")
        low = message.lower()
        if status == 429:
            daily = any(w in low for w in _DAILY_QUOTA_WORDS) or (retry_after or 0) > 3600
            until = _tomorrow() if daily else self.clock() + timedelta(seconds=max(retry_after or 60, 30))
            self.usage.rest(route, until, "daily free quota used up" if daily else "rate limited")
        elif status in (401, 403):
            self.usage.rest(route, _tomorrow(), "key rejected")
        elif status == 404:
            self.usage.rest(route, _tomorrow(), "model not available")
        else:
            log.warning("provider %s failed (%s): %s", route.label, status, message[:200])


async def _to_thread(fn, *args, **kwargs):
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)
