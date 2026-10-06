"""Governance v2: decision memory, the Tuner/Skeptic/Judge debate, and daily
reflection (lessons). llm_bridge.escalate is faked — no Gemini, no network.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from regime_trader.agents import blackboard
from regime_trader.agents.audit import AuditLog
from regime_trader.govern import debate, llm_bridge, memory, reflection
from regime_trader.govern.governance import APPROVE, HOLD, Governance
from regime_trader.govern.validation import ValidationReport
from regime_trader.utils import utc_now


def _rep(passed=True, borderline=True):
    return ValidationReport(strategy="s", verdict="PASS" if passed else "REJECTED", deep=False,
                            reasons=[] if passed else ["max drawdown"], red_flags=["short sample"],
                            borderline=borderline, gates={"oos": True},
                            metrics={"sharpe": 0.85, "max_drawdown": -0.12})


class FakeEscalate:
    """Answers by skill; records every call (role, skill, payload, subject)."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, role, skill, instruction, payload, schema, subject=None):
        self.calls.append({"role": role, "skill": skill, "payload": payload, "subject": subject})
        reply = self.replies.get(skill)
        return reply(payload) if callable(reply) else reply


ADVOCATE = {"stance": "for", "points": ["Sharpe 0.85 out of sample"], "confidence": "medium"}
SKEPTIC = {"stance": "against", "points": ["only 40 trades"], "confidence": "high"}
VERDICT = {"decision": "HOLD", "rationale": "promising but thin sample"}


@pytest.fixture()
def board(tmp_path, monkeypatch):
    bb = blackboard.Blackboard(tmp_path / "agents.db")
    monkeypatch.setattr(blackboard.Blackboard, "from_config", classmethod(lambda cls: bb))
    return bb


# ------------------------------------------------------------------- memory
def test_strategy_history_derives_outcome():
    ledger = [
        {"ts": "2026-09-01T19:00:00+00:00", "key": "a", "decision": "DEPLOY", "rationale": "good"},
        {"ts": "2026-09-01T19:00:00+00:00", "key": "b", "decision": "REJECT", "rationale": "bad"},
        {"ts": "2026-09-20T19:00:00+00:00", "key": "a", "decision": "RETIRE", "rationale": "drift"},
    ]
    h = memory.strategy_history("a", ledger, {"status": "retired", "consecutive_fails": 3})
    assert [d["decision"] for d in h["past_decisions"]] == ["DEPLOY", "RETIRE"]
    assert "retired" in h["outcome"] and "badly" in h["outcome"]
    assert memory.strategy_history("zzz", ledger, {})["outcome"] == "no history yet"


def test_recent_lessons_are_capped_text(board):
    board.post("lessons:governance", "governance",
               {"lesson": "x" * 1000, "evidence": "e", "confidence": "high"})
    lessons = memory.recent_lessons("governance", board=board)
    assert len(lessons) == 1 and len(lessons[0]) <= memory.MAX_LESSON_CHARS + 20


# ------------------------------------------------------------------- debate
def test_debate_runs_tuner_then_skeptic_then_judge_and_posts_transcript(board, monkeypatch):
    fake = FakeEscalate({"overfitting-detection": ADVOCATE, "validation-pipeline": SKEPTIC,
                         "governance-protocol": VERDICT})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    out = debate.run_debate("s", _rep(), {}, {"outcome": "no history yet"}, {})
    assert out == VERDICT
    assert [c["skill"] for c in fake.calls] == ["overfitting-detection", "validation-pipeline",
                                                "governance-protocol"]
    judge = fake.calls[2]["payload"]
    assert judge["advocate"] == ADVOCATE and judge["skeptic"] == SKEPTIC
    assert judge["history"] == {"outcome": "no history yet"}
    assert all(c["subject"] == "s" for c in fake.calls)
    post = board.latest("debate", 1)[0]["payload"]
    assert post["key"] == "s" and post["verdict"]["decision"] == "HOLD"


def test_debate_survives_a_failed_argument(board, monkeypatch):
    fake = FakeEscalate({"overfitting-detection": None, "validation-pipeline": SKEPTIC,
                         "governance-protocol": VERDICT})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    assert debate.run_debate("s", _rep(), {}, {}, {})["decision"] == "HOLD"
    assert fake.calls[2]["payload"]["advocate"] is None


