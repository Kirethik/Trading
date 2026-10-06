"""Bridge to the agent runtime (the 'intelligent thinking' layer).

Escalations run on Google Gemini through the ADK runtime in
`regime_trader.agents`. The rubric (project skill) becomes the agent's system
prompt; the reply is enforced against a strict schema. Any failure (no key,
kill switch, budget, timeout, bad JSON) returns None and the caller falls back
to deterministic rules.

This is the ONLY place governance code reaches an LLM. It never blocks the
daily job: a None result is a normal, handled outcome, not an error.
"""
from __future__ import annotations

from ..agents import guards, runtime, specs
from ..config import get_config
from ..monitor.log_setup import get_logger

log = get_logger("governance")


def _enabled() -> bool:
    cfg = get_config()
    return bool(cfg.agents.enabled) and bool(cfg.governance.agents.escalation_enabled)


def available() -> bool:
    if not _enabled():
        return False
    if not get_config().google_api_key:
        log.warning("GOOGLE_API_KEY not set; governance runs deterministic-only")
        return False
    if guards.kill_switch_engaged():
        log.warning("agent kill switch engaged; governance runs deterministic-only")
        return False
    return True


def escalate(role: str, skill: str, instruction: str, payload: dict, schema: dict,
             subject: str | None = None) -> dict | None:
    """Run one agent call; return the parsed JSON dict or None."""
    if not available():
        log.info("escalation skipped (agents unavailable): %s", role)
        return None
    spec = specs.get_spec(skill)
    if spec is None:
        log.warning("no agent registered for skill %r; deterministic fallback", skill)
        return None
    return runtime.run_agent(spec, instruction, payload, schema, subject=subject)
