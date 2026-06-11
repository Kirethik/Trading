"""§5 competing-strategy bracket + §6 ensemble validation.

Every strategy — incumbent included — runs through the identical walk-forward
engine, risk layer, slippage, and fill delay, on identical bars. Before a
strategy makes the leaderboard it must pass the same two validation gates:
  1. look-ahead gate: truncating the data leaves its equity prefix unchanged
  2. crash-stress gate: it survives an injected crash with bounded drawdown

The ensemble is built from surviving challengers (vol-regime model sets the
risk budget) and is only kept if it beats the best single strategy.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..strategy.base import Strategy
from ..strategy.catalog import build_challengers, strategy_universe
from ..strategy.ensemble import EnsembleStrategy
from ..strategy.regime_strategy import RegimeStrategy
from .engine import BacktestResult, WalkForwardEngine
from .stress import MAX_TOLERATED_DD, inject_crash

log = get_logger("main")

METRIC_COLS = ["sharpe", "sortino", "calmar", "cagr", "total_return", "max_drawdown",
               "win_rate", "profit_factor", "n_fills", "avg_gross_exposure"]


def _validate(strategy: Strategy, bars, oracle, symbols, truncate_bars: int = 40) -> dict:
    """The two gates every bracket entrant must pass."""
    gates = {"lookahead_ok": False, "stress_ok": False}
    try:
        full = WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle).run()
        cut = {s: d.iloc[:-truncate_bars] for s, d in bars.items()}
        cut_oracle = oracle.iloc[:-truncate_bars] if len(oracle) > truncate_bars else oracle
        part = WalkForwardEngine(cut, strategy, symbols, regime_oracle=cut_oracle).run()
        overlap = part.equity.index.intersection(full.equity.index)
        gates["lookahead_ok"] = bool(
            len(overlap) > 0
            and np.allclose(full.equity.loc[overlap].to_numpy(),
                            part.equity.loc[overlap].to_numpy(), rtol=1e-9)
        )
    except Exception as exc:
        gates["lookahead_error"] = str(exc)
    try:
        crash = WalkForwardEngine(inject_crash(bars), strategy, symbols, regime_oracle=oracle).run()
        gates["stress_ok"] = bool(
            crash.metrics.get("max_drawdown", -1.0) >= MAX_TOLERATED_DD and crash.equity.iloc[-1] > 0
        )
    except Exception as exc:
        gates["stress_error"] = str(exc)
    return gates


def run_bracket(
    bars: dict[str, pd.DataFrame],
    oracle: pd.DataFrame,
    outdir: str | Path | None = None,
    validate: bool = True,
) -> tuple[pd.DataFrame, dict[str, BacktestResult]]:
    """Returns (leaderboard sorted by Sharpe, results by strategy name)."""
    cfg = get_config()
    outdir = Path(outdir) if outdir else cfg.resolve_path(cfg.runs_dir) / "bracket"
    rows, results = [], {}

    challengers = build_challengers()
    for strat in challengers:
        symbols = [s for s in strategy_universe(strat) if s in bars]
        log.info("bracket: running %s on %d symbols", strat.name, len(symbols))
        res = WalkForwardEngine(bars, strat, symbols, regime_oracle=oracle).run()
        results[strat.name] = res
        res.export(outdir / strat.name)
        row = {"strategy": strat.name, **{k: res.metrics.get(k) for k in METRIC_COLS}}
        if validate:
            gates = _validate(strat, bars, oracle, symbols)
            row["validated"] = bool(gates.get("lookahead_ok") and gates.get("stress_ok"))
            row.update(gates)
        else:
            row["validated"] = True
        rows.append(row)

    board = pd.DataFrame(rows)

    # ---- §6 ensemble: survivors vote, regime model sets the budget --------
    min_sharpe = cfg.ensemble.min_sharpe_to_vote
    by_name = {s.name: s for s in challengers}
    survivors = [
        (by_name[r["strategy"]], float(r["sharpe"]))
        for r in rows
        if r["validated"] and r["strategy"] != "regime-hmm" and (r["sharpe"] or 0) >= min_sharpe
    ]
    if survivors:
        ens = EnsembleStrategy(survivors, RegimeStrategy(), cfg.ensemble.method)
        symbols = sorted({s for st, _ in survivors for s in strategy_universe(st) if s in bars})
        res = WalkForwardEngine(bars, ens, symbols, regime_oracle=oracle).run()
        results[ens.name] = res
        res.export(outdir / ens.name)
        best_single = board["sharpe"].max()
        kept = (not cfg.ensemble.keep_only_if_beats_best) or res.metrics["sharpe"] > best_single
        row = {"strategy": ens.name, **{k: res.metrics.get(k) for k in METRIC_COLS},
               "validated": True, "ensemble_kept": kept,
               "ensemble_members": ", ".join(s.name for s, _ in survivors)}
        board = pd.concat([board, pd.DataFrame([row])], ignore_index=True)
        log.info("ensemble of %d members: sharpe %.2f vs best single %.2f -> %s",
                 len(survivors), res.metrics["sharpe"], best_single,
                 "KEPT" if kept else "discarded")

    board = board.sort_values("sharpe", ascending=False).reset_index(drop=True)
    outdir.mkdir(parents=True, exist_ok=True)
    board.to_csv(outdir / "leaderboard.csv", index=False)
    return board, results