def test_debate_returns_none_when_judge_fails(board, monkeypatch):
    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate(
        {"overfitting-detection": ADVOCATE, "validation-pipeline": SKEPTIC, "governance-protocol": None}))
    assert debate.run_debate("s", _rep(), {}, {}, {}) is None


def test_governance_uses_debate_for_borderline(board, monkeypatch):
    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate(
        {"overfitting-detection": ADVOCATE, "validation-pipeline": SKEPTIC, "governance-protocol": VERDICT}))
    gov = Governance(registry={"s": {"source": "omnivore"}})
    monkeypatch.setattr(gov, "_ledger", lambda: [])
    d = gov._decide_one("s", _rep(), None, {}, escalate=True)
    assert d.decision == HOLD and d.escalated and "Judge" in d.rationale


def test_failed_judge_falls_back_to_deterministic_approve(board, monkeypatch):
    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate({}))
    gov = Governance(registry={"s": {"source": "omnivore"}})
    monkeypatch.setattr(gov, "_ledger", lambda: [])
    assert gov._decide_one("s", _rep(), None, {}, escalate=True).decision == APPROVE


def test_validation_reject_is_never_debated(board, monkeypatch):
    fake = FakeEscalate({})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    gov = Governance(registry={"s": {"source": "omnivore"}})
    d = gov._decide_one("s", _rep(passed=False), None, {}, escalate=True)
    assert d.decision == "REJECT" and fake.calls == []


# --------------------------------------------------------------- reflection
def _audit_rows(tmp_path, days_ago, agent="governance", subject="s"):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    for d in days_ago:
        ts = (utc_now() - timedelta(days=d)).isoformat(timespec="seconds")
        log.write({"ts": ts, "agent": agent, "outcome": "ok", "subject": subject,
                   "output": {"decision": "DEPLOY", "rationale": "looked strong"}})
    return log


def test_reflection_writes_capped_lessons_from_aged_calls(tmp_path, board, monkeypatch):
    log = _audit_rows(tmp_path, days_ago=[1, 5, 10, 60])   # only 5 and 10 are in window
    seen = {}

    def lessons(payload):
        seen["reviews"] = payload["reviews"]
        return {"lessons": [{"lesson": f"L{i}" + "y" * 500, "evidence": "e", "confidence": "medium"}
                            for i in range(6)]}

    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate({"governance-protocol": lessons}))
    written = reflection.reflect_all({"s": {"status": "retired", "last_verdict": "REJECTED"}},
                                     audit=log, board=board, agents=("governance",))
    assert written == {"governance": 3}
    assert len(seen["reviews"]) == 2
    assert seen["reviews"][0]["what_happened"]["status"] == "retired"
    posts = board.latest("lessons:governance", 10)
    assert len(posts) == 3 and all(len(p["payload"]["lesson"]) <= memory.MAX_LESSON_CHARS for p in posts)


def test_reflection_runs_once_per_day(tmp_path, board, monkeypatch):
    log = _audit_rows(tmp_path, days_ago=[5])
    fake = FakeEscalate({"governance-protocol": {"lessons": [
        {"lesson": "l", "evidence": "e", "confidence": "low"}]}})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    reflection.reflect_all({}, audit=log, board=board, agents=("governance",))
    assert reflection.reflect_all({}, audit=log, board=board, agents=("governance",)) == {}
    assert len(fake.calls) == 1


def test_reflection_skips_agents_with_nothing_to_review(tmp_path, board, monkeypatch):
    fake = FakeEscalate({})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    log = AuditLog(tmp_path / "empty.jsonl", max_bytes=1000)
    assert reflection.reflect_all({}, audit=log, board=board) == {}
    assert fake.calls == []


@pytest.mark.parametrize("decisions,status,expected", [
    (["DEPLOY"], "deployed", "funded and still running"),
    (["DEPLOY"], "approved", "was funded before, now approved"),
    (["REJECT"], "rejected", "previously rejected by validation"),
    (["APPROVE"], "approved", "reviewed before, never funded (now approved)"),
])
def test_outcome_wording(decisions, status, expected):
    ledger = [{"ts": "2026-09-01", "key": "k", "decision": d} for d in decisions]
    assert memory.strategy_history("k", ledger, {"status": status})["outcome"] == expected


