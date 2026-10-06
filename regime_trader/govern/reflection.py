"""Daily reflection: each agent compares its own past calls (3–30 days old)
with what actually happened to those strategies, and writes a few short
lessons for itself. Lessons feed back into future prompts (govern/memory.py)
and are shown on each agent's dashboard page.

Runs at most once per UTC day; skips agents with nothing old enough to
review. Budget / kill switch / failures are handled by llm_bridge (None).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..agents.audit import AuditLog
from ..agents.blackboard import Blackboard
from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import utc_now
from . import llm_bridge
from .memory import MAX_LESSON_CHARS

log = get_logger("governance")

LESSON_SCHEMA = {"lessons": [{"lesson": "string", "evidence": "string",
                              "confidence": "low|medium|high"}]}
_SKILLS = {"governance": "governance-protocol", "validation": "validation-pipeline",
           "optimization": "overfitting-detection"}
_ROLES = {"governance": "The Judge", "validation": "The Skeptic", "optimization": "The Tuner"}
_MAX_REVIEWS = 15
_RUN_TOPIC = "reflection-run"


def _settings() -> tuple[int, int, int]:
    r = get_config().governance.get("reflection")
    if r is None:
        return 3, 30, 3
    return int(r.get("min_age_days", 3)), int(r.get("lookback_days", 30)), int(r.get("max_lessons", 3))


def _reviews(rows: list[dict], agent: str, registry_state: dict, now: datetime,
             min_age: int, lookback: int, since: datetime | None = None) -> list[dict]:
    """Calls aged min_age..lookback days, excluding anything already reviewed
    (older than `since`), so each call is reflected on exactly once."""
    lo, hi = now - timedelta(days=lookback), now - timedelta(days=min_age)
    if since is not None and since > lo:
        lo = since
    out = []
    for r in rows:
        if (r.get("agent") != agent or r.get("outcome") != "ok" or not r.get("subject")
                or r.get("subject") == "reflection"):   # never review its own reflections
            continue
        try:
            ts = datetime.fromisoformat(str(r["ts"]))
        except (KeyError, ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            continue
        if not lo < ts <= hi:
            continue
        e = registry_state.get(r["subject"], {})
        out.append({
            "date": ts.date().isoformat(), "strategy": r["subject"],
            "what_i_said": str(r.get("output"))[:240],
            "what_happened": {k: e.get(k) for k in ("status", "last_verdict", "consecutive_fails",
                                                    "last_sharpe")},
        })
    return out[-_MAX_REVIEWS:]


def reflect_all(registry_state: dict, audit: AuditLog | None = None,
                board: Blackboard | None = None,
                agents: tuple[str, ...] = ("governance", "validation", "optimization")) -> dict[str, int]:
    """Returns {agent: lessons_written}. Empty when already run today."""
    board = board or Blackboard.from_config()
    audit = audit or AuditLog.from_config()
    now = utc_now()
    today = now.date().isoformat()
    last = board.latest(_RUN_TOPIC, 1)
    if last and last[0]["payload"].get("date") == today:
        return {}
    min_age, lookback, max_lessons = _settings()
    rows = audit.tail(20000, include_rotated=True)
    since = None
    if last and last[0]["payload"].get("reviewed_until"):
        try:
            since = datetime.fromisoformat(last[0]["payload"]["reviewed_until"])
        except ValueError:
            since = None
    written: dict[str, int] = {}
    failed = False
    for agent in agents:
        reviews = _reviews(rows, agent, registry_state, now, min_age, lookback, since)
        if not reviews:
            continue
        out = llm_bridge.escalate(
            f"{_ROLES[agent]} (reflecting on past calls)", _SKILLS[agent],
            f"Compare what you said with what happened next. Write at most {max_lessons} short, "
            "specific lessons that would improve your future calls, each with the evidence behind "
            "it. Plain English, one sentence per lesson.",
            {"reviews": reviews}, LESSON_SCHEMA, subject="reflection",
        )
        if out is None:          # budget / kill switch / model failure: retry next run
            failed = True
            continue
        lessons = out.get("lessons") or []
        for item in lessons[:max_lessons]:
            board.post(f"lessons:{agent}", agent, {
                "lesson": str(item.get("lesson", ""))[:MAX_LESSON_CHARS],
                "evidence": str(item.get("evidence", ""))[:MAX_LESSON_CHARS],
                "confidence": item.get("confidence", "low"),
                "reviewed": len(reviews),
            })
        written[agent] = min(len(lessons), max_lessons)
    if not failed:
        reviewed_until = (now - timedelta(days=min_age)).isoformat(timespec="seconds")
        board.post(_RUN_TOPIC, "governance",
                   {"date": today, "written": written, "reviewed_until": reviewed_until})
    log.info("reflection complete: %s", written)
    return written
