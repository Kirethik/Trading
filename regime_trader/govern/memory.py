"""Decision memory: what governance decided about a strategy before, how
that turned out, and the lessons each agent has written for itself.

Lessons are model-written text fed back into prompts, so they are length-
capped and always framed as advisory — they can never override the hard
constraints in the system prompt.
"""
from __future__ import annotations

from ..agents.blackboard import Blackboard
from ..config import get_config
from ..utils import load_json

MAX_LESSON_CHARS = 240


def load_ledger() -> list[dict]:
    cfg = get_config()
    return load_json(cfg.resolve_path(cfg.governance.decisions_file), default=[]) or []


def _outcome(decisions: list[str], status: str | None) -> str:
    funded = any(d in ("DEPLOY", "INCREASE_ALLOCATION") for d in decisions)
    if not decisions:
        return "no history yet"
    if funded and status == "retired":
        return "was funded, later retired — went badly"
    if funded and status == "deployed":
        return "funded and still running"
    if funded:
        return f"was funded before, now {status or 'unknown'}"
    if "REJECT" in decisions:
        return "previously rejected by validation"
    return f"reviewed before, never funded (now {status or 'unknown'})"


def strategy_history(key: str, ledger: list[dict], registry_entry: dict, n: int = 5) -> dict:
    """Last `n` governance decisions for `key` plus a plain-language outcome."""
    past = [r for r in ledger if r.get("key") == key][-n:]
    status = registry_entry.get("status")
    return {
        # decisions + dates only: past rationales are model output, not facts,
        # and must not re-enter prompts as "precedent"
        "past_decisions": [{"date": str(r.get("ts", ""))[:10], "decision": r.get("decision")}
                           for r in past],
        "current_status": status,
        "consecutive_fails": registry_entry.get("consecutive_fails", 0),
        "outcome": _outcome([str(r.get("decision")) for r in past], status),
    }


def recent_lessons(agent: str, n: int = 5, board: Blackboard | None = None) -> list[str]:
    """The agent's own most recent lessons as short strings (oldest first)."""
    try:
        board = board or Blackboard.from_config()
        posts = board.latest(f"lessons:{agent}", n)
    except Exception:  # memory is a nice-to-have; never block a decision on it
        return []
    out = []
    for p in posts:
        payload = p.get("payload") or {}
        text = str(payload.get("lesson", ""))[:MAX_LESSON_CHARS]
        if text:
            out.append(f"{text} (confidence: {payload.get('confidence', '?')})")
    return out


def lessons_for_all(n: int = 5, board: Blackboard | None = None) -> dict[str, list[str]]:
    return {a: recent_lessons(a, n, board) for a in ("governance", "validation", "optimization")}
