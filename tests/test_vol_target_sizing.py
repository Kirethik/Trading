"""Verify that vol_target sizing mode bypasses the fixed-fractional risk_cap
while all other RiskManager caps (max_single, gross, sector) still apply."""
from __future__ import annotations

import pytest
from regime_trader.risk.manager import OrderIntent, PortfolioState, RiskManager


def _portfolio(equity: float = 100_000.0) -> PortfolioState:
    p = PortfolioState(equity=equity, cash=equity)
    p.peak_equity = equity
    p.day_start_equity = equity
    p.week_start_equity = equity
    return p


def _intent(symbol="ETH/USD", target=0.40, sizing_mode="risk", price=3000.0, stop=2700.0):
    return OrderIntent(
        signal_id="t1", symbol=symbol, side="buy",
        target_weight=target, current_weight=0.0,
        price=price, stop_price=stop,
        sizing_mode=sizing_mode,
    )


def test_risk_mode_is_capped_by_stop_distance():
    rm = RiskManager()
    p = _portfolio()
    # stop_dist = (3000-2700)/3000 = 10%, risk_cap = 1%/(10%+3%) ≈ 7.7%
    # target 40% should be shrunk to ~7.7%
    d = rm.evaluate([_intent(sizing_mode="risk")], p)[0]
    assert d.approved
    assert d.final_weight < 0.12, f"expected risk_cap shrinkage, got {d.final_weight:.2%}"
    assert any("risk cap" in r for r in d.reasons)


def test_vol_target_mode_not_shrunk_by_risk_cap():
    rm = RiskManager()
    p = _portfolio()
    # Same stop distance — vol_target mode must NOT apply the fixed-fractional cap
    d = rm.evaluate([_intent(sizing_mode="vol_target")], p)[0]
    assert d.approved
    # Should be bounded only by max_single (20% for stocks; this is crypto so 1.0 cap
    # but max_single in config is 0.20 for the shared manager). Either way it must be
    # substantially higher than the 7.7% risk_cap that "risk" mode would produce.
    assert d.final_weight > 0.15, f"vol_target should deploy more, got {d.final_weight:.2%}"
    assert not any("risk cap" in r for r in d.reasons)


def test_vol_target_still_capped_by_max_single():
    rm = RiskManager()
    p = _portfolio()
    # Even with vol_target, max_single_position (0.20 from config) applies
    d = rm.evaluate([_intent(target=0.90, sizing_mode="vol_target")], p)[0]
    assert d.approved
    assert d.final_weight <= rm.max_single + 1e-9


def test_protective_stop_uses_custom_mult():
    rm = RiskManager()
    entry, atr = 100.0, 5.0
    default_stop = rm.protective_stop(entry, atr)
    wide_stop = rm.protective_stop(entry, atr, mult=6.0)
    assert wide_stop < default_stop, "wider mult should produce a lower (further) stop"
    assert abs(wide_stop - (entry - 6.0 * atr)) < 1e-9
