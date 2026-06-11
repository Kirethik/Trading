"""Independent risk layer with absolute veto power.

Operates on ACTUAL portfolio P&L — no model input is trusted or required, so
it keeps working even if regime detection fails completely. Every signal in
the system (incumbent, challengers, ensemble, omnivore cohort, backtests)
passes through `evaluate`. The same code runs in backtest and live.

risk management > signal generation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import utc_now_iso

log = get_logger("trades")


# --------------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    sector: str = "unknown"
    strategy: str = "regime-hmm"
    signal_id: str = ""

    def tighten_stop(self, new_stop: float) -> bool:
        """Stops only ever tighten (rise, for a long book). Returns True if moved."""
        if new_stop > self.stop_price:
            self.stop_price = new_stop
            return True
        return False

    def market_value(self, price: float) -> float:
        return self.qty * price


@dataclass
class PortfolioState:
    equity: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    trades_today: int = 0
    prices: dict[str, float] = field(default_factory=dict)

    def weight_of(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        if pos is None or self.equity <= 0:
            return 0.0
        return pos.market_value(self.prices.get(symbol, pos.entry_price)) / self.equity

    def gross_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(self.weight_of(s) for s in self.positions)

    def daily_pnl_pct(self) -> float:
        return self.equity / self.day_start_equity - 1.0 if self.day_start_equity > 0 else 0.0

    def weekly_pnl_pct(self) -> float:
        return self.equity / self.week_start_equity - 1.0 if self.week_start_equity > 0 else 0.0

    def peak_drawdown_pct(self) -> float:
        return self.equity / self.peak_equity - 1.0 if self.peak_equity > 0 else 0.0


class BreakerMode(str, Enum):
    NORMAL = "normal"
    REDUCE = "reduce"        # new exposure scaled down
    HALT = "halt"            # no new exposure; exits and stops still work
    LOCKED = "locked"        # hard peak-drawdown lock — manual release required


@dataclass
class BreakerStatus:
    mode: BreakerMode
    reasons: list[str] = field(default_factory=list)

    @property
    def allows_buys(self) -> bool:
        return self.mode in (BreakerMode.NORMAL, BreakerMode.REDUCE)

    @property
    def buy_scale(self) -> float:
        return 0.5 if self.mode == BreakerMode.REDUCE else 1.0


@dataclass
class OrderIntent:
    signal_id: str
    symbol: str
    side: str                  # "buy" | "sell"
    target_weight: float       # desired final weight of equity (>= 0; never short)
    current_weight: float
    price: float               # reference price for sizing
    stop_price: float | None   # REQUIRED for buys — refused without one
    strategy: str = "regime-hmm"
    sector: str = "unknown"


@dataclass
class RiskDecision:
    intent: OrderIntent
    approved: bool
    final_weight: float        # possibly modified target
    reasons: list[str] = field(default_factory=list)

    @property
    def modified(self) -> bool:
        return self.approved and abs(self.final_weight - self.intent.target_weight) > 1e-9

    def log_record(self) -> dict:
        return {
            "ts": utc_now_iso(),
            "signal_id": self.intent.signal_id,
            "symbol": self.intent.symbol,
            "side": self.intent.side,
            "strategy": self.intent.strategy,
            "requested": round(self.intent.target_weight, 5),
            "final": round(self.final_weight, 5),
            "approved": self.approved,
            "reasons": self.reasons,
        }


# --------------------------------------------------------------------------
class RiskManager:
    def __init__(self, lock_file: str | Path | None = None):
        cfg = get_config()
        r = cfg.risk
        self.max_gross = r.max_gross_exposure
        self.max_single = r.max_single_position
        self.max_positions = r.max_positions
        self.max_daily_trades = r.max_daily_trades
        self.max_leverage = r.max_leverage
        self.max_sector = r.max_sector_exposure
        self.corr_lookback = r.correlation_lookback
        self.corr_limit = r.correlation_limit
        self.corr_scale_start = r.correlation_scale_start
        self.risk_per_trade = r.risk_per_trade
        self.stop_atr_mult = r.stop_atr_mult
        self.overnight_gap = r.overnight_gap_risk
        self.breakers = r.breakers.as_dict()
        self.uncertainty_max_lev = cfg.allocation.uncertainty_max_leverage
        self.lock_file = Path(lock_file) if lock_file else cfg.resolve_path(r.lock_file)
        self.sectors = cfg.universe.sectors.as_dict()

    # ------------------------------------------------------ circuit breakers
    def breaker_status(self, p: PortfolioState) -> BreakerStatus:
        """Tiered breakers on actual P&L: reduce -> halt -> hard lock."""
        if self.lock_file.exists():
            return BreakerStatus(BreakerMode.LOCKED, ["manual-release lock file present"])

        peak_dd = p.peak_drawdown_pct()
        if peak_dd <= self.breakers["peak_lock"]:
            self._engage_lock(peak_dd)
            return BreakerStatus(
                BreakerMode.LOCKED,
                [f"peak drawdown {peak_dd:.1%} breached hard lock {self.breakers['peak_lock']:.0%}"],
            )

        reasons, mode = [], BreakerMode.NORMAL
        daily, weekly = p.daily_pnl_pct(), p.weekly_pnl_pct()
        if daily <= self.breakers["daily_halt"]:
            mode = BreakerMode.HALT
            reasons.append(f"daily P&L {daily:.1%} <= halt tier {self.breakers['daily_halt']:.0%}")
        elif weekly <= self.breakers["weekly_halt"]:
            mode = BreakerMode.HALT
            reasons.append(f"weekly P&L {weekly:.1%} <= halt tier {self.breakers['weekly_halt']:.0%}")
        elif daily <= self.breakers["daily_reduce"]:
            mode = BreakerMode.REDUCE
            reasons.append(f"daily P&L {daily:.1%} <= reduce tier {self.breakers['daily_reduce']:.0%}")
        elif weekly <= self.breakers["weekly_reduce"]:
            mode = BreakerMode.REDUCE
            reasons.append(f"weekly P&L {weekly:.1%} <= reduce tier {self.breakers['weekly_reduce']:.0%}")
        return BreakerStatus(mode, reasons)

    def _engage_lock(self, peak_dd: float) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.lock_file.exists():
            self.lock_file.write_text(
                f"HARD PEAK-DRAWDOWN LOCK engaged {utc_now_iso()} at drawdown {peak_dd:.2%}.\n"
                "Trading halted. Delete this file (or run `python -m regime_trader release-lock`) "
                "to resume after manual review.\n",
                encoding="utf-8",
            )
            log.critical("HARD LOCK ENGAGED at peak drawdown %.2f%%", peak_dd * 100)

    def manual_release(self) -> bool:
        if self.lock_file.exists():
            os.remove(self.lock_file)
            log.warning("hard lock manually released")
            return True
        return False

    # ----------------------------------------------------------- evaluation
    def evaluate(
        self,
        intents: list[OrderIntent],
        portfolio: PortfolioState,
        closes: pd.DataFrame | None = None,
        uncertainty: bool = False,
    ) -> list[RiskDecision]:
        """The veto gate. Sells (risk reductions) pass even under HALT/LOCK;
        buys run the full gauntlet. Every modification carries its reasons."""
        status = self.breaker_status(portfolio)
        decisions: list[RiskDecision] = []
        trades_used = portfolio.trades_today

        sells = [i for i in intents if i.side == "sell"]
        buys = [i for i in intents if i.side == "buy"]

        for intent in sells:  # reducing risk is always allowed
            decisions.append(self._approve(intent, intent.target_weight, ["risk reduction"]))
            trades_used += 1

        approved_buys: list[RiskDecision] = []
        for intent in buys:
            d = self._evaluate_buy(intent, portfolio, closes, status, trades_used)
            if d.approved:
                trades_used += 1
                approved_buys.append(d)
            decisions.append(d)

        self._apply_portfolio_caps(approved_buys, portfolio, status, uncertainty)

        for d in decisions:
            log.info("risk decision", extra={"ctx": d.log_record()})
        return decisions

    def _approve(self, intent: OrderIntent, weight: float, reasons: list[str]) -> RiskDecision:
        return RiskDecision(intent=intent, approved=True, final_weight=weight, reasons=reasons)

    def _reject(self, intent: OrderIntent, reason: str) -> RiskDecision:
        return RiskDecision(intent=intent, approved=False, final_weight=intent.current_weight,
                            reasons=[reason])

    def _evaluate_buy(
        self,
        intent: OrderIntent,
        p: PortfolioState,
        closes: pd.DataFrame | None,
        status: BreakerStatus,
        trades_used: int,
    ) -> RiskDecision:
        if not status.allows_buys:
            return self._reject(intent, f"circuit breaker {status.mode.value}: {'; '.join(status.reasons)}")
        if intent.target_weight < 0:
            return self._reject(intent, "short exposure forbidden: system is always-long")
        if intent.stop_price is None or intent.stop_price <= 0:
            return self._reject(intent, "order refused: every position must carry a stop")
        if intent.stop_price >= intent.price:
            return self._reject(intent, "stop must be below entry for a long position")
        if trades_used >= self.max_daily_trades:
            return self._reject(intent, f"max daily trades ({self.max_daily_trades}) reached")
        if intent.symbol not in p.positions and len(p.positions) >= self.max_positions:
            return self._reject(intent, f"max concurrent positions ({self.max_positions}) reached")

        weight = intent.target_weight
        reasons: list[str] = []

        # stop-distance sizing: cap so a stop-out (plus an adverse overnight
        # gap) cannot lose more than risk_per_trade of equity
        stop_dist = (intent.price - intent.stop_price) / intent.price
        risk_cap = self.risk_per_trade / max(stop_dist + self.overnight_gap, 1e-6)
        if weight > risk_cap:
            weight = risk_cap
            reasons.append(f"sized down by stop distance: risk cap {risk_cap:.1%}")

        if weight > self.max_single:
            weight = self.max_single
            reasons.append(f"capped at max single position {self.max_single:.0%}")

        # sector concentration
        sector = self.sectors.get(intent.symbol, intent.sector)
        sector_w = sum(
            p.weight_of(s) for s, pos in p.positions.items()
            if self.sectors.get(s, pos.sector) == sector and s != intent.symbol
        )
        room = self.max_sector - sector_w
        if room <= 0.005:
            return self._reject(intent, f"sector '{sector}' exposure {sector_w:.0%} at cap {self.max_sector:.0%}")
        if weight > room:
            weight = room
            reasons.append(f"scaled into remaining '{sector}' sector room {room:.1%}")

        # correlation vs the existing book
        corr = self._portfolio_correlation(intent.symbol, p, closes)
        if corr is not None:
            if corr >= self.corr_limit:
                return self._reject(intent, f"correlation {corr:.2f} vs book >= limit {self.corr_limit}")
            if corr > self.corr_scale_start:
                frac = 1.0 - (corr - self.corr_scale_start) / (self.corr_limit - self.corr_scale_start)
                weight *= frac
                reasons.append(f"scaled {frac:.0%} for correlation {corr:.2f} vs book")

        if status.mode == BreakerMode.REDUCE:
            increase = max(0.0, weight - intent.current_weight) * status.buy_scale
            weight = intent.current_weight + increase
            reasons.append("breaker REDUCE: new exposure halved")

        if not reasons:
            reasons = ["approved unmodified"]
        return self._approve(intent, weight, reasons)

    def _portfolio_correlation(
        self, symbol: str, p: PortfolioState, closes: pd.DataFrame | None
    ) -> float | None:
        if closes is None or symbol not in closes.columns or not p.positions:
            return None
        rets = closes.pct_change().tail(self.corr_lookback)
        if symbol not in rets or rets[symbol].dropna().shape[0] < self.corr_lookback // 2:
            return None
        held = [s for s in p.positions if s in rets.columns and s != symbol]
        if not held:
            return None
        weights = np.array([max(p.weight_of(s), 1e-9) for s in held])
        weights = weights / weights.sum()
        book_ret = (rets[held] * weights).sum(axis=1)
        corr = rets[symbol].corr(book_ret)
        return float(corr) if np.isfinite(corr) else None

    def _apply_portfolio_caps(
        self,
        approved_buys: list[RiskDecision],
        p: PortfolioState,
        status: BreakerStatus,
        uncertainty: bool,
    ) -> None:
        """Collective caps: gross exposure / leverage across the final book,
        with leverage forced to neutral under uncertainty or any breaker."""
        if not approved_buys:
            return
        cap = min(self.max_gross, self.max_leverage)
        if uncertainty or status.mode != BreakerMode.NORMAL:
            cap = min(cap, self.uncertainty_max_lev)

        untouched = sum(
            p.weight_of(s)
            for s in p.positions
            if s not in {d.intent.symbol for d in approved_buys}
        )
        buys_total = sum(d.final_weight for d in approved_buys)
        if untouched + buys_total <= cap:
            return
        room = max(0.0, cap - untouched)
        scale = room / buys_total if buys_total > 0 else 0.0
        for d in approved_buys:
            d.final_weight *= scale
            d.reasons.append(f"portfolio cap {cap:.2f}x: buys scaled to {scale:.0%}")

    # -------------------------------------------------------------- helpers
    def protective_stop(self, entry_price: float, atr_value: float) -> float:
        return max(0.01, entry_price - self.stop_atr_mult * atr_value)
