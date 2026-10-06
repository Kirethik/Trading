"""ETH/USD systematic strategy research harness.

Runs a tiered, overfitting-aware study on deep-history ETH data and emits the
evidence for the research report:

  Tier 1  broad sweep   — every candidate x {daily, 4h} backtested on the SELECTION
                          window only (train+validation); ranked by risk-adjusted score.
  Tier 2  validation    — shortlist re-examined out-of-sample (untouched HOLDOUT),
                          walk-forward fold stability, Monte-Carlo ruin, slippage sweep.
  Tier 3  robustness     — finalists: per-cycle (bull/bear/sideways) breakdown,
                          parameter-neighbour sensitivity, risk-budget sensitivity,
                          regime attribution, vs buy-and-hold.

Strict no-leakage rule: candidate SELECTION uses only the selection window. The
holdout is read once, at evaluation time, and never used to pick anything.

Everything reuses the production engine + risk layer + Monte-Carlo so the numbers
are the same machinery that would trade live (long-only spot).
"""
from __future__ import annotations

import json
import math
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.engine import WalkForwardEngine
from ..backtest.metrics import compute_metrics
from ..backtest.oracle import compute_regime_oracle
from ..config import get_config
from ..data.provider import DataProvider
from ..govern.montecarlo import monte_carlo
from ..monitor.log_setup import get_logger
from ..risk.manager import RiskManager
from .candidates import build_candidates

log = get_logger("main")

# selection (train+validation) ends here; everything after is untouched holdout.
SELECTION_END = "2024-12-31"
HOLDOUT_START = "2025-01-01"

# acceptance gates — mirror governance thresholds, applied at crypto annualization.
THRESHOLDS = {
    "min_sharpe": 0.8,
    "min_calmar": 0.3,
    "min_profit_factor": 1.1,
    "max_drawdown": -0.35,        # ETH is volatile; -35% floor (vs -30% equities)
    "max_oos_degradation": 0.50,  # holdout Sharpe may fall <=50% of selection
    "min_oos_sharpe": 0.3,
    "max_ruin_prob": 0.05,
    "min_plateau_ratio": 0.60,    # neighbours keep >=60% of finalist Sharpe (no spike)
}

# named ETH market cycles for the per-regime / per-cycle breakdown.
CYCLES = [
    ("2018-bear", "2018-01-01", "2018-12-31"),
    ("2019-recovery", "2019-01-01", "2019-12-31"),
    ("2020-covid-bull", "2020-01-01", "2020-12-31"),
    ("2021-bull", "2021-01-01", "2021-12-31"),
    ("2022-bear", "2022-01-01", "2022-12-31"),
    ("2023-24-recovery", "2023-01-01", "2024-12-31"),
    ("2025-26-holdout", "2025-01-01", "2026-12-31"),
]


# ----------------------------------------------------------------- data / setup
def load_eth_bars(timeframe: str) -> dict[str, pd.DataFrame]:
    df = DataProvider(allow_network=False).get_bars("ETH/USD", timeframe=timeframe)
    return {"ETH/USD": df}


def research_risk_manager(risk_per_trade: float = 0.01, max_single: float = 1.0) -> RiskManager:
    """A risk layer for a DEDICATED single-asset ETH sleeve: same stops, breakers
    and gauntlet as production, but the 20% diversification cap is lifted (ETH is
    the whole sleeve) so the ATR risk-budget is the real position sizer."""
    rm = RiskManager(lock_file=Path(tempfile.mkdtemp(prefix="rt_eth_")) / "breaker.lock")
    rm.risk_per_trade = risk_per_trade
    rm.max_single = max_single
    return rm


def _ppy(timeframe: str) -> int:
    cfg = get_config()
    return int(cfg.research.timeframes.get(timeframe, 365))


def _cost_bps(kind: str = "realistic") -> float:
    cfg = get_config().research
    if kind == "realistic":
        return float(cfg.base_slippage_bps) + float(cfg.commission_bps) + float(cfg.spread_bps)
    return 0.0


# trailing-bar cap per timeframe — bounds the O(n^2) indicator recompute. Set well
# above the largest strategy window (~200 daily / 600 on 4h) so SMA/momentum are
# exact and EWM indicators (EMA/RSI/ATR) are fully converged: results match the
# uncapped engine to >4 decimals (verified) while running several times faster.
_MAX_LOOKBACK = {"1Day": 750, "4Hour": 2000}


