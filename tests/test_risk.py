"""Risk enforcement: extreme signals capped, rapid-fire blocked, no-stop
refused, breakers tiered, lock manual-release, never short."""
from __future__ import annotations

import pandas as pd

from regime_trader.risk.manager import (
    BreakerMode,
    OrderIntent,
    PortfolioState,
    RiskManager,
)

from conftest import position


def buy(symbol="AAPL", target=0.10, current=0.0, price=200.0, stop=190.0, **kw):
    return OrderIntent(signal_id=f"t-{symbol}", symbol=symbol, side="buy",
                       target_weight=target, current_weight=current,
                       price=price, stop_price=stop, **kw)


def sell(symbol="AAPL", target=0.0, current=0.10, price=200.0):
    return OrderIntent(signal_id=f"t-x-{symbol}", symbol=symbol, side="sell",
                       target_weight=target, current_weight=current,
                       price=price, stop_price=None)


class TestOrderGauntlet:
    def test_no_stop_refused(self, risk, healthy_portfolio):
        d = risk.evaluate([buy(stop=None)], healthy_portfolio)[0]
        assert not d.approved
        assert "stop" in d.reasons[0]

    def test_stop_above_entry_refused(self, risk, healthy_portfolio):
        d = risk.evaluate([buy(stop=250.0)], healthy_portfolio)[0]
        assert not d.approved

    def test_never_short(self, risk, healthy_portfolio):
        d = risk.evaluate([buy(target=-0.5)], healthy_portfolio)[0]
        assert not d.approved
        assert "always-long" in d.reasons[0]

    def test_extreme_signal_capped(self, risk, healthy_portfolio):
        """A 500% single-name signal must come out bounded by every cap."""
        d = risk.evaluate([buy(target=5.0)], healthy_portfolio)[0]
        assert d.approved
        assert d.final_weight <= risk.max_single + 1e-9
        assert d.modified and d.reasons

    def test_stop_distance_sizing(self, risk, healthy_portfolio):
        # 5% stop distance + 3% assumed gap -> cap = 1% / 8% = 12.5%
        d = risk.evaluate([buy(target=0.18, price=200.0, stop=190.0)], healthy_portfolio)[0]
        assert d.approved
        assert abs(d.final_weight - risk.risk_per_trade / (0.05 + risk.overnight_gap)) < 1e-9

    def test_rapid_fire_blocked(self, risk, healthy_portfolio):
        intents = [buy(symbol=f"S{i:02d}", target=0.01, price=100.0, stop=95.0)
                   for i in range(60)]
        decisions = risk.evaluate(intents, healthy_portfolio)
        approved = [d for d in decisions if d.approved]
        assert len(approved) <= risk.max_daily_trades
        blocked = [d for d in decisions if not d.approved]
        assert any("max daily trades" in d.reasons[0] or "max concurrent" in d.reasons[0]
                   for d in blocked)

    def test_max_positions_counts_batch(self, risk, healthy_portfolio):
        intents = [buy(symbol=f"S{i:02d}", target=0.01, price=100.0, stop=95.0)
                   for i in range(risk.max_positions + 5)]
        decisions = risk.evaluate(intents, healthy_portfolio)
        new_positions = sum(1 for d in decisions if d.approved)
        assert new_positions <= risk.max_positions

    def test_gross_exposure_collective_cap(self, risk, healthy_portfolio):
        intents = [buy(symbol=f"S{i}", target=0.5, price=100.0, stop=80.0) for i in range(8)]
        decisions = risk.evaluate(intents, healthy_portfolio)
        total = sum(d.final_weight for d in decisions if d.approved)
        assert total <= min(risk.max_gross, risk.max_leverage) + 1e-6

    def test_uncertainty_forces_neutral_leverage(self, risk, healthy_portfolio):
        intents = [buy(symbol=f"S{i}", target=0.4, price=100.0, stop=80.0) for i in range(8)]
        decisions = risk.evaluate(intents, healthy_portfolio, uncertainty=True)
        total = sum(d.final_weight for d in decisions if d.approved)
        assert total <= risk.uncertainty_max_lev + 1e-6

    def test_sector_cap(self, risk, healthy_portfolio):
        p = healthy_portfolio
        p.positions = {
            "AAPL": position("AAPL", 220, 180.0, 170.0),   # ~44% of equity at $200
        }
        p.prices["AAPL"] = 200.0
        d = risk.evaluate([buy(symbol="MSFT", target=0.10, price=420.0, stop=400.0)], p)[0]
        # tech sector (AAPL ~44%) + MSFT must stay under the 45% cap
        assert (not d.approved) or d.final_weight <= risk.max_sector - 0.43

    def test_every_decision_carries_reasons(self, risk, healthy_portfolio):
        decisions = risk.evaluate([buy(), sell()], healthy_portfolio)
        assert all(d.reasons for d in decisions)

    def test_approved_buy_never_below_current_weight(self, risk, healthy_portfolio):
        """An approved BUY must never be sized below what we already hold — a
        reduction mislabeled as a buy corrupts live position tracking."""
        p = healthy_portfolio
        # large untouched bond position eats most of the gross budget
        p.positions = {"TLT": position("TLT", 280, 100.0, 90.0, sector="bonds")}
        p.prices["TLT"] = 100.0   # ~28% * many -> push near cap via several buys
        intents = [buy(symbol=f"S{i}", target=0.5, current=0.10, price=100.0, stop=80.0)
                   for i in range(6)]
        # give each a real current position so a downscale could go below current
        p.positions.update({f"S{i}": position(f"S{i}", 50, 100.0, 80.0) for i in range(6)})
        for i in range(6):
            p.prices[f"S{i}"] = 100.0
        decisions = risk.evaluate(intents, p, uncertainty=True)  # tightest cap
        for d in decisions:
            if d.approved and d.intent.side == "buy":
                assert d.final_weight >= d.intent.current_weight - 1e-9, (
                    f"approved buy {d.intent.symbol} shrank below current weight"
                )