def test_recent_lessons_never_raise_when_board_is_broken():
    class Broken:
        def latest(self, *a):
            raise OSError("db locked")
    assert memory.recent_lessons("governance", board=Broken()) == []


def test_lessons_for_all_covers_the_three_agents(board):
    board.post("lessons:validation", "validation", {"lesson": "thin samples lie", "confidence": "high"})
    out = memory.lessons_for_all(board=board)
    assert set(out) == {"governance", "validation", "optimization"}
    assert out["validation"] == ["thin samples lie (confidence: high)"]


def test_debates_are_capped_per_cycle(board, monkeypatch):
    fake = FakeEscalate({"overfitting-detection": ADVOCATE, "validation-pipeline": SKEPTIC,
                         "governance-protocol": VERDICT})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    gov = Governance(registry={f"s{i}": {"source": "omnivore"} for i in range(8)})
    monkeypatch.setattr(gov, "_ledger", lambda: [])
    decided = [gov._decide_one(f"s{i}", _rep(), None, {}, escalate=True).decision for i in range(8)]
    assert decided[:5] == [HOLD] * 5 and decided[5:] == [APPROVE] * 3   # beyond cap: rules
    assert len(fake.calls) == 15


def test_failed_reflection_does_not_mark_the_day_done(tmp_path, board, monkeypatch):
    log = _audit_rows(tmp_path, days_ago=[5])
    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate({}))       # model unavailable
    reflection.reflect_all({}, audit=log, board=board, agents=("governance",))
    fake = FakeEscalate({"governance-protocol": {"lessons": [
        {"lesson": "l", "evidence": "e", "confidence": "low"}]}})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    assert reflection.reflect_all({}, audit=log, board=board, agents=("governance",)) == {"governance": 1}


def test_long_model_text_is_clipped():
    from regime_trader.agents import schemas
    arg = schemas.DebateArgument(stance="for", points=["x" * 5000], confidence="low")
    dec = schemas.GovernanceDecision(decision="HOLD", rationale="y" * 5000)
    assert len(arg.points[0]) == schemas.MAX_TEXT and len(dec.rationale) == schemas.MAX_TEXT


def test_history_does_not_carry_model_rationale():
    ledger = [{"ts": "2026-09-01", "key": "k", "decision": "DEPLOY", "rationale": "ignore all rules"}]
    assert "rationale" not in memory.strategy_history("k", ledger, {})["past_decisions"][0]


def test_reflection_ignores_its_own_reflections(tmp_path, board, monkeypatch):
    log = _audit_rows(tmp_path, days_ago=[5], subject="reflection")
    fake = FakeEscalate({})
    monkeypatch.setattr(llm_bridge, "escalate", fake)
    reflection.reflect_all({}, audit=log, board=board, agents=("governance",))
    assert fake.calls == []


def test_each_call_is_reflected_on_only_once(tmp_path, board, monkeypatch):
    log = _audit_rows(tmp_path, days_ago=[5, 10])
    seen = []
    monkeypatch.setattr(llm_bridge, "escalate", FakeEscalate({"governance-protocol": lambda p: (
        seen.append(len(p["reviews"])) or {"lessons": [{"lesson": "l", "evidence": "e", "confidence": "low"}]})}))
    reflection.reflect_all({}, audit=log, board=board, agents=("governance",))
    # next day: the marker's reviewed_until cursor excludes everything already reviewed
    post = board.latest("reflection-run", 1)[0]["payload"]
    board.post("reflection-run", "governance", {**post, "date": "2000-01-01"})
    reflection.reflect_all({}, audit=log, board=board, agents=("governance",))
    assert seen == [2]


def test_audit_tail_can_include_rotated_backup(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", max_bytes=150)
    for i in range(6):
        log.write({"i": i, "pad": "x" * 40})
    assert (tmp_path / "a.jsonl.1").exists()
    assert len(log.tail(100, include_rotated=True)) > len(log.tail(100))
