"""Multi-model ensemble: the regime model sets the risk budget, surviving
challengers vote on how to deploy it. The risk layer still vetoes the result.

The combination is itself validated in the walk-forward engine and is only
kept if it beats the best single strategy on risk-adjusted return (see
backtest/leaderboard.py); this module is just the combiner.
"""
from __future__ import annotations

from .base import Strategy, StrategyContext
from .regime_strategy import RegimeStrategy


class EnsembleStrategy(Strategy):
    name = "ensemble"

    def __init__(
        self,
        members: list[tuple[Strategy, float]],
        regime_strategy: RegimeStrategy | None = None,
        method: str = "rank_weighted",
    ):
        """members: (strategy, score) pairs — score is walk-forward Sharpe.
        Strategies that did not survive validation must not be passed in."""
        self.regime_strategy = regime_strategy or RegimeStrategy()
        self.method = method
        self.members = [(s, sc) for s, sc in members]
        self.vote_weights = self._vote_weights()

    def _vote_weights(self) -> dict[str, float]:
        if not self.members:
            return {}
        if self.method == "equal":
            raw = {s.name: 1.0 for s, _ in self.members}
        elif self.method == "confidence_weighted":
            raw = {s.name: max(0.0, sc) for s, sc in self.members}
        else:  # rank_weighted: worst rank weight 1, best rank weight N
            ranked = sorted(self.members, key=lambda t: t[1])
            raw = {s.name: float(i + 1) for i, (s, _) in enumerate(ranked)}
        total = sum(raw.values()) or 1.0
        return {k: v / total for k, v in raw.items()}

    def compute_weights(self, ctx: StrategyContext) -> dict[str, float]:
        # 1) risk budget from the volatility-regime model
        budget = self.regime_strategy.gross_exposure(ctx)
        if budget <= 0 or not self.members:
            return {}

        # 2) members vote on deployment
        combined: dict[str, float] = {}
        for strat, _ in self.members:
            w_vote = self.vote_weights.get(strat.name, 0.0)
            if w_vote <= 0:
                continue
            for sym, w in strat.target_weights(ctx).items():
                combined[sym] = combined.get(sym, 0.0) + w_vote * w

        gross = sum(combined.values())
        if gross <= 1e-9:
            return {}  # every member is in cash -> respect that

        # 3) deploy within the budget — never lever up a book the members
        #    themselves sized small (their cash votes are risk information),
        #    but always compress a book that exceeds the regime budget
        scale = 1.0 if gross <= budget else budget / gross
        return {s: w * scale for s, w in combined.items()}