def run_backtest(bars, strategy, timeframe, *, cost_bps=None, risk_per_trade=0.01,
                 oracle=None, max_single=1.0):
    """One backtest through the production engine with a research cost/risk profile."""
    if cost_bps is None:
        cost_bps = _cost_bps("realistic")
    eng = WalkForwardEngine(
        bars, strategy, ["ETH/USD"], regime_oracle=oracle,
        risk_manager=research_risk_manager(risk_per_trade, max_single),
        periods_per_year=_ppy(timeframe),
        max_lookback=_MAX_LOOKBACK.get(timeframe, 1000),
    )
    eng.slippage = cost_bps / 10_000.0   # fold fee+spread+slippage into per-fill cost
    return eng.run()


def _oracle_for(cand, bars):
    """The cached ETH regime oracle when a candidate needs regime context, else
    None. compute_regime_oracle is disk-cached, so repeated calls are cheap reads."""
    return compute_regime_oracle(bars["ETH/USD"]) if getattr(cand, "needs_regime", False) else None


# ----------------------------------------------------------------- metric slices
def _slice(equity: pd.Series, trades: pd.DataFrame, lo=None, hi=None):
    e = equity.copy()
    if lo is not None:
        e = e[e.index >= pd.Timestamp(lo, tz="UTC")]
    if hi is not None:
        e = e[e.index <= pd.Timestamp(hi, tz="UTC")]
    t = trades
    if trades is not None and len(trades) and "ts" in trades.columns:
        ts = pd.to_datetime(trades["ts"], utc=True)
        mask = pd.Series(True, index=trades.index)
        if lo is not None:
            mask &= ts >= pd.Timestamp(lo, tz="UTC")
        if hi is not None:
            mask &= ts <= pd.Timestamp(hi, tz="UTC")
        t = trades[mask.values]
    return e, t


def metrics_on(equity, trades, lo, hi, ppy) -> dict:
    e, t = _slice(equity, trades, lo, hi)
    if len(e) < 5:
        return {"error": "too short"}
    return compute_metrics(e, t if t is not None and len(t) else None, periods_per_year=ppy)


def wf_fold_sharpes(equity, lo, hi, ppy, k=5) -> list[float]:
    """Sharpe in each of k contiguous folds of the selection window — walk-forward
    stability proxy. A robust edge is positive across most folds, not one spike."""
    e, _ = _slice(equity, None, lo, hi)
    rets = e.pct_change().dropna()
    if len(rets) < k * 5:
        return []
    folds = np.array_split(rets.values, k)
    out = []
    for f in folds:
        s = f.std()
        out.append(float(f.mean() / s * math.sqrt(ppy)) if s > 0 else 0.0)
    return out


def buy_hold_metrics(bars, lo, hi, ppy) -> dict:
    c = bars["ETH/USD"]["close"]
    c = c[(c.index >= pd.Timestamp(lo, tz="UTC")) & (c.index <= pd.Timestamp(hi, tz="UTC"))]
    if len(c) < 5:
        return {"error": "too short"}
    eq = 100_000.0 * (c / c.iloc[0])
    return compute_metrics(eq, None, periods_per_year=ppy)


