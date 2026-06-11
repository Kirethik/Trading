"""Allocation-based walk-forward backtest engine.

Realism rules (all enforced here, none optional):
- Signals computed at bar t's close fill at bar t+1's OPEN (one-bar delay).
- Slippage charged on every fill; commission explicit (default zero).
- Rebalance threshold: small weight wiggles produce no orders.
- Every position carries a protective ATR stop, processed intrabar; stops
  only tighten. Orders without stops never exist (risk layer refuses them).
- The SAME RiskManager as live trading vets every bar's intents.

Determinism: no wall clock, no unseeded RNG. Truncating the input data
produces a bit-identical equity prefix (proven in tests/test_no_lookahead.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_config
from ..data import features as F
from ..monitor.log_setup import get_logger
from ..regime.hmm import RegimeView
from ..regime.stability import StabilityTracker
from ..risk.manager import OrderIntent, PortfolioState, Position, RiskManager
from ..strategy.base import Strategy, StrategyContext
from ..utils import new_id
from .metrics import compute_metrics, confidence_buckets, per_regime_breakdown

log = get_logger("main")


@dataclass
class PendingOrder:
    signal_id: str
    symbol: str
    side: str           # buy | sell
    weight_delta: float  # |fraction of equity| to add or remove
    stop_price: float | None
    strategy: str
    reason: str


@dataclass
class BacktestResult:
    name: str
    equity: pd.Series
    trades: pd.DataFrame
    regime_history: pd.DataFrame
    weights: pd.DataFrame
    metrics: dict
    per_regime: pd.DataFrame
    conf_buckets: pd.DataFrame
    decisions: list[dict] = field(default_factory=list)

    def export(self, outdir: str | Path) -> Path:
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        self.equity.rename("equity").to_csv(outdir / "equity.csv")
        self.trades.to_csv(outdir / "trades.csv", index=False)
        if not self.regime_history.empty:
            self.regime_history.to_csv(outdir / "regime_history.csv")
        self.weights.to_csv(outdir / "weights.csv")
        if not self.per_regime.empty:
            self.per_regime.to_csv(outdir / "per_regime.csv", index=False)
        if not self.conf_buckets.empty:
            self.conf_buckets.to_csv(outdir / "confidence_buckets.csv", index=False)
        with open(outdir / "metrics.json", "w", encoding="utf-8") as fh:
            json.dump({"name": self.name, **self.metrics}, fh, indent=2, default=str)
        with open(outdir / "decisions.json", "w", encoding="utf-8") as fh:
            json.dump(self.decisions, fh, indent=2, default=str)
        return outdir


class WalkForwardEngine:
    def __init__(
        self,
        bars: dict[str, pd.DataFrame],
        strategy: Strategy,
        symbols: list[str],
        regime_oracle: pd.DataFrame | None = None,
        start_ts: pd.Timestamp | None = None,
        risk_manager: RiskManager | None = None,
        initial_capital: float | None = None,
    ):
        cfg = get_config()
        self.cfg = cfg
        self.strategy = strategy
        self.symbols = [s for s in symbols if s in bars]
        self.market_proxy = cfg.universe.market_proxy
        self.oracle = regime_oracle
        if risk_manager is None:
            # ephemeral lock file: a simulated peak-drawdown lock must never
            # halt (or be halted by) live trading or other backtests
            import tempfile
            risk_manager = RiskManager(
                lock_file=Path(tempfile.mkdtemp(prefix="rt_bt_")) / "breaker.lock"
            )
        self.risk = risk_manager
        self.capital = initial_capital or cfg.backtest.initial_capital
        self.slippage = cfg.backtest.slippage_bps / 10_000.0
        self.commission = cfg.backtest.commission
        self.threshold = cfg.allocation.rebalance_threshold
        self.low_confidence = float(cfg.allocation.get("low_confidence", 0.45))

        self.bars = bars
        self.closes = pd.DataFrame({s: d["close"] for s, d in bars.items()}).sort_index().ffill(limit=5)
        self.opens = pd.DataFrame({s: d["open"] for s, d in bars.items()}).reindex(self.closes.index)
        self.lows = pd.DataFrame({s: d["low"] for s, d in bars.items()}).reindex(self.closes.index)
        self.atrs = pd.DataFrame(
            {s: F.atr(d, cfg.risk.atr_window) for s, d in bars.items()}
        ).reindex(self.closes.index).ffill(limit=5)

        # trading begins where the regime oracle begins (first out-of-sample
        # bar after the first train window); challengers use the same start
        # so every leaderboard entry is scored on identical bars
        if start_ts is None and self.oracle is not None:
            start_ts = self.oracle.index[0]
        if start_ts is None:
            start_ts = self.closes.index[min(260, len(self.closes) - 2)]
        self.start_ts = start_ts

    # ------------------------------------------------------------------ run
    def run(self) -> BacktestResult:
        idx = self.closes.index
        start_i = int(idx.searchsorted(self.start_ts))
        if start_i >= len(idx) - 1:
            raise RuntimeError("not enough bars after walk-forward start")

        p = PortfolioState(
            equity=self.capital, cash=self.capital,
            peak_equity=self.capital, day_start_equity=self.capital,
            week_start_equity=self.capital,
        )
        tracker = StabilityTracker(
            self.cfg.regime.confirm_bars,
            self.cfg.regime.flicker_window,
            self.cfg.regime.flicker_max_switches,
        )
        pending: list[PendingOrder] = []
        equity_curve: dict[pd.Timestamp, float] = {}
        weights_rows: dict[pd.Timestamp, dict] = {}
        trade_rows: list[dict] = []
        decisions_log: list[dict] = []
        last_week = None
        prev_equity = self.capital

        for i in range(start_i, len(idx)):
            t = idx[i]
            # -- bar boundary bookkeeping (daily bars: each bar is a day)
            p.day_start_equity = prev_equity
            week = t.isocalendar()[:2]
            if week != last_week:
                p.week_start_equity = prev_equity
                last_week = week
            p.trades_today = 0

            # -- 1. fill pending orders at this bar's open
            for od in pending:
                self._fill(od, p, t, trade_rows)
            pending = []

            # -- 2. protective stops, intrabar
            self._process_stops(p, t, trade_rows)

            # -- 3. mark to market at close
            for s in list(p.positions):
                px = self.closes.at[t, s]
                if np.isfinite(px):
                    p.prices[s] = float(px)
            p.equity = p.cash + sum(
                pos.qty * p.prices.get(s, pos.entry_price) for s, pos in p.positions.items()
            )
            p.peak_equity = max(p.peak_equity, p.equity)
            equity_curve[t] = p.equity

            # -- 4. trailing stops tighten only
            for s, pos in p.positions.items():
                atr_v = self.atrs.at[t, s] if s in self.atrs.columns else np.nan
                px = p.prices.get(s)
                if px and np.isfinite(atr_v):
                    pos.tighten_stop(self.risk.protective_stop(px, float(atr_v)))

            # -- 5. signals at the close -> orders for the next bar's open
            if i < len(idx) - 1:
                view, stab = self._regime_at(t, tracker)
                ctx = StrategyContext(
                    bars={s: d.loc[:t] for s, d in self.bars.items()},
                    closes=self.closes.loc[:t],
                    symbols=self.symbols,
                    market_proxy=self.market_proxy,
                    regime=view,
                    stability=stab,
                )
                targets = self.strategy.target_weights(ctx)
                intents = self._build_intents(targets, p, t)
                uncertainty = bool(
                    (stab and stab.uncertainty_mode)
                    or (view and view.confidence < self.low_confidence)
                )
                if intents:
                    decisions = self.risk.evaluate(intents, p, self.closes.loc[:t], uncertainty)
                    for d in decisions:
                        decisions_log.append(d.log_record())
                        if not d.approved:
                            continue
                        delta = d.final_weight - d.intent.current_weight
                        if d.intent.side == "buy" and delta > 1e-6:
                            pending.append(PendingOrder(d.intent.signal_id, d.intent.symbol, "buy",
                                                        delta, d.intent.stop_price,
                                                        self.strategy.name, "rebalance"))
                        elif d.intent.side == "sell" and delta < -1e-6:
                            pending.append(PendingOrder(d.intent.signal_id, d.intent.symbol, "sell",
                                                        -delta, None, self.strategy.name, "rebalance"))
            weights_rows[t] = {s: p.weight_of(s) for s in p.positions}
            prev_equity = p.equity

        equity = pd.Series(equity_curve).sort_index()
        trades = pd.DataFrame(trade_rows)
        regime_hist = (
            self.oracle.reindex(equity.index) if self.oracle is not None else pd.DataFrame(index=equity.index)
        )
        weights = pd.DataFrame.from_dict(weights_rows, orient="index").fillna(0.0).sort_index()
        result = BacktestResult(
            name=self.strategy.name,
            equity=equity,
            trades=trades,
            regime_history=regime_hist,
            weights=weights,
            metrics=compute_metrics(equity, trades if len(trades) else None),
            per_regime=per_regime_breakdown(equity, regime_hist) if self.oracle is not None else pd.DataFrame(),
            conf_buckets=confidence_buckets(equity, regime_hist) if self.oracle is not None else pd.DataFrame(),
            decisions=decisions_log,
        )
        result.metrics["avg_gross_exposure"] = float(weights.sum(axis=1).mean()) if len(weights) else 0.0
        return result

    # ------------------------------------------------------------- internals
    def _regime_at(self, t, tracker: StabilityTracker):
        if self.oracle is None or t not in self.oracle.index:
            return None, None
        row = self.oracle.loc[t]
        view = RegimeView(
            state=int(row["state"]), label=str(row["label"]), prob=float(row["prob"]),
            confidence=float(row["confidence"]), vol_rank=float(row["vol_rank"]),
            expected_vol=float(row["expected_vol"]), expected_ret=float(row["expected_ret"]),
            n_states=int(row["n_states"]),
        )
        return view, tracker.update(view.state)

    def _build_intents(self, targets: dict[str, float], p: PortfolioState, t) -> list[OrderIntent]:
        intents = []
        all_syms = set(targets) | set(p.positions)
        for s in sorted(all_syms):
            target = float(targets.get(s, 0.0))
            current = p.weight_of(s)
            delta = target - current
            if abs(delta) < self.threshold:  # friction reduction: skip churn
                continue
            px = self.closes.at[t, s] if s in self.closes.columns else np.nan
            if not np.isfinite(px):
                continue
            atr_v = self.atrs.at[t, s] if s in self.atrs.columns else np.nan
            stop = self.risk.protective_stop(float(px), float(atr_v)) if np.isfinite(atr_v) else None
            intents.append(
                OrderIntent(
                    signal_id=new_id(), symbol=s, side="buy" if delta > 0 else "sell",
                    target_weight=target, current_weight=current, price=float(px),
                    stop_price=stop if delta > 0 else None, strategy=self.strategy.name,
                )
            )
        return intents

    def _fill(self, od: PendingOrder, p: PortfolioState, t, trade_rows: list[dict]) -> None:
        px = self.opens.at[t, od.symbol] if od.symbol in self.opens.columns else np.nan
        if not np.isfinite(px) or px <= 0:
            return  # halted/missing bar: order dropped, never silently queued
        if od.side == "buy":
            fill_px = float(px) * (1 + self.slippage)
            notional = od.weight_delta * p.equity
            qty = notional / fill_px
            pos = p.positions.get(od.symbol)
            if pos is None:
                stop = od.stop_price if od.stop_price else fill_px * 0.9
                p.positions[od.symbol] = Position(od.symbol, qty, fill_px, stop,
                                                  self.risk.sectors.get(od.symbol, "unknown"),
                                                  od.strategy, od.signal_id)
            else:
                total_cost = pos.entry_price * pos.qty + fill_px * qty
                pos.qty += qty
                pos.entry_price = total_cost / pos.qty
                if od.stop_price:
                    pos.tighten_stop(od.stop_price)
            p.cash -= qty * fill_px + self.commission
            realized = None
        else:
            pos = p.positions.get(od.symbol)
            if pos is None:
                return
            fill_px = float(px) * (1 - self.slippage)
            notional = od.weight_delta * p.equity
            qty = min(pos.qty, notional / fill_px)
            pos.qty -= qty
            p.cash += qty * fill_px - self.commission
            realized = (fill_px - pos.entry_price) * qty
            if pos.qty * fill_px < 1.0:  # dust -> fully closed
                realized += (fill_px - pos.entry_price) * pos.qty
                p.cash += pos.qty * fill_px
                del p.positions[od.symbol]
        p.trades_today += 1
        trade_rows.append(
            {
                "ts": t, "signal_id": od.signal_id, "symbol": od.symbol, "side": od.side,
                "qty": round(qty, 6), "price": round(fill_px, 4),
                "value": round(qty * fill_px, 2), "strategy": od.strategy,
                "reason": od.reason, "realized_pnl": realized,
            }
        )

    def _process_stops(self, p: PortfolioState, t, trade_rows: list[dict]) -> None:
        for s in list(p.positions):
            pos = p.positions[s]
            low = self.lows.at[t, s] if s in self.lows.columns else np.nan
            opn = self.opens.at[t, s] if s in self.opens.columns else np.nan
            if not np.isfinite(low) or low > pos.stop_price:
                continue
            # gap below the stop fills at the open, not at the stop price
            base = min(float(opn), pos.stop_price) if np.isfinite(opn) else pos.stop_price
            fill_px = base * (1 - self.slippage)
            p.cash += pos.qty * fill_px - self.commission
            p.trades_today += 1
            trade_rows.append(
                {
                    "ts": t, "signal_id": pos.signal_id, "symbol": s, "side": "sell",
                    "qty": round(pos.qty, 6), "price": round(fill_px, 4),
                    "value": round(pos.qty * fill_px, 2), "strategy": pos.strategy,
                    "reason": "stop", "realized_pnl": (fill_px - pos.entry_price) * pos.qty,
                }
            )
            del p.positions[s]