class TestCircuitBreakers:
    def test_daily_reduce_tier(self, risk, healthy_portfolio):
        p = healthy_portfolio
        p.equity = 97_500.0  # -2.5% on the day
        status = risk.breaker_status(p)
        assert status.mode == BreakerMode.REDUCE

    def test_daily_halt_tier_blocks_buys_allows_sells(self, risk, healthy_portfolio):
        p = healthy_portfolio
        p.equity = 95_000.0  # -5% on the day
        assert risk.breaker_status(p).mode == BreakerMode.HALT
        decisions = risk.evaluate([buy(), sell()], p)
        by_side = {d.intent.side: d for d in decisions}
        assert not by_side["buy"].approved
        assert by_side["sell"].approved

    def test_weekly_tiers(self, risk, healthy_portfolio):
        p = healthy_portfolio
        p.day_start_equity = p.equity = 94_000.0
        p.week_start_equity = 100_000.0
        assert risk.breaker_status(p).mode == BreakerMode.REDUCE
        p.day_start_equity = p.equity = 91_000.0
        assert risk.breaker_status(p).mode == BreakerMode.HALT

    def test_peak_lock_engages_and_requires_manual_release(self, risk, healthy_portfolio):
        p = healthy_portfolio
        p.peak_equity = 130_000.0
        p.equity = p.day_start_equity = p.week_start_equity = 100_000.0  # -23% from peak
        status = risk.breaker_status(p)
        assert status.mode == BreakerMode.LOCKED
        assert risk.lock_file.exists()
        # equity recovery does NOT release the lock
        p.equity = p.day_start_equity = p.week_start_equity = 129_000.0
        assert risk.breaker_status(p).mode == BreakerMode.LOCKED
        assert risk.manual_release()
        assert risk.breaker_status(p).mode == BreakerMode.NORMAL

    def test_breakers_need_no_model(self, risk):
        """The breaker fires on pure P&L — no regime, no features, no model."""
        p = PortfolioState(equity=90_000.0, cash=90_000.0, positions={},
                           peak_equity=100_000.0, day_start_equity=100_000.0,
                           week_start_equity=100_000.0)
        assert risk.breaker_status(p).mode == BreakerMode.HALT


class TestCorrelation:
    def test_high_correlation_rejected_or_scaled(self, risk, healthy_portfolio):
        idx = pd.bdate_range("2023-01-02", periods=80, tz="UTC")
        base = pd.Series(range(80), index=idx, dtype=float).cumsum() + 100
        closes = pd.DataFrame({
            "AAPL": base * 1.0,
            "TWIN": base * 2.0,        # perfectly correlated with AAPL
        })
        p = healthy_portfolio
        p.positions = {"AAPL": position("AAPL", 100, 180.0, 170.0)}
        p.prices.update({"AAPL": 200.0, "TWIN": 400.0})
        d = risk.evaluate([buy(symbol="TWIN", target=0.10, price=400.0, stop=380.0)],
                          p, closes=closes)[0]
        assert not d.approved
        assert "correlation" in d.reasons[0]
