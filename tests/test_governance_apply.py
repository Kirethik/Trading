"""Governance.apply(): the bridge from decisions to the omnivore paper ledger.

A funding change must move cash by the allocation delta (not just relabel
`allocated`), and a strategy that stops passing must be disabled so it can't
keep trading on stale capital. These are the two invariants `_fund()` and the
disable-on-reject branch encode.
"""
from __future__ import annotations

import copy

import pytest

from regime_trader.govern.governance import (
    DEPLOY, HOLD, REDUCE_RISK, REJECT, Governance, GovernanceDecision,
)
from regime_trader.govern.registry import DEPLOYED, REJECTED


class FakeDeployer:
    """Just enough of OmnivoreDeployer for apply(): a ledger plus an empty
    cohort, so _close_strategy's `om is None` early-return is taken."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.cohort = []
        self.dry_run = True
        self.saved = False

    def save(self):
        self.saved = True


def _ledger_entry(allocated=2000.0, cash=2000.0, enabled=True, positions=None):
    return {
        "allocated": allocated, "cash": cash, "enabled": enabled,
        "positions": positions or {}, "realized_pnl": 0.0, "trades": 0,
        "equity_history": [],
        "watermarks": {"day_key": "2026-06-12", "day_start": allocated,
                        "week_key": "2026-W24", "week_start": allocated, "peak": allocated},
    }


def _gov():
    return Governance(registry={"test-strat": {}, "regime-hmm": {}})


def test_fund_deposit_moves_cash_and_rebases_watermarks():
    led = _ledger_entry(allocated=2000.0, cash=2000.0)
    Governance._fund(led, 8000.0)
    assert led["allocated"] == 8000.0
    assert led["cash"] == 8000.0
    assert led["watermarks"]["day_start"] == 8000.0
    assert led["watermarks"]["week_start"] == 8000.0
    assert led["watermarks"]["peak"] == 8000.0


def test_fund_withdrawal_clamped_to_available_cash():
    led = _ledger_entry(allocated=8000.0, cash=500.0)  # mostly invested
    Governance._fund(led, 0.0)
    # only $500 of cash can be withdrawn even though allocation drops to 0
    assert led["cash"] == 0.0
    assert led["allocated"] == 7500.0
    assert led["watermarks"]["day_start"] == 7500.0
    assert led["watermarks"]["peak"] == 7500.0


def test_apply_deploy_funds_strategy_and_moves_cash():
    gov = _gov()
    key = "test-strat"
    ledger = {key: _ledger_entry(allocated=2000.0, cash=2000.0)}
    deployer = FakeDeployer(ledger)
    decisions = {key: GovernanceDecision(key=key, source="omnivore", decision=DEPLOY, allocation=8000.0)}

    applied = gov.apply(decisions, deployer)

    led = ledger[key]
    assert applied["funded"] == 1
    assert applied["capital"] == 8000.0
    assert led["allocated"] == 8000.0
    assert led["cash"] == 8000.0
    assert led["enabled"] is True
    assert gov.registry.get(key)["status"] == DEPLOYED
    assert deployer.saved is True


def test_apply_reject_disables_previously_funded_strategy():
    gov = _gov()
    key = "test-strat"
    ledger = {key: _ledger_entry(allocated=8000.0, cash=8000.0, enabled=True)}
    deployer = FakeDeployer(ledger)
    decisions = {key: GovernanceDecision(key=key, source="omnivore", decision=REJECT, allocation=0.0,
                                          reasons=["sharpe below threshold"])}

    applied = gov.apply(decisions, deployer)

    led = ledger[key]
    assert applied["disabled"] == 1
    assert led["enabled"] is False
    assert gov.registry.get(key)["status"] == REJECTED


def test_apply_hold_preserves_ledger_state():
    gov = _gov()
    key = "test-strat"
    led_before = _ledger_entry(allocated=8000.0, cash=3000.0,
                                positions={"AAPL": {"qty": 1, "entry": 100.0, "stop": 90.0}})
    led_before["watermarks"]["peak"] = 8200.0
    ledger = {key: copy.deepcopy(led_before)}
    deployer = FakeDeployer(ledger)
    gov.registry.get(key)["status"] = DEPLOYED
    decisions = {key: GovernanceDecision(key=key, source="omnivore", decision=HOLD, allocation=0.0,
                                          rationale="Opus: monitor", escalated=True)}

    applied = gov.apply(decisions, deployer)

    assert ledger[key] == led_before
    assert applied == {"funded": 0, "retired": 0, "reduced": 0, "disabled": 0,
                        "recommend_only": 0, "capital": 0.0}
    assert gov.registry.get(key)["status"] == DEPLOYED


def test_apply_reduce_risk_shrinks_allocation_and_cash():
    gov = _gov()
    key = "test-strat"
    ledger = {key: _ledger_entry(allocated=8000.0, cash=8000.0)}
    deployer = FakeDeployer(ledger)
    decisions = {key: GovernanceDecision(key=key, source="omnivore", decision=REDUCE_RISK, allocation=0.0)}

    applied = gov.apply(decisions, deployer)

    led = ledger[key]
    expected = 8000.0 * gov.cfg.allocation.reduce_factor
    assert applied["reduced"] == 1
    assert led["allocated"] == pytest.approx(expected)
    assert led["cash"] == pytest.approx(expected)


def test_apply_non_omnivore_source_is_recommend_only():
    gov = _gov()
    key = "regime-hmm"
    deployer = FakeDeployer(ledger={})
    decisions = {key: GovernanceDecision(key=key, source="challenger", decision=DEPLOY, allocation=5000.0)}

    applied = gov.apply(decisions, deployer)

    assert applied["recommend_only"] == 1
    assert applied["funded"] == 0
    assert gov.registry.get(key)["status"] == DEPLOYED
    assert gov.registry.get(key)["allocation"] == 5000.0


def test_apply_with_no_deployer_is_research_only():
    """--no-trade: deployer=None means nothing is funded, only recorded."""
    gov = _gov()
    key = "test-strat"
    decisions = {key: GovernanceDecision(key=key, source="omnivore", decision=DEPLOY, allocation=8000.0)}

    applied = gov.apply(decisions, None)

    assert applied["recommend_only"] == 1
    assert applied["funded"] == 0
    assert gov.registry.get(key)["status"] == DEPLOYED
