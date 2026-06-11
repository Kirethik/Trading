"""§13 trial deployment: run the whole cohort concurrently on Alpaca paper.

Deploy first, correct later — every generated strategy gets a fixed capital
slice and trades it as a virtual sub-portfolio of the one paper account.
A persistent ledger attributes every fill, position and P&L to its strategy
(client_order_id is prefixed with the strategy id, so attribution survives
on the Alpaca side too). `report` ranks the cohort on realized performance;
`prune` retires losers and closes their books.

The global risk layer still vetoes every order: each slice is checked as its
own mini-portfolio, and the hard peak-drawdown lock (the REAL lock file)
halts the entire cohort at once.
"""
from __future__ import annotations

import uuid

import numpy as np
import pandas as pd

from ..config import get_config
from ..data import features as F
from ..data.provider import DataProvider, aligned_closes
from ..monitor.log_setup import get_logger
from ..risk.manager import OrderIntent, PortfolioState, Position, RiskManager
from ..strategy.base import StrategyContext
from ..utils import atomic_write_json, load_json, utc_now, utc_now_iso
from .factory import OmniStrategy, generate_cohort

log = get_logger("trades")

EQUITY_HISTORY_CAP = 1000


class OmnivoreDeployer:
    def __init__(self, broker=None, dry_run: bool = True):
        self.cfg = get_config()
        self.broker = broker
        self.dry_run = dry_run
        self.provider = DataProvider()
        self.risk = RiskManager()  # real lock file: a hard lock halts the cohort
        self.ledger_path = self.cfg.resolve_path(self.cfg.omnivore.ledger_file)
        self.ledger: dict = load_json(self.ledger_path, default={})
        self.cohort: list[OmniStrategy] = generate_cohort()
        self._ensure_ledger_entries()

    # -------------------------------------------------------------- ledger
    def _ensure_ledger_entries(self) -> None:
        cap = self.cfg.omnivore.capital_per_strategy
        for om in self.cohort:
            if om.id not in self.ledger:
                self.ledger[om.id] = {
                    "market": om.market,
                    "enabled": True,
                    "allocated": cap,
                    "cash": cap,
                    "positions": {},          # sym -> {qty, entry, stop}
                    "realized_pnl": 0.0,
                    "trades": 0,
                    "equity_history": [],     # [ts, equity]
                    "watermarks": {"day_key": "", "day_start": cap,
                                   "week_key": "", "week_start": cap, "peak": cap},
                    "created": utc_now_iso(),
                }
            self.ledger[om.id]["meta"] = om.meta

    def save(self) -> None:
        atomic_write_json(self.ledger_path, self.ledger)

    def _slice_equity(self, entry: dict, prices: dict[str, float]) -> float:
        held = sum(
            p["qty"] * prices.get(s, p["entry"]) for s, p in entry["positions"].items()
        )
        return entry["cash"] + held

    def _portfolio_state(self, entry: dict, prices: dict[str, float]) -> PortfolioState:
        positions = {
            s: Position(s, p["qty"], p["entry"], p["stop"],
                        self.risk.sectors.get(s, "unknown"))
            for s, p in entry["positions"].items()
        }
        wm = entry["watermarks"]
        eq = self._slice_equity(entry, prices)
        return PortfolioState(
            equity=eq, cash=entry["cash"], positions=positions,
            peak_equity=wm["peak"], day_start_equity=wm["day_start"],
            week_start_equity=wm["week_start"], trades_today=0, prices=prices,
        )

    # --------------------------------------------------------------- cycle
    def run_cycle(self) -> dict:
        """One pass over the whole cohort. Returns a cycle summary."""
        all_symbols = sorted({s for om in self.cohort for s in om.symbols})
        bars = self.provider.get_universe_bars(all_symbols, refresh=True)
        closes = aligned_closes(bars)
        prices = {s: float(closes[s].dropna().iloc[-1]) for s in closes.columns if closes[s].notna().any()}
        now = utc_now()

        summary = {"ts": utc_now_iso(), "orders": 0, "stops": 0, "skipped": 0, "dry_run": self.dry_run}
        for om in self.cohort:
            entry = self.ledger[om.id]
            if not entry["enabled"]:
                summary["skipped"] += 1
                continue
            try:
                self._cycle_strategy(om, entry, bars, closes, prices, now, summary)
            except Exception as exc:
                log.error("omnivore %s cycle failed: %s", om.id, exc)
        self.save()
        log.info("omnivore cycle done", extra={"ctx": summary})
        return summary

    def _cycle_strategy(self, om: OmniStrategy, entry: dict, bars, closes, prices, now, summary) -> None:
        # watermarks roll on actual slice equity
        eq = self._slice_equity(entry, prices)
        wm = entry["watermarks"]
        day_key = now.strftime("%Y-%m-%d")
        week_key = "-".join(map(str, now.isocalendar()[:2]))
        if wm["day_key"] != day_key:
            wm.update(day_key=day_key, day_start=eq)
        if wm["week_key"] != week_key:
            wm.update(week_key=week_key, week_start=eq)
        wm["peak"] = max(wm["peak"], eq)

        # soft stops first — every position has one, every market
        for sym in list(entry["positions"]):
            pos = entry["positions"][sym]
            px = prices.get(sym)
            if px is not None and px <= pos["stop"]:
                self._exit(om, entry, sym, pos["qty"], px, "stop")
                summary["stops"] += 1

        # signals on this strategy's own universe
        syms = [s for s in om.symbols if s in bars]
        if not syms:
            return
        ctx = StrategyContext(
            bars={s: bars[s] for s in syms},
            closes=closes[[c for c in closes.columns if c in syms]],
            symbols=syms,
        )
        targets = om.strategy.target_weights(ctx)

        p = self._portfolio_state(entry, prices)
        intents = []
        for s in sorted(set(targets) | set(entry["positions"])):
            target = float(targets.get(s, 0.0))
            current = p.weight_of(s)
            if abs(target - current) < self.cfg.allocation.rebalance_threshold:
                continue
            px = prices.get(s)
            if not px:
                continue
            stop = None
            if target > current and s in bars and len(bars[s]) > self.cfg.risk.atr_window + 2:
                a = F.atr(bars[s], self.cfg.risk.atr_window).iloc[-1]
                if np.isfinite(a):
                    stop = self.risk.protective_stop(px, float(a))
            intents.append(OrderIntent(
                signal_id=f"{om.id}-{uuid.uuid4().hex[:8]}", symbol=s,
                side="buy" if target > current else "sell",
                target_weight=target, current_weight=current,
                price=px, stop_price=stop, strategy=om.id,
            ))
        if not intents:
            return

        decisions = self.risk.evaluate(intents, p, ctx.closes)
        for d in decisions:
            if not d.approved:
                continue
            delta = d.final_weight - d.intent.current_weight
            sym, px = d.intent.symbol, d.intent.price
            if d.intent.side == "buy" and delta > 1e-6:
                qty = delta * p.equity / px
                self._enter(om, entry, sym, qty, px, d.intent.stop_price, d.intent.signal_id)
                summary["orders"] += 1
            elif d.intent.side == "sell" and delta < -1e-6:
                pos = entry["positions"].get(sym)
                if pos:
                    qty = min(pos["qty"], -delta * p.equity / px)
                    self._exit(om, entry, sym, qty, px, "rebalance")
                    summary["orders"] += 1

        # mark equity history
        hist = entry["equity_history"]
        hist.append([utc_now_iso(), round(self._slice_equity(entry, prices), 2)])
        del hist[:-EQUITY_HISTORY_CAP]

    # ----------------------------------------------------------- execution
    def _enter(self, om: OmniStrategy, entry: dict, sym: str, qty: float, px: float,
               stop: float | None, signal_id: str) -> None:
        if qty * px < 10:  # ignore dust orders
            return
        if not self.dry_run and self.broker is not None:
            offset = self.cfg.broker.limit_offset_bps / 10_000.0
            self.broker.submit_entry(sym, qty, px * (1 + offset),
                                     stop or px * 0.9, signal_id=signal_id)
        pos = entry["positions"].get(sym)
        if pos is None:
            entry["positions"][sym] = {"qty": qty, "entry": px, "stop": stop or px * 0.9}
        else:
            cost = pos["entry"] * pos["qty"] + px * qty
            pos["qty"] += qty
            pos["entry"] = cost / pos["qty"]
            if stop and stop > pos["stop"]:
                pos["stop"] = stop
        entry["cash"] -= qty * px
        entry["trades"] += 1
        log.info("omnivore entry", extra={"ctx": {"strategy": om.id, "symbol": sym,
                                                  "qty": round(qty, 6), "price": px,
                                                  "signal_id": signal_id, "dry_run": self.dry_run}})

    def _exit(self, om: OmniStrategy, entry: dict, sym: str, qty: float, px: float, reason: str) -> None:
        pos = entry["positions"].get(sym)
        if pos is None:
            return
        qty = min(qty, pos["qty"])
        signal_id = f"{om.id}-x-{uuid.uuid4().hex[:6]}"
        if not self.dry_run and self.broker is not None:
            try:
                self.broker.submit_exit(sym, qty, signal_id=signal_id)
            except Exception as exc:
                log.error("omnivore exit failed %s %s: %s", om.id, sym, exc)
                return
        entry["cash"] += qty * px
        entry["realized_pnl"] += (px - pos["entry"]) * qty
        pos["qty"] -= qty
        if pos["qty"] * px < 1.0:
            entry["cash"] += pos["qty"] * px
            entry["realized_pnl"] += (px - pos["entry"]) * pos["qty"]
            del entry["positions"][sym]
        entry["trades"] += 1
        log.info("omnivore exit", extra={"ctx": {"strategy": om.id, "symbol": sym,
                                                 "qty": round(qty, 6), "price": px,
                                                 "reason": reason, "dry_run": self.dry_run}})

    # ------------------------------------------------------------ reporting
    def report(self) -> pd.DataFrame:
        """Cohort ranked by realized + unrealized P&L with slice Sharpe."""
        rows = []
        for sid, entry in self.ledger.items():
            hist = entry.get("equity_history", [])
            eq = pd.Series([h[1] for h in hist], dtype=float)
            sharpe = 0.0
            if len(eq) > 10:
                rets = eq.pct_change().dropna()
                if rets.std() > 0:
                    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
            current = eq.iloc[-1] if len(eq) else entry["allocated"]
            rows.append({
                "strategy": sid,
                "market": entry.get("market", "?"),
                "enabled": entry["enabled"],
                "equity": round(current, 2),
                "total_pnl": round(current - entry["allocated"], 2),
                "pnl_pct": round(current / entry["allocated"] - 1.0, 4),
                "realized_pnl": round(entry["realized_pnl"], 2),
                "sharpe": round(sharpe, 2),
                "trades": entry["trades"],
                "open_positions": len(entry["positions"]),
                "observations": len(hist),
            })
        df = pd.DataFrame(rows)
        return df.sort_values("pnl_pct", ascending=False).reset_index(drop=True) if len(df) else df

    def prune(self, min_observations: int = 20) -> list[str]:
        """Observe & correct: retire strategies that earned a real losing
        record, close their books. Returns the retired ids."""
        retired = []
        board = self.report()
        prices_needed = bool(len(board))
        prices: dict[str, float] = {}
        if prices_needed:
            syms = sorted({s for e in self.ledger.values() for s in e["positions"]})
            if syms:
                bars = self.provider.get_universe_bars(syms)
                closes = aligned_closes(bars)
                prices = {s: float(closes[s].dropna().iloc[-1]) for s in closes.columns if closes[s].notna().any()}
        for row in board.itertuples():
            if not row.enabled or row.observations < min_observations:
                continue
            if row.total_pnl < 0 and row.sharpe < 0:
                entry = self.ledger[row.strategy]
                for sym in list(entry["positions"]):
                    px = prices.get(sym, entry["positions"][sym]["entry"])
                    om = next((o for o in self.cohort if o.id == row.strategy), None)
                    if om:
                        self._exit(om, entry, sym, entry["positions"][sym]["qty"], px, "pruned")
                entry["enabled"] = False
                retired.append(row.strategy)
                log.warning("omnivore strategy pruned: %s (pnl %.2f, sharpe %.2f)",
                            row.strategy, row.total_pnl, row.sharpe)
        self.save()
        return retired
