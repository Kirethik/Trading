"""llm_bridge: the single choke point callers use, now backed by Gemini/ADK."""
from __future__ import annotations

import pytest

from regime_trader.govern import llm_bridge


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr(llm_bridge.guards, "kill_switch_engaged", lambda path=None: False)
    monkeypatch.setattr(llm_bridge.guards, "agents_paused", lambda path=None: None)


def test_unavailable_without_google_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert llm_bridge.available() is False
    assert llm_bridge.escalate("r", "governance-protocol", "i", {}, {"decision": "x"}) is None


def test_unavailable_when_kill_switch(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(llm_bridge.guards, "kill_switch_engaged", lambda path=None: True)
    assert llm_bridge.available() is False


def test_unknown_skill_returns_none(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert llm_bridge.escalate("r", "no-such-skill", "i", {}, {"decision": "x"}) is None


def test_escalate_delegates_to_runtime(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    seen = {}

    def fake_run(spec, instruction, payload, schema, subject=None):
        seen.update(agent=spec.name, instruction=instruction, payload=payload)
        return {"decision": "HOLD", "rationale": "r"}

    monkeypatch.setattr(llm_bridge.runtime, "run_agent", fake_run)
    out = llm_bridge.escalate("Chief Governance", "governance-protocol", "decide",
                              {"strategy": "s"}, {"decision": "x", "rationale": "s"})
    assert out == {"decision": "HOLD", "rationale": "r"}
    assert seen == {"agent": "governance", "instruction": "decide", "payload": {"strategy": "s"}}


def test_escalation_disabled_in_config(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(llm_bridge, "_enabled", lambda: False)
    assert llm_bridge.available() is False


def test_unavailable_while_watchman_pause_active(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(llm_bridge.guards, "agents_paused", lambda path=None: {"reason": "x"})
    assert llm_bridge.available() is False
