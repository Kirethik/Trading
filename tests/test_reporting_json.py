"""write_run() must also emit a structured strategies/<key>.json per strategy,
matching ValidationReport.to_dict() plus the governance decision + registry
standing — this is what the web dashboard graphs.
"""
from __future__ import annotations

import json

import pandas as pd

from regime_trader.govern.governance import GovernanceDecision
from regime_trader.govern.reporting import write_run
from regime_trader.govern.validation import ValidationReport


def _registry():
    return {
        "regime-hmm": {"source": "core", "market": "equities", "status": "deployed",
                       "allocation": 5000.0, "last_validated": "2026-06-12T21:00:00+00:00",
                       "last_deep_validated": None},
        "mean-reversion": {"source": "candidate", "market": "equities", "status": "candidate",
                           "allocation": 0.0, "last_validated": "2026-06-12T21:00:00+00:00",
                           "last_deep_validated": None},
    }


def _reports():
    return {
        "regime-hmm": ValidationReport(
            strategy="regime-hmm", verdict="PASS", deep=True,
            gates={"min_sharpe": True, "max_drawdown": True},
            metrics={"sharpe": 1.25, "calmar": 0.6, "max_drawdown": -0.12},
            blocks={"monte_carlo": {"n_sims": 1000, "prob_of_ruin": 0.02,
                                     "return_distribution": {"p50": 0.1}}},
        ),
        "mean-reversion": ValidationReport(
            strategy="mean-reversion", verdict="REJECTED", deep=False,
            reasons=["max_drawdown breached"], red_flags=["high turnover"],
            gates={"min_sharpe": False},
            metrics={"sharpe": 0.2, "calmar": 0.1, "max_drawdown": -0.35},
            blocks={},
        ),
    }


def _decisions():
    return {
        "regime-hmm": GovernanceDecision(key="regime-hmm", source="core", decision="DEPLOY",
                                          allocation=5000.0, rationale="Strong Sharpe."),
        "mean-reversion": GovernanceDecision(key="mean-reversion", source="candidate", decision="REJECT",
                                              allocation=0.0, rationale="Failed max drawdown gate.",
                                              reasons=["max_drawdown breached"]),
    }


def _summary_meta():
    return {"date": "2026-06-12", "tier": "cheap-daily", "can_trade": True}


def test_write_run_emits_structured_strategy_json(tmp_path):
    outdir = write_run(tmp_path, _reports(), _decisions(), _registry(), _summary_meta())

    rep = json.loads((outdir / "strategies" / "regime-hmm.json").read_text(encoding="utf-8"))
    assert rep["strategy"] == "regime-hmm"
    assert rep["verdict"] == "PASS"
    assert rep["deep"] is True
    assert rep["metrics"]["sharpe"] == 1.25
    assert rep["blocks"]["monte_carlo"]["return_distribution"]["p50"] == 0.1

    # decision + registry standing are folded in
    assert rep["key"] == "regime-hmm"
    assert rep["decision"] == "DEPLOY"
    assert rep["allocation"] == 5000.0
    assert rep["escalated"] is False
    assert rep["rationale"] == "Strong Sharpe."
    assert rep["registry"]["status"] == "deployed"

    rejected = json.loads((outdir / "strategies" / "mean-reversion.json").read_text(encoding="utf-8"))
    assert rejected["verdict"] == "REJECTED"
    assert rejected["reasons"] == ["max_drawdown breached"]
    assert rejected["red_flags"] == ["high turnover"]
    assert rejected["decision"] == "REJECT"
    assert rejected["blocks"] == {}


def test_write_run_other_artifacts(tmp_path):
    outdir = write_run(tmp_path, _reports(), _decisions(), _registry(), _summary_meta())

    assert (outdir / "strategies" / "regime-hmm.md").exists()
    assert (outdir / "strategies" / "mean-reversion.md").exists()
    assert (outdir / "master_leaderboard.md").exists()
    assert (outdir / "summary.md").exists()

    decisions_ledger = json.loads((outdir / "governance_decisions.json").read_text(encoding="utf-8"))
    assert {d["key"] for d in decisions_ledger} == {"regime-hmm", "mean-reversion"}
    deploy = next(d for d in decisions_ledger if d["key"] == "regime-hmm")
    assert deploy["decision"] == "DEPLOY"
    assert deploy["allocation"] == 5000.0
    assert "ts" in deploy

    board = pd.read_csv(outdir / "master_leaderboard.csv")
    assert list(board.columns) == [
        "strategy", "status", "decision", "allocation", "escalated", "verdict",
        "borderline", "sharpe", "calmar", "max_drawdown", "prob_of_ruin", "red_flags",
    ]
    # sorted by allocation desc, so the deployed strategy comes first
    assert board.iloc[0]["strategy"] == "regime-hmm"
    assert board.iloc[0]["status"] == "deployed"
    assert board.iloc[0]["allocation"] == 5000.0
    assert board.iloc[0]["prob_of_ruin"] == 0.02
    assert board.iloc[1]["strategy"] == "mean-reversion"
    assert board.iloc[1]["red_flags"] == 1

    summary_text = (outdir / "summary.md").read_text(encoding="utf-8")
    assert "- **date:** 2026-06-12" in summary_text
    assert "- **tier:** cheap-daily" in summary_text
    assert "- strategies evaluated: 2" in summary_text
    assert "- PASS: 1 | REJECTED: 1" in summary_text
