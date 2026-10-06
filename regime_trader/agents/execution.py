"""Autonomous PAPER order placement for agents — no human approval, but every
order goes through the same RiskManager veto and broker path as the regime
orchestrator (`live/execution.py`).

Agents trade an isolated SLEEVE of the paper account, kept as a real book in
its own ledger: cash (seeded once at max_sleeve_pct of account equity) plus
positions marked to market. Breakers therefore see the sleeve's ACTUAL P&L
(day / week / peak baselines are persisted). Weights an agent passes are
fractions of sleeve equity, not of the whole account.

Known limitation (addressed by sub-project B, live oversight): if the regime
orchestrator holds the same symbol, an agent entry that never filled cannot
be told apart from orchestrator shares until the next day's reconciliation.
"""
from __future__ import annotations

import fcntl
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from ..config import get_config
from ..live import execution as shared
from ..monitor.log_setup import get_logger
from ..risk.manager import OrderIntent, PortfolioState, Position, RiskManager
from ..utils import atomic_write_json, load_json, utc_now
from . import guards

log = get_logger("agents")

TOOL_NAME = "place_paper_order"
_MAX_SIGNALS = 2000


class PaperExecutor:
    def __init__(self, agent: str, broker, risk: RiskManager,
                 get_bars: Callable[[str], pd.DataFrame | None], ledger_path: str | Path,
                 max_sleeve_pct: float, max_orders_per_day: int,
                 on_event: Callable[[dict], None] | None = None,
                 main_lock: str | Path | None = None):
        self.agent = agent
        self.broker = broker
        self.risk = risk
        self.get_bars = get_bars
        self.ledger_path = Path(ledger_path)
        self.max_sleeve_pct = float(max_sleeve_pct)
        self.max_orders_per_day = int(max_orders_per_day)
        self.on_event = on_event or (lambda e: None)
        self.main_lock = main_lock   # the main system's hard lock: honored, never written
        cfg = get_config()
        self.allowed_symbols = frozenset(cfg.universe.sectors.as_dict())
        self.threshold = float(cfg.allocation.rebalance_threshold)
        self.atr_window = int(cfg.risk.atr_window)
        self.limit_offset_bps = float(cfg.broker.limit_offset_bps)

    # ------------------------------------------------------------ ledger
    def ledger(self) -> dict:
        now = utc_now()
        today, week = now.date().isoformat(), "%d-W%02d" % now.isocalendar()[:2]
        led = load_json(self.ledger_path, default=None) or {}
        day = led.get("day") or {}
        if day.get("date") != today:
            day = {"date": today, "orders": 0, "start_equity": None}
        wk = led.get("week") or {}
        if wk.get("key") != week:
            wk = {"key": week, "start_equity": None}
        return {"cash": led.get("cash"), "positions": dict(led.get("positions", {})),
                "signals": list(led.get("signals", [])),
                "peak_equity": float(led.get("peak_equity", 0.0)), "day": dict(day), "week": dict(wk)}

    def _save(self, led: dict) -> None:
        atomic_write_json(self.ledger_path, {**led, "signals": led["signals"][-_MAX_SIGNALS:]})

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize place() across processes (cron jobs + supervisor)."""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # ------------------------------------------------------------- place
    def place(self, symbol: str, side: str, target_weight: float, rationale: str,
              signal_id: str) -> dict:
        guards.assert_paper_only()
        if getattr(self.broker, "paper", False) is not True:
            raise guards.PaperOnlyViolation("agent executor was handed a non-paper broker")

        base = {"agent": self.agent, "symbol": symbol, "side": side,
                "target_weight": target_weight, "signal_id": signal_id, "rationale": rationale[:500]}
        if guards.kill_switch_engaged():
            return self._emit(base, "blocked", "agent kill switch engaged")
        if side == "buy" and guards.agents_paused():
            return self._emit(base, "blocked", "agent buying paused by the Watchman")
        if side == "buy" and self.main_lock is not None and Path(self.main_lock).exists():
            return self._emit(base, "blocked", "the main system's emergency stop is on")
        problem = self._validate(symbol, side, target_weight)
        if problem:
            return self._emit(base, "invalid", problem)
        with self._locked():
            return self._place_locked(base, symbol, side, float(target_weight), signal_id)

    def _place_locked(self, base: dict, symbol: str, side: str, w: float, signal_id: str) -> dict:
        led = self.ledger()
        if signal_id in led["signals"]:
            return self._emit(base, "duplicate", "identical order already handled")
        if led["day"]["orders"] >= self.max_orders_per_day:
            return self._emit(base, "blocked", f"daily cap of {self.max_orders_per_day} orders reached")
        if not self.broker.is_market_open():
            return self._emit(base, "deferred", "market closed")
        df = self.get_bars(symbol)
        if df is None or df.empty:
            return self._emit(base, "invalid", "no price data")

        price = float(df["close"].iloc[-1])
        led = self._reconcile(led)
        p, led = self._book(led, symbol, price)
        stop = shared.protective_stop_for(self.risk, df, price, self.atr_window) if side == "buy" else None
        intent = OrderIntent(signal_id=signal_id, symbol=symbol, side=side, target_weight=w,
                             current_weight=p.weight_of(symbol), price=price, stop_price=stop,
                             strategy=f"agent:{self.agent}", sector=self.risk.sectors.get(symbol, "unknown"))
        decision = self.risk.evaluate([intent], p)[0]
        led = {**led, "signals": [*led["signals"], signal_id]}

        if not decision.approved:
            status, reason = "vetoed", "; ".join(decision.reasons)
        elif not shared.is_actionable(decision, self.threshold):
            status, reason = "noop", "risk-adjusted size equals current holding"
        elif (qty := shared.broker_qty(decision, p)) <= 0:
            status, reason = "noop", "sized below one tradable unit"
        else:
            order, qty = shared.submit(self.broker, decision, qty, self.limit_offset_bps)
            if order is not None:
                self._save(_after_fill(led, decision.intent, qty))
                return self._emit({**base, "qty": qty, "price": price, "stop": stop,
                                   "final_weight": decision.final_weight,
                                   "risk_notes": decision.reasons}, "submitted", None)
            status, reason = "noop", "broker returned no order"
        self._save(led)
        return self._emit(base, status, reason)

    def trim_all(self, fraction: float, rationale: str, tag: str) -> list[dict]:
        """Sell `fraction` of every agent-sleeve holding (risk-reducing only).
        Idempotent per `tag`, so a persisting problem trims once per tag."""
        led = self.ledger()
        if not led["positions"]:
            return []
        prices = {s: self._mark(s, pos) for s, pos in led["positions"].items()}
        cash = led["cash"] if led["cash"] is not None else 0.0
        equity = cash + sum(pos["qty"] * prices[s] for s, pos in led["positions"].items())
        if equity <= 0:
            return []
        out = []
        for sym, pos in led["positions"].items():
            weight = pos["qty"] * prices[sym] / equity
            sid = signal_id_for(self.agent, f"trim|{tag}", sym, "sell", 0.0)
            out.append({"symbol": sym, **self.place(sym, "sell", weight * (1 - fraction), rationale, sid)})
        return out

    # ----------------------------------------------------------- helpers
    def _validate(self, symbol: str, side: str, w: float) -> str | None:
        if symbol not in self.allowed_symbols:
            return f"{symbol!r} is not in the configured universe"
        if side not in ("buy", "sell"):
            return "side must be 'buy' or 'sell' (long-only, never short)"
        if not isinstance(w, (int, float)) or not 0.0 <= float(w) <= 1.0:
            return "target_weight must be within [0, 1] of the agent sleeve"
        return None

    def _reconcile(self, led: dict) -> dict:
        """Clamp positions opened before today to what the broker actually
        holds — unfilled DAY entries and broker-side stop-outs disappear."""
        held = {s: float(v.get("qty", 0.0)) for s, v in self.broker.positions().items()}
        today = led["day"]["date"]
        out = {}
        for s, pos in led["positions"].items():
            qty = pos["qty"] if pos.get("opened", "") >= today else min(pos["qty"], held.get(s, 0.0))
            if qty < pos["qty"]:
                log.warning("agent ledger %s %.6f -> %.6f (broker holds %.6f)",
                            s, pos["qty"], qty, held.get(s, 0.0))
            if qty > 1e-9:
                out[s] = {**pos, "qty": qty}
        return {**led, "positions": out}

    def _book(self, led: dict, symbol: str, price: float) -> tuple[PortfolioState, dict]:
        """Sleeve PortfolioState marked to market, plus the ledger with its
        cash seed and day/week/peak baselines filled in (input not mutated)."""
        cash = led["cash"]
        if cash is None:
            equity, _ = self.broker.account_equity()
            cash = float(equity) * self.max_sleeve_pct
        prices = {s: self._mark(s, pos) for s, pos in led["positions"].items()}
        prices[symbol] = price
        positions = {s: Position(s, float(pos["qty"]), float(pos["entry"]), float(pos["stop"]),
                                 self.risk.sectors.get(s, "unknown"), f"agent:{self.agent}")
                     for s, pos in led["positions"].items()}
        equity = cash + sum(pos.qty * prices[s] for s, pos in positions.items())
        day_start = led["day"]["start_equity"] if led["day"]["start_equity"] is not None else equity
        week_start = led["week"]["start_equity"] if led["week"]["start_equity"] is not None else equity
        peak = max(led["peak_equity"], equity)
        new_led = {**led, "cash": cash, "peak_equity": peak,
                   "day": {**led["day"], "start_equity": day_start},
                   "week": {**led["week"], "start_equity": week_start}}
        return PortfolioState(equity=equity, cash=cash, positions=positions, peak_equity=peak,
                              day_start_equity=day_start, week_start_equity=week_start,
                              trades_today=led["day"]["orders"], prices=prices), new_led

    def _mark(self, symbol: str, pos: dict) -> float:
        df = self.get_bars(symbol)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return float(pos.get("price", pos["entry"]))

    def _emit(self, base: dict, status: str, reason: str | None) -> dict:
        event = {**base, "status": status, "reason": reason}
        log.info("agent order %s: %s %s %s", status, base.get("side"), base.get("symbol"), reason or "")
        self.on_event(event)
        return {"status": status, "reason": reason,
                **{k: event[k] for k in ("qty", "price", "stop", "final_weight") if k in event}}


def _after_fill(led: dict, intent: OrderIntent, qty: float) -> dict:
    """New ledger after a submitted order (never mutates the input)."""
    s, px = intent.symbol, intent.price
    cur = led["positions"].get(s)
    positions = dict(led["positions"])
    if intent.side == "buy":
        if cur is None:
            positions[s] = {"qty": qty, "entry": px, "stop": intent.stop_price, "price": px,
                            "opened": led["day"]["date"]}
        else:
            total = cur["qty"] + qty
            positions[s] = {**cur, "qty": total, "price": px,
                            "entry": (cur["entry"] * cur["qty"] + px * qty) / total,
                            "stop": max(cur["stop"], intent.stop_price or cur["stop"]),  # tighten only
                            "opened": led["day"]["date"]}
        cash = led["cash"] - qty * px
    else:
        remaining = (cur["qty"] if cur else 0.0) - qty
        if remaining <= 1e-9:
            positions.pop(s, None)
        else:
            positions[s] = {**cur, "qty": remaining, "price": px}
        cash = led["cash"] + qty * px
    return {**led, "cash": cash, "positions": positions,
            "day": {**led["day"], "orders": led["day"]["orders"] + 1}}


def signal_id_for(agent: str, invocation_id: str, symbol: str, side: str, weight: float) -> str:
    """Short (<48 chars, Alpaca client_order_id) and idempotent per
    invocation + order; a different weight is a different order."""
    key = f"{agent}|{invocation_id}|{symbol}|{side}|{weight:.4f}"
    return "ag-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def make_order_tool(executor: PaperExecutor) -> Callable[..., dict]:
    """Wrap an executor as an ADK function tool (EXECUTE_PAPER tier)."""

    def place_paper_order(symbol: str, side: str, target_weight: float, rationale: str,
                          tool_context: Any) -> dict:
        """Place an order on the PAPER trading account, autonomously.

        The order is checked by the risk manager, which may shrink or veto it.
        Long-only: 'sell' only reduces an existing holding. Repeating the exact
        same symbol/side/weight in one turn returns "duplicate"; after a veto,
        retry with a different (smaller) weight or stop.

        Args:
            symbol: Ticker from the configured universe, e.g. "AAPL".
            side: "buy" or "sell".
            target_weight: Desired final weight of this symbol in the agent
                sleeve, between 0 and 1 (0 with side "sell" closes it).
            rationale: One or two sentences explaining why.

        Returns:
            dict with status (submitted / vetoed / noop / deferred / blocked /
            duplicate / invalid) and reason.
        """
        inv = getattr(tool_context, "invocation_id", "") or utc_now().isoformat()
        sid = signal_id_for(executor.agent, inv, symbol, side, float(target_weight))
        return executor.place(symbol, side, float(target_weight), rationale, sid)

    guards.register_tool_tier(TOOL_NAME, guards.ToolTier.EXECUTE_PAPER)
    return place_paper_order


def executor_from_config(agent: str, broker, on_event: Callable[[dict], None] | None = None) -> PaperExecutor:
    from ..data.provider import DataProvider

    cfg = get_config()
    ex = cfg.agents.execution
    provider = DataProvider()
    sleeve_lock = cfg.resolve_path(ex.get("lock_file") or "state/agent_sleeve.lock")
    return PaperExecutor(
        agent=agent, broker=broker, risk=RiskManager(lock_file=sleeve_lock),
        main_lock=cfg.resolve_path(cfg.risk.lock_file),
        get_bars=lambda s: provider.get_bars(s, timeframe=cfg.data.timeframe, refresh=True),
        ledger_path=cfg.resolve_path(ex.ledger_file),
        max_sleeve_pct=ex.max_sleeve_pct, max_orders_per_day=ex.max_orders_per_day,
        on_event=on_event or _audit_and_alert,
    )


_MENTION = re.compile(r"[@#<>]|https?://\S+")


def _safe(text: object, limit: int = 200) -> str:
    """Model-written text headed for a chat webhook: no mentions/links/markup."""
    return _MENTION.sub("", str(text or ""))[:limit]


def _audit_and_alert(event: dict) -> None:
    """Default order-event sink: every attempt is audited (dashboard feed);
    submitted and vetoed orders also go to the alert webhook."""
    from ..monitor.alerts import AlertKind, AlertManager
    from .audit import AuditLog

    AuditLog.from_config().write({"kind": "order", **event})
    if event["status"] in ("submitted", "vetoed"):
        AlertManager().send(
            AlertKind.AGENT,
            f"agent {event['agent']} {event['status']} {event['side']} {event['symbol']}"
            f" -> {event['target_weight']:.0%} of sleeve: {_safe(event.get('reason') or event['rationale'])}",
            dedupe_key=f"agent-order:{event['signal_id']}",
        )
