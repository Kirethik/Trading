"""Blackboard (shared agent state) + long-running agent supervisor."""
from __future__ import annotations

import json
import signal

import pytest

from regime_trader.agents import blackboard, guards, supervisor


# --------------------------------------------------------------- blackboard
def test_blackboard_post_and_read_by_topic(tmp_path):
    bb = blackboard.Blackboard(tmp_path / "agents.db")
    first = bb.post("oversight", "watcher", {"msg": "drawdown rising"})
    bb.post("intel", "news", {"msg": "cpi beat"})
    second = bb.post("oversight", "watcher", {"msg": "recovered"})
    rows = bb.read("oversight")
    assert [r["payload"]["msg"] for r in rows] == ["drawdown rising", "recovered"]
    assert all(r["author"] == "watcher" and r["ts"] for r in rows)
    assert [r["payload"]["msg"] for r in bb.read("oversight", since_id=first)] == ["recovered"]
    assert second > first


def test_blackboard_survives_reopen_and_limits(tmp_path):
    path = tmp_path / "agents.db"
    for i in range(5):
        blackboard.Blackboard(path).post("t", "a", {"i": i})
    bb = blackboard.Blackboard(path)
    assert [r["payload"]["i"] for r in bb.read("t", limit=2)] == [0, 1]
    assert [r["payload"]["i"] for r in bb.latest("t", limit=2)] == [3, 4]


# --------------------------------------------------------------- supervisor
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: False)
    return tmp_path


def _sup(tmp_path, clock, **kw):
    return supervisor.Supervisor(heartbeat_path=tmp_path / "hb.json", heartbeat_seconds=10,
                                 clock=lambda: clock[0], sleep=lambda s: None, **kw)


def test_jobs_run_on_their_interval(env):
    clock, runs = [0.0], []
    sup = _sup(env, clock)
    sup.register(supervisor.Job("fast", 5, lambda: runs.append(("fast", clock[0]))))
    sup.register(supervisor.Job("slow", 20, lambda: runs.append(("slow", clock[0]))))
    for t in (0, 4, 5, 10, 20):
        clock[0] = float(t)
        sup.tick()
    assert runs == [("fast", 0.0), ("slow", 0.0), ("fast", 5.0), ("fast", 10.0),
                    ("fast", 20.0), ("slow", 20.0)]


def test_failing_job_is_isolated_and_recorded(env):
    clock, alerts = [0.0], []
    sup = _sup(env, clock, alert=alerts.append)
    sup.register(supervisor.Job("bad", 1, lambda: 1 / 0))
    sup.register(supervisor.Job("good", 1, lambda: None))
    sup.tick()
    hb = json.loads((env / "hb.json").read_text())
    assert hb["status"] == "running" and hb["pid"]
    assert "ZeroDivisionError" in hb["jobs"]["bad"]["last_error"]
    assert hb["jobs"]["good"]["runs"] == 1 and hb["jobs"]["good"]["last_error"] is None
    assert any("bad" in a for a in alerts)


def test_kill_switch_pauses_jobs_but_keeps_heartbeat(env, monkeypatch):
    clock, runs = [0.0], []
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: True)
    sup = _sup(env, clock)
    sup.register(supervisor.Job("j", 1, lambda: runs.append(1)))
    sup.tick()
    assert runs == [] and json.loads((env / "hb.json").read_text())["status"] == "paused"


def test_run_stops_on_sigterm_and_writes_stopped(env):
    clock = [0.0]
    sup = _sup(env, clock)

    def advance():
        clock[0] += 1
        if clock[0] >= 3:
            sup._on_signal(signal.SIGTERM, None)

    sup.register(supervisor.Job("tick", 1, advance))
    sup.run(install_signals=False)
    assert json.loads((env / "hb.json").read_text())["status"] == "stopped"


def test_run_refuses_live_account(env, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(guards.PaperOnlyViolation):
        _sup(env, [0.0]).run(install_signals=False, max_ticks=1)


def test_run_requires_google_key(env, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        _sup(env, [0.0]).run(install_signals=False, max_ticks=1)
