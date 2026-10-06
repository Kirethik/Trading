"""Render the governance run: per-strategy markdown + master leaderboard +
day's decision slice + summary, under runs/governance/<date>/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

console = Console()

_PCT = {"total_return", "cagr", "max_drawdown", "ann_vol", "win_rate", "average_drawdown",
        "time_underwater", "prob_of_ruin"}


def _fmt(k, v):
    if isinstance(v, float):
        return f"{v:.2%}" if k in _PCT else f"{v:.3f}"
    return str(v)


def _metric_lines(metrics: dict) -> str:
    order = ["total_return", "cagr", "sharpe", "sortino", "calmar", "recovery_factor",
             "max_drawdown", "average_drawdown", "max_drawdown_duration_bars", "recovery_time_bars",
             "time_underwater", "ann_vol", "win_rate", "profit_factor", "expectancy", "exposure", "bars"]
    rows = [f"| {k} | {_fmt(k, metrics[k])} |" for k in order if k in metrics]
    return "| metric | value |\n|---|---|\n" + "\n".join(rows)


def strategy_markdown(key, rep, decision, entry) -> str:
    b = rep.blocks
    md = [f"# {key}", "", f"- **source:** {entry.get('source')}  ",
          f"- **market:** {entry.get('market')}  ",
          f"- **validation verdict:** **{rep.verdict}**{' (borderline)' if rep.borderline else ''}  ",
          f"- **governance decision:** **{decision.decision}**"
          f"{' [decided by the Judge agent]' if decision.escalated else ''}  ",
          f"- **allocation:** ${decision.allocation:,.0f}  ",
          f"- **tier:** {'DEEP' if rep.deep else 'cheap-daily'}  ", ""]
    if rep.reasons:
        md += ["## Rejection reasons", *[f"- {r}" for r in rep.reasons], ""]
    if rep.red_flags:
        md += ["## Overfitting red flags", *[f"- {f}" for f in rep.red_flags], ""]
    md += ["## Gates", "| gate | pass |", "|---|---|",
           *[f"| {g} | {'✅' if ok else '❌'} |" for g, ok in rep.gates.items()], ""]
    md += ["## Risk metrics (full set)", _metric_lines(rep.metrics), ""]
    if b.get("monte_carlo"):
        mc = b["monte_carlo"]
        md += ["## Monte Carlo robustness",
               f"- simulations: {mc.get('n_sims')}  ",
               f"- **probability of ruin:** {mc.get('prob_of_ruin', 0):.2%}  ",
               f"- prob. profitable: {mc.get('prob_profit', 0):.2%}  ",
               f"- worst-case return (p1): {mc.get('worst_case_return_p1', 0):.2%}  ",
               f"- worst-case drawdown (p1): {mc.get('worst_case_drawdown_p1', 0):.2%}  ", ""]
    if b.get("out_of_sample"):
        o = b["out_of_sample"]
        md += ["## Out-of-sample (held-out test)",
               f"- train Sharpe: {o.get('train_sharpe')}  ",
               f"- test Sharpe: {o.get('test_sharpe')}  ",
               f"- degradation: {o.get('degradation')}  ", ""]
    if b.get("slippage"):
        md += ["## Slippage sweep",
               *[f"- {k}: Sharpe {v['sharpe']:.2f} @ {v['bps']}bps"
                 for k, v in b["slippage"].items() if isinstance(v, dict)], ""]
    if b.get("transaction_cost"):
        t = b["transaction_cost"]
        md += ["## Transaction costs",
               f"- gross return: {t.get('gross_total_return', 0):.2%} | net: {t.get('net_total_return', 0):.2%} "
               f"| fee impact: {t.get('fee_impact_return', 0):.2%}", ""]
    if b.get("parameter_sensitivity", {}).get("applicable"):
        s = b["parameter_sensitivity"]
        md += ["## Parameter sensitivity",
               f"- center Sharpe {s['center_sharpe']:.2f}, mean neighbor {s['mean_neighbor_sharpe']:.2f}, "
               f"degradation {s['degradation']:.0%}", ""]
    if b.get("stress"):
        s = b["stress"]
        md += ["## Stress testing",
               f"- synthetic passed: {s.get('synthetic_passed')} | events passed: {s.get('events_passed')}",
               *[f"  - {n}: {v}" for n, v in (s.get("events") or {}).items()], ""]
    if b.get("regime_breakdown"):
        md += ["## Per-regime performance",
               "| regime | bars | ann_return | sharpe |", "|---|---|---|---|",
               *[f"| {r.get('regime')} | {r.get('bars')} | {r.get('ann_return', 0):.2%} | {r.get('sharpe', 0):.2f} |"
                 for r in b["regime_breakdown"]], ""]
    md += ["## Governance rationale", decision.rationale or "—", ""]
    return "\n".join(md)


def master_frame(reports, decisions, registry) -> pd.DataFrame:
    rows = []
    for key, rep in reports.items():
        d = decisions[key]
        m = rep.metrics
        rows.append({
            "strategy": key, "status": registry.get(key).get("status"),
            "decision": d.decision, "allocation": round(d.allocation, 0), "escalated": d.escalated,
            "verdict": rep.verdict, "borderline": rep.borderline,
            "sharpe": round(m.get("sharpe", 0) or 0, 3), "calmar": round(m.get("calmar", 0) or 0, 3),
            "max_drawdown": round(m.get("max_drawdown", 0) or 0, 4),
            "prob_of_ruin": round(rep.blocks.get("monte_carlo", {}).get("prob_of_ruin", 0), 4),
            "red_flags": len(rep.red_flags),
        })
    df = pd.DataFrame(rows)
    return df.sort_values(["allocation", "sharpe"], ascending=False).reset_index(drop=True) if len(df) else df


def write_run(outdir, reports, decisions, registry, summary_meta) -> Path:
    outdir = Path(outdir)
    (outdir / "strategies").mkdir(parents=True, exist_ok=True)
    for key, rep in reports.items():
        (outdir / "strategies" / f"{key}.md").write_text(
            strategy_markdown(key, rep, decisions[key], registry.get(key)), encoding="utf-8")
        # structured twin of the markdown, so the web dashboard can graph the
        # validation blocks (Monte Carlo, per-regime, slippage, sensitivity)
        # without parsing prose. Decision + registry standing are folded in.
        d = decisions[key]
        payload = {**rep.to_dict(), "key": key,
                   "decision": d.decision, "allocation": d.allocation,
                   "escalated": d.escalated, "rationale": d.rationale,
                   "registry": registry.get(key)}
        with open(outdir / "strategies" / f"{key}.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)

    board = master_frame(reports, decisions, registry)
    board.to_csv(outdir / "master_leaderboard.csv", index=False)
    (outdir / "master_leaderboard.md").write_text(
        "# Governance Master Leaderboard\n\n" + (board.to_markdown(index=False) if len(board) else "_empty_"),
        encoding="utf-8")

    with open(outdir / "governance_decisions.json", "w", encoding="utf-8") as fh:
        json.dump([d.to_dict() for d in decisions.values()], fh, indent=2, default=str)

    summary = ["# Governance Daily Summary", "",
               *[f"- **{k}:** {v}" for k, v in summary_meta.items()], "",
               f"- strategies evaluated: {len(reports)}",
               f"- PASS: {sum(1 for r in reports.values() if r.passed)} | "
               f"REJECTED: {sum(1 for r in reports.values() if not r.passed)}", ""]
    (outdir / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    return outdir


def print_console(board: pd.DataFrame, title: str) -> None:
    if board is None or len(board) == 0:
        console.print(f"[dim]{title}: empty[/dim]")
        return
    t = Table(title=title, header_style="bold")
    for col in ["strategy", "status", "decision", "allocation", "verdict", "sharpe", "max_drawdown", "prob_of_ruin"]:
        if col in board.columns:
            t.add_column(col, justify="right" if col not in ("strategy", "status", "decision", "verdict") else "left")
    for _, r in board.head(25).iterrows():
        t.add_row(str(r["strategy"]), str(r.get("status", "")), str(r["decision"]),
                  f"${r['allocation']:,.0f}", str(r["verdict"]),
                  f"{r['sharpe']:.2f}", f"{r['max_drawdown']:.1%}", f"{r['prob_of_ruin']:.1%}")
    console.print(t)
