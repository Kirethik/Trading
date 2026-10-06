"""Agent runtime core: output schemas, paper/tool guards, budget, audit log.

No network, no Gemini — pure units over tmp paths.
"""
from __future__ import annotations

import json

import pytest

from regime_trader.agents import audit, budget, guards, schemas


# ------------------------------------------------------------------ schemas
def test_schema_for_maps_every_legacy_shape():
    assert schemas.schema_for({"decision": "x", "rationale": "s"}) is schemas.GovernanceDecision
    assert schemas.schema_for({"selected": ["k"], "rationale": "s"}) is schemas.AllocationSelection
    assert schemas.schema_for({"candidates": [{}], "rationale": "s"}) is schemas.ParamProposal
    assert schemas.schema_for({"caution": "s", "overfit_risk": "low"}) is schemas.OverfitCaution
    assert schemas.schema_for({"overfit": True, "reason": "s"}) is schemas.ValidationVerdict
    assert schemas.schema_for({"something": "else"}) is None


def test_param_proposal_converts_to_legacy_dict_shape():
    model = schemas.ParamProposal(
        candidates=[schemas.ParamSet(params=[schemas.ParamValue(name="lookback", value=30),
                                             schemas.ParamValue(name="skip", value=5)])],
        rationale="r",
    )
    assert model.to_legacy() == {"candidates": [{"lookback": 30.0, "skip": 5.0}], "rationale": "r"}


def test_governance_decision_rejects_unknown_decision():
    with pytest.raises(ValueError):
        schemas.GovernanceDecision(decision="YOLO", rationale="r")


# ------------------------------------------------------------------- guards
def test_paper_guard_passes_on_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    guards.assert_paper_only()


def test_paper_guard_raises_when_live(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(guards.PaperOnlyViolation):
        guards.assert_paper_only()


class _Tool:
    def __init__(self, name):
        self.name = name


def test_tool_guard_blocks_tool_above_agent_tier():
    guards.register_tool_tier("place_paper_order", guards.ToolTier.EXECUTE_PAPER)
    guard = guards.make_tool_guard(guards.ToolTier.PROPOSE, allowlist={"place_paper_order"})
    result = guard(_Tool("place_paper_order"), {}, None)
    assert result is not None and result["status"] == "blocked"


def test_tool_guard_blocks_tool_not_in_allowlist():
    guards.register_tool_tier("read_dashboard", guards.ToolTier.READ)
    guard = guards.make_tool_guard(guards.ToolTier.EXECUTE_PAPER, allowlist=set())
    assert guard(_Tool("read_dashboard"), {}, None)["status"] == "blocked"


def test_tool_guard_allows_permitted_tool():
    guards.register_tool_tier("read_dashboard", guards.ToolTier.READ)
    guard = guards.make_tool_guard(guards.ToolTier.PROPOSE, allowlist={"read_dashboard"})
    assert guard(_Tool("read_dashboard"), {}, None) is None


def test_unregistered_tool_is_blocked():
    guard = guards.make_tool_guard(guards.ToolTier.EXECUTE_PAPER, allowlist={"mystery"})
    assert guard(_Tool("mystery"), {}, None)["status"] == "blocked"


def test_kill_switch(tmp_path):
    flag = tmp_path / "agents.disabled"
    assert not guards.kill_switch_engaged(flag)
    flag.write_text("off")
    assert guards.kill_switch_engaged(flag)


# ------------------------------------------------------------------- budget
PRICING = {"cheap": {"input": 1.0, "output": 2.0}, "dear": {"input": 10.0, "output": 20.0}}


def test_budget_records_cost_and_persists(tmp_path):
    b = budget.Budget(tmp_path / "b.json", daily_usd=1.0, pricing=PRICING)
    cost = b.record("cheap", input_tokens=100_000, output_tokens=50_000)
    assert cost == pytest.approx(0.2)
    again = budget.Budget(tmp_path / "b.json", daily_usd=1.0, pricing=PRICING)
    assert again.spent_today() == pytest.approx(0.2)
    assert not again.exhausted()


def test_budget_exhausts_at_cap(tmp_path):
    b = budget.Budget(tmp_path / "b.json", daily_usd=0.5, pricing=PRICING)
    b.record("dear", input_tokens=50_000, output_tokens=0)
    assert b.exhausted()


def test_budget_unknown_model_priced_at_most_expensive(tmp_path):
    b = budget.Budget(tmp_path / "b.json", daily_usd=100.0, pricing=PRICING)
    assert b.cost("who-knows", 1_000_000, 0) == pytest.approx(10.0)


def test_budget_rolls_over_on_new_day(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"date": "2000-01-01", "spent_usd": 999.0, "calls": 5, "by_model": {}}))
    b = budget.Budget(path, daily_usd=1.0, pricing=PRICING)
    assert b.spent_today() == 0.0 and not b.exhausted()


# -------------------------------------------------------------------- audit
def test_audit_appends_and_tails(tmp_path):
    log = audit.AuditLog(tmp_path / "a.jsonl", max_bytes=1_000_000)
    for i in range(5):
        log.write({"agent": "x", "i": i})
    tail = log.tail(3)
    assert [r["i"] for r in tail] == [2, 3, 4]
    assert all("ts" in r for r in tail)


def test_audit_rotates_when_large(tmp_path):
    path = tmp_path / "a.jsonl"
    log = audit.AuditLog(path, max_bytes=200)
    for i in range(20):
        log.write({"agent": "x", "pad": "y" * 20, "i": i})
    assert (tmp_path / "a.jsonl.1").exists()
    assert path.stat().st_size <= 400


def test_payload_hash_is_stable_and_order_independent():
    assert audit.payload_hash({"a": 1, "b": 2}) == audit.payload_hash({"b": 2, "a": 1})
    assert audit.payload_hash({"a": 1}) != audit.payload_hash({"a": 2})