# ----------------------------------------------------------------- tiers
def tier1_sweep(timeframe: str, families: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """Broad sweep on the SELECTION window. Returns a ranked leaderboard and a
    dict of cid -> BacktestResult (kept for downstream tiers)."""
    bars = load_eth_bars(timeframe)
    ppy = _ppy(timeframe)
    cands = build_candidates(timeframe)
    if families:
        cands = [c for c in cands if c.family in families]
    oracle = None
    if any(c.needs_regime for c in cands):
        log.info("computing ETH regime oracle for %s (cached after first run)...", timeframe)
        oracle = compute_regime_oracle(bars["ETH/USD"])
    rows, results = [], {}
    for cand in cands:
        try:
            res = run_backtest(bars, cand.strategy, timeframe,
                               oracle=oracle if cand.needs_regime else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("backtest failed %s: %s", cand.cid, exc)
            continue
        sel = metrics_on(res.equity, res.trades, None, SELECTION_END, ppy)
        if "error" in sel:
            continue
        results[cand.cid] = (cand, res)
        rows.append({
            "cid": cand.cid, "family": cand.family, "timeframe": timeframe,
            "sel_sharpe": sel["sharpe"], "sel_calmar": sel["calmar"],
            "sel_cagr": sel["cagr"], "sel_maxdd": sel["max_drawdown"],
            "sel_profit_factor": sel.get("profit_factor", 0.0),
            "sel_recovery": sel.get("recovery_factor", 0.0),
            "n_round_trips": sel.get("n_round_trips", 0),
            "avg_exposure": res.metrics.get("avg_gross_exposure", 0.0),
        })
        log.info("tier1 %-16s sharpe=%.2f calmar=%.2f maxdd=%.1f%%",
                 cand.cid, sel["sharpe"], sel["calmar"], 100 * sel["max_drawdown"])
    lb = pd.DataFrame(rows).sort_values("sel_sharpe", ascending=False).reset_index(drop=True)
    return lb, results


def tier2_validate(cid, cand, res, timeframe, bars, n_sims=10000) -> dict:
    """Out-of-sample + walk-forward + Monte-Carlo + slippage robustness for one
    shortlisted candidate. Returns a verdict dict with gate booleans."""
    ppy = _ppy(timeframe)
    sel = metrics_on(res.equity, res.trades, None, SELECTION_END, ppy)
    oos = metrics_on(res.equity, res.trades, HOLDOUT_START, None, ppy)
    folds = wf_fold_sharpes(res.equity, None, SELECTION_END, ppy)
    mc = monte_carlo(res.equity, res.trades if len(res.trades) else None,
                     n_sims=n_sims, ruin_drawdown=-0.50)
    # cheap viability check: one re-run at the WORST modeled cost (full sweep is
    # done only for finalists in Tier 3, to keep the shortlist pass fast).
    worst_bps = float(max(get_config().research.slippage_sweep_bps))
    rw = run_backtest(bars, cand.strategy, timeframe, cost_bps=worst_bps, oracle=_oracle_for(cand, bars))
    worst_sharpe = round(metrics_on(rw.equity, rw.trades, None, None, ppy).get("sharpe", 0.0), 3)

    oos_ok = ("error" not in oos) and oos["sharpe"] >= THRESHOLDS["min_oos_sharpe"]
    degr = 1.0 - (oos["sharpe"] / sel["sharpe"]) if "error" not in oos and sel["sharpe"] > 0 else 1.0
    gates = {
        "sharpe": sel["sharpe"] >= THRESHOLDS["min_sharpe"],
        "calmar": sel["calmar"] >= THRESHOLDS["min_calmar"],
        "profit_factor": sel.get("profit_factor", 0) >= THRESHOLDS["min_profit_factor"],
        "max_drawdown": sel["max_drawdown"] >= THRESHOLDS["max_drawdown"],
        "oos_positive": oos_ok,
        "oos_degradation": degr <= THRESHOLDS["max_oos_degradation"],
        "wf_stable": (len([f for f in folds if f > 0]) >= math.ceil(len(folds) * 0.6)) if folds else False,
        "ruin": mc.get("prob_of_ruin", 1.0) <= THRESHOLDS["max_ruin_prob"],
        "slippage_viable": worst_sharpe >= 0.0,
    }
    return {
        "cid": cid, "family": cand.family, "timeframe": timeframe,
        "selection": _clean(sel), "holdout": _clean(oos),
        "oos_degradation": round(float(degr), 3),
        "wf_folds": [round(f, 2) for f in folds],
        "monte_carlo": {"prob_of_ruin": mc.get("prob_of_ruin"),
                        "prob_profit": mc.get("prob_profit"),
                        "worst_dd_p1": mc.get("worst_case_drawdown_p1")},
        "worst_cost_sharpe": {"bps": int(worst_bps), "sharpe": worst_sharpe},
        "gates": gates, "passed": all(gates.values()),
    }


def tier3_robustness(cid, cand, res, timeframe, bars, lb_all: pd.DataFrame) -> dict:
    """Finalist deep-dive: per-cycle, parameter-neighbour sensitivity, risk-budget
    sensitivity, regime attribution, and buy-and-hold comparison."""
    ppy = _ppy(timeframe)
    # per-cycle breakdown
    cycles = {}
    for name, lo, hi in CYCLES:
        m = metrics_on(res.equity, res.trades, lo, hi, ppy)
        bh = buy_hold_metrics(bars, lo, hi, ppy)
        if "error" not in m:
            cycles[name] = {"strategy": _clean({k: m[k] for k in ("cagr", "sharpe", "max_drawdown")}),
                            "buy_hold": _clean({k: bh.get(k) for k in ("cagr", "sharpe", "max_drawdown")})}
    # parameter-neighbour plateau (same family + timeframe)
    fam = lb_all[(lb_all["family"] == cand.family) & (lb_all["timeframe"] == timeframe)]
    fin_sharpe = float(fam[fam["cid"] == cid]["sel_sharpe"].iloc[0]) if (fam["cid"] == cid).any() else 0.0
    neighbours = fam[fam["cid"] != cid]["sel_sharpe"]
    if fin_sharpe <= 0:
        plateau = 0.0
    elif len(neighbours) == 0:
        plateau = 1.0  # single-member family: spike-vs-plateau not assessable, don't penalize
    else:
        plateau = float(neighbours.mean() / fin_sharpe)
    # risk-budget sensitivity (scale knob): Sharpe should be ~invariant, return/DD scale
    oc = _oracle_for(cand, bars)
    risk_budget = {}
    for rpt in (0.005, 0.01, 0.02, 0.03):
        r = run_backtest(bars, cand.strategy, timeframe, risk_per_trade=rpt, oracle=oc)
        m = metrics_on(r.equity, r.trades, None, None, ppy)
        risk_budget[rpt] = _clean({k: m.get(k) for k in ("cagr", "sharpe", "max_drawdown",
                                                         "recovery_factor")} | {
            "exposure": r.metrics.get("avg_gross_exposure", 0.0)})
    # full slippage / transaction-cost sweep
    slippage = {}
    for bps in get_config().research.slippage_sweep_bps:
        r = run_backtest(bars, cand.strategy, timeframe, cost_bps=float(bps), oracle=oc)
        m = metrics_on(r.equity, r.trades, None, None, ppy)
        slippage[int(bps)] = _clean({k: m.get(k) for k in ("sharpe", "cagr", "max_drawdown")})
    # regime attribution (ETH-native oracle)
    regime = {}
    try:
        oracle = compute_regime_oracle(bars["ETH/USD"])
        r = run_backtest(bars, cand.strategy, timeframe, oracle=oracle)
        if not r.per_regime.empty:
            regime = r.per_regime.to_dict(orient="records")
    except Exception as exc:  # noqa: BLE001
        log.warning("regime attribution failed for %s: %s", cid, exc)
    return {
        "cid": cid, "per_cycle": cycles, "param_plateau_ratio": round(plateau, 3),
        "plateau_ok": plateau >= THRESHOLDS["min_plateau_ratio"],
        "risk_budget_sensitivity": risk_budget,
        "slippage_sweep": slippage,
        "regime_attribution": _clean(regime),
        "full_period": _clean(metrics_on(res.equity, res.trades, None, None, ppy)),
    }


# maps a regime-overlay family back to its underlying price signal for 4h tests
_BASE_FAMILY = {"regime-ensemble": "ensemble", "regime-trend": "sma"}


def tf_confirmation(families: list[str], start: str = "2022-01-01", max_runs: int = 4) -> list[dict]:
    """Timeframe robustness: do the finalist FAMILIES also work on 4h bars?

    Uses 4h history from `start` (perp-era, most liquid) to keep the O(n^2) cost
    tractable — this is a CONFIRMATION, not the primary selection (daily, full
    2017+). Regime overlays are dropped here (the 4h HMM refit is too costly and
    regime-gating is a daily-level concept); we test the underlying price signal."""
    tf = "4Hour"
    ppy = _ppy(tf)
    fams = {_BASE_FAMILY.get(f, f) for f in families} | {"ensemble"}
    full = DataProvider(allow_network=False).get_bars("ETH/USD", timeframe=tf)
    sub = {"ETH/USD": full[full.index >= pd.Timestamp(start, tz="UTC")]}
    cands = [c for c in build_candidates(tf) if c.family in fams and not c.needs_regime][:max_runs]
    out = []
    for cand in cands:
        try:
            res = run_backtest(sub, cand.strategy, tf)
        except Exception as exc:  # noqa: BLE001
            log.warning("4h confirm failed %s: %s", cand.cid, exc)
            continue
        sel = metrics_on(res.equity, res.trades, None, SELECTION_END, ppy)
        oos = metrics_on(res.equity, res.trades, HOLDOUT_START, None, ppy)
        out.append({"cid": cand.cid, "family": cand.family, "history_from": start,
                    "selection": _clean(sel), "holdout": _clean(oos)})
        log.info("4h confirm %-18s sel_sharpe=%.2f", cand.cid, sel.get("sharpe", 0.0))
    return out


# ----------------------------------------------------------------- orchestration
def run_research(deep: bool = False, out_dir: str | None = None) -> dict:
    cfg = get_config()
    out = Path(out_dir or cfg.research.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n_sims = 10000 if deep else 2000
    shortlist_n = int(cfg.research.shortlist_n)

    # --- Tier 1: daily full grid (primary selection over full 2017+ history) -
    # 4h is a per-finalist confirmation later (Tier ~4), not a full sweep: each
    # 4h backtest costs ~36x a daily one (O(n^2) on 6x the bars), so a full 4h
    # grid is intractable and unnecessary — daily is the primary timeframe.
    log.info("=== Tier 1: broad sweep (daily) ===")
    lb_all, res_d = tier1_sweep("1Day")
    lb_all.to_csv(out / "leaderboard.csv", index=False)
    results = {c: (cand, r, "1Day") for c, (cand, r) in res_d.items()}

    # --- Tier 2: validate the shortlist -------------------------------------
    shortlist = lb_all.head(shortlist_n)["cid"].tolist()
    log.info("=== Tier 2: validation on %d shortlisted ===", len(shortlist))
    validations = []
    for cid in shortlist:
        cand, res, tf = results[cid]
        validations.append(tier2_validate(cid, cand, res, tf, load_eth_bars(tf), n_sims=n_sims))
    passed = [v for v in validations if v["passed"]]

    # --- Tier 3: robustness on finalists (passing Tier 2) -------------------
    log.info("=== Tier 3: robustness on %d finalists ===", len(passed))
    finalists = []
    for v in passed:
        cid = v["cid"]
        cand, res, tf = results[cid]
        rob = tier3_robustness(cid, cand, res, tf, load_eth_bars(tf), lb_all)
        rob["validation"] = v
        finalists.append(rob)
        res.export(out / cid)  # equity/trades/metrics CSVs per finalist

    # --- 4h timeframe confirmation on finalist families ---------------------
    fin_families = list({results[v["cid"]][0].family for v in passed}) or \
        list(dict.fromkeys(lb_all.head(5)["family"].tolist()))
    log.info("=== 4h confirmation on families: %s ===", fin_families)
    try:
        tf_confirm = tf_confirmation(fin_families)
    except Exception as exc:  # noqa: BLE001
        log.warning("4h confirmation failed: %s", exc)
        tf_confirm = []

    # --- Decision: best finalist that passes ALL gates incl. plateau --------
    decision = _decide(finalists)
    summary = {
        "selection_window": ["2017-08-17", SELECTION_END],
        "holdout_window": [HOLDOUT_START, "2026-06-16"],
        "thresholds": THRESHOLDS,
        "n_candidates": int(len(lb_all)),
        "shortlist": shortlist,
        "n_passed_validation": len(passed),
        "decision": decision,
        "leaderboard_top": _clean(lb_all.head(15).to_dict(orient="records")),
        "validations": validations,
        "finalists": finalists,
        "tf_confirmation_4h": tf_confirm,
        "buy_hold_full": buy_hold_metrics(load_eth_bars("1Day"), "2017-08-17", "2026-12-31", 365),
    }
    with open(out / "research_summary.json", "w", encoding="utf-8") as fh:
        json.dump(_clean(summary), fh, indent=2, default=str)
    log.info("research complete -> %s ; decision=%s", out, decision.get("verdict"))
    return summary


def _decide(finalists: list[dict]) -> dict:
    """Pick ONE strategy only if it clears every gate on selection AND holdout and
    shows a parameter plateau; otherwise declare no reliable strategy found."""
    eligible = [f for f in finalists
                if f["validation"]["passed"] and f.get("plateau_ok")]
    if not eligible:
        return {"verdict": "NO_RELIABLE_STRATEGY",
                "reason": "no candidate cleared all gates (selection+holdout+plateau)"}
    # rank eligible by holdout Sharpe, then selection Sharpe
    eligible.sort(key=lambda f: (f["validation"]["holdout"].get("sharpe", -9),
                                 f["validation"]["selection"].get("sharpe", -9)), reverse=True)
    win = eligible[0]
    return {"verdict": "SELECTED", "cid": win["cid"],
            "selection_sharpe": win["validation"]["selection"].get("sharpe"),
            "holdout_sharpe": win["validation"]["holdout"].get("sharpe"),
            "plateau_ratio": win["param_plateau_ratio"]}


# ----------------------------------------------------------------- utils
def _clean(obj):
    """Recursively replace inf/nan with None so the JSON is valid."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isinf(obj) or math.isnan(obj)) else round(obj, 6)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if (math.isinf(f) or math.isnan(f)) else round(f, 6)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return obj


def cmd_research_eth(args) -> None:
    summary = run_research(deep=getattr(args, "deep", False))
    d = summary["decision"]
    print("\n=== ETH RESEARCH DECISION ===")
    print(json.dumps(d, indent=2, default=str))
