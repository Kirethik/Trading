"""Agent definitions: who each agent is, which model tier, which rubric,
which tools. Rubrics stay as reviewable markdown under .claude/skills/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import get_config
from .guards import ToolTier


@dataclass(frozen=True)
class AgentSpec:
    name: str                  # ADK agent name (identifier; appears in audit/events)
    skill: str                 # rubric: <rubric_dir>/<skill>/SKILL.md
    role: str                  # human-readable role, first line of the system prompt
    model_tier: str            # key into config agents.models ("pro" | "flash")
    tool_tier: ToolTier = ToolTier.READ
    tools: tuple[Callable, ...] = field(default_factory=tuple)

    def model_id(self) -> str:
        return str(get_config().agents.models.get(self.model_tier))

    def rubric(self) -> str:
        cfg = get_config()
        path = cfg.resolve_path(cfg.agents.rubric_dir) / self.skill / "SKILL.md"
        return path.read_text(encoding="utf-8")

    def tool_names(self) -> set[str]:
        return {t.__name__ for t in self.tools}


_SPECS: dict[str, AgentSpec] = {
    s.skill: s
    for s in (
        AgentSpec("governance", "governance-protocol",
                  "Chief Decision & Governance agent", "pro"),
        AgentSpec("optimization", "overfitting-detection",
                  "Continuous Optimization Team member (advisory only)", "flash"),
        AgentSpec("validation", "validation-pipeline",
                  "Independent Validation Team auditor", "flash"),
    )
}


def get_spec(skill: str) -> AgentSpec | None:
    return _SPECS.get(skill)
