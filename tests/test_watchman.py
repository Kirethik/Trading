"""The Watchman: deterministic checks, de-duplicated alerts, automatic
protection (pause agent buying + sell-only sleeve trim), and plain-English
explanations with a rule-text fallback. No broker, no Gemini."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from regime_trader.agents import blackboard, guards, watch_checks as wc
from regime_trader.agents.watchman import Watchman

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
CFG = {"max_gross_exposure": 1.5, "big_loss_pct": -0.03, "sleeve_loss_pct": -0.04,
       "reconcile_tolerance": 0.02, "stale_minutes": 150, "failure_streak": 3,
       "budget_warn_pct": 0.8, "pause_hours": 4, "trim_fraction": 0.5, "realert_hours": 6,
       "max_explanations_per_hour": 6}


def snap(**kw) -> wc.Snapshot:
    base = dict(now=NOW, market_open=True, account_equity=100_000.0, broker_positions={},
                dashboard={}, session={}, omnivore={}, agent_ledger={}, breaker_locked=False,
                recent_outcomes=[], budget={"spent_usd": 0.1, "daily_usd": 2.0})
    return wc.Snapshot(**{**base, **kw})


def codes(s, cfg=CFG):
    return {f.code: f.severity for f in wc.run_checks(s, cfg)}


# ------------------------------------------------------------------ checks
def test_healthy_system_has_no_findings():
    assert codes(snap()) == {}


def test_broker_unreachable_is_a_warning():
    assert codes(snap(market_open=None, broker_positions=None, account_equity=None)) == {
        "broker_unreachable": "warn"}


def test_combined_exposure_uses_broker_totals():
    pos = {"SPY": {"qty": 300, "market_value": 160_000.0}}
    assert codes(snap(broker_positions=pos, session={"positions": {"SPY": {"qty": 300}}})) == {
        "combined_exposure": "critical"}
    pos = {"SPY": {"qty": 300, "market_value": 140_000.0}}
    assert codes(snap(broker_positions=pos, session={"positions": {"SPY": {"qty": 300}}})) == {
        "exposure_high": "warn"}


def test_books_mismatch_normalizes_crypto_symbols():
    pos = {"ETHUSD": {"qty": 2.0, "market_value": 5000.0}, "AAPL": {"qty": 10, "market_value": 2000.0}}
    ok = snap(broker_positions=pos, omnivore={"s1": {"positions": {"ETH/USD": {"qty": 2.0}}}},
              agent_ledger={"positions": {"AAPL": {"qty": 10}}})
    assert codes(ok) == {}
    bad = snap(broker_positions=pos, omnivore={"s1": {"positions": {"ETH/USD": {"qty": 1.0}}}})
    f = next(f for f in wc.run_checks(bad, CFG) if f.code == "books_mismatch")
    assert set(f.data["symbols"]) == {"ETHUSD", "AAPL"}


def test_sleeve_loss_is_critical():
    led = {"cash": 20_000.0, "day": {"date": "2026-10-06", "start_equity": 25_000.0},
           "positions": {"AAPL": {"qty": 20, "price": 200.0, "entry": 210.0}}}   # 24k: -4%
    assert codes(snap(agent_ledger=led))["sleeve_loss"] == "critical"


def test_breakers_and_daily_loss():
    d = {"risk": {"breaker_mode": "halt", "reasons": ["daily P&L -4%"]}, "portfolio": {"daily_pnl_pct": -0.041}}
    assert codes(snap(dashboard=d)) == {"breaker_halt": "warn", "big_daily_loss": "warn"}
    assert codes(snap(breaker_locked=True))["hard_lock"] == "critical"


def test_loop_silent_only_while_market_open_and_recently_alive():
    old = (NOW - timedelta(hours=3)).isoformat()
    assert "loop_silent" in codes(snap(session={"last_cycle": old}))
    assert "loop_silent" not in codes(snap(session={"last_cycle": old}, market_open=False))
    ancient = (NOW - timedelta(days=10)).isoformat()                     # not deployed: ignore
    assert "loop_silent" not in codes(snap(session={"last_cycle": ancient}))


def test_agent_health_failure_streak_and_budget():
    s = snap(recent_outcomes=["ok", "error", "timeout", "blocked", "error"],
             budget={"spent_usd": 1.7, "daily_usd": 2.0})
    assert codes(s) == {"agents_failing": "warn", "budget_high": "warn"}


# ---------------------------------------------------------------- watchman
class FakeExecutor:
    def __init__(self, status="submitted"):
        self.trims, self.status = [], status

    def trim_all(self, fraction, rationale, tag):
        self.trims.append((fraction, tag))
        return [{"symbol": "AAPL", "status": self.status}]


EXPOSED = dict(broker_positions={"SPY": {"qty": 300, "market_value": 160_000.0},
                                 "AAPL": {"qty": 20, "market_value": 4_000.0}},
               session={"positions": {"SPY": {"qty": 300}}},
               agent_ledger={"positions": {"AAPL": {"qty": 20, "price": 200.0}}})


@pytest.fixture()
def wm(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: False)
    paused = []
    monkeypatch.setattr(guards, "pause_agents", lambda hours, reason, path=None: paused.append((hours, reason)))
    alerts, explained = [], []
    w = Watchman(None, state_path=tmp_path / "wm.json", board=blackboard.Blackboard(tmp_path / "a.db"),
                 explain=lambda f, ctx: explained.append(f.code) or {
                     "headline": f"H:{f.code}", "explanation": "E", "suggestion": "S"},
                 executor=FakeExecutor(), alert=alerts.append, settings=CFG, clock=lambda: NOW)
    w.alerts, w.paused, w.explained = alerts, paused, explained
    return w


def test_critical_finding_pauses_trims_explains_and_alerts_at_once(wm):
    events = wm.tick(snap(**EXPOSED))
    assert [e["code"] for e in events] == ["combined_exposure"]
    e = events[0]
    assert e["headline"] == "H:combined_exposure" and e["status"] == "new"
    assert any("Paused" in a for a in e["actions"]) and any("Sold 50%" in a for a in e["actions"])
    assert wm.paused[0][0] == 4 and wm.executor.trims == [(0.5, "2026-10-06-combined_exposure")]
    assert wm.alerts[0].startswith("[Watchman] CRITICAL: H:combined_exposure | I did:")
    assert wm.board.latest("watch", 1)[0]["payload"]["code"] == "combined_exposure"


def test_trim_happens_once_per_day_but_pause_is_renewed_every_tick(wm):
    for _ in range(3):
        wm.tick(snap(**EXPOSED))
    assert len(wm.executor.trims) == 1 and len(wm.paused) == 3 and len(wm.alerts) == 1


def test_no_trim_when_the_agents_hold_almost_nothing(wm):
    tiny = {**EXPOSED, "agent_ledger": {"positions": {"AAPL": {"qty": 1, "price": 200.0}}},
            "broker_positions": {"SPY": {"qty": 300, "market_value": 160_000.0}, "AAPL": {"qty": 1, "market_value": 200.0}}}
    e = wm.tick(snap(**tiny))[0]
    assert wm.executor.trims == [] and wm.paused and not any("Sold" in a for a in e["actions"])


def test_market_closed_trim_is_retried_quietly_then_reported(wm):
    wm.executor.status = "deferred"
    first = wm.tick(snap(**EXPOSED))
    assert any("as soon as the market opens" in a for a in first[0]["actions"])
    assert wm.tick(snap(**EXPOSED)) == []                       # still closed: retried, not re-alerted
    assert len(wm.executor.trims) == 2
    wm.executor.status = "submitted"
    events = wm.tick(snap(**EXPOSED))
    assert events and events[0]["status"] == "ongoing" and any("Sold" in a for a in events[0]["actions"])
    wm.tick(snap(**EXPOSED))
    assert len(wm.executor.trims) == 3                          # done for today


def test_warnings_need_two_ticks_and_never_act(wm):
    down = snap(market_open=None, broker_positions=None, account_equity=None)
    assert wm.tick(down) == [] and wm.alerts == []
    assert len(wm.tick(down)) == 1 and len(wm.alerts) == 1
    assert wm.paused == [] and wm.executor.trims == []


def test_same_finding_is_not_repeated_until_it_worsens_or_goes_stale(wm):
    hi = {"SPY": {"qty": 300, "market_value": 140_000.0}}
    s = snap(broker_positions=hi, session={"positions": {"SPY": {"qty": 300}}})
    wm.tick(s)
    assert len(wm.tick(s)) == 1
    assert wm.tick(s) == []
    later = NOW + timedelta(hours=7)
    wm.clock = lambda: later
    events = wm.tick(snap(broker_positions=hi, now=later, session={"positions": {"SPY": {"qty": 300}}}))
    assert [e["status"] for e in events] == ["ongoing"]


def test_resolution_needs_two_clean_ticks_and_blips_never_page(wm):
    down = snap(market_open=None, broker_positions=None, account_equity=None)
    wm.tick(down); wm.tick(down)
    assert wm.tick(snap()) == []                                # one clean tick: not yet
    events = wm.tick(snap())
    assert [(e["status"], e["code"]) for e in events] == [("resolved", "broker_unreachable")]
    assert wm.alerts[-1].startswith("[Watchman] RESOLVED")
    n = len(wm.alerts)
    wm.tick(down); wm.tick(snap()); wm.tick(snap())             # a one-tick blip
    assert len(wm.alerts) == n


def test_explanation_failure_falls_back_to_rule_text(wm):
    wm.explain = lambda f, ctx: None
    e = wm.tick(snap(breaker_locked=True))[0]
    assert e["headline"] == "The emergency stop is ON — all buying has halted"


def test_alert_and_pause_failures_do_not_break_the_tick(wm, monkeypatch):
    def boom(*a, **k):
        raise OSError("webhook down")
    wm.alert = boom
    monkeypatch.setattr(guards, "pause_agents", boom)
    assert len(wm.tick(snap(**EXPOSED))) == 1
    assert wm.tick(snap(**EXPOSED)) == []                       # state saved: no re-alert storm


def test_corrupt_state_file_is_survivable(wm):
    wm.state_path.write_text("{not json")
    assert len(wm.tick(snap(breaker_locked=True))) == 1


def test_kill_switch_silences_the_watchman(wm, monkeypatch):
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: True)
    assert wm.tick(snap(breaker_locked=True)) == [] and wm.alerts == []


def test_stale_sleeve_baseline_is_ignored():
    led = {"cash": 20_000.0, "day": {"date": "2026-10-01", "start_equity": 25_000.0},
           "positions": {"AAPL": {"qty": 20, "price": 200.0}}}
    assert "sleeve_loss" not in codes(snap(agent_ledger=led))


def test_sleeve_loss_uses_broker_marks():
    led = {"cash": 20_000.0, "day": {"date": "2026-10-06", "start_equity": 25_000.0},
           "positions": {"AAPL": {"qty": 20, "price": 260.0}}}          # stale fill price looks fine
    pos = {"AAPL": {"qty": 20, "market_value": 4_000.0}}                  # broker mark: 200 -> -4%
    assert codes(snap(agent_ledger=led, broker_positions=pos))["sleeve_loss"] == "critical"


def test_loop_silence_counts_from_market_open():
    yesterday = (NOW - timedelta(hours=18)).isoformat()
    just_opened = NOW - timedelta(minutes=10)
    assert "loop_silent" not in codes(snap(session={"last_cycle": yesterday}, market_open_since=just_opened))
    opened_long_ago = NOW - timedelta(hours=4)
    assert "loop_silent" in codes(snap(session={"last_cycle": yesterday}, market_open_since=opened_long_ago))


# ------------------------------------------------------------- pause guard
def test_pause_expires_and_blocks_only_while_active(tmp_path, monkeypatch):
    from regime_trader.agents import guards as g
    path = tmp_path / "paused.json"
    assert g.agents_paused(path) is None
    g.pause_agents(4, "test", path)
    assert g.agents_paused(path)["reason"] == "test"
    g.pause_agents(-1, "expired", path)
    assert g.agents_paused(path) is None
