"""Governance — the Chief Decision authority (Opus 4.8 role).

Final authority over approve / reject / re-optimize / research / reduce-risk /
increase-allocation / retire / deploy / roll-back, and over capital allocation.

Rules-first: deterministic logic decides every unambiguous case and ALWAYS
produces a valid decision. The Opus agent is consulted only for borderline
single-strategy calls and for capital allocation when more strategies qualify
than `top_n` — and it is hard-bounded: it can never approve a strategy the
independent Validation Team rejected, and every resulting order still passes the
risk veto. risk management > signal generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import atomic_write_json, load_json, utc_now_iso
from . import llm_bridge
from .registry import APPROVED, CANDIDATE, DEPLOYED, REJECTED, RETIRED, GovernanceRegistry

log = get_logger("governance")

APPROVE, REJECT, REOPTIMIZE, RESEARCH = "APPROVE", "REJECT", "REOPTIMIZE", "RESEARCH"
REDUCE_RISK, INCREASE_ALLOCATION = "REDUCE_RISK", "INCREASE_ALLOCATION"
RETIRE, DEPLOY, ROLLBACK, HOLD = "RETIRE", "DEPLOY", "ROLLBACK", "HOLD"

_DECISION_SCHEMA = {"decision": "DEPLOY|APPROVE|REDUCE_RISK|HOLD", "rationale": "string"}


@dataclass
class GovernanceDecision:
    key: str
    source: str
    decision: str
    allocation: float = 0.0
    rationale: str = ""
    escalated: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ts": utc_now_iso(), "key": self.key, "source": self.source,
            "decision": self.decision, "allocation": round(self.allocation, 2),
            "escalated": self.escalated, "rationale": self.rationale, "reasons": self.reasons,
        }


class Governance:
    def __init__(self, registry: GovernanceRegistry | None = None):
        self.cfg = get_config().governance
        self.registry = registry or GovernanceRegistry()

    # ----------------------------------------------------- per-strategy
    def decide(self, reports: dict, proposals: dict, drift: dict, escalate: bool = True
               ) -> dict[str, GovernanceDecision]:
        decisions: dict[str, GovernanceDecision] = {}
        for key, rep in reports.items():
            decisions[key] = self._decide_one(key, rep, proposals.get(key), drift.get(key, {}), escalate)
        self._allocate(decisions, reports, escalate)
        return decisions

    def _decide_one(self, key, rep, proposal, drift, escalate) -> GovernanceDecision:
        entry = self.registry.get(key)
        source = entry.get("source", "?")
        deployed = entry.get("status") == DEPLOYED
        fails = entry.get("consecutive_fails", 0)

        if not rep.passed:
            if deployed and fails >= self.cfg.drift.retire_consecutive_fails:
                return GovernanceDecision(key, source, RETIRE, 0.0,
                                          rationale="deployed strategy failed validation repeatedly",
                                          reasons=rep.reasons)
            decision = REOPTIMIZE if proposal else REJECT
            return GovernanceDecision(key, source, decision, 0.0,
                                      "validation rejected; " + ("optimization proposal exists" if proposal
                                                                 else "no improvement found"),
                                      reasons=rep.reasons)

        # passed validation -------------------------------------------------
        if deployed and drift.get("degraded"):
            if fails >= 1 or drift.get("severe"):
                return GovernanceDecision(key, source, REDUCE_RISK, 0.0,
                                          "live performance drift on a deployed strategy", reasons=["live drift"])

        if rep.borderline and escalate:
            verdict = self._escalate_decision(key, rep, drift)
            if verdict:
                return verdict

        # clear pass: eligible for deployment; capital set in _allocate()
        return GovernanceDecision(key, source, APPROVE, 0.0,
                                  "passed all gates" + (" (borderline)" if rep.borderline else ""))

    def _escalate_decision(self, key, rep, drift) -> GovernanceDecision | None:
        payload = {"strategy": key, "metrics": rep.metrics, "red_flags": rep.red_flags,
                   "gates": rep.gates, "borderline": rep.borderline, "live_drift": drift}
        out = llm_bridge.escalate(
            "Chief Governance (Opus 4.8)", "governance-protocol",
            "Decide the standing of this borderline-but-passing strategy.",
            payload, _DECISION_SCHEMA,
        )
        if not out or out.get("decision") not in (DEPLOY, APPROVE, REDUCE_RISK, HOLD):
            return None
        entry = self.registry.get(key)
        return GovernanceDecision(key, entry.get("source", "?"), out["decision"], 0.0,
                                  f"Opus: {out.get('rationale', '')}", escalated=True)

    # ------------------------------------------------------- allocation
    def _allocate(self, decisions, reports, escalate) -> None:
        """Capital across deploy-eligible strategies: top_n by Sharpe, sized
        proportional to Sharpe within [min, max], summed within total."""
        a = self.cfg.allocation
        eligible = [k for k, d in decisions.items() if d.decision in (APPROVE, DEPLOY, INCREASE_ALLOCATION)]
        ranked = sorted(eligible, key=lambda k: reports[k].metrics.get("sharpe", 0.0) or 0.0, reverse=True)

        chosen = ranked[: a.top_n]
        if escalate and len(ranked) > a.top_n:
            chosen = self._escalate_allocation(ranked, reports, a.top_n) or chosen

        weights = {k: max(0.0, reports[k].metrics.get("sharpe", 0.0) or 0.0) for k in chosen}
        total_w = sum(weights.values())
        for k, d in decisions.items():
            if k not in chosen or total_w <= 0:
                if d.decision in (APPROVE, DEPLOY, INCREASE_ALLOCATION) and k not in chosen:
                    d.decision, d.allocation = APPROVE, 0.0  # approved but not funded (over top_n)
                continue
            raw = a.total_capital * weights[k] / total_w
            d.allocation = float(min(a.max_per_strategy, max(a.min_per_strategy, raw)))
            d.decision = DEPLOY

    def _escalate_allocation(self, ranked, reports, top_n) -> list[str] | None:
        payload = {"candidates": [{"key": k, "sharpe": reports[k].metrics.get("sharpe"),
                                   "calmar": reports[k].metrics.get("calmar"),
                                   "max_drawdown": reports[k].metrics.get("max_drawdown")} for k in ranked],
                   "top_n": top_n}
        out = llm_bridge.escalate(
            "Chief Governance (Opus 4.8)", "governance-protocol",
            f"Select the {top_n} strategies to fund from these validated candidates; "
            "favor diversification and risk-adjusted return.",
            payload, {"selected": ["key", "..."], "rationale": "string"},
        )
        if out and isinstance(out.get("selected"), list):
            sel = [k for k in out["selected"] if k in reports]
            if sel:
                log.info("Opus selected %d funded strategies", len(sel))
                return sel[:top_n]
        return None

    # ------------------------------------------------- apply to paper
    def apply(self, decisions: dict[str, GovernanceDecision], deployer) -> dict:
        """Push decisions into the omnivore paper ledger (allocation/enabled).
        Ledger STATE always updates; actual order submission is governed by the
        deployer's own dry_run (set from market-open). `deployer=None` means
        research-only: challenger + omnivore decisions are recorded as registry
        recommendations, no ledger touched. Never bypasses the risk veto."""
        applied = {"funded": 0, "retired": 0, "reduced": 0, "disabled": 0,
                   "recommend_only": 0, "capital": 0.0}
        ledger = deployer.ledger if deployer is not None else {}
        for key, d in decisions.items():
            entry = self.registry.get(key)
            if d.source != "omnivore" or key not in ledger:
                # incumbent/classic challengers run via the main orchestrator;
                # governance only records the recommendation for them
                self._sync_registry(key, d)
                applied["recommend_only"] += 1
                continue
            led = ledger[key]
            if d.decision in (DEPLOY, INCREASE_ALLOCATION):
                self._fund(led, d.allocation)
                led["enabled"] = True
                entry["status"] = DEPLOYED
                applied["funded"] += 1
                applied["capital"] += d.allocation
            elif d.decision == REDUCE_RISK:
                self._fund(led, led.get("allocated", 0.0) * self.cfg.allocation.reduce_factor)
                applied["reduced"] += 1
            elif d.decision in (RETIRE, ROLLBACK):
                self._close_strategy(deployer, key)   # _exit respects deployer.dry_run
                led["enabled"] = False
                entry["status"] = RETIRED
                applied["retired"] += 1
            elif d.decision == HOLD:
                pass  # preserve current ledger state pending more information
            else:  # REJECT / REOPTIMIZE / RESEARCH / APPROVE (not funded this cycle)
                if led.get("enabled"):
                    self._close_strategy(deployer, key)   # _exit respects deployer.dry_run
                    led["enabled"] = False
                    applied["disabled"] += 1
                entry["status"] = (REJECTED if d.decision == REJECT
                                    else CANDIDATE if d.decision in (REOPTIMIZE, RESEARCH)
                                    else APPROVED)
            entry["allocation"] = d.allocation
        if deployer is not None:
            deployer.save()
        return applied

    @staticmethod
    def _fund(led: dict, new_allocated: float) -> None:
        """Move the slice's tradable cash by the change in its capital
        allocation and rebase the day/week/peak watermarks by the same
        amount, so a funding change reads as a deposit/withdrawal rather
        than P&L — otherwise a fresh DEPLOY (cash unchanged, allocated
        jumping from the default to its real size) would look like an
        instant large drawdown and trip drift/breaker checks next cycle."""
        old = led.get("allocated", 0.0)
        delta = new_allocated - old
        if delta < 0:
            delta = max(delta, -led.get("cash", 0.0))  # never withdraw below zero cash
        if delta:
            led["cash"] = led.get("cash", 0.0) + delta
            wm = led.get("watermarks", {})
            for k in ("day_start", "week_start", "peak"):
                if k in wm:
                    wm[k] += delta
        led["allocated"] = old + delta

    def _close_strategy(self, deployer, key: str) -> None:
        entry = deployer.ledger.get(key, {})
        om = next((o for o in deployer.cohort if o.id == key), None)
        if om is None:
            return
        from ..data.provider import aligned_closes
        syms = list(entry.get("positions", {}))
        if not syms:
            return
        bars = deployer.provider.get_universe_bars(syms)
        closes = aligned_closes(bars)
        for sym in syms:
            if sym in closes.columns and closes[sym].notna().any():
                px = float(closes[sym].dropna().iloc[-1])
                deployer._exit(om, entry, sym, entry["positions"][sym]["qty"], px, "governance-retire")

    def _sync_registry(self, key: str, d: GovernanceDecision) -> None:
        entry = self.registry.get(key)
        entry["allocation"] = d.allocation
        if d.decision in (DEPLOY, INCREASE_ALLOCATION):
            entry["status"] = DEPLOYED
        elif d.decision == RETIRE:
            entry["status"] = RETIRED
        elif d.decision == REJECT:
            entry["status"] = REJECTED
        elif d.decision == APPROVE:
            entry["status"] = APPROVED

    # ---------------------------------------------------------- ledger
    def append_ledger(self, decisions: dict[str, GovernanceDecision]) -> None:
        path = get_config().resolve_path(self.cfg.decisions_file)
        history = load_json(path, default=[]) or []
        history.extend(d.to_dict() for d in decisions.values())
        atomic_write_json(path, history[-5000:])
