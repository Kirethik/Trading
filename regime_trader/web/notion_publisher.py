"""Mirror each governance run into Notion (the "Trading" hub).

Used by both the nightly `notion-sync` CLI command and the on-demand
`/api/run notion-sync` dashboard control. Reads the same on-disk artifacts as
`web/data.py` and idempotently upserts rows into three Notion databases whose
IDs live in `config.yaml` under `notion.databases`.

Safe by construction:
- No-ops cleanly (returns a "skipped" result, never raises) if `NOTION_API_KEY`
  is unset, `notion.enabled` is false, or no database IDs are configured.
- Never logs the token.
- Read-only against the trading stack — this module only talks to the Notion
  REST API and the filesystem.
"""
from __future__ import annotations

import os
import time

import requests

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import utc_now
from . import data

log = get_logger("governance")

NOTION_BASE = "https://api.notion.com/v1"


# --------------------------------------------------------------- Notion client
def _notion_cfg():
    return get_config().get("notion", {})


def _token(notion_cfg) -> str | None:
    env_name = notion_cfg.get("token_env", "NOTION_API_KEY")
    return os.environ.get(env_name)


def _headers(token: str, version: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, token: str, version: str, **kwargs) -> dict:
    url = f"{NOTION_BASE}{path}"
    headers = _headers(token, version)
    for attempt in range(3):
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code == 429 and attempt < 2:
            time.sleep(float(resp.headers.get("Retry-After", 1)))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------ property helpers
def _title(text) -> dict:
    return {"title": [{"text": {"content": str(text)[:2000]}}]}


def _rich_text(text) -> dict:
    return {"rich_text": [{"text": {"content": str(text)[:2000]}}]}


def _select(name) -> dict:
    return {"select": {"name": str(name)[:100]}} if name else {"select": None}


def _number(value) -> dict:
    if value is None:
        return {"number": None}
    try:
        return {"number": round(float(value), 6)}
    except (TypeError, ValueError):
        return {"number": None}


def _checkbox(value) -> dict:
    return {"checkbox": bool(value)}


def _date(value) -> dict:
    return {"date": {"start": str(value)}} if value else {"date": None}


# ----------------------------------------------------------------- upsert core
def _find_page(token: str, version: str, db_id: str, prop: str, prop_type: str, value: str) -> str | None:
    body = _request("POST", f"/databases/{db_id}/query", token, version,
                    json={"filter": {"property": prop, prop_type: {"equals": value}}, "page_size": 1})
    results = body.get("results", [])
    return results[0]["id"] if results else None


def _upsert(token: str, version: str, db_id: str, key_prop: str, key_type: str,
            key_value: str, properties: dict) -> str:
    page_id = _find_page(token, version, db_id, key_prop, key_type, key_value)
    if page_id:
        _request("PATCH", f"/pages/{page_id}", token, version, json={"properties": properties})
        return "updated"
    _request("POST", "/pages", token, version,
             json={"parent": {"database_id": db_id}, "properties": properties})
    return "created"


# -------------------------------------------------------------------- per-table
def _sync_governance_run(token: str, version: str, db_id: str, date: str) -> dict:
    summary = data.parse_summary(date)
    rows = data.load_leaderboard(date).get("rows", [])
    pass_n = sum(1 for r in rows if r.get("verdict") == "PASS")
    reject_n = sum(1 for r in rows if r.get("verdict") == "REJECTED")
    deploy_n = sum(1 for r in rows if r.get("decision") == "DEPLOY")
    capital = sum(float(r.get("allocation") or 0) for r in rows)
    blurb = (f"{len(rows)} strategies — {pass_n} PASS / {reject_n} REJECT — "
             f"{deploy_n} DEPLOY — ${capital:,.0f} allocated")
    props = {
        "Name": _title(date),
        "Tier": _select(summary.get("tier")),
        "Strategies": _number(len(rows)),
        "Pass": _number(pass_n),
        "Reject": _number(reject_n),
        "Deploy": _number(deploy_n),
        "Capital Deployed": _number(capital),
        "Summary": _rich_text(blurb),
    }
    outcome = _upsert(token, version, db_id, "Name", "title", date, props)
    return {outcome: 1}


