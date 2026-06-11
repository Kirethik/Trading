"""Recoverable session state: kill the process, restart, carry on.

Everything needed to resume without double-entering positions is persisted
atomically after every cycle: tracked positions (with stops), equity
watermarks for the breakers, the stability tracker, and pending signal ids.
"""
from __future__ import annotations

from pathlib import Path

from ..config import get_config
from ..regime.stability import StabilityTracker
from ..risk.manager import PortfolioState, Position
from ..utils import atomic_write_json, load_json, utc_now_iso


class SessionState:
    def __init__(self, path: str | Path | None = None):
        cfg = get_config()
        self.path = Path(path) if path else cfg.resolve_path(cfg.state_dir) / "session.json"
        self.data: dict = {
            "positions": {},          # symbol -> Position fields
            "peak_equity": 0.0,
            "day_start_equity": 0.0,
            "week_start_equity": 0.0,
            "day_key": "",
            "week_key": "",
            "trades_today": 0,
            "stability": {},
            "last_regime": {},
            "last_cycle": "",
            "submitted_signal_ids": [],   # idempotency guard against double entry
        }

    # ------------------------------------------------------------ lifecycle
    def load(self) -> bool:
        existing = load_json(self.path)
        if existing:
            self.data.update(existing)
            return True
        return False

    def save(self) -> None:
        self.data["last_cycle"] = utc_now_iso()
        atomic_write_json(self.path, self.data)

    # ------------------------------------------------------------ positions
    def positions(self) -> dict[str, Position]:
        out = {}
        for sym, d in self.data["positions"].items():
            out[sym] = Position(
                symbol=sym, qty=d["qty"], entry_price=d["entry_price"],
                stop_price=d["stop_price"], sector=d.get("sector", "unknown"),
                strategy=d.get("strategy", "regime-hmm"), signal_id=d.get("signal_id", ""),
            )
        return out

    def set_positions(self, positions: dict[str, Position]) -> None:
        self.data["positions"] = {
            s: {
                "qty": p.qty, "entry_price": p.entry_price, "stop_price": p.stop_price,
                "sector": p.sector, "strategy": p.strategy, "signal_id": p.signal_id,
            }
            for s, p in positions.items()
        }

    # ------------------------------------------------------- watermark sync
    def roll_watermarks(self, equity: float, now) -> None:
        """Update day/week anchors and the all-time peak from actual equity."""
        day_key = now.strftime("%Y-%m-%d")
        week_key = "-".join(map(str, now.isocalendar()[:2]))
        if self.data["day_key"] != day_key:
            self.data["day_key"] = day_key
            self.data["day_start_equity"] = equity
            self.data["trades_today"] = 0
        if self.data["week_key"] != week_key:
            self.data["week_key"] = week_key
            self.data["week_start_equity"] = equity
        self.data["peak_equity"] = max(self.data.get("peak_equity", 0.0), equity)

    def portfolio_state(self, equity: float, cash: float, prices: dict[str, float]) -> PortfolioState:
        return PortfolioState(
            equity=equity, cash=cash, positions=self.positions(),
            peak_equity=self.data["peak_equity"],
            day_start_equity=self.data["day_start_equity"] or equity,
            week_start_equity=self.data["week_start_equity"] or equity,
            trades_today=self.data.get("trades_today", 0),
            prices=prices,
        )

    # ----------------------------------------------------------- idempotency
    def already_submitted(self, signal_id: str) -> bool:
        return signal_id in self.data["submitted_signal_ids"]

    def mark_submitted(self, signal_id: str) -> None:
        ids = self.data["submitted_signal_ids"]
        ids.append(signal_id)
        del ids[:-500]

    # ------------------------------------------------------------- stability
    def stability_tracker(self) -> StabilityTracker:
        cfg = get_config().regime
        d = self.data.get("stability") or {}
        return StabilityTracker.from_dict(
            d, cfg.confirm_bars, cfg.flicker_window, cfg.flicker_max_switches
        )

    def save_stability(self, tracker: StabilityTracker) -> None:
        self.data["stability"] = tracker.to_dict()
