"""Kill-and-restart recovery: state round-trips, no double entry, stops
only tighten."""
from __future__ import annotations

from regime_trader.live.state import SessionState
from regime_trader.risk.manager import Position

from conftest import position


def test_position_stops_only_tighten():
    p = position("SPY", 10, 500.0, 480.0)
    assert p.tighten_stop(490.0)          # raise: allowed
    assert p.stop_price == 490.0
    assert not p.tighten_stop(470.0)      # loosen: refused
    assert p.stop_price == 490.0


def test_session_state_round_trip(tmp_path):
    s1 = SessionState(tmp_path / "session.json")
    s1.data["peak_equity"] = 123_456.0
    s1.set_positions({
        "SPY": Position("SPY", 12.5, 500.0, 488.0, "index", "regime-hmm", "sig-abc"),
        "BTC/USD": Position("BTC/USD", 0.05, 60_000.0, 55_000.0, "crypto", "omni", "sig-btc"),
    })
    s1.mark_submitted("sig-abc")
    s1.save()

    s2 = SessionState(tmp_path / "session.json")   # simulated restart
    assert s2.load()
    pos = s2.positions()
    assert pos["SPY"].qty == 12.5 and pos["SPY"].stop_price == 488.0
    assert pos["BTC/USD"].strategy == "omni"
    assert s2.data["peak_equity"] == 123_456.0


def test_no_double_entry_after_restart(tmp_path):
    s1 = SessionState(tmp_path / "session.json")
    s1.mark_submitted("sig-001")
    s1.save()
    s2 = SessionState(tmp_path / "session.json")
    s2.load()
    assert s2.already_submitted("sig-001")     # restart must not resubmit
    assert not s2.already_submitted("sig-002")


def test_watermarks_roll_on_new_day_and_week(tmp_path):
    import datetime as dt

    s = SessionState(tmp_path / "session.json")
    monday = dt.datetime(2026, 6, 8, 21, tzinfo=dt.timezone.utc)
    s.roll_watermarks(100_000.0, monday)
    assert s.data["day_start_equity"] == 100_000.0
    s.roll_watermarks(103_000.0, monday)           # same day: anchor unchanged
    assert s.data["day_start_equity"] == 100_000.0
    tuesday = monday + dt.timedelta(days=1)
    s.roll_watermarks(103_000.0, tuesday)          # new day: anchor rolls
    assert s.data["day_start_equity"] == 103_000.0
    assert s.data["week_start_equity"] == 100_000.0  # same ISO week
    next_monday = monday + dt.timedelta(days=7)
    s.roll_watermarks(99_000.0, next_monday)
    assert s.data["week_start_equity"] == 99_000.0
    assert s.data["peak_equity"] == 103_000.0      # peak never decays


def test_portfolio_state_reconstruction(tmp_path):
    s = SessionState(tmp_path / "session.json")
    s.set_positions({"QQQ": Position("QQQ", 20.0, 400.0, 390.0, "index")})
    s.data["peak_equity"] = 110_000.0
    s.data["day_start_equity"] = 105_000.0
    s.data["week_start_equity"] = 102_000.0
    p = s.portfolio_state(equity=108_000.0, cash=100_000.0, prices={"QQQ": 405.0})
    assert p.peak_equity == 110_000.0
    assert abs(p.weight_of("QQQ") - 20.0 * 405.0 / 108_000.0) < 1e-12
    assert p.daily_pnl_pct() > 0
