"""Orchestration: boot -> verify -> recover -> bar loop -> graceful shutdown.

Per bar: refresh features (no future data) -> filtered regime -> stability ->
target allocations -> EVERY signal through risk -> execute survivors ->
tighten stops -> breakers -> persist state + dashboard snapshot.

Failure policy:
- model error      -> hold the last known regime, size conservatively
- data loss        -> pause trading, keep stops working
- API error        -> broker layer retries with backoff; alert on exhaustion
- shutdown/SIGINT  -> save state, leave positions AND their stops in place
"""
from __future__ import annotations

import signal
import time

import numpy as np
import pandas as pd

from ..config import get_config
from ..data import features as F
from ..data.provider import DataProvider, aligned_closes, is_crypto
from ..monitor.alerts import AlertKind, AlertManager
from ..monitor.log_setup import get_logger, setup_logging
from ..regime.hmm import RegimeModel
from ..risk.manager import OrderIntent, RiskManager
from ..strategy.catalog import all_required_symbols
from ..strategy.regime_strategy import RegimeStrategy
from ..strategy.base import StrategyContext
from ..utils import atomic_write_json, new_id, utc_now, utc_now_iso
from .state import SessionState

log = get_logger("main")
regime_log = get_logger("regime")


class Orchestrator:
    def __init__(self, broker=None, dry_run: bool = True):
        self.cfg = get_config()
        setup_logging(self.cfg.resolve_path(self.cfg.monitoring.log_dir))
        self.dry_run = dry_run
        self.broker = broker
        self.alerts = AlertManager()
        self.risk = RiskManager()  # uses the REAL lock file
        self.state = SessionState()
        self.provider = DataProvider()
        self.strategy = RegimeStrategy()
        self.model: RegimeModel | None = None
        self.model_path = self.cfg.resolve_path(self.cfg.state_dir) / "regime_model.pkl"
        self.dashboard_path = self.cfg.resolve_path(self.cfg.state_dir) / "dashboard.json"
        self._stop = False
        self._last_view = None

    # ------------------------------------------------------------------ boot
    def boot(self) -> None:
        log.info("=== Regime-Trader boot (%s mode) ===", "DRY-RUN" if self.dry_run else "TRADING")
        recovered = self.state.load()
        log.info("session state %s", "recovered from disk" if recovered else "fresh")

        if self.broker is not None:
            info = self.broker.health_check()
            tracked = {s: p.qty for s, p in self.state.positions().items()}
            report = self.broker.reconcile(tracked)
            if report["qty_mismatch"] or report["missing_at_broker"]:
                self.alerts.send(
                    AlertKind.SYSTEM,
                    f"position reconciliation differences: {report['qty_mismatch']} "
                    f"missing={report['missing_at_broker']}",
                )
            log.info("broker verified: equity %.2f (paper=%s)", info["equity"], info["paper"])

        self.load_or_train_model()

    def load_or_train_model(self, retrain: bool = False) -> None:
        if self.model_path.exists() and not retrain:
            try:
                self.model = RegimeModel.load(self.model_path)
                log.info("regime model loaded: %d states", self.model.n_states)
                return
            except Exception as exc:
                log.warning("saved model unreadable (%s); retraining", exc)
        proxy = self.cfg.universe.market_proxy
        bars = self.provider.get_bars(proxy)
        X = F.regime_features(bars, self.cfg.regime.vol_window)
        if len(X) < self.cfg.regime.min_train_bars:
            raise RuntimeError(f"only {len(X)} bars of features; need {self.cfg.regime.min_train_bars}")
        self.model = RegimeModel().fit(X)
        self.model.save(self.model_path)
        self.alerts.send(AlertKind.MODEL_RETRAIN,
                         f"regime model retrained: {self.model.n_states} states (BIC)")

    # ------------------------------------------------------------- one cycle
    def run_once(self) -> dict:
        """One full bar pipeline. Returns a cycle summary dict."""
        summary = {"ts": utc_now_iso(), "dry_run": self.dry_run, "actions": []}
        now = utc_now()

        # 1. data refresh — strictly historical bars only
        symbols = all_required_symbols()
        try:
            bars = self.provider.get_universe_bars(symbols, refresh=True)
            proxy_bars = bars[self.cfg.universe.market_proxy]
            closes = aligned_closes(bars)
        except Exception as exc:
            self.alerts.send(AlertKind.DATA_LOSS, f"data refresh failed: {exc}")
            summary["error"] = f"data loss: {exc} — trading paused, stops remain live"
            return summary  # pause, but resting stops at broker stay in force

        # 2-3. regime + stability (hold last regime on model error)
        tracker = self.state.stability_tracker()
        try:
            X = F.regime_features(proxy_bars, self.cfg.regime.vol_window)
            probs = self.model.filtered_probabilities(X)
            view = self.model.view_at(probs[-1])
            self._last_view = view
        except Exception as exc:
            self.alerts.send(AlertKind.SYSTEM, f"model inference failed: {exc}; holding last regime")
            view = self._last_view
        if view is None:
            summary["error"] = "no regime available"
            return summary
        stab = tracker.update(view.state)
        self.state.save_stability(tracker)

        prev = self.state.data.get("last_regime", {})
        if prev and prev.get("state") != view.state:
            self.alerts.send(AlertKind.REGIME_CHANGE,
                             f"raw regime {prev.get('label')} -> {view.label} "
                             f"(conf {view.confidence:.0%}, unconfirmed)")
        if stab.flickering:
            self.alerts.send(AlertKind.FLICKER,
                             f"regime flicker: {stab.raw_switches_in_window} switches "
                             f"in {self.cfg.regime.flicker_window} bars — uncertainty mode")
        self.state.data["last_regime"] = view.to_dict()
        regime_log.info("regime %s", view.label, extra={"ctx": {**view.to_dict(), **stab.__dict__}})

        # portfolio truth: broker if available, else tracked state
        prices = {s: float(closes[s].dropna().iloc[-1]) for s in closes.columns if closes[s].notna().any()}
        if self.broker is not None:
            equity, cash = self.broker.account_equity()
        else:
            positions = self.state.positions()
            held = sum(p.qty * prices.get(s, p.entry_price) for s, p in positions.items())
            equity = self.cfg.backtest.initial_capital if not self.state.data["peak_equity"] else held + (
                self.state.data.get("cash_estimate") or self.cfg.backtest.initial_capital - held)
            cash = equity - held
        self.state.roll_watermarks(equity, now)
        p = self.state.portfolio_state(equity, cash, prices)

        # 4. target allocations
        ctx = StrategyContext(
            bars=bars, closes=closes, symbols=list(self.cfg.universe.equities),
            market_proxy=self.cfg.universe.market_proxy, regime=view, stability=stab,
        )
        targets = self.strategy.target_weights(ctx)

        # 5. risk gauntlet
        uncertainty = stab.uncertainty_mode or view.confidence < float(
            self.cfg.allocation.get("low_confidence", 0.45)
        )
        intents = self._intents_from_targets(targets, p, bars, closes)
        decisions = self.risk.evaluate(intents, p, closes, uncertainty) if intents else []
        status = self.risk.breaker_status(p)
        if status.mode.value != "normal":
            self.alerts.send(AlertKind.CIRCUIT_BREAKER,
                             f"breaker {status.mode.value}: {'; '.join(status.reasons)}")

        # 6. execute survivors
        for d in decisions:
            action = self._execute(d, p)
            if action:
                summary["actions"].append(action)

        # 7. stops only tighten (server-side for stocks, soft for crypto)
        self._update_stops(p, bars, prices, summary)

        # 8-9. persist + dashboard snapshot
        self.state.set_positions(p.positions)
        self.state.data["trades_today"] = p.trades_today
        self.state.save()
        self._write_dashboard(view, stab, p, status, summary)
        log.info("cycle complete", extra={"ctx": {
            "regime": view.label, "conf": round(view.confidence, 3),
            "equity": round(equity, 2), "gross": round(p.gross_exposure(), 3),
            "actions": len(summary["actions"]), "breaker": status.mode.value,
        }})
        return summary

    # ------------------------------------------------------------ execution
    def _intents_from_targets(self, targets, p, bars, closes) -> list[OrderIntent]:
        threshold = self.cfg.allocation.rebalance_threshold
        intents = []
        for s in sorted(set(targets) | set(p.positions)):
            target = float(targets.get(s, 0.0))
            current = p.weight_of(s)
            if abs(target - current) < threshold:
                continue
            px = p.prices.get(s)
            if not px:
                continue
            stop = None
            if target > current:
                df = bars.get(s)
                if df is not None and len(df) > self.cfg.risk.atr_window + 2:
                    a = F.atr(df, self.cfg.risk.atr_window).iloc[-1]
                    if np.isfinite(a):
                        stop = self.risk.protective_stop(px, float(a))
            intents.append(OrderIntent(
                signal_id=new_id(), symbol=s, side="buy" if target > current else "sell",
                target_weight=target, current_weight=current, price=px, stop_price=stop,
                strategy=self.strategy.name,
            ))
        return intents

    def _execute(self, d, p) -> dict | None:
        if not d.approved:
            self.alerts.send(AlertKind.ORDER_REJECTED,
                             f"{d.intent.symbol} {d.intent.side} vetoed: {d.reasons}",
                             dedupe_key=f"rej:{d.intent.symbol}")
            return None
        if self.state.already_submitted(d.intent.signal_id):
            return None  # restart idempotency: never double-enter
        delta = d.final_weight - d.intent.current_weight
        notional = abs(delta) * p.equity
        px = d.intent.price
        qty = notional / px
        action = {
            "signal_id": d.intent.signal_id, "symbol": d.intent.symbol,
            "side": d.intent.side, "qty": round(qty, 6), "ref_price": px,
            "stop": d.intent.stop_price, "reasons": d.reasons, "dry_run": self.dry_run,
        }
        if self.dry_run:
            log.info("DRY-RUN order (not sent)", extra={"ctx": action})
        else:
            try:
                if d.intent.side == "buy" and delta > 0:
                    offset = self.cfg.broker.limit_offset_bps / 10_000.0
                    self.broker.submit_entry(
                        d.intent.symbol, qty, px * (1 + offset), d.intent.stop_price,
                        signal_id=d.intent.signal_id,
                    )
                elif d.intent.side == "sell" and delta < 0:
                    pos = p.positions.get(d.intent.symbol)
                    if pos:
                        self.broker.submit_exit(d.intent.symbol, min(qty, pos.qty),
                                                signal_id=d.intent.signal_id)
                self.state.mark_submitted(d.intent.signal_id)
            except Exception as exc:
                self.alerts.send(AlertKind.API_LOSS, f"order failed for {d.intent.symbol}: {exc}")
                action["error"] = str(exc)
        # track locally (paper fills assumed near ref price; reconciled at boot)
        self._apply_local(d, p, qty)
        return action

    def _apply_local(self, d, p, qty: float) -> None:
        from ..risk.manager import Position

        s = d.intent.symbol
        if d.intent.side == "buy":
            pos = p.positions.get(s)
            if pos is None:
                p.positions[s] = Position(s, qty, d.intent.price, d.intent.stop_price or d.intent.price * 0.9,
                                          self.risk.sectors.get(s, "unknown"), self.strategy.name,
                                          d.intent.signal_id)
            else:
                cost = pos.entry_price * pos.qty + d.intent.price * qty
                pos.qty += qty
                pos.entry_price = cost / pos.qty
                if d.intent.stop_price:
                    pos.tighten_stop(d.intent.stop_price)
        else:
            pos = p.positions.get(s)
            if pos:
                pos.qty -= min(qty, pos.qty)
                if pos.qty <= 1e-9:
                    del p.positions[s]
        p.trades_today += 1

    def _update_stops(self, p, bars, prices, summary) -> None:
        for s, pos in list(p.positions.items()):
            df = bars.get(s)
            px = prices.get(s)
            if df is None or px is None or len(df) < self.cfg.risk.atr_window + 2:
                continue
            a = F.atr(df, self.cfg.risk.atr_window).iloc[-1]
            if not np.isfinite(a):
                continue
            new_stop = self.risk.protective_stop(px, float(a))
            if pos.tighten_stop(new_stop):
                if not self.dry_run and self.broker is not None and not is_crypto(s):
                    try:
                        self.broker.tighten_stop(s, new_stop)
                    except Exception as exc:
                        log.warning("stop tighten failed for %s: %s", s, exc)
                summary["actions"].append({"symbol": s, "stop_tightened_to": round(new_stop, 2)})
            # crypto soft stop: no server-side stop exists, enforce here
            if is_crypto(s) and px <= pos.stop_price:
                if not self.dry_run and self.broker is not None:
                    try:
                        self.broker.submit_exit(s, pos.qty, signal_id=pos.signal_id)
                    except Exception as exc:
                        self.alerts.send(AlertKind.API_LOSS, f"crypto stop exit failed {s}: {exc}")
                del p.positions[s]
                summary["actions"].append({"symbol": s, "soft_stop_exit": px})

    # ------------------------------------------------------------ dashboard
    def _write_dashboard(self, view, stab, p, status, summary) -> None:
        atomic_write_json(self.dashboard_path, {
            "ts": utc_now_iso(),
            "regime": view.to_dict(),
            "stability": {
                "confirmed_state": stab.confirmed_state, "consecutive_bars": stab.consecutive_bars,
                "in_transition": stab.in_transition, "flickering": stab.flickering,
                "uncertainty_mode": stab.uncertainty_mode,
            },
            "portfolio": {
                "equity": p.equity, "cash": p.cash, "gross_exposure": p.gross_exposure(),
                "daily_pnl_pct": p.daily_pnl_pct(), "weekly_pnl_pct": p.weekly_pnl_pct(),
                "peak_drawdown_pct": p.peak_drawdown_pct(),
            },
            "positions": {
                s: {"qty": pos.qty, "entry": pos.entry_price, "stop": pos.stop_price,
                    "price": p.prices.get(s), "strategy": pos.strategy}
                for s, pos in p.positions.items()
            },
            "risk": {
                "breaker_mode": status.mode.value, "breaker_reasons": status.reasons,
                "max_gross": self.risk.max_gross, "max_single": self.risk.max_single,
            },
            "recent_actions": summary["actions"][-10:],
            "recent_alerts": self.alerts.recent[-10:],
            "dry_run": self.dry_run,
        })

    # ------------------------------------------------------------- main loop
    def run_loop(self, interval_minutes: int = 30) -> None:
        """Poll on an interval; each pass runs one full pipeline cycle.
        Daily bars only change once a day, but intra-day passes keep stops,
        breakers and the dashboard fresh."""
        signal.signal(signal.SIGINT, self._handle_sigint)
        self.boot()
        log.info("entering main loop (every %d min). Ctrl+C for graceful shutdown.", interval_minutes)
        while not self._stop:
            try:
                self.run_once()
            except Exception as exc:
                self.alerts.send(AlertKind.SYSTEM, f"cycle error: {exc}")
                log.exception("cycle error")
            for _ in range(interval_minutes * 60):
                if self._stop:
                    break
                time.sleep(1)
        self.shutdown()

    def _handle_sigint(self, *_):
        log.info("SIGINT received — graceful shutdown requested")
        self._stop = True

    def shutdown(self) -> None:
        """Graceful: persist state, leave positions and stops untouched."""
        self.state.save()
        log.info("shutdown complete: state saved, positions held, stops remain live")
