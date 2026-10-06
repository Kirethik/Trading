"""Continuous Optimization Team (Sonnet role).

Never assumes a strategy is finished: searches each strategy's neighbor
parameter space for a higher-Sharpe variant. Daily it runs light (drift flags +
single-step nudges on deployed laggards); weekly/deep it runs a small greedy
grid over every tunable parameter.

It only PROPOSES. Proposals go to the independent Validation Team and then to
Governance — optimization can never deploy or approve anything itself.
Optionally asks the Sonnet agent for direction (advisory) when a search stalls.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ..backtest.engine import WalkForwardEngine
from ..config import get_config
from ..monitor.log_setup import get_logger
from . import llm_bridge, sweeps
from .registry import GovernedStrategy

log = get_logger("governance")


@dataclass
class OptimizationProposal:
    key: str                          # base strategy key
    base_sharpe: float
    proposed_sharpe: float
    changes: dict = field(default_factory=dict)       # attr -> new value
    proposed_strategy: object = None  # cloned strategy carrying the changes
    rationale: str = ""

    @property
    def improves(self) -> bool:
        return self.proposed_strategy is not None and self.proposed_sharpe > self.base_sharpe


def _sharpe(bars, strategy, symbols, oracle) -> float:
    return float(WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle).run().metrics.get("sharpe", 0.0))


def llm_propose_params(gs: GovernedStrategy, base: float, max_candidates: int) -> list[dict]:
    """Ask the Claude agent for concrete parameter sets (the daily improver).

    Returns sanitized candidate dicts (only known tunable attrs, coerced to the
    right type). Empty list on any agent failure — callers fall back to the
    deterministic greedy search, so this can never block the cycle.
    """
    spec = sweeps.tunable_params(gs.strategy)
    if not spec:
        return []
    current = {a: getattr(gs.strategy, a) for a in spec}
    out = llm_bridge.escalate(
        "Optimization Team (Sonnet)", "overfitting-detection",
        f"Propose up to {max_candidates} alternative parameter sets for this strategy "
        "that are likely to improve risk-adjusted return on its symbols. Prefer robust, "
        "round values over fine-tuned ones; stay within sensible bounds (lookbacks/windows "
        "5-300 bars, RSI levels 1-99, n_std 0.5-4.0, target_vol 0.05-0.60). Use only the "
        "attributes listed in tunable_attrs.",
        {"strategy": gs.key, "type": type(gs.strategy).__name__, "symbols": gs.symbols,
         "tunable_attrs": {a: ("int" if i else "float") for a, i in spec.items()},
         "current_params": current, "base_sharpe": base},
        {"candidates": [{"<attr>": "number"}], "rationale": "string"},
    )
    if not out:
        return []
    clean: list[dict] = []
    for c in (out.get("candidates") or [])[:max_candidates]:
        if isinstance(c, dict) and isinstance(c.get("params"), dict):
            c = c["params"]  # tolerate {"params": {...}} wrapping
        if not isinstance(c, dict):
            continue
        params: dict = {}
        for attr, val in c.items():
            if attr not in spec:
                continue
            try:
                v = int(round(float(val))) if spec[attr] else float(val)
            except (TypeError, ValueError):
                params = {}
                break
            if (spec[attr] and v < 0) or (not spec[attr] and not v > 0):
                params = {}  # negative window / non-positive float param: nonsense
                break
            params[attr] = v
        if params and any(params[a] != current[a] for a in params):
            clean.append(params)
    return clean


def propose(gs: GovernedStrategy, bars, oracle, deep: bool,
            min_improvement: float = 0.10, max_params: int = 2,
            use_llm: bool = False) -> OptimizationProposal | None:
    """Greedy per-parameter neighbor search for a better-Sharpe variant.

    With use_llm, the Claude agent additionally proposes whole parameter sets
    (the daily improver); every candidate is backtested and only adopted if it
    beats the greedy result. The winner still goes through Validation and
    Governance like any other proposal.
    """
    symbols = [s for s in gs.symbols if s in bars]
    if not symbols:
        return None
    spec = sweeps.tunable_params(gs.strategy)
    if not spec:
        return None  # config-driven strategies (e.g. regime-hmm) aren't param-searched here

    g = get_config().governance.parameter_sensitivity
    neighbors = g.neighbors if deep else 1
    step = g.step
    base = _sharpe(bars, gs.strategy, symbols, oracle)
    best_strategy = copy.deepcopy(gs.strategy)
    best_sharpe, changes = base, {}

    for attr, is_int in list(spec.items())[:max_params]:
        center = getattr(best_strategy, attr)
        local_best, local_val = best_sharpe, center
        for k in list(range(-neighbors, 0)) + list(range(1, neighbors + 1)):
            cand = copy.deepcopy(best_strategy)
            new = max(1, int(round(center * (1 + k * step)))) if is_int else float(center * (1 + k * step))
            if new == center:
                continue
            setattr(cand, attr, new)
            sh = _sharpe(bars, cand, symbols, oracle)
            if sh > local_best:
                local_best, local_val = sh, new
        if local_val != center:
            setattr(best_strategy, attr, local_val)
            best_sharpe, changes[attr] = local_best, local_val

    # daily improver: LLM-proposed parameter sets, each independently backtested
    source = "greedy neighbor search"
    if use_llm:
        icfg = get_config().governance.get("improver")
        n_cand = int(icfg.get("max_candidates", 3)) if icfg else 3
        for params in llm_propose_params(gs, base, n_cand):
            cand = copy.deepcopy(gs.strategy)
            for attr, val in params.items():
                setattr(cand, attr, val)
            sh = _sharpe(bars, cand, symbols, oracle)
            if sh > best_sharpe:
                best_strategy, best_sharpe, changes = cand, sh, dict(params)
                source = "LLM-proposed parameter set"

    if not changes or best_sharpe < base + min_improvement:
        return None
    rationale = f"{source} lifted Sharpe {base:.2f}->{best_sharpe:.2f} via {changes}"
    if deep:  # advisory narrative only; does not change the numeric proposal
        adv = llm_bridge.escalate(
            "Optimization Team (Sonnet)", "overfitting-detection",
            "Comment briefly on whether this parameter change risks overfitting.",
            {"strategy": gs.key, "base_sharpe": base, "changes": changes, "proposed_sharpe": best_sharpe},
            {"caution": "string", "overfit_risk": "low|medium|high"},
        )
        if adv:
            rationale += f" | Sonnet: {adv.get('caution', '')} (risk={adv.get('overfit_risk')})"
    return OptimizationProposal(key=gs.key, base_sharpe=base, proposed_sharpe=best_sharpe,
                                changes=changes, proposed_strategy=best_strategy, rationale=rationale)


def run_optimization(governed: list[GovernedStrategy], bars, oracle, deep: bool,
                     only_keys: set[str] | None = None,
                     llm_keys: set[str] | None = None) -> dict[str, OptimizationProposal]:
    """Produce proposals. Daily: only `only_keys` (deployed laggards + focus
    cohort). Deep/weekly: the whole governed set. Strategies in `llm_keys`
    additionally get LLM-proposed parameter sets (the daily improver)."""
    llm_keys = llm_keys or set()
    icfg = get_config().governance.get("improver")
    min_imp = float(icfg.get("min_improvement", 0.10)) if icfg else 0.10
    proposals: dict[str, OptimizationProposal] = {}
    for gs in governed:
        if only_keys is not None and gs.key not in only_keys:
            continue
        try:
            p = propose(gs, bars, oracle, deep, min_improvement=min_imp,
                        use_llm=gs.key in llm_keys)
            if p and p.improves:
                proposals[gs.key] = p
                log.info("optimization proposal %s: %s", gs.key, p.rationale)
        except Exception as exc:
            log.warning("optimization failed for %s: %s", gs.key, exc)
    return proposals
