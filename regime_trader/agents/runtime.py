"""Run one agent call on Gemini through Google ADK — safely.

Contract: `run_agent` returns a dict matching the caller's legacy schema, or
None on ANY failure (kill switch, budget, timeout, bad JSON, API error). A
None is a normal, handled outcome — callers fall back to deterministic
rules. The single exception is a non-paper account: that raises loudly.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import ValidationError

from ..config import get_config
from ..monitor.log_setup import get_logger
from . import guards
from .audit import AuditLog, payload_hash
from .budget import Budget
from .schemas import schema_for
from .specs import AgentSpec

log = get_logger("agents")

HARD_CONSTRAINT = (
    "Hard constraints: you may never approve a strategy that failed a hard validation gate; "
    "risk management outranks signal generation; this system trades a PAPER account only."
)
APP_NAME = "regime_trader"

BLOCKED_TEXT = "BLOCKED: kill switch or budget"

# (spec, system_text, user_text, output_model, usage) -> (reply_text, input_tokens, output_tokens)
# `usage` is a live {"in": n, "out": n} tally so tokens spent before a timeout are still billed.
ModelCall = Callable[[AgentSpec, str, str, Any, dict], Awaitable[tuple[str, int, int]]]

_failure_streak = 0


def failure_streak() -> int:
    return _failure_streak


def reset_failure_streak() -> None:
    global _failure_streak
    _failure_streak = 0


# ----------------------------------------------------------------- prompts
def system_text(spec: AgentSpec, legacy_schema: dict, strict: bool) -> str:
    parts = [
        f"You are the {spec.role} for an always-long volatility-regime trading system.",
        "Apply the following rubric.\n\n" + spec.rubric(),
        HARD_CONSTRAINT,
    ]
    if not strict:
        parts.append("Respond with ONE JSON object and nothing else, matching this schema:\n"
                     + json.dumps(legacy_schema, indent=2))
    return "\n\n".join(parts)


def user_text(instruction: str, payload: dict) -> str:
    return f"{instruction}\n\nINPUT (JSON):\n{json.dumps(payload, default=str, indent=2)}"


# --------------------------------------------------------------- ADK wiring
def _model_guard(callback_context: Any, llm_request: Any):
    """before_model_callback: stop mid-run tool loops if the kill switch flips
    or the budget runs out. Returning an LlmResponse skips the model call."""
    if guards.kill_switch_engaged() or Budget.from_config().exhausted():
        from google.adk.models import LlmResponse
        from google.genai import types
        return LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=BLOCKED_TEXT)]))
    return None


def build_agent(spec: AgentSpec, system: str, output_model: Any):
    from google.adk.agents import LlmAgent
    from google.genai import types

    return LlmAgent(
        name=spec.name,
        model=spec.model_id(),
        static_instruction=system,  # sent verbatim: no {state} templating of the rubric
        output_schema=output_model,
        tools=list(spec.tools),
        before_tool_callback=guards.make_tool_guard(spec.tool_tier, spec.tool_names()),
        before_model_callback=_model_guard,
        generate_content_config=types.GenerateContentConfig(
            temperature=float(get_config().agents.temperature)),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


async def collect(events: AsyncIterator[Any], author: str,
                  usage: dict | None = None) -> tuple[str, int, int]:
    """Sum token usage over all events (thinking tokens bill as output) into
    `usage` as they arrive; keep the final non-thought text from `author`."""
    usage = usage if usage is not None else {"in": 0, "out": 0}
    text = ""
    async for ev in events:
        meta = getattr(ev, "usage_metadata", None)
        if meta is not None:
            usage["in"] += int(getattr(meta, "prompt_token_count", 0) or 0)
            usage["out"] += int(getattr(meta, "candidates_token_count", 0) or 0)
            usage["out"] += int(getattr(meta, "thoughts_token_count", 0) or 0)
        if ev.author == author and ev.is_final_response() and ev.content and ev.content.parts:
            text = "".join(p.text or "" for p in ev.content.parts
                           if getattr(p, "text", None) and not getattr(p, "thought", False))
    return text, usage["in"], usage["out"]


async def adk_model_call(spec: AgentSpec, system: str, user: str, output_model: Any,
                         usage: dict | None = None):
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    agent = build_agent(spec, system, output_model)
    runner = Runner(app_name=APP_NAME, agent=agent,
                    session_service=InMemorySessionService(), auto_create_session=True)
    events = runner.run_async(
        user_id="regime_trader", session_id=f"{spec.name}-{time.time_ns()}",
        new_message=types.Content(role="user", parts=[types.Part(text=user)]),
        run_config=RunConfig(max_llm_calls=int(get_config().agents.max_llm_calls_per_run)),
    )
    return await collect(events, spec.name, usage)


# --------------------------------------------------------------- execution
def _run_sync(coro_factory: Callable[[], Awaitable]):
    """asyncio.run, or a worker thread when a loop is already running (FastAPI)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def parse_reply(text: str, output_model: Any) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if output_model is None:
        return raw if isinstance(raw, dict) else None
    try:
        return output_model.model_validate(raw).to_legacy()
    except ValidationError:
        return None


