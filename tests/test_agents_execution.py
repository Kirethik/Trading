"""Agent paper-order tool: autonomous, but always through the RiskManager veto.

Fake broker + REAL RiskManager (tmp lock file) + synthetic bars. No network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from regime_trader.agents import execution, guards
from regime_trader.risk.manager import RiskManager

from conftest import make_synthetic_bars


class FakeBroker:
    paper = True

    def __init__(self, equity=100_000.0, market_open=True):
        self.equity, self.open, self.sent, self.held = equity, market_open, [], {}

    def positions(self):
        return {s: {"qty": q} for s, q in self.held.items() if q > 0}

    def is_market_open(self):
        return self.open

    def account_equity(self):
        return self.equity, self.equity

    def submit_entry(self, symbol, qty, limit_price, stop_loss, take_profit=None, signal_id=""):
        self.sent.append(("buy", symbol, qty, stop_loss, signal_id))
        self.held[symbol] = self.held.get(symbol, 0.0) + qty
        return SimpleNamespace(qty=qty)

    def submit_exit(self, symbol, qty, limit_price=None, signal_id=""):
        self.sent.append(("sell", symbol, qty, None, signal_id))
        self.held[symbol] = self.held.get(symbol, 0.0) - qty
        return SimpleNamespace(qty=qty)


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: False)
    bars = make_synthetic_bars(n=300)
    broker = FakeBroker()
    events: list = []
    ex = execution.PaperExecutor(
        agent="tester", broker=broker, risk=RiskManager(lock_file=tmp_path / "breaker.lock"),
        get_bars=lambda s: bars, ledger_path=tmp_path / "ledger.json",
        max_sleeve_pct=0.25, max_orders_per_day=3, on_event=events.append,
    )
    return SimpleNamespace(ex=ex, broker=broker, events=events, tmp=tmp_path)


def test_buy_passes_risk_and_reaches_broker_with_stop(setup):
    r = setup.ex.place("AAPL", "buy", 0.10, "trend", signal_id="s1")
    assert r["status"] == "submitted", r
    side, sym, qty, stop, sid = setup.broker.sent[0]
    assert (side, sym, sid) == ("buy", "AAPL", "s1")
    assert qty > 0 and stop is not None
    # sleeve = 25% of 100k -> 25k; risk caps can only shrink the 10% target
    assert qty * float(make_synthetic_bars(n=300)["close"].iloc[-1]) <= 25_000 * 0.10 + 1
    assert setup.ex.ledger()["positions"]["AAPL"]["qty"] == qty
    assert setup.events and setup.events[-1]["status"] == "submitted"


def test_sell_reduces_ledger_position(setup):
    setup.ex.place("AAPL", "buy", 0.10, "in", signal_id="b")
    held = setup.ex.ledger()["positions"]["AAPL"]["qty"]
    r = setup.ex.place("AAPL", "sell", 0.0, "out", signal_id="s")
    assert r["status"] == "submitted"
    assert setup.broker.sent[-1][0] == "sell" and setup.broker.sent[-1][2] == held
    assert "AAPL" not in setup.ex.ledger()["positions"]


def test_duplicate_signal_id_not_resubmitted(setup):
    setup.ex.place("AAPL", "buy", 0.10, "x", signal_id="dup")
    r = setup.ex.place("AAPL", "buy", 0.12, "x", signal_id="dup")
    assert r["status"] == "duplicate" and len(setup.broker.sent) == 1


def test_daily_order_cap(setup):
    for i, sym in enumerate(["AAPL", "MSFT", "SPY"]):
        setup.ex.place(sym, "buy", 0.05, "x", signal_id=f"c{i}")
    r = setup.ex.place("QQQ", "buy", 0.05, "x", signal_id="c4")
    assert r["status"] == "blocked" and "orders" in r["reason"]


def test_hard_lock_vetoes_buys_but_allows_sells(setup):
    setup.ex.place("AAPL", "buy", 0.10, "in", signal_id="b")
    (setup.tmp / "breaker.lock").write_text("locked")
    r = setup.ex.place("MSFT", "buy", 0.10, "x", signal_id="b2")
    assert r["status"] == "vetoed"
    assert setup.ex.place("AAPL", "sell", 0.0, "derisk", signal_id="s")["status"] == "submitted"


def test_buy_without_history_has_no_stop_and_is_vetoed(setup):
    setup.ex.get_bars = lambda s: make_synthetic_bars(n=5)
    assert setup.ex.place("AAPL", "buy", 0.10, "x", signal_id="n")["status"] == "vetoed"
    assert setup.broker.sent == []


@pytest.mark.parametrize("sym,side,w", [("DOGE", "buy", 0.1), ("AAPL", "short", 0.1),
                                        ("AAPL", "buy", 1.5), ("AAPL", "buy", -0.1)])
def test_invalid_inputs_rejected(setup, sym, side, w):
    assert setup.ex.place(sym, side, w, "x", signal_id="v")["status"] == "invalid"
    assert setup.broker.sent == []


def test_market_closed_defers(setup):
    setup.broker.open = False
    assert setup.ex.place("AAPL", "buy", 0.1, "x", signal_id="m")["status"] == "deferred"


def test_live_account_raises_before_broker(setup, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(guards.PaperOnlyViolation):
        setup.ex.place("AAPL", "buy", 0.1, "x", signal_id="l")
    assert setup.broker.sent == []


def test_non_paper_broker_raises(setup):
    setup.broker.paper = False
    with pytest.raises(guards.PaperOnlyViolation):
        setup.ex.place("AAPL", "buy", 0.1, "x", signal_id="l")


def test_tool_wrapper_is_registered_execute_tier_and_derives_signal_id(setup):
    tool = execution.make_order_tool(setup.ex)
    assert tool.__name__ == "place_paper_order"
    assert guards.tool_tier("place_paper_order") == guards.ToolTier.EXECUTE_PAPER
    ctx = SimpleNamespace(invocation_id="inv-1")
    first = tool("AAPL", "buy", 0.1, "x", ctx)
    again = tool("AAPL", "buy", 0.1, "x", ctx)
    assert first["status"] == "submitted" and again["status"] == "duplicate"
    # a PROPOSE-tier agent can never call it
    guard = guards.make_tool_guard(guards.ToolTier.PROPOSE, {"place_paper_order"})
    assert guard(SimpleNamespace(name="place_paper_order"), {}, ctx)["status"] == "blocked"


def test_sleeve_losses_trip_the_breakers(setup):
    """Marked-to-market sleeve: a crash in an agent holding halts agent buys."""
    bars = make_synthetic_bars(n=300)
    setup.ex.get_bars = lambda s: bars
    assert setup.ex.place("AAPL", "buy", 0.20, "in", signal_id="b1")["status"] == "submitted"
    crashed = bars.copy()
    crashed.loc[crashed.index[-1], ["open", "high", "low", "close"]] *= 0.3  # AAPL -70% today (position ~6.7% of sleeve -> ~-4.7% sleeve P&L)
    setup.ex.get_bars = lambda s: crashed if s == "AAPL" else bars
    r = setup.ex.place("MSFT", "buy", 0.10, "x", signal_id="b2")
    assert r["status"] == "vetoed" and "P&L" in r["reason"]


def test_cash_is_tracked_and_week_baseline_is_separate(setup):
    setup.ex.place("AAPL", "buy", 0.10, "in", signal_id="b")
    led = setup.ex.ledger()
    pos = led["positions"]["AAPL"]
    assert led["cash"] == pytest.approx(25_000 - pos["qty"] * pos["entry"])
    assert led["week"]["start_equity"] == pytest.approx(25_000)


def test_reconcile_drops_stale_positions_broker_no_longer_holds(setup):
    import json
    setup.ex.place("AAPL", "buy", 0.10, "in", signal_id="b")
    led = json.loads(setup.ex.ledger_path.read_text())
    led["positions"]["AAPL"]["opened"] = "2000-01-01"   # entered on an earlier day
    setup.ex.ledger_path.write_text(json.dumps(led))
    setup.broker.held = {}                               # e.g. broker-side stop fired
    r = setup.ex.place("AAPL", "sell", 0.0, "out", signal_id="s")
    assert r["status"] in ("noop", "vetoed") and setup.broker.sent[-1][0] == "buy"
    assert "AAPL" not in setup.ex.ledger()["positions"]


def test_kill_switch_blocks_order_tool(setup, monkeypatch):
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: True)
    assert setup.ex.place("AAPL", "buy", 0.1, "x", signal_id="k")["status"] == "blocked"
    assert setup.broker.sent == []


def test_broker_without_paper_flag_is_refused(setup):
    del setup.broker.__class__.paper
    try:
        with pytest.raises(guards.PaperOnlyViolation):
            setup.ex.place("AAPL", "buy", 0.1, "x", signal_id="p")
    finally:
        FakeBroker.paper = True


def test_signal_ids_are_short_and_weight_specific():
    a = execution.signal_id_for("agent", "e-" + "f" * 36, "AAPL", "buy", 0.1)
    assert len(a) <= 48
    assert a == execution.signal_id_for("agent", "e-" + "f" * 36, "AAPL", "buy", 0.1)
    assert a != execution.signal_id_for("agent", "e-" + "f" * 36, "AAPL", "buy", 0.05)


def test_alert_text_is_sanitized():
    assert execution._safe("buy @everyone see https://evil.x <b>now</b>") == "buy everyone see  bnow/b"
