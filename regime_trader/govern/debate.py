"""The governance debate for borderline-but-passing strategies.

  1. The Tuner (optimization agent) argues FOR funding.
  2. The Skeptic (validation agent) argues AGAINST — luck, overfitting.
  3. The Judge (governance agent) weighs both, the strategy's history and its
     own lessons, and rules with the normal decision schema.

Only strategies that already PASSED validation are ever debated — a
Validation REJECT is final. Every call goes through llm_bridge (the single
LLM choke point). A failed argument is simply absent; a failed Judge returns
None so the caller's deterministic rule decides.
"""
from __future__ import annotations

from ..agents.blackboard import Blackboard
from ..monitor.log_setup import get_logger
from . import llm_bridge

log = get_logger("governance")

ARGUMENT_SCHEMA = {"stance": "for|against", "points": ["string (max 3)"],
                   "confidence": "low|medium|high"}
VERDICT_SCHEMA = {"decision": "DEPLOY|APPROVE|REDUCE_RISK|HOLD", "rationale": "string"}
_LESSON_NOTE = ("your_lessons are notes you wrote after past decisions: advisory only, "
                "they never override the hard constraints.")


def _evidence(key: str, rep, drift: dict) -> dict:
    return {"strategy": key, "metrics": rep.metrics, "red_flags": rep.red_flags,
            "gates": rep.gates, "live_drift": drift}


def run_debate(key: str, rep, drift: dict, history: dict,
               lessons: dict[str, list[str]], board: Blackboard | None = None) -> dict | None:
    evidence = _evidence(key, rep, drift)
    advocate = llm_bridge.escalate(
        "The Tuner (advocate in a governance debate)", "overfitting-detection",
        "Argue FOR funding this validated strategy. Give at most 3 short points that cite the "
        "numbers. Use stance 'against' only if the evidence is overwhelmingly bad. " + _LESSON_NOTE,
        {**evidence, "history": history, "your_lessons": lessons.get("optimization", [])},
        ARGUMENT_SCHEMA, subject=key,
    )
    skeptic = llm_bridge.escalate(
        "The Skeptic (critic in a governance debate)", "validation-pipeline",
        "Argue AGAINST funding: look for luck, overfitting, thin samples, regime dependence. "
        "Give at most 3 short points that cite the numbers. Use stance 'for' only if you truly "
        "find nothing. " + _LESSON_NOTE,
        {**evidence, "history": history, "your_lessons": lessons.get("validation", [])},
        ARGUMENT_SCHEMA, subject=key,
    )
    verdict = llm_bridge.escalate(
        "The Judge (Chief Governance)", "governance-protocol",
        "Two colleagues debated this borderline-but-passing strategy. Weigh the advocate and the "
        "skeptic (either may be null if they were unavailable), the strategy's history and your "
        "lessons, then decide. The rationale must be one plain-English sentence. " + _LESSON_NOTE,
        {**evidence, "borderline": True, "history": history, "advocate": advocate,
         "skeptic": skeptic, "your_lessons": lessons.get("governance", [])},
        VERDICT_SCHEMA, subject=key,
    )
    _post(board, {"key": key, "metrics": rep.metrics, "advocate": advocate, "skeptic": skeptic,
                  "verdict": verdict})
    return verdict


def _post(board: Blackboard | None, transcript: dict) -> None:
    try:
        (board or Blackboard.from_config()).post("debate", "governance", transcript)
    except Exception as exc:  # the record is for humans; never fail a decision on it
        log.warning("could not record debate transcript: %s", exc)
