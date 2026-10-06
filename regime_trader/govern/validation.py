"""Independent Validation Team — the system's internal auditor.

Its job is NOT to improve strategies; it is to find reasons to REJECT them.
It receives only (strategy, data) — never the optimization rationale — so it
cannot be talked into a pass. The numeric HARD GATES below are coded; the
Sonnet validation agent (if available) only writes the narrative and arbitrates
genuinely borderline overfitting calls.

Tiering (user choice): cheap daily gates on everything; the full mandatory
10-step pipeline (OOS, walk-forward, 10k Monte Carlo, slippage, cost, drawdown,
parameter sensitivity, stress, regime, risk metrics) runs in deep mode (weekly).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest.engine import WalkForwardEngine
from ..backtest.leaderboard import _validate as lookahead_stress_gate
from ..config import get_config
from ..monitor.log_setup import get_logger
from . import montecarlo, sweeps
from .metrics_ext import extended_metrics
from .stress_ext import run_full_stress

log = get_logger("governance")


@dataclass
class ValidationReport:
    strategy: str
    verdict: str                       # PASS | REJECTED
    deep: bool
    reasons: list[str] = field(default_factory=list)   # why rejected (if so)
    red_flags: list[str] = field(default_factory=list)  # overfitting signals
    borderline: bool = False           # within margin band -> governance escalates
    gates: dict = field(default_factory=dict)           # gate name -> bool
    metrics: dict = field(default_factory=dict)         # headline metrics
    blocks: dict = field(default_factory=dict)          # full pipeline detail

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy, "verdict": self.verdict, "deep": self.deep,
            "reasons": self.reasons, "red_flags": self.red_flags, "borderline": self.borderline,
            "gates": self.gates, "metrics": self.metrics, "blocks": self.blocks,
        }


def _seed_for(name: str, base: int) -> int:
    return base ^ (int.from_bytes(hashlib.md5(name.encode()).digest()[:4], "big") & 0x7FFFFFFF)


def _run(bars, strategy, symbols, oracle, start_ts=None) -> "object":
    return WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle, start_ts=start_ts).run()


def validate(strategy, bars, oracle, symbols, deep: bool = False,
             prior_sharpe: float | None = None) -> ValidationReport:
    """Run the validation pipeline; return a PASS/REJECTED report with detail."""
    cfg = get_config().governance
    th = cfg.thresholds
    symbols = [s for s in symbols if s in bars]
    rep = ValidationReport(strategy=strategy.name, verdict="PASS", deep=deep)

    # ---- baseline full run (always) ------------------------------------
    base = _run(bars, strategy, symbols, oracle)
    em = extended_metrics(base.equity, base.trades, base.weights,
                          ruin_drawdown=cfg.monte_carlo.ruin_drawdown)
    rep.metrics = em
    rep.blocks["regime_breakdown"] = base.per_regime.to_dict("records") if len(base.per_regime) else []
    rep.blocks["confidence_buckets"] = base.conf_buckets.to_dict("records") if len(base.conf_buckets) else []
    rep.blocks["drawdown_analysis"] = {
        "max_drawdown": em.get("max_drawdown"), "average_drawdown": em.get("average_drawdown"),
        "time_underwater": em.get("time_underwater"), "recovery_time_bars": em.get("recovery_time_bars"),
        "max_drawdown_duration_bars": em.get("max_drawdown_duration_bars"),
    }

    # ---- Monte Carlo (light daily, full deep) --------------------------
    mc = montecarlo.monte_carlo(
        base.equity, base.trades,
        n_sims=cfg.monte_carlo.n_sims if deep else max(1000, cfg.monte_carlo.n_sims // 10),
        methods=tuple(cfg.monte_carlo.methods), block=cfg.monte_carlo.block,
        ruin_drawdown=cfg.monte_carlo.ruin_drawdown, seed=_seed_for(strategy.name, cfg.monte_carlo.seed),
        initial_capital=get_config().backtest.initial_capital,
    )
    rep.blocks["monte_carlo"] = mc

    # ---- risk-metric floor gates (always) ------------------------------
    g = rep.gates
    g["sharpe_floor"] = (em.get("sharpe", 0.0) or 0.0) >= th.min_sharpe
    g["calmar_floor"] = (em.get("calmar", 0.0) or 0.0) >= th.min_calmar
    g["profit_factor_floor"] = (em.get("profit_factor", 0.0) or 0.0) >= th.min_profit_factor
    g["max_drawdown_ok"] = (em.get("max_drawdown", -1.0) or -1.0) >= th.max_drawdown
    g["ruin_ok"] = mc.get("prob_of_ruin", 1.0) <= cfg.monte_carlo.max_ruin_prob

    if deep:
        _deep_pipeline(rep, strategy, bars, oracle, symbols, base, cfg, th)

    _finalize(rep, cfg, th, prior_sharpe)
    log.info("validation %s: %s", strategy.name, rep.verdict,
             extra={"ctx": {"gates": rep.gates, "red_flags": rep.red_flags, "borderline": rep.borderline}})
    return rep


def _deep_pipeline(rep, strategy, bars, oracle, symbols, base, cfg, th) -> None:
    # 1. Out-of-sample split (train vs held-out test)
    tr0, tr1 = pd.Timestamp(cfg.oos_train[0], tz="UTC"), pd.Timestamp(cfg.oos_train[1], tz="UTC")
    te0, te1 = pd.Timestamp(cfg.oos_test[0], tz="UTC"), pd.Timestamp(cfg.oos_test[1], tz="UTC")
    try:
        train_bars = {s: d[d.index <= tr1] for s, d in bars.items() if s in symbols}
        train_oracle = oracle[oracle.index <= tr1] if oracle is not None else None
        test_bars = {s: d[d.index <= te1] for s, d in bars.items() if s in symbols}
        test_oracle = oracle[oracle.index <= te1] if oracle is not None else None
        tr_sharpe = float(_run(train_bars, strategy, symbols, train_oracle, start_ts=tr0).metrics.get("sharpe", 0.0))
        te_sharpe = float(_run(test_bars, strategy, symbols, test_oracle, start_ts=te0).metrics.get("sharpe", 0.0))
        degradation = (tr_sharpe - te_sharpe) / abs(tr_sharpe) if abs(tr_sharpe) > 1e-9 else 0.0
        rep.blocks["out_of_sample"] = {"train_sharpe": tr_sharpe, "test_sharpe": te_sharpe,
                                       "degradation": degradation}
        rep.gates["oos_sharpe_floor"] = te_sharpe >= cfg.min_oos_sharpe
        rep.gates["oos_degradation_ok"] = degradation <= cfg.max_oos_degradation
    except Exception as exc:
        rep.blocks["out_of_sample"] = {"error": str(exc)}
        rep.gates["oos_sharpe_floor"] = rep.gates["oos_degradation_ok"] = False

    # 2. Walk-forward stability: segment the equity, check consistency
    rep.blocks["walk_forward"] = _segment_stability(base.equity)
    rep.gates["walk_forward_stable"] = rep.blocks["walk_forward"]["stable"]

    # 3. (Monte Carlo already done at full n_sims in validate())
    # 4. Slippage sweep
    rep.blocks["slippage"] = sweeps.slippage_sweep(bars, strategy, symbols, oracle,
                                                   min_sharpe=cfg.slippage_min_sharpe)
    rep.gates["slippage_viable"] = rep.blocks["slippage"]["viable_at_high"]
    # 5. Transaction-cost model
    rep.blocks["transaction_cost"] = sweeps.cost_sweep(bars, strategy, symbols, oracle)
    rep.gates["net_profitable"] = (rep.blocks["transaction_cost"].get("net_total_return", 0.0) or 0.0) > 0
    # 7. Parameter sensitivity
    sens = sweeps.parameter_sensitivity(bars, strategy, symbols, oracle)
    rep.blocks["parameter_sensitivity"] = sens
    if sens.get("applicable"):
        rep.gates["sensitivity_ok"] = sens["degradation"] <= cfg.parameter_sensitivity.max_degradation
    # 8. Stress (real events + synthetic)
    rep.blocks["stress"] = run_full_stress(strategy, bars, oracle, symbols)
    rep.gates["stress_passed"] = rep.blocks["stress"]["passed"]
    # 9. Market-regime testing handled via base.per_regime (already attached)

    # look-ahead + crash structural gate (reuse the bracket's proven check)
    la = lookahead_stress_gate(strategy, bars, oracle, symbols)
    rep.blocks["lookahead"] = la
    rep.gates["lookahead_ok"] = bool(la.get("lookahead_ok"))


def _segment_stability(equity: pd.Series, n_segments: int = 4) -> dict:
    eq = equity.dropna()
    if len(eq) < n_segments * 5:
        return {"stable": True, "reason": "too short to segment", "segment_sharpes": []}
    segs = np.array_split(eq.to_numpy(), n_segments)
    sharpes = []
    for s in segs:
        r = pd.Series(s).pct_change().dropna()
        sharpes.append(float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0)
    pos = sum(1 for x in sharpes if x > 0)
    return {"stable": pos >= n_segments // 2, "segment_sharpes": sharpes,
            "positive_segments": pos, "n_segments": n_segments}


def _finalize(rep: ValidationReport, cfg, th, prior_sharpe: float | None) -> None:
    em = rep.metrics

    # ---- overfitting detection (red flags) -----------------------------
    flags = []
    oos = rep.blocks.get("out_of_sample", {})
    if oos.get("degradation", 0.0) > cfg.max_oos_degradation:
        flags.append(f"OOS Sharpe collapses ({oos.get('degradation'):.0%} degradation)")
    sens = rep.blocks.get("parameter_sensitivity", {})
    if sens.get("applicable") and sens.get("degradation", 0.0) > cfg.parameter_sensitivity.max_degradation:
        flags.append(f"excessive parameter sensitivity ({sens.get('degradation'):.0%})")
    if not rep.blocks.get("walk_forward", {}).get("stable", True):
        flags.append("unstable walk-forward behavior")
    if (em.get("sharpe", 0.0) or 0.0) > 5.0 or (em.get("profit_factor", 0.0) or 0.0) > 10.0:
        flags.append("unrealistic backtest metrics (Sharpe/PF implausibly high)")
    if prior_sharpe is not None and prior_sharpe - (em.get("sharpe", 0.0) or 0.0) > 0.75:
        flags.append("sharp decline vs previously validated Sharpe")
    rep.red_flags = flags

    # ---- hard gates -> REJECTED ----------------------------------------
    failed = [name for name, ok in rep.gates.items() if not ok]
    if failed:
        rep.verdict = "REJECTED"
        rep.reasons = [f"failed gate: {n}" for n in failed]
    if flags and rep.verdict == "PASS":
        # overfitting suspected even though numeric floors held -> reject to redesign
        rep.verdict = "REJECTED"
        rep.reasons = ["overfitting suspected: " + "; ".join(flags)]

    # ---- borderline detection (governance escalates these) -------------
    band = th.margin_band
    sharpe = em.get("sharpe", 0.0) or 0.0
    near = abs(sharpe - th.min_sharpe) <= band * max(abs(th.min_sharpe), 1.0)
    rep.borderline = bool(near or (0 < len(flags) <= 1))
