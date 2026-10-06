"""Strict pydantic output models for agent replies.

Callers (governance.py / optimization.py) still pass the legacy informal
schema dicts; `schema_for` maps each shape to a strict model that Gemini's
structured output can enforce, and `to_legacy()` converts the reply back to
the exact dict the callers already sanitize.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Reply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    def to_legacy(self) -> dict:
        return self.model_dump()


class GovernanceDecision(_Reply):
    decision: Literal["DEPLOY", "APPROVE", "REDUCE_RISK", "HOLD"]
    rationale: str


class AllocationSelection(_Reply):
    selected: list[str]
    rationale: str


class ParamValue(_Reply):
    name: str
    value: float


class ParamSet(_Reply):
    params: list[ParamValue]


class ParamProposal(_Reply):
    """Free-form {attr: number} maps are unreliable in structured output, so
    the model returns name/value lists that are folded back into dicts."""

    candidates: list[ParamSet]
    rationale: str

    def to_legacy(self) -> dict:
        return {
            "candidates": [{p.name: p.value for p in c.params} for c in self.candidates],
            "rationale": self.rationale,
        }


class OverfitCaution(_Reply):
    caution: str
    overfit_risk: Literal["low", "medium", "high"]


class ValidationVerdict(_Reply):
    overfit: bool
    reason: str


# identifying top-level key -> strict model
_BY_KEY: dict[str, type[_Reply]] = {
    "decision": GovernanceDecision,
    "selected": AllocationSelection,
    "candidates": ParamProposal,
    "caution": OverfitCaution,
    "overfit": ValidationVerdict,
}


def schema_for(legacy_schema: dict) -> type[_Reply] | None:
    """The strict model for a caller's legacy schema dict, or None if unknown."""
    for key, model in _BY_KEY.items():
        if key in legacy_schema:
            return model
    return None
