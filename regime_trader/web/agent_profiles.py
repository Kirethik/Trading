"""Plain-language agent profiles for the dashboard.

Turns raw agent audit rows and blackboard posts into short sentences a
non-trader can read ("Decided to WAIT on tsmom-eth: sample too thin"). Read-only:
it never calls a model, never trades, and never creates state files.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..agents import specs
from ..agents.audit import AuditLog
from . import data

ACTIVE_HOURS = 24
_TAIL = 5000

# words a non-trader understands
_DECISION_WORDS = {
    "DEPLOY": ("fund it", "good"), "APPROVE": ("approve it (no money yet)", "good"),
    "HOLD": ("wait and watch", "neutral"), "REDUCE_RISK": ("give it less money", "bad"),
}
_METRIC_WORDS = {
    "sharpe": "return for the risk taken (Sharpe)", "base_sharpe": "current Sharpe",
    "proposed_sharpe": "Sharpe after the tweak", "max_drawdown": "worst drop",
    "calmar": "return vs worst drop (Calmar)", "live_sharpe": "live Sharpe",
    "pnl_pct": "live profit/loss", "cagr": "yearly growth", "win_rate": "win rate",
    "top_n": "how many can be funded",
}
_FAILURE_WORDS = {
    "timeout": "Took too long to answer — the normal rules decided instead",
    "invalid": "Gave an unclear answer — the normal rules decided instead",
    "error": "Couldn't be reached (service busy) — the normal rules decided instead",
    "blocked": "Was paused (daily spending limit or off switch)",
}


# ------------------------------------------------------------------ sources
def _audit_rows() -> list[dict]:
    return AuditLog(data._agent_path("audit_file"), max_bytes=1).tail(_TAIL)


def _board_path() -> Path:
    cfg = data._cfg()
    agents = cfg.get("agents")
    rel = agents.get("blackboard_db") if agents is not None else None
    return cfg.resolve_path(rel) if rel else cfg.resolve_path(cfg.state_dir) / "agents.db"


def _posts(topic: str, limit: int) -> list[dict]:
    """Newest-last posts on a topic; [] when the blackboard doesn't exist yet."""
    path = _board_path()
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            rows = db.execute("SELECT ts, payload FROM posts WHERE topic = ? ORDER BY id DESC LIMIT ?",
                              (topic, int(limit))).fetchall()
    except sqlite3.Error as exc:
        data.log.warning("agent blackboard unreadable (%s): %s", path, exc)
        return []
    out = []
    for ts, payload in reversed(rows):
        try:
            out.append({"ts": ts, **json.loads(payload)})
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _parse_ts(ts: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- humanizer
def humanize(row: dict) -> dict:
    """One audit row -> {when, headline, detail, tone}."""
    when, subject = row.get("ts"), row.get("subject") or "a strategy"
    outcome = row.get("outcome")
    if row.get("kind") == "order":
        verb = "Bought" if row.get("side") == "buy" else "Sold"
        ok = row.get("status") == "submitted"
        return {"when": when, "tone": "good" if ok else "bad",
                "headline": f"{verb} {row.get('symbol')}" if ok
                else f"Tried to trade {row.get('symbol')} — {row.get('status')}",
                "detail": str(row.get("reason") or row.get("rationale") or "")}
    if outcome and outcome != "ok":
        return {"when": when, "tone": "bad" if outcome != "blocked" else "neutral",
                "headline": _FAILURE_WORDS.get(outcome, "Didn't answer"),
                "detail": f"About {subject}" if row.get("subject") else ""}
    out = row.get("output") or {}
    if "decision" in out:
        words, tone = _DECISION_WORDS.get(out["decision"], (str(out["decision"]).lower(), "neutral"))
        return {"when": when, "tone": tone, "headline": f"Decided to {words}: {subject}",
                "detail": str(out.get("rationale", ""))}
    if "selected" in out:
        return {"when": when, "tone": "good",
                "headline": f"Chose {len(out['selected'])} strategies to receive money",
                "detail": str(out.get("rationale", ""))}
    if "stance" in out:
        side = "for" if out["stance"] == "for" else "against"
        return {"when": when, "tone": "good" if side == "for" else "bad",
                "headline": f"Argued {side} {subject} ({out.get('confidence', '?')} confidence)",
                "detail": " • ".join(str(p) for p in out.get("points", []))}
    if "caution" in out:
        risk = out.get("overfit_risk", "?")
        return {"when": when, "tone": {"low": "good", "high": "bad"}.get(risk, "neutral"),
                "headline": f"Checked a suggested tweak to {subject} — risk of fooling itself: {risk}",
                "detail": str(out.get("caution", ""))}
    if "candidates" in out:
        return {"when": when, "tone": "neutral",
                "headline": f"Suggested {len(out['candidates'])} new settings to try for {subject}",
                "detail": str(out.get("rationale", ""))}
    if "lessons" in out:
        return {"when": when, "tone": "good",
                "headline": f"Looked back at past calls and wrote {len(out['lessons'])} lessons",
                "detail": ""}
    return {"when": when, "tone": "neutral", "headline": f"Worked on {subject}", "detail": ""}


def _side(arg: dict | None) -> dict | None:
    if not arg:
        return None
    return {"stance": arg.get("stance"), "confidence": arg.get("confidence"),
            "points": [str(p) for p in (arg.get("points") or [])][:3]}


def _debate(post: dict) -> dict:
    verdict = post.get("verdict") or {}
    decision = verdict.get("decision")
    words, tone = _DECISION_WORDS.get(decision or "", (
        str(decision).lower() if decision else "no decision (normal rules used)", "neutral"))
    return {"when": post.get("ts"), "strategy": post.get("key"),
            "tuner": _side(post.get("advocate")), "skeptic": _side(post.get("skeptic")),
            "verdict": {"decision": verdict.get("decision"), "words": words, "tone": tone,
                        "rationale": str(verdict.get("rationale", ""))}}


def _lessons(name: str, limit: int = 30) -> list[dict]:
    return [{"when": p["ts"], "lesson": str(p.get("lesson", "")), "evidence": str(p.get("evidence", "")),
             "confidence": p.get("confidence", "low")} for p in reversed(_posts(f"lessons:{name}", limit))]


# ------------------------------------------------------------------- stats
def _stats(rows: list[dict], name: str, kill_switch: bool) -> dict:
    now = datetime.now(timezone.utc)
    mine = [r for r in rows if r.get("agent") == name and r.get("kind") != "order"]
    week = [r for r in mine if (t := _parse_ts(r.get("ts"))) and t >= now - timedelta(days=7)]
    today = now.date().isoformat()
    last = _parse_ts(mine[-1]["ts"]) if mine else None
    if kill_switch:
        status = "paused"
    elif last and last >= now - timedelta(hours=ACTIVE_HOURS):
        status = "active"
    else:
        status = "resting" if mine else "not started"
    ok = sum(r.get("outcome") == "ok" for r in week)
    return {
        "status": status, "last_active": mine[-1]["ts"] if mine else None,
        "tasks_this_week": len(week),
        "success_rate": round(ok / len(week), 3) if week else None,
        "spend_today": round(sum(float(r.get("cost_usd") or 0) for r in mine
                                 if str(r.get("ts", "")).startswith(today)), 4),
        "lessons": len(_posts(f"lessons:{name}", 1000)),
    }


def _looked_at(rows: list[dict], name: str) -> dict:
    mine = [r for r in rows if r.get("agent") == name][-500:]
    subjects = Counter(r["subject"] for r in mine if r.get("subject") and r["subject"] != "reflection")
    metrics: Counter = Counter()
    for r in mine:
        for k in (r.get("input_summary") or {}):
            if k in _METRIC_WORDS:
                metrics[_METRIC_WORDS[k]] += 1
    return {"strategies": [{"name": k, "times": v} for k, v in subjects.most_common(12)],
            "numbers": [{"name": k, "times": v} for k, v in metrics.most_common(8)]}


def _persona(spec) -> dict:
    try:
        return spec.public()
    except AttributeError:  # config without an agents section (tests / partial deploys)
        return {**{k: getattr(spec, k) for k in ("name", "display_name", "title", "voice",
                                                 "about", "icon", "color")}, "model": ""}


# -------------------------------------------------------------------- views
def team() -> dict:
    rows = _audit_rows()
    activity = data.load_agent_activity(limit=1)
    debates = _posts("debate", 1)
    agents = []
    for spec in specs.all_specs():
        last = next((humanize(r) for r in reversed(rows) if r.get("agent") == spec.name), None)
        agents.append({**_persona(spec), "stats": _stats(rows, spec.name, activity["kill_switch"]),
                       "latest": last})
    return {"as_of": activity["as_of"], "agents": agents,
            "latest_debate": _debate(debates[-1]) if debates else None,
            "team": {k: activity[k] for k in ("budget", "supervisor", "kill_switch", "sleeve")}}


def profile(name: str) -> dict | None:
    spec = specs.by_name(name)
    if spec is None:
        return None
    rows = _audit_rows()
    kill = data.load_agent_activity(limit=1)["kill_switch"]
    diary = [humanize(r) for r in reversed(rows) if r.get("agent") == name][:100]
    return {**_persona(spec), "stats": _stats(rows, name, kill), "looked_at": _looked_at(rows, name),
            "diary": diary, "lessons": _lessons(name),
            "debates": [_debate(p) for p in reversed(_posts("debate", 20)) if _took_part(name, p)]}


def _took_part(name: str, post: dict) -> bool:
    """The Judge sits every debate; the Tuner/Skeptic only when they answered."""
    if name == "optimization":
        return bool(post.get("advocate"))
    if name == "validation":
        return bool(post.get("skeptic"))
    return True