def _sync_strategies(token: str, version: str, db_id: str, date: str) -> dict:
    counts = {"created": 0, "updated": 0}
    for key in data.list_strategies(date):
        rep = data.load_strategy(key, date)
        if not rep:
            continue
        metrics = rep.get("metrics") or {}
        blocks = rep.get("blocks") or {}
        registry = rep.get("registry") or {}
        mc = blocks.get("monte_carlo") or {}
        props = {
            "Name": _title(key),
            "Status": _select(registry.get("status")),
            "Decision": _select(rep.get("decision")),
            "Verdict": _select(rep.get("verdict")),
            "Sharpe": _number(metrics.get("sharpe")),
            "Calmar": _number(metrics.get("calmar")),
            "Max DD": _number(metrics.get("max_drawdown")),
            "Prob of Ruin": _number(mc.get("prob_of_ruin")),
            "Red Flags": _number(len(rep.get("red_flags") or [])),
            "Allocation": _number(rep.get("allocation")),
            "Last Validated": _date(registry.get("last_validated")),
        }
        outcome = _upsert(token, version, db_id, "Name", "title", key, props)
        counts[outcome] += 1
    return counts


def _sync_decisions(token: str, version: str, db_id: str, date: str) -> dict:
    counts = {"created": 0, "updated": 0}
    for d in data.load_run_decisions(date):
        key = d.get("key", "")
        run_key = f"{date}|{key}"
        props = {
            "Name": _title(f"{date} · {key} · {d.get('decision', '')}"),
            "Date": _date(d.get("ts") or date),
            "Key": _rich_text(key),
            "Decision": _select(d.get("decision")),
            "Allocation": _number(d.get("allocation")),
            "Escalated": _checkbox(d.get("escalated", False)),
            "Rationale": _rich_text(d.get("rationale", "")),
            "RunKey": _rich_text(run_key),
        }
        outcome = _upsert(token, version, db_id, "RunKey", "rich_text", run_key, props)
        counts[outcome] += 1
    return counts


# --------------------------------------------------------------------- entry
def sync_run(date: str | None = None) -> dict:
    """Mirror one governance run's reports into Notion.

    Returns a small status dict; never raises. Cleanly skips (status
    "skipped") if Notion isn't configured yet, so callers (nightly cron,
    dashboard control) can always treat this as best-effort.
    """
    notion_cfg = _notion_cfg()
    result: dict = {"status": "skipped", "date": date, "synced": {}}

    if not notion_cfg.get("enabled", True):
        log.info("notion-sync: disabled (notion.enabled=false), skipping")
        result["reason"] = "disabled"
        return result

    token = _token(notion_cfg)
    if not token:
        log.info("notion-sync: %s not set, skipping", notion_cfg.get("token_env", "NOTION_API_KEY"))
        result["reason"] = "no token"
        return result

    dbs = notion_cfg.get("databases", {})
    runs_db = (dbs.get("runs") or "").strip()
    strategies_db = (dbs.get("strategies") or "").strip()
    decisions_db = (dbs.get("decisions") or "").strip()
    if not (runs_db or strategies_db or decisions_db):
        log.info("notion-sync: no database IDs configured (notion.databases.*), skipping")
        result["reason"] = "no databases configured"
        return result

    date = date or utc_now().strftime("%Y-%m-%d")
    result["date"] = date
    version = notion_cfg.get("api_version", "2022-06-28")

    try:
        if runs_db:
            result["synced"]["runs"] = _sync_governance_run(token, version, runs_db, date)
        if strategies_db:
            result["synced"]["strategies"] = _sync_strategies(token, version, strategies_db, date)
        if decisions_db:
            result["synced"]["decisions"] = _sync_decisions(token, version, decisions_db, date)
        result["status"] = "ok"
    except requests.RequestException as exc:
        log.warning("notion-sync: request failed: %s", exc)
        result["status"] = "error"
        result["error"] = str(exc)
    return result
