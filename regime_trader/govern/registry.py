"""The governed strategy registry: one unified, persistent view of every
strategy under governance (challengers + omnivore cohort) and its standing.

"No strategy remains permanently approved" — each entry carries last_validated /
last_deep_validated / consecutive_fails so the daily cycle can make every
strategy re-earn its place. State persists to state/governance_registry.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..strategy.base import Strategy
from ..strategy.catalog import build_challengers, strategy_universe
from ..omnivore.factory import generate_cohort
from ..utils import atomic_write_json, load_json, utc_now_iso

# governance lifecycle states
CANDIDATE, APPROVED, DEPLOYED, REJECTED, RETIRED = (
    "candidate", "approved", "deployed", "rejected", "retired",
)


@dataclass
class GovernedStrategy:
    key: str
    strategy: Strategy
    symbols: list[str]
    market: str
    source: str                 # "challenger" | "omnivore"
    meta: dict = field(default_factory=dict)


def build_governed_universe() -> list[GovernedStrategy]:
    """Challengers (incumbent + classic bracket) + the omnivore cohort."""
    cfg = get_config()
    etfs = set(cfg.universe.etf_rotation)
    governed: list[GovernedStrategy] = []

    for strat in build_challengers():
        syms = strategy_universe(strat)
        market = "etf" if set(syms) <= etfs and syms else "equities"
        governed.append(GovernedStrategy(key=strat.name, strategy=strat, symbols=syms,
                                         market=market, source="challenger"))
    for om in generate_cohort():
        governed.append(GovernedStrategy(key=om.id, strategy=om.strategy, symbols=om.symbols,
                                         market=om.market, source="omnivore", meta=om.meta))
    return governed


class GovernanceRegistry:
    def __init__(self, path=None):
        cfg = get_config()
        self.path = cfg.resolve_path(path or cfg.governance.registry_file)
        self.state: dict = load_json(self.path, default={}) or {}
        self.universe = build_governed_universe()
        self._ensure_entries()

    def _ensure_entries(self) -> None:
        for gs in self.universe:
            if gs.key not in self.state:
                self.state[gs.key] = {
                    "source": gs.source, "market": gs.market, "status": CANDIDATE,
                    "allocation": 0.0, "last_validated": None, "last_deep_validated": None,
                    "consecutive_fails": 0, "last_verdict": None, "last_sharpe": None,
                    "created": utc_now_iso(),
                }

    def get(self, key: str) -> dict:
        return self.state.setdefault(key, {})

    def update(self, key: str, **fields) -> None:
        self.get(key).update(fields)

    def record_validation(self, key: str, verdict: str, sharpe: float | None, deep: bool) -> None:
        e = self.get(key)
        e["last_verdict"] = verdict
        e["last_sharpe"] = sharpe
        e["last_validated"] = utc_now_iso()
        if deep:
            e["last_deep_validated"] = utc_now_iso()
        e["consecutive_fails"] = 0 if verdict == "PASS" else e.get("consecutive_fails", 0) + 1

    def deployed_keys(self) -> list[str]:
        return [k for k, e in self.state.items() if e.get("status") == DEPLOYED]

    def prior_sharpe(self, key: str) -> float | None:
        return self.get(key).get("last_sharpe")

    def save(self) -> None:
        atomic_write_json(self.path, self.state)
