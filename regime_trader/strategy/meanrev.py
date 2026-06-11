"""Mean-reversion challengers — buy weakness inside an intact uptrend."""
from __future__ import annotations

from ..data import features as F
from .base import Strategy, StrategyContext, in_position_last_signal


class RSI2Pullback(Strategy):
    """Connors-style: long when RSI(2) dips below threshold while price holds
    above the 200-SMA; exit when RSI(2) recovers or the trend breaks."""

    name = "rsi2-pullback"

    def __init__(self, entry_rsi: float = 10.0, exit_rsi: float = 70.0, trend_window: int = 200):
        self.entry_rsi, self.exit_rsi, self.trend_window = entry_rsi, exit_rsi, trend_window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        open_pos = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.trend_window + 5:
                continue
            r = F.rsi(c, 2)
            trend_ok = c > F.sma(c, self.trend_window)
            entry = (r < self.entry_rsi) & trend_ok
            exit_ = (r > self.exit_rsi) | ~trend_ok
            if in_position_last_signal(entry, exit_):
                open_pos.append(s)
        return {s: 1.0 / max(1, len(ctx.symbols)) for s in open_pos}


class BollingerReversion(Strategy):
    """Long a close below the lower band (trend intact); exit at the middle band."""

    name = "bollinger-reversion"

    def __init__(self, window: int = 20, n_std: float = 2.0, trend_window: int = 200):
        self.window, self.n_std, self.trend_window = window, n_std, trend_window

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        open_pos = []
        for s in ctx.symbols:
            c = ctx.closes.get(s)
            if c is None:
                continue
            c = c.dropna()
            if len(c) < self.trend_window + 5:
                continue
            bb = F.bollinger(c, self.window, self.n_std)
            trend_ok = c > F.sma(c, self.trend_window)
            entry = (c < bb["lower"]) & trend_ok
            exit_ = (c >= bb["mid"]) | ~trend_ok
            if in_position_last_signal(entry, exit_):
                open_pos.append(s)
        return {s: 1.0 / max(1, len(ctx.symbols)) for s in open_pos}