_SECRET = re.compile(r"(key|token|secret|authorization)[=:\s]+(?:bearer\s+)?\S+", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip credentials from provider error text before it is stored or sent."""
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return _SECRET.sub(r"\1=[REDACTED]", text)


def _default_alert(message: str) -> None:
    from ..monitor.alerts import AlertKind, AlertManager
    AlertManager().send(AlertKind.AGENT, message, dedupe_key=f"agent:{message[:40]}")


def run_agent(spec: AgentSpec, instruction: str, payload: dict, legacy_schema: dict, *,
              model_call: ModelCall | None = None, budget: Budget | None = None,
              audit: AuditLog | None = None, alert: Callable[[str], None] | None = None,
              timeout: float | None = None) -> dict | None:
    guards.assert_paper_only()  # raises: never silently degrade on a live account
    cfg = get_config().agents
    budget = budget or Budget.from_config()
    audit = audit or AuditLog.from_config()
    alert = alert or _default_alert
    model_call = model_call or adk_model_call
    timeout = float(timeout if timeout is not None else cfg.timeout_seconds)
    model = spec.model_id()
    base = {"agent": spec.name, "model": model, "payload_hash": payload_hash(payload)}

    if guards.kill_switch_engaged():
        audit.write({**base, "outcome": "blocked", "error": "kill switch"})
        return None
    if budget.exhausted():
        audit.write({**base, "outcome": "blocked", "error": "daily budget exhausted"})
        alert(f"agent daily budget exhausted (${budget.daily_usd:.2f}); deterministic fallback")
        return None

    output_model = schema_for(legacy_schema)
    system = system_text(spec, legacy_schema, strict=output_model is not None)
    user = user_text(instruction, payload)
    t0 = time.monotonic()
    usage = {"in": 0, "out": 0}
    try:
        text, tin, tout = _run_sync(lambda: asyncio.wait_for(
            model_call(spec, system, user, output_model, usage), timeout))
        usage = {"in": max(usage["in"], tin), "out": max(usage["out"], tout)}
        result = parse_reply(text, output_model)
        if result is not None:
            outcome, error = "ok", None
        elif BLOCKED_TEXT in text:
            outcome, error = "blocked", "kill switch or budget tripped mid-run"
        else:
            outcome, error = "invalid", redact(text[:300])
    except (asyncio.TimeoutError, TimeoutError):
        result, outcome, error = None, "timeout", f"no reply within {timeout}s"
    except Exception as exc:  # API/quota/network — degrade, never crash the caller
        result, outcome, error = None, "error", redact(f"{type(exc).__name__}: {exc}")[:300]

    tin, tout = usage["in"], usage["out"]
    try:
        cost = budget.record(model, tin, tout) if (tin or tout) else 0.0
        audit.write({**base, "outcome": outcome, "output": result, "error": error,
                     "input_tokens": tin, "output_tokens": tout, "cost_usd": round(cost, 6),
                     "latency_ms": int((time.monotonic() - t0) * 1000)})
    except OSError as exc:  # disk full etc. — bookkeeping must not crash the caller
        log.error("agent bookkeeping failed: %s", exc)
    _track(outcome, spec.name, error, alert, max(1, int(cfg.failure_alert_streak)))
    return result


def _track(outcome: str, agent: str, error: str | None, alert: Callable[[str], None],
           threshold: int) -> None:
    global _failure_streak
    if outcome == "ok":
        _failure_streak = 0
        return
    _failure_streak += 1
    log.warning("agent %s %s: %s", agent, outcome, error)
    if _failure_streak == threshold:
        alert(f"{_failure_streak} consecutive agent failures (last: {agent} {outcome})")
