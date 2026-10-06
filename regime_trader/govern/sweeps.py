"""Robustness sweeps that re-run the walk-forward engine under perturbations.

- slippage_sweep: viability across low/med/high/extreme transaction friction.
- cost_sweep: full fee model -> gross vs net profit and fee impact.
- parameter_sensitivity: perturb a strategy's params to neighbors; a robust
  strategy degrades gracefully, an overfit one collapses.

All reuse `WalkForwardEngine`; nothing here mutates global config — the engine's
per-instance `.slippage`/`.commission` are overridden on the built object.
"""
from __future__ import annotations

import copy

import pandas as pd

from ..backtest.engine import WalkForwardEngine
from ..config import get_config

# tunable numeric params per strategy family (attribute name -> is_int).
# RegimeStrategy/BreadthOverlay are config/wrapper-driven -> no direct sweep.
PARAM_SPECS: dict[str, dict[str, bool]] = {
    "SMATrendFilter": {"window": True},
    "TimeSeriesMomentum": {"lookback": True, "skip": True},
    "CrossSectionalMomentum": {"top_k": True, "lookback": True},
    "DonchianBreakout": {"entry_window": True, "exit_window": True},
    "RSI2Pullback": {"entry_rsi": False, "exit_rsi": False},
    "BollingerReversion": {"window": True, "n_std": False},
    "VolTargeting": {"target_vol": False},
    "DualMomentum": {"lookback": True},
    "TAARotation": {"top_n": True, "trend_window": True},
}


def _sharpe(bars, strategy, symbols, oracle, slippage=None, commission=None) -> float:
    eng = WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle)
    if slippage is not None:
        eng.slippage = slippage
    if commission is not None:
        eng.commission = commission
    return float(eng.run().metrics.get("sharpe", 0.0))


def slippage_sweep(bars, strategy, symbols, oracle, tiers_bps=None, min_sharpe: float = 0.0) -> dict:
    cfg = get_config().governance
    tiers = tiers_bps or list(cfg.slippage_sweep_bps)
    labels = ["low", "medium", "high", "extreme"]
    results = {}
    for i, bps in enumerate(tiers):
        label = labels[i] if i < len(labels) else f"{bps}bps"
        results[label] = {"bps": bps, "sharpe": _sharpe(bars, strategy, symbols, oracle, slippage=bps / 10_000.0)}
    # viability judged at the HIGH tier (index 2 if present, else the last)
    high_key = labels[2] if len(tiers) > 2 else list(results)[-1]
    results["viable_at_high"] = bool(results[high_key]["sharpe"] >= min_sharpe)
    return results


def cost_sweep(bars, strategy, symbols, oracle) -> dict:
    """Gross (frictionless) vs net (full fee model) — and the fee impact."""
    c = get_config().governance.costs
    # fold the per-side fee model into an effective slippage (bps), plus broker flat
    eff_bps = float(c.exchange_fee_bps) + float(c.spread_bps) / 2.0 + float(c.regulatory_bps)
    gross_eng = WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle)
    gross_eng.slippage, gross_eng.commission = 0.0, 0.0
    gross = gross_eng.run().metrics

    net_eng = WalkForwardEngine(bars, strategy, symbols, regime_oracle=oracle)
    net_eng.slippage = eff_bps / 10_000.0
    net_eng.commission = float(c.broker_fee_per_share)  # flat per fill proxy
    net = net_eng.run().metrics

    return {
        "effective_cost_bps_per_side": eff_bps,
        "gross_total_return": gross.get("total_return"),
        "net_total_return": net.get("total_return"),
        "gross_sharpe": gross.get("sharpe"),
        "net_sharpe": net.get("sharpe"),
        "fee_impact_return": float((gross.get("total_return", 0.0) or 0.0) - (net.get("total_return", 0.0) or 0.0)),
    }


def tunable_params(strategy) -> dict[str, bool]:
    return PARAM_SPECS.get(type(strategy).__name__, {})


def _neighbor(strategy, attr: str, value, is_int: bool):
    clone = copy.deepcopy(strategy)
    new = max(1, int(round(value))) if is_int else float(value)
    setattr(clone, attr, new)
    return clone, new


def parameter_sensitivity(bars, strategy, symbols, oracle,
                          neighbors: int | None = None, step: float | None = None,
                          max_params: int = 2) -> dict:
    """Mean Sharpe degradation when params are nudged to neighbor values.

    Robust strategies hold; overfit ones fall off a cliff. Returns 'n/a' for
    config-driven strategies (RegimeStrategy) with no direct tunables.
    """
    g = get_config().governance.parameter_sensitivity
    neighbors = neighbors if neighbors is not None else g.neighbors
    step = step if step is not None else g.step
    spec = tunable_params(strategy)
    if not spec:
        return {"applicable": False, "reason": f"{type(strategy).__name__} has no direct tunable params"}

    center = _sharpe(bars, strategy, symbols, oracle)
    runs, neighbor_sharpes = [], []
    for attr, is_int in list(spec.items())[:max_params]:
        base = getattr(strategy, attr)
        for k in list(range(-neighbors, 0)) + list(range(1, neighbors + 1)):
            clone, new = _neighbor(strategy, attr, base * (1.0 + k * step), is_int)
            if new == base:
                continue
            sh = _sharpe(bars, clone, symbols, oracle)
            neighbor_sharpes.append(sh)
            runs.append({"param": attr, "value": new, "sharpe": sh})
    if not neighbor_sharpes:
        return {"applicable": False, "reason": "no distinct neighbors"}

    mean_neighbor = float(pd.Series(neighbor_sharpes).mean())
    degradation = float((center - mean_neighbor) / abs(center)) if abs(center) > 1e-9 else float("inf")
    return {
        "applicable": True,
        "center_sharpe": center,
        "mean_neighbor_sharpe": mean_neighbor,
        "degradation": degradation,            # 0 = identical; 1 = neighbors lose all edge
        "neighbors": runs,
    }
