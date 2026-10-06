"""Hard safety rails for every agent: paper-only, tool tiers, kill switch.

Tool tiers are cumulative: an agent at tier T may call tools of tier <= T
that are also on its explicit allowlist. A tool that was never registered
is always blocked (fail closed).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import atomic_write_json, load_json, utc_now

log = get_logger("agents")


class PaperOnlyViolation(RuntimeError):
    """Raised whenever the agent runtime would touch a non-paper account."""


class ToolTier(IntEnum):
    READ = 0           # observe state only
    PROPOSE = 1        # write proposals / blackboard posts
    EXECUTE_PAPER = 2  # place PAPER orders (still through RiskManager)


_TOOL_TIERS: dict[str, ToolTier] = {}


def assert_paper_only() -> None:
    if not get_config(reload=False).alpaca_paper:
        raise PaperOnlyViolation(
            "agent runtime is paper-only: ALPACA_PAPER must be true (it is not)"
        )


def register_tool_tier(tool_name: str, tier: ToolTier) -> None:
    _TOOL_TIERS[tool_name] = tier


def tool_tier(tool_name: str) -> ToolTier | None:
    return _TOOL_TIERS.get(tool_name)


def _blocked(reason: str) -> dict:
    return {"status": "blocked", "reason": reason}


def make_tool_guard(agent_tier: ToolTier, allowlist: set[str]) -> Callable[..., dict | None]:
    """ADK before_tool_callback: return a dict to skip the tool, None to run it."""
    allowed = frozenset(allowlist)

    def guard(tool: Any, args: dict, tool_context: Any) -> dict | None:
        name = getattr(tool, "name", str(tool))
        tier = _TOOL_TIERS.get(name)
        if tier is None:
            reason = f"tool {name!r} is not registered"
        elif name not in allowed:
            reason = f"tool {name!r} is not on this agent's allowlist"
        elif tier > agent_tier:
            reason = f"tool {name!r} needs tier {tier.name}, agent has {agent_tier.name}"
        else:
            return None
        log.warning("tool call blocked: %s", reason)
        return _blocked(reason)

    return guard


def _pause_path() -> Path:
    cfg = get_config()
    rel = cfg.agents.get("pause_file") or "state/agents.paused"
    return cfg.resolve_path(rel)


def agents_paused(path: str | Path | None = None) -> dict | None:
    """The Watchman's temporary pause (expires on its own), or None. Unlike the
    human kill switch it only stops agent *buying* and governance escalations;
    risk-reducing sells and the Watchman itself keep working."""
    info = load_json(Path(path) if path else _pause_path(), default=None)
    if not isinstance(info, dict):
        return None
    try:
        until = datetime.fromisoformat(str(info.get("until")))
    except ValueError:
        return None
    return info if until > utc_now() else None


def pause_agents(hours: float, reason: str, path: str | Path | None = None) -> dict:
    info = {"until": (utc_now() + timedelta(hours=hours)).isoformat(timespec="seconds"),
            "reason": reason[:300], "by": "watchman", "since": utc_now().isoformat(timespec="seconds")}
    atomic_write_json(Path(path) if path else _pause_path(), info)
    log.warning("agents paused until %s: %s", info["until"], reason)
    return info


def kill_switch_engaged(path: str | Path | None = None) -> bool:
    if path is None:
        cfg = get_config()
        path = cfg.resolve_path(cfg.agents.kill_switch_file)
    return Path(path).exists()
