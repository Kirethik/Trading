"""Agent runtime: run_agent orchestration + ADK wiring, with the model faked.

`model_call` is injected, so nothing here reaches Gemini.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from regime_trader.agents import audit, budget, guards, runtime, schemas, specs

DECISION_SCHEMA = {"decision": "DEPLOY|APPROVE|REDUCE_RISK|HOLD", "rationale": "string"}
PRICING = {"gemini-3.8-flash": {"input": 1.0, "output": 1.0},
           "gemini-3.1-pro-preview": {"input": 1.0, "output": 1.0}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: False)
    runtime.reset_failure_streak()
    return SimpleNamespace(
        budget=budget.Budget(tmp_path / "b.json", daily_usd=1.0, pricing=PRICING),
        audit=audit.AuditLog(tmp_path / "a.jsonl", max_bytes=1_000_000),
        alerts=[],
    )


def _fake(reply: str, tokens=(1000, 200), delay: float = 0.0, calls: list | None = None):
    async def call(spec, system_text, user_text, output_model, usage=None):
        if calls is not None:
            calls.append({"system": system_text, "user": user_text, "model": output_model})
        if delay:
            await asyncio.sleep(delay)
        return reply, tokens[0], tokens[1]
    return call


def _run(env, reply="{}", schema=DECISION_SCHEMA, skill="governance-protocol", **kw):
    return runtime.run_agent(
        specs.get_spec(skill), "decide", {"strategy": "x"}, schema,
        model_call=kw.pop("model_call", _fake(reply)), budget=env.budget, audit=env.audit,
        alert=lambda msg: env.alerts.append(msg), **kw,
    )


def test_ok_returns_validated_legacy_dict_and_audits(env):
    out = _run(env, json.dumps({"decision": "HOLD", "rationale": "thin edge"}))
    assert out == {"decision": "HOLD", "rationale": "thin edge"}
    rec = env.audit.tail(1)[0]
    assert rec["outcome"] == "ok" and rec["agent"] == "governance"
    assert rec["model"] == "gemini-3.1-pro-preview"
    assert env.budget.spent_today() > 0


def test_rubric_and_hard_constraint_go_to_system_text(env):
    calls: list = []
    _run(env, '{"decision":"HOLD","rationale":"r"}', model_call=_fake(
        '{"decision":"HOLD","rationale":"r"}', calls=calls))
    assert "never approve a strategy that failed a hard validation gate" in calls[0]["system"]
    assert "governance" in calls[0]["system"].lower()
    assert '"strategy": "x"' in calls[0]["user"]
    assert calls[0]["model"] is schemas.GovernanceDecision


def test_param_proposal_reply_is_folded_to_legacy_shape(env):
    reply = {"candidates": [{"params": [{"name": "lookback", "value": 30}]}], "rationale": "r"}
    out = _run(env, json.dumps(reply), schema={"candidates": [{"<attr>": "number"}], "rationale": "s"},
               skill="overfitting-detection")
    assert out == {"candidates": [{"lookback": 30.0}], "rationale": "r"}


def test_reply_wrapped_in_prose_still_parses(env):
    out = _run(env, 'Sure! {"decision": "APPROVE", "rationale": "ok"} done')
    assert out["decision"] == "APPROVE"


def test_unknown_schema_returns_raw_json(env):
    out = _run(env, '{"anything": 1}', schema={"anything": "number"})
    assert out == {"anything": 1}


@pytest.mark.parametrize("reply", ["no json here", '{"decision": "YOLO", "rationale": "x"}'])
def test_invalid_reply_returns_none(env, reply):
    assert _run(env, reply) is None
    assert env.audit.tail(1)[0]["outcome"] == "invalid"


def test_timeout_returns_none(env):
    out = _run(env, model_call=_fake("{}", delay=0.5), timeout=0.05)
    assert out is None
    assert env.audit.tail(1)[0]["outcome"] == "timeout"


def test_model_exception_returns_none(env):
    async def boom(*a):
        raise RuntimeError("quota")
    assert _run(env, model_call=boom) is None
    assert env.audit.tail(1)[0]["outcome"] == "error"


def test_budget_exhausted_skips_model(env):
    env.budget.record("gemini-3.8-flash", 2_000_000, 0)  # $2 > $1 cap
    calls: list = []
    assert _run(env, model_call=_fake("{}", calls=calls)) is None
    assert calls == []
    assert env.audit.tail(1)[0]["outcome"] == "blocked"
    assert any("budget" in a for a in env.alerts)


def test_kill_switch_skips_model(env, monkeypatch):
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: True)
    calls: list = []
    assert _run(env, model_call=_fake("{}", calls=calls)) is None
    assert calls == []


def test_live_account_raises(env, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(guards.PaperOnlyViolation):
        _run(env, '{"decision":"HOLD","rationale":"r"}')


def test_failure_streak_alerts_once_threshold_hit(env):
    for _ in range(3):
        _run(env, "garbage")
    assert sum("consecutive" in a for a in env.alerts) == 1
    _run(env, '{"decision":"HOLD","rationale":"r"}')  # success resets the streak
    assert runtime.failure_streak() == 0


def test_runs_inside_an_existing_event_loop(env):
    async def inside():
        return _run(env, '{"decision":"HOLD","rationale":"r"}')
    assert asyncio.run(inside())["decision"] == "HOLD"


# ----------------------------------------------------------------- ADK wiring
def test_build_agent_wires_static_instruction_schema_and_guards():
    spec = specs.get_spec("governance-protocol")
    agent = runtime.build_agent(spec, "SYSTEM {not_a_state_var}", schemas.GovernanceDecision)
    assert agent.name == "governance"
    assert agent.model == "gemini-3.1-pro-preview"
    assert agent.static_instruction == "SYSTEM {not_a_state_var}"
    assert agent.output_schema is schemas.GovernanceDecision
    assert agent.before_tool_callback is not None and agent.before_model_callback is not None


def test_collect_sums_usage_and_takes_final_text_from_author():
    def ev(author, text, final, p=0, c=0):
        part = SimpleNamespace(text=text)
        return SimpleNamespace(
            author=author, content=SimpleNamespace(parts=[part]) if text else None,
            usage_metadata=SimpleNamespace(prompt_token_count=p, candidates_token_count=c),
            is_final_response=lambda: final,
        )

    async def events():
        for e in [ev("governance", None, False, 100, 5), ev("other", "nope", True),
                  ev("governance", '{"ok": 1}', True, 50, 20)]:
            yield e

    text, tin, tout = asyncio.run(runtime.collect(events(), "governance"))
    assert (text, tin, tout) == ('{"ok": 1}', 150, 25)


def test_collect_bills_thinking_tokens_and_skips_thought_text():
    meta = SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=40)
    parts = [SimpleNamespace(text="{thinking", thought=True), SimpleNamespace(text='{"a": 1}', thought=False)]
    ev = SimpleNamespace(author="g", content=SimpleNamespace(parts=parts), usage_metadata=meta,
                         is_final_response=lambda: True)

    async def events():
        yield ev

    assert asyncio.run(runtime.collect(events(), "g")) == ('{"a": 1}', 10, 45)


def test_specs_load_rubric_files():
    for skill in ("governance-protocol", "overfitting-detection", "validation-pipeline"):
        assert len(specs.get_spec(skill).rubric()) > 100
    assert specs.get_spec("nope") is None


def test_timeout_still_bills_tokens_spent_before_it(env):
    async def slow(spec, system, user, model, usage=None):
        usage["in"] += 500_000
        await asyncio.sleep(1)
        return "{}", 0, 0
    assert _run(env, model_call=slow, timeout=0.05) is None
    rec = env.audit.tail(1)[0]
    assert rec["outcome"] == "timeout" and rec["input_tokens"] == 500_000
    assert env.budget.spent_today() == pytest.approx(0.5)


def test_mid_run_block_is_reported_as_blocked_not_invalid(env):
    assert _run(env, runtime.BLOCKED_TEXT) is None
    assert env.audit.tail(1)[0]["outcome"] == "blocked"


def test_errors_are_redacted(env, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSECRET123")
    async def boom(*a, **k):
        raise RuntimeError("bad request key=AIzaSECRET123 Authorization: Bearer xyz")
    _run(env, model_call=boom)
    err = env.audit.tail(1)[0]["error"]
    assert "AIzaSECRET123" not in err and "xyz" not in err


def test_audit_failure_does_not_crash_caller(env, monkeypatch):
    def broken(record):
        raise OSError("disk full")
    monkeypatch.setattr(env.audit, "write", broken)
    assert _run(env, '{"decision":"HOLD","rationale":"r"}')["decision"] == "HOLD"


def test_empty_pricing_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        budget.Budget(tmp_path / "b.json", daily_usd=1.0, pricing={})
