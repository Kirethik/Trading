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
    # plain-language persona for the dashboard (no jargon: the reader may not trade)
    display_name: str = ""
    title: str = ""            # one-line job
    voice: str = ""            # first-person sentence
    about: str = ""            # short paragraph
    icon: str = ""
    color: str = "#6366F1"

    def public(self) -> dict:
        return {"name": self.name, "display_name": self.display_name, "title": self.title,
                "voice": self.voice, "about": self.about, "icon": self.icon, "color": self.color,
                "model": self.model_id()}

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
        AgentSpec(
            "governance", "governance-protocol", "Chief Decision & Governance agent", "pro",
            display_name="The Judge", icon="⚖️", color="#6366F1",
            title="Makes the final call on which strategies get money",
            voice="I listen to both sides, remember how my past calls turned out, and decide.",
            about="When a strategy passes every safety test but only just, the Judge decides "
                  "what happens to it: fund it, wait, or give it less money. Before deciding it "
                  "hears the Tuner argue for the strategy and the Skeptic argue against it."),
        AgentSpec(
            "optimization", "overfitting-detection",
            "Continuous Optimization Team member (advisory only)", "flash",
            display_name="The Tuner", icon="🔧", color="#10B981",
            title="Suggests small improvements and argues for promising strategies",
            voice="I look for settings that could work a little better, and speak up for good ideas.",
            about="The Tuner proposes small changes to a strategy's settings and, in debates, "
                  "makes the case for funding a strategy. Every suggestion it makes is tested "
                  "and checked by the Skeptic, so it can never sneak a change through."),
        AgentSpec(
            "validation", "validation-pipeline", "Independent Validation Team auditor", "flash",
            display_name="The Skeptic", icon="🔍", color="#F59E0B",
            title="Tries to prove a strategy only looks good by luck",
            voice="My job is to find reasons NOT to trust a strategy before real money depends on it.",
            about="The Skeptic hunts for warning signs: results that might be luck, too few "
                  "trades, or a strategy that only works in one kind of market. It is "
                  "deliberately hard to convince — that is what keeps the system safe."),
        AgentSpec(
            "watchman", "live-oversight", "Live oversight Watchman (explains, never decides)",
            "flash", display_name="The Watchman", icon="🛡️", color="#0EA5E9",
            title="Watches the live system around the clock and steps in when something goes wrong",
            voice="I keep an eye on everything while it runs, and I tell you the moment something looks off.",
            about="Every few minutes the Watchman checks the whole practice account: sudden losses, "
                  "too much money invested at once, records that don't match the broker, a trading "
                  "loop that went quiet, and the safety brakes. When something is serious it acts "
                  "on its own — it pauses the other agents' buying and trims their positions — "
                  "and always tells you what it did. It never touches the main system's trades "
                  "and never loosens a safety limit."),
        AgentSpec(
            "reporter", "news-analysis", "News Reporter (reads news, never trades directly)",
            "flash", display_name="The Reporter", icon="📰", color="#EC4899",
            title="Reads the news and tells everyone what it means",
            voice="I read the news so the team doesn't have to, and I only act when several sources agree.",
            about="The Reporter reads company and crypto news, official SEC filings and web articles, "
                  "and works out whether each story is good or bad for a stock. Strongly bad news makes "
                  "the team more careful. Strongly good news, confirmed by at least two independent "
                  "sources, may lead to a small purchase with practice money in the agents' own share "
                  "of the account. The AI only describes the news — fixed rules decide what happens, so "
                  "a misleading article can't talk it into a trade."),
    )
}


def get_spec(skill: str) -> AgentSpec | None:
    return _SPECS.get(skill)


def all_specs() -> list[AgentSpec]:
    return list(_SPECS.values())


def by_name(name: str) -> AgentSpec | None:
    return next((s for s in _SPECS.values() if s.name == name), None)
