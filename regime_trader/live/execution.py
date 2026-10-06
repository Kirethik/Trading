"""Order mechanics shared by the regime orchestrator and the agent order tool.

Pure sizing/stop helpers plus the single broker submission path, so every
order — model-driven or agent-driven — is sized and sent the same way AFTER
it has passed RiskManager.evaluate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import features as F
from ..data.provider import is_crypto
from ..risk.manager import PortfolioState, RiskDecision, RiskManager


def protective_stop_for(risk: RiskManager, df: pd.DataFrame | None, price: float,
                        atr_window: int) -> float | None:
    """ATR stop for a buy, or None when there is not enough history (the risk
    layer then refuses the buy — no stop, no entry)."""
    if df is None or len(df) <= atr_window + 2:
        return None
    a = F.atr(df, atr_window).iloc[-1]
    return risk.protective_stop(price, float(a)) if np.isfinite(a) else None


def is_actionable(d: RiskDecision, threshold: float) -> bool:
    """The risk layer may scale a buy to a hold (delta ~ 0) or a sell to a
    hold; never let the sign disagree with the action we actually take."""
    delta = d.final_weight - d.intent.current_weight
    if d.intent.side == "buy":
        return delta > threshold * 1e-3
    return delta < -threshold * 1e-3


def broker_qty(d: RiskDecision, p: PortfolioState) -> float:
    """Quantity the BROKER will actually transact: stocks floor to whole
    shares, crypto is fractional, sells are capped at the held quantity —
    so the locally tracked book matches broker reconciliation."""
    s = d.intent.symbol
    raw = abs(d.final_weight - d.intent.current_weight) * p.equity / d.intent.price
    if d.intent.side == "buy":
        return round(raw, 6) if is_crypto(s) else float(int(raw))
    pos = p.positions.get(s)
    cap = pos.qty if pos else 0.0
    return min(raw if is_crypto(s) else float(int(raw)), cap)


def submit(broker, d: RiskDecision, qty: float, limit_offset_bps: float):
    """Send one risk-approved order. Returns (order | None, transacted_qty).
    Buys are limit (+offset) brackets carrying the risk-layer stop."""
    s = d.intent.symbol
    if d.intent.side == "buy":
        offset = limit_offset_bps / 10_000.0
        order = broker.submit_entry(s, qty, d.intent.price * (1 + offset), d.intent.stop_price,
                                    signal_id=d.intent.signal_id)
        if order is not None and getattr(order, "qty", None):
            qty = float(order.qty)  # authoritative filled size
        return order, qty
    return broker.submit_exit(s, qty, signal_id=d.intent.signal_id), qty
