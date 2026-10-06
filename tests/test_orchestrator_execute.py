"""Orchestrator order execution: a risk-shrunk buy must never grow the locally
tracked position, and tracked qty must match what the broker would transact.

These guard the two critical live-only bugs the adversarial review found:
the dry-run/backtest paths never exercised _execute's local-apply logic.
"""
from __future__ import annotations

import numpy as np
import pytest

from regime_trader.live.orchestrator import Orchestrator
from regime_trader.risk.manager import OrderIntent, PortfolioState, RiskDecision, Position


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    """Dry-run orchestrator with no broker; state isolated to tmp_path."""
    o = Orchestrator(broker=None, dry_run=True)
    o.state.path = tmp_path / "session.json"
    o.dashboard_path = tmp_path / "dashboard.json"
    return o


def _decision(side, target, current, price=100.0, stop=90.0, symbol="AAPL"):
    intent = OrderIntent(signal_id=f"t-{symbol}", symbol=symbol, side=side,
                         target_weight=target, current_weight=current,
                         price=price, stop_price=stop if side == "buy" else None)
    return RiskDecision(intent=intent, approved=True, final_weight=target, reasons=["ok"])


def _portfolio(positions=None, equity=100_000.0):
    return PortfolioState(equity=equity, cash=equity, positions=positions or {},
                          peak_equity=equity, day_start_equity=equity,
                          week_start_equity=equity,
                          prices={s: p.entry_price for s, p in (positions or {}).items()})


def test_buy_shrunk_below_current_is_noop(orch):
    """final_weight < current_weight on a buy -> no local change, no growth."""
    pos = Position("AAPL", 50.0, 100.0, 90.0)       # current weight 0.05 @ 100k
    p = _portfolio({"AAPL": pos})
    p.prices["AAPL"] = 100.0
    before = pos.qty
    d = _decision("buy", target=0.02, current=p.weight_of("AAPL"))  # wants to shrink
    result = orch._execute(d, p)
    assert pos.qty == before, "a down-sized buy grew the position"
    assert result is None or result.get("skipped")


def test_buy_tracks_whole_shares_for_stocks(orch):
    """Local qty must equal the floored share count the broker would send."""
    p = _portfolio()
    d = _decision("buy", target=0.107, current=0.0, price=333.0)  # 0.107*100k/333 ~ 32.13
    orch._execute(d, p)
    pos = p.positions.get("AAPL")
    assert pos is not None
    assert pos.qty == float(int(pos.qty)), "tracked fractional shares for a stock"
    assert pos.qty == 32.0


def test_sub_one_share_buy_not_tracked(orch):
    p = _portfolio(equity=1000.0)
    d = _decision("buy", target=0.05, current=0.0, price=300.0)  # 0.05*1000/300 = 0.16 sh
    result = orch._execute(d, p)
    assert "AAPL" not in p.positions, "booked a sub-1-share phantom position"
    assert result is None or result.get("skipped")


def test_crypto_keeps_fractional(orch):
    p = _portfolio()
    d = _decision("buy", target=0.10, current=0.0, price=60_000.0, symbol="BTC/USD")
    orch._execute(d, p)
    pos = p.positions.get("BTC/USD")
    assert pos is not None and 0 < pos.qty < 1, "crypto should hold fractional qty"
