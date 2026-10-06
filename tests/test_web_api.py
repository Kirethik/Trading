"""FastAPI dashboard: auth gate, read views, and the safe-control allowlist.

Builds a synthetic runs/governance/<date>/ + state/*.json + logs/ tree under
tmp_path, points the shared config singleton at it, and drives the app through
fastapi.testclient. Never starts a real job (jobs.start_job spawns a
subprocess) -- /api/run is only exercised on its rejection paths, and
_build_argv is covered directly as a pure function.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from regime_trader.config import DEFAULT_CONFIG_PATH, get_config
from regime_trader.web import jobs
from regime_trader.web.app import app

DATE = "2026-06-12"


def _registry() -> dict:
    return {
        "regime-hmm": {"source": "core", "market": "equities", "status": "deployed",
                       "allocation": 5000.0, "last_validated": "2026-06-12T21:00:00+00:00",
                       "last_deep_validated": None},
        "mean-reversion": {"source": "candidate", "market": "equities", "status": "candidate",
                           "allocation": 0.0, "last_validated": "2026-06-12T21:00:00+00:00",
                           "last_deep_validated": None},
    }


def _decisions() -> list[dict]:
    return [
        {"ts": "2026-06-12T21:00:00+00:00", "key": "regime-hmm", "source": "core", "decision": "DEPLOY",
         "allocation": 5000.0, "rationale": "Strong Sharpe, passes all gates.", "escalated": False, "reasons": []},
        {"ts": "2026-06-12T21:00:00+00:00", "key": "mean-reversion", "source": "candidate", "decision": "REJECT",
         "allocation": 0.0, "rationale": "Failed max drawdown gate.", "escalated": False,
         "reasons": ["max_drawdown"]},
    ]


@pytest.fixture()
def web_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass123")
    monkeypatch.setenv("DASHBOARD_SECRET", "testsecret")

    runs_dir = tmp_path / "runs"
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    run_dir = runs_dir / "governance" / DATE
    (run_dir / "strategies").mkdir(parents=True)
    state_dir.mkdir()
    logs_dir.mkdir()
    (logs_dir / "jobs").mkdir()

    (run_dir / "summary.md").write_text(
        "# Governance Daily Summary\n\n"
        "- **date:** 2026-06-12\n"
        "- **tier:** cheap-daily\n"
        "- **can_trade:** True\n\n"
        "- strategies evaluated: 2\n"
        "- PASS: 1 | REJECTED: 1\n",
        encoding="utf-8",
    )

    pd.DataFrame([
        {"strategy": "regime-hmm", "status": "deployed", "decision": "DEPLOY", "allocation": 5000.0,
         "escalated": False, "verdict": "PASS", "borderline": False, "sharpe": 1.25, "calmar": 0.6,
         "max_drawdown": -0.12, "prob_of_ruin": 0.02, "red_flags": 0},
        {"strategy": "mean-reversion", "status": "candidate", "decision": "REJECT", "allocation": 0.0,
         "escalated": False, "verdict": "REJECTED", "borderline": False, "sharpe": 0.2, "calmar": 0.1,
         "max_drawdown": -0.35, "prob_of_ruin": 0.4, "red_flags": 3},
    ]).to_csv(run_dir / "master_leaderboard.csv", index=False)

    decisions = _decisions()
    (run_dir / "governance_decisions.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    (state_dir / "governance_decisions.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")

    registry = _registry()
    (state_dir / "governance_registry.json").write_text(json.dumps(registry, indent=2), encoding="utf-8")

    regime_hmm_report = {
        "strategy": "regime-hmm", "verdict": "PASS", "deep": True,
        "reasons": [], "red_flags": [], "borderline": False,
        "gates": {"min_sharpe": True, "min_calmar": True, "max_drawdown": True},
        "metrics": {"sharpe": 1.25, "calmar": 0.6, "max_drawdown": -0.12, "cagr": 0.18},
        "blocks": {
            "monte_carlo": {
                "n_sims": 10000, "prob_of_ruin": 0.02, "prob_profit": 0.91,
                "return_distribution": {"p1": -0.3, "p5": -0.15, "p25": -0.02, "p50": 0.1,
                                         "p75": 0.22, "p95": 0.4, "p99": 0.55},
                "drawdown_distribution": {"p1": -0.5, "p5": -0.4, "p25": -0.25, "p50": -0.15,
                                           "p75": -0.08, "p95": -0.03, "p99": -0.01},
                "worst_case_return_p1": -0.3, "worst_case_drawdown_p1": -0.5,
            },
            "regime_breakdown": [
                {"regime": "calm", "bars": 400, "ann_return": 0.2, "sharpe": 1.5},
                {"regime": "turbulent", "bars": 200, "ann_return": 0.05, "sharpe": 0.4},
            ],
            "out_of_sample": {"train_sharpe": 1.4, "test_sharpe": 1.1, "degradation": 0.21},
            "slippage": {
                "low": {"bps": 1, "sharpe": 1.3}, "medium": {"bps": 5, "sharpe": 1.2},
                "high": {"bps": 15, "sharpe": 1.0}, "extreme": {"bps": 50, "sharpe": 0.6},
                "viable_at_high": True,
            },
            "parameter_sensitivity": {
                "applicable": True, "center_sharpe": 1.25, "mean_neighbor_sharpe": 1.1, "degradation": 0.12,
                "neighbors": [{"param": "vol_window", "value": 18, "sharpe": 1.1},
                              {"param": "vol_window", "value": 24, "sharpe": 1.15}],
            },
            "stress": {"synthetic_passed": True, "events_passed": True, "synthetic": {},
                       "events": {"covid-2020": {"survived": True, "max_drawdown": -0.22,
                                                   "total_return": 0.05, "bars": 50}}},
        },
        "key": "regime-hmm", "decision": "DEPLOY", "allocation": 5000.0, "escalated": False,
        "rationale": "Strong Sharpe, passes all gates.", "registry": registry["regime-hmm"],
    }
    (run_dir / "strategies" / "regime-hmm.json").write_text(json.dumps(regime_hmm_report, indent=2), encoding="utf-8")

    mean_reversion_report = {
        "strategy": "mean-reversion", "verdict": "REJECTED", "deep": False,
        "reasons": ["max_drawdown breached: -0.35 < -0.30"],
        "red_flags": ["high turnover", "param count too high", "OOS degradation"],
        "borderline": False, "gates": {"min_sharpe": False, "min_calmar": False, "max_drawdown": False},
        "metrics": {"sharpe": 0.2, "calmar": 0.1, "max_drawdown": -0.35, "cagr": 0.02},
        "blocks": {},
        "key": "mean-reversion", "decision": "REJECT", "allocation": 0.0, "escalated": False,
        "rationale": "Failed max drawdown gate.", "registry": registry["mean-reversion"],
    }
    (run_dir / "strategies" / "mean-reversion.json").write_text(
        json.dumps(mean_reversion_report, indent=2), encoding="utf-8")

    omnivore = {
        "regime-hmm": {
            "market": "equities", "enabled": True, "allocated": 5000.0, "cash": 3800.0,
            "realized_pnl": 120.5, "trades": 8,
            "positions": {"SPY": {"qty": 3, "entry": 400.0, "stop": 380.0}},
            "equity_history": [["2026-06-01T00:00:00+00:00", 5000.0], ["2026-06-12T00:00:00+00:00", 5120.5]],
        },
    }
    (state_dir / "omnivore_ledger.json").write_text(json.dumps(omnivore, indent=2), encoding="utf-8")

    (logs_dir / "governance.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (logs_dir / "governance_20260612_210000.log").write_text("cron line1\ncron line2\n", encoding="utf-8")

    config_yaml = {
        "runs_dir": str(runs_dir), "state_dir": str(state_dir),
        "governance": {"registry_file": str(state_dir / "governance_registry.json"),
                       "decisions_file": str(state_dir / "governance_decisions.json")},
        "omnivore": {"ledger_file": str(state_dir / "omnivore_ledger.json")},
        "monitoring": {"log_dir": str(logs_dir)},
        "web": {"title": "Regime-Trader Test", "password_env": "DASHBOARD_PASSWORD",
                "session_secret_env": "DASHBOARD_SECRET", "session_hours": 12,
                "job_log_dir": str(logs_dir / "jobs")},
        "notion": {"enabled": False, "token_env": "NOTION_API_KEY",
                   "databases": {"runs": "", "strategies": "", "decisions": ""}},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config_yaml), encoding="utf-8")
    get_config(path=config_path, reload=True)

    yield tmp_path

    get_config(path=DEFAULT_CONFIG_PATH, reload=True)


@pytest.fixture()
def client(web_env):
    return TestClient(app)


def _login(client) -> None:
    r = client.post("/login", json={"password": "testpass123"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------- auth
def test_health_is_open(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_serves_spa(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Regime-Trader" in r.text


def test_session_before_login(client):
    r = client.get("/api/session")
    assert r.status_code == 200
    assert r.json() == {"authed": False, "password_set": True, "title": "Regime-Trader Test"}


def test_protected_route_requires_auth(client):
    r = client.get("/api/overview")
    assert r.status_code == 401


def test_login_wrong_password(client):
    r = client.post("/login", json={"password": "wrong"})
    assert r.status_code == 401

    r = client.get("/api/overview")
    assert r.status_code == 401


def test_login_then_session_and_logout(client):
    _login(client)

    r = client.get("/api/session")
    assert r.json() == {"authed": True, "password_set": True, "title": "Regime-Trader Test"}

    r = client.get("/api/overview")
    assert r.status_code == 200

    r = client.post("/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.get("/api/overview")
    assert r.status_code == 401


# ------------------------------------------------------------------ overview
def test_overview(client):
    _login(client)
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()

    assert body["latest_date"] == DATE
    assert body["available_dates"] == [DATE]
    assert body["strategies"] == 2
    assert body["capital_allocated"] == 5000.0
    assert body["status_counts"] == {"deployed": 1, "candidate": 1}
    assert body["decision_counts"] == {"DEPLOY": 1, "REJECT": 1}
    assert body["verdict_counts"] == {"PASS": 1, "REJECTED": 1}
    assert body["summary"]["tier"] == "cheap-daily"
    assert body["breaker"] == {"locked": False, "detail": None}
    assert body["title"] == "Regime-Trader Test"


# --------------------------------------------------------------------- runs
def test_runs(client):
    _login(client)
    r = client.get("/api/runs")
    assert r.status_code == 200
    assert r.json() == {"dates": [DATE], "latest": DATE}


# ------------------------------------------------------------- leaderboard
def test_leaderboard_latest(client):
    _login(client)
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == DATE
    rows = {row["strategy"]: row for row in body["rows"]}
    assert rows["regime-hmm"]["decision"] == "DEPLOY"
    assert rows["regime-hmm"]["sharpe"] == 1.25
    assert rows["mean-reversion"]["verdict"] == "REJECTED"
    assert rows["mean-reversion"]["red_flags"] == 3


def test_leaderboard_explicit_date(client):
    _login(client)
    r = client.get(f"/api/leaderboard?date={DATE}")
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 2


def test_leaderboard_unknown_date(client):
    _login(client)
    r = client.get("/api/leaderboard?date=2099-01-01")
    assert r.status_code == 200
    assert r.json() == {"date": None, "rows": []}


# ---------------------------------------------------------------- registry
def test_registry(client):
    _login(client)
    r = client.get("/api/registry")
    assert r.status_code == 200
    assert r.json() == _registry()


# --------------------------------------------------------------- decisions
def test_decisions(client):
    _login(client)
    r = client.get("/api/decisions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["decisions"]) == 2
    assert {d["key"] for d in body["decisions"]} == {"regime-hmm", "mean-reversion"}


def test_decisions_limit(client):
    _login(client)
    r = client.get("/api/decisions?limit=1")
    assert r.status_code == 200
    body = r.json()
    assert len(body["decisions"]) == 1
    assert body["decisions"][0]["key"] == "mean-reversion"


# -------------------------------------------------------------- strategies
def test_strategies_list(client):
    _login(client)
    r = client.get("/api/strategies")
    assert r.status_code == 200
    assert r.json() == {"date": DATE, "keys": ["mean-reversion", "regime-hmm"]}


def test_strategy_detail_with_live_equity(client):
    _login(client)
    r = client.get("/api/strategy/regime-hmm")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "PASS"
    assert body["blocks"]["monte_carlo"]["prob_of_ruin"] == 0.02
    assert body["blocks"]["regime_breakdown"][0]["regime"] == "calm"
    assert body["equity_history"] == [["2026-06-01T00:00:00+00:00", 5000.0],
                                       ["2026-06-12T00:00:00+00:00", 5120.5]]
    assert body["live"]["trades"] == 8
    assert body["live"]["positions"] == {"SPY": {"qty": 3, "entry": 400.0, "stop": 380.0}}


def test_strategy_detail_without_live_equity(client):
    _login(client)
    r = client.get("/api/strategy/mean-reversion")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "REJECTED"
    assert "equity_history" not in body
    assert "live" not in body


def test_strategy_detail_404(client):
    _login(client)
    r = client.get("/api/strategy/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------- omnivore
def test_omnivore(client):
    _login(client)
    r = client.get("/api/omnivore")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["strategy"] == "regime-hmm"
    assert row["total_pnl"] == 120.5
    assert row["pnl_pct"] == round(5120.5 / 5000.0 - 1.0, 4)
    assert row["trades"] == 8
    assert row["open_positions"] == 1
    assert row["observations"] == 2
    assert body["equity"]["regime-hmm"] == [["2026-06-01T00:00:00+00:00", 5000.0],
                                             ["2026-06-12T00:00:00+00:00", 5120.5]]


# -------------------------------------------------------------------- portfolio
def test_portfolio_live_prices(client, monkeypatch):
    monkeypatch.setattr(
        "regime_trader.data.provider.DataProvider.get_latest_prices",
        lambda self, symbols: {"SPY": 410.0},
    )
    _login(client)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is True

    t = body["totals"]
    assert t["total_invested"] == 5000.0
    assert t["total_cash"] == 3800.0
    assert t["total_market_value"] == 1230.0
    assert t["total_equity"] == 5030.0
    assert t["total_pnl"] == 30.0
    assert t["total_realized_pnl"] == 120.5
    assert t["total_unrealized_pnl"] == 30.0
    assert t["symbols"] == 1
    assert t["symbols_priced"] == 1

    assert body["positions"] == [{
        "strategy": "regime-hmm", "symbol": "SPY", "qty": 3, "entry": 400.0,
        "price": 410.0, "value": 1230.0, "unrealized_pnl": 30.0,
        "unrealized_pnl_pct": 0.025, "stale": False,
    }]

    strat = body["strategies"][0]
    assert strat["equity"] == 5030.0
    assert strat["total_pnl"] == 30.0


def test_portfolio_falls_back_when_prices_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        "regime_trader.data.provider.DataProvider.get_latest_prices",
        lambda self, symbols: {},
    )
    _login(client)
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is False

    pos = body["positions"][0]
    assert pos["price"] == 400.0  # falls back to entry price
    assert pos["unrealized_pnl"] == 0.0
    assert pos["stale"] is True


def test_portfolio_requires_auth(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 401


# -------------------------------------------------------------------- logs
def test_logs_tail_governance(client):
    _login(client)
    r = client.get("/api/logs/tail?name=governance&n=2")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "governance"
    assert body["path"] == "governance.log"
    assert body["lines"] == ["line2", "line3"]


def test_logs_tail_governance_run(client):
    _login(client)
    r = client.get("/api/logs/tail?name=governance-run")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "governance_20260612_210000.log"
    assert body["lines"] == ["cron line1", "cron line2"]


def test_logs_tail_unknown_channel(client):
    _login(client)
    r = client.get("/api/logs/tail?name=bogus")
    assert r.status_code == 200
    assert r.json() == {"name": "bogus", "lines": [], "error": "unknown log"}


# ---------------------------------------------------------------- controls
def test_commands(client):
    _login(client)
    r = client.get("/api/commands")
    assert r.status_code == 200
    assert r.json() == {"commands": jobs.COMMANDS}


def test_run_rejects_command_outside_allowlist(client):
    _login(client)
    r = client.post("/api/run", json={"command": "live"})
    assert r.status_code == 400
    assert "command not allowed: live" in r.json()["detail"]


def test_run_rejects_invalid_strategy_token(client):
    _login(client)
    r = client.post("/api/run", json={"command": "govern-validate", "params": {"strategy": "../etc"}})
    assert r.status_code == 400


def test_run_rejects_missing_strategy(client):
    _login(client)
    r = client.post("/api/run", json={"command": "govern-validate", "params": {}})
    assert r.status_code == 400


def test_run_rejects_bad_notion_date(client):
    _login(client)
    r = client.post("/api/run", json={"command": "notion-sync", "params": {"date": "06-12-2026"}})
    assert r.status_code == 400


def test_run_requires_auth(client):
    r = client.post("/api/run", json={"command": "live"})
    assert r.status_code == 401


def test_jobs_list_empty(client):
    _login(client)
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json() == {"jobs": []}


def test_job_not_found(client):
    _login(client)
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_job_log_unknown_job(client):
    _login(client)
    r = client.get("/api/jobs/does-not-exist/log")
    assert r.status_code == 200
    assert r.json() == {"id": "does-not-exist", "lines": [], "error": "unknown job"}


# --------------------------------------------------------- _build_argv (pure)
def test_build_argv_govern_daily_notrade():
    argv, label = jobs._build_argv("govern-daily-notrade", {})
    assert argv == ["govern-daily", "--no-trade"]
    assert label == "Run daily governance (no orders)"


def test_build_argv_govern_daily_notrade_deep():
    argv, _label = jobs._build_argv("govern-daily-notrade", {"deep": True})
    assert argv == ["govern-daily", "--deep", "--no-trade"]


def test_build_argv_govern_validate():
    argv, label = jobs._build_argv("govern-validate", {"strategy": "regime-hmm"})
    assert argv == ["govern-validate", "--strategy", "regime-hmm"]
    assert label == "Validate regime-hmm"


def test_build_argv_govern_validate_deep():
    argv, _label = jobs._build_argv("govern-validate", {"strategy": "regime-hmm", "deep": True})
    assert argv == ["govern-validate", "--strategy", "regime-hmm", "--deep"]


def test_build_argv_govern_validate_rejects_bad_strategy():
    with pytest.raises(ValueError):
        jobs._build_argv("govern-validate", {"strategy": "../etc"})
    with pytest.raises(ValueError):
        jobs._build_argv("govern-validate", {})


def test_build_argv_govern_optimize():
    argv, label = jobs._build_argv("govern-optimize", {})
    assert argv == ["govern-optimize"]
    assert label == "Run optimization proposals"

    argv, _label = jobs._build_argv("govern-optimize", {"strategy": "regime-hmm"})
    assert argv == ["govern-optimize", "--strategy", "regime-hmm"]

    with pytest.raises(ValueError):
        jobs._build_argv("govern-optimize", {"strategy": "../etc"})


def test_build_argv_refresh_data():
    argv, label = jobs._build_argv("refresh-data", {})
    assert argv == ["backtest", "--strategy", "regime-hmm", "--refresh", "--no-benchmarks"]
    assert label == "Refresh market-data cache"


def test_build_argv_notion_sync():
    argv, label = jobs._build_argv("notion-sync", {})
    assert argv == ["notion-sync"]
    assert label == "Sync reports to Notion"

    argv, _label = jobs._build_argv("notion-sync", {"date": "2026-06-12"})
    assert argv == ["notion-sync", "--date", "2026-06-12"]

    with pytest.raises(ValueError):
        jobs._build_argv("notion-sync", {"date": "06-12-2026"})


def test_build_argv_unknown_command():
    with pytest.raises(ValueError, match="command not allowed: live"):
        jobs._build_argv("live", {})


# ---------------------------------------------------------- agent activity
def test_agent_activity_requires_auth(client):
    assert client.get("/api/agents/activity").status_code == 401


def test_agent_activity_empty_when_agents_never_ran(client):
    _login(client)
    body = client.get("/api/agents/activity").json()
    assert body["events"] == [] and body["supervisor"] is None
    assert body["budget"]["spent_usd"] == 0.0 and body["kill_switch"] is False
    assert body["sleeve"]["positions"] == {}


def test_agent_activity_reports_audit_heartbeat_budget_and_sleeve(client, web_env):
    state = web_env / "state"
    rows = [{"ts": "2026-10-05T10:00:00+00:00", "agent": "governance", "outcome": "ok", "cost_usd": 0.01},
            {"ts": "2026-10-05T10:01:00+00:00", "agent": "tester", "kind": "order", "status": "submitted"}]
    (state / "agent_audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "torn{",
                                             encoding="utf-8")
    (state / "agent_supervisor.json").write_text(json.dumps(
        {"ts": "2026-10-05T10:02:00+00:00", "pid": 1, "status": "running", "jobs": {}}), encoding="utf-8")
    (state / "agent_ledger.json").write_text(json.dumps(
        {"positions": {"AAPL": {"qty": 3, "entry": 200, "stop": 190, "price": 201}}}), encoding="utf-8")
    (state / "agents.disabled").write_text("paused", encoding="utf-8")
    _login(client)
    body = client.get("/api/agents/activity?limit=10").json()
    assert [e["agent"] for e in body["events"]] == ["tester", "governance"]  # newest first
    assert body["supervisor"]["status"] == "running" and body["supervisor"]["age_seconds"] >= 0
    assert body["kill_switch"] is True
    assert body["sleeve"]["positions"]["AAPL"]["qty"] == 3
