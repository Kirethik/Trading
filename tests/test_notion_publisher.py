"""notion_publisher.sync_run() must never raise: clean "skipped" results when
unconfigured, idempotent upserts when configured, and "error" (not an
exception) on request failures.
"""
from __future__ import annotations

import requests

from regime_trader.web import notion_publisher


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


def _fake_cfg(**overrides):
    cfg = {
        "enabled": True, "token_env": "NOTION_API_KEY", "api_version": "2022-06-28",
        "databases": {"runs": "db-runs", "strategies": "db-strategies", "decisions": "db-decisions"},
    }
    cfg.update(overrides)
    return cfg


# ------------------------------------------------------------------- skip paths
def test_sync_run_no_token(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.setattr(notion_publisher, "_notion_cfg", lambda: _fake_cfg())

    result = notion_publisher.sync_run("2026-06-12")

    assert result == {"status": "skipped", "date": "2026-06-12", "synced": {}, "reason": "no token"}


def test_sync_run_disabled(monkeypatch):
    monkeypatch.setattr(notion_publisher, "_notion_cfg", lambda: _fake_cfg(enabled=False))

    result = notion_publisher.sync_run("2026-06-12")

    assert result["status"] == "skipped"
    assert result["reason"] == "disabled"


def test_sync_run_no_databases(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret-token")
    monkeypatch.setattr(
        notion_publisher, "_notion_cfg",
        lambda: _fake_cfg(databases={"runs": "", "strategies": "", "decisions": ""}),
    )

    result = notion_publisher.sync_run("2026-06-12")

    assert result["status"] == "skipped"
    assert result["reason"] == "no databases configured"


def test_sync_run_request_error_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret-token")
    monkeypatch.setattr(notion_publisher, "_notion_cfg", lambda: _fake_cfg())
    monkeypatch.setattr(notion_publisher.data, "parse_summary", lambda date: {"tier": "cheap-daily"})
    monkeypatch.setattr(notion_publisher.data, "load_leaderboard", lambda date: {"rows": []})

    def boom(*a, **kw):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(notion_publisher.requests, "request", boom)

    result = notion_publisher.sync_run("2026-06-12")

    assert result["status"] == "error"
    assert "boom" in result["error"]


# -------------------------------------------------------------- full sync path
def test_sync_run_full(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret-token")
    monkeypatch.setattr(notion_publisher, "_notion_cfg", lambda: _fake_cfg())

    monkeypatch.setattr(notion_publisher.data, "parse_summary", lambda date: {"tier": "cheap-daily"})
    monkeypatch.setattr(notion_publisher.data, "load_leaderboard", lambda date: {"rows": [
        {"strategy": "regime-hmm", "decision": "DEPLOY", "verdict": "PASS", "allocation": 5000},
        {"strategy": "mean-reversion", "decision": "REJECT", "verdict": "REJECTED", "allocation": 0},
    ]})
    monkeypatch.setattr(notion_publisher.data, "list_strategies", lambda date: ["mean-reversion", "regime-hmm"])

    reports = {
        "regime-hmm": {
            "metrics": {"sharpe": 1.25, "calmar": 0.6, "max_drawdown": -0.12},
            "blocks": {"monte_carlo": {"prob_of_ruin": 0.02}},
            "registry": {"status": "deployed", "last_validated": "2026-06-12T21:00:00+00:00"},
            "decision": "DEPLOY", "verdict": "PASS", "red_flags": [], "allocation": 5000.0,
        },
        "mean-reversion": {
            "metrics": {"sharpe": 0.2, "calmar": 0.1, "max_drawdown": -0.35},
            "blocks": {},
            "registry": {"status": "candidate", "last_validated": "2026-06-12T21:00:00+00:00"},
            "decision": "REJECT", "verdict": "REJECTED", "red_flags": ["high turnover"], "allocation": 0.0,
        },
    }
    monkeypatch.setattr(notion_publisher.data, "load_strategy", lambda key, date: reports[key])
    monkeypatch.setattr(notion_publisher.data, "load_run_decisions", lambda date: [
        {"ts": "2026-06-12T21:00:00+00:00", "key": "regime-hmm", "decision": "DEPLOY",
         "allocation": 5000.0, "escalated": False, "rationale": "Strong Sharpe."},
    ])

    created_pages = []

    def fake_request(method, url, headers=None, timeout=None, json=None, **kw):
        if method == "POST" and url.endswith("/query"):
            return FakeResponse(200, {"results": []})
        if method == "POST" and url.endswith("/pages"):
            created_pages.append(json)
            return FakeResponse(200, {"id": "page-id"})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(notion_publisher.requests, "request", fake_request)

    result = notion_publisher.sync_run("2026-06-12")

    assert result["status"] == "ok"
    assert result["synced"]["runs"] == {"created": 1}
    assert result["synced"]["strategies"] == {"created": 2, "updated": 0}
    assert result["synced"]["decisions"] == {"created": 1, "updated": 0}

    run_page = next(p for p in created_pages if p["parent"]["database_id"] == "db-runs")
    assert run_page["properties"]["Name"]["title"][0]["text"]["content"] == "2026-06-12"
    assert run_page["properties"]["Tier"]["select"]["name"] == "cheap-daily"
    assert run_page["properties"]["Pass"]["number"] == 1
    assert run_page["properties"]["Reject"]["number"] == 1
    assert run_page["properties"]["Deploy"]["number"] == 1
    assert run_page["properties"]["Capital Deployed"]["number"] == 5000

    strat_pages = [p for p in created_pages if p["parent"]["database_id"] == "db-strategies"]
    names = {p["properties"]["Name"]["title"][0]["text"]["content"] for p in strat_pages}
    assert names == {"regime-hmm", "mean-reversion"}
    rh = next(p for p in strat_pages if p["properties"]["Name"]["title"][0]["text"]["content"] == "regime-hmm")
    assert rh["properties"]["Sharpe"]["number"] == 1.25
    assert rh["properties"]["Status"]["select"]["name"] == "deployed"
    assert rh["properties"]["Prob of Ruin"]["number"] == 0.02
    assert rh["properties"]["Red Flags"]["number"] == 0

    dec_pages = [p for p in created_pages if p["parent"]["database_id"] == "db-decisions"]
    assert len(dec_pages) == 1
    assert dec_pages[0]["properties"]["RunKey"]["rich_text"][0]["text"]["content"] == "2026-06-12|regime-hmm"
    assert dec_pages[0]["properties"]["Escalated"]["checkbox"] is False
    assert dec_pages[0]["properties"]["Decision"]["select"]["name"] == "DEPLOY"


# ------------------------------------------------------------- property helpers
def test_property_helpers():
    assert notion_publisher._title("abc") == {"title": [{"text": {"content": "abc"}}]}
    assert notion_publisher._rich_text("abc") == {"rich_text": [{"text": {"content": "abc"}}]}
    assert notion_publisher._select("PASS") == {"select": {"name": "PASS"}}
    assert notion_publisher._select(None) == {"select": None}
    assert notion_publisher._select("") == {"select": None}
    assert notion_publisher._number(1.23456789) == {"number": 1.234568}
    assert notion_publisher._number(None) == {"number": None}
    assert notion_publisher._number("bad") == {"number": None}
    assert notion_publisher._checkbox(1) == {"checkbox": True}
    assert notion_publisher._checkbox(0) == {"checkbox": False}
    assert notion_publisher._date("2026-06-12") == {"date": {"start": "2026-06-12"}}
    assert notion_publisher._date(None) == {"date": None}
    assert notion_publisher._date("") == {"date": None}
