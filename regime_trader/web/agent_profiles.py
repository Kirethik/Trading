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
from ..utils import load_json
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
_PROVIDER_NAMES = {"groq": "Groq", "gemini": "Gemini", "openrouter": "OpenRouter", "mistral": "Mistral"}


def friendly_model(label: object) -> str:
    """'groq:openai/gpt-oss-120b' -> 'Groq · gpt-oss-120b'."""
    text = str(label or "")
    provider, sep, model = text.partition(":")
    if not sep:
        return f"Gemini · {text}" if text.startswith("gemini") else text
    return f"{_PROVIDER_NAMES.get(provider, provider)} · {model.split('/')[-1].replace(':free', '')}"


def humanize(row: dict) -> dict:
    """One audit row -> {when, headline, detail, tone, via}."""
    item = _humanize(row)
    return {**item, "via": friendly_model(row.get("model")) if row.get("model") and row.get("kind") != "order" else ""}


def _humanize(row: dict) -> dict:
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
    if "sentiment" in out:
        try:
            words, tone = _mood(float(out.get("sentiment", 0)))
        except (TypeError, ValueError):
            words, tone = "unclear", "neutral"
        return {"when": when, "tone": tone,
                "headline": f"Read the news on {subject}: {words} ({out.get('confidence', '?')} confidence)",
                "detail": str(out.get("why", ""))}
    if "headline" in out:
        return {"when": when, "tone": "neutral", "headline": f"Explained a problem: {out['headline']}",
                "detail": str(out.get("explanation", ""))}
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


# ---------------------------------------------------------------- watchman
_SEVERITY_TONE = {"critical": "bad", "warn": "neutral", "info": "good"}


def _watch_log(limit: int = 60) -> list[dict]:
    return [{"when": p["ts"], "severity": p.get("severity", "warn"),
             "tone": _SEVERITY_TONE.get(p.get("severity", "warn"), "neutral"),
             "status": p.get("status"), "headline": str(p.get("headline") or p.get("title") or ""),
             "explanation": str(p.get("explanation") or p.get("detail") or ""),
             "suggestion": str(p.get("suggestion") or ""), "actions": [str(a) for a in p.get("actions") or []]}
            for p in reversed(_posts("watch", limit))]


def watch_status() -> dict:
    """Current health from the Watchman's own state file + any agent pause."""
    cfg = data._cfg()
    state = load_json(cfg.resolve_path(cfg.state_dir) / "watchman.json", default=None) or {}
    pause = load_json(data._agent_path("pause_file"), default=None)
    active = [{"code": k, "severity": v.get("severity"), "title": v.get("title"), "since": v.get("first_seen")}
              for k, v in (state.get("active") or {}).items()
              if v.get("alerted", True) and not v.get("clear")]   # confirmed, still present
    paused_until = None
    if isinstance(pause, dict) and (t := _parse_ts(pause.get("until"))) and t > datetime.now(timezone.utc):
        paused_until = pause.get("until")
    return {"status": "not started" if not state else ("issues" if active else "all clear"),
            "last_check": state.get("last_tick"), "issues": active,
            "agents_paused_until": paused_until,
            "pause_reason": pause.get("reason") if paused_until else None}


# ---------------------------------------------------------------- reporter
_MOOD = [(-1.5, "very worried", "bad"), (-0.5, "worried", "bad"), (0.5, "neutral", "neutral"),
         (1.5, "upbeat", "good"), (99, "very upbeat", "good")]
_ACTION_WORDS = {"buy": "Bought (small, practice money)", "exit": "Sold (closing a news position)",
                 "caution": "Raised a caution", "considered": "Considered, but did not buy"}


def _safe_url(url: str) -> str:
    return url if str(url).lower().startswith(("https://", "http://")) else ""


def _news_store():
    from ..news.store import NewsStore
    cfg = data._cfg()
    news = cfg.get("news")
    rel = news.get("db") if news is not None else None
    path = cfg.resolve_path(rel) if rel else cfg.resolve_path(cfg.state_dir) / "news.db"
    return NewsStore(path) if path.exists() else None


def _card(a: dict) -> dict:
    return {"id": a["id"], "source": a["source"], "publisher": a["publisher"], "headline": a["headline"],
            "summary": a["summary"][:300], "url": _safe_url(a["url"]),
            "when": a["published_at"] or a["seen_at"], "symbols": a["symbols"]}


def _mood(sentiment: float) -> tuple[str, str]:
    return next((w, t) for limit, w, t in _MOOD if sentiment <= limit)


def news_feed(limit: int = 30, symbol: str | None = None) -> dict:
    """What the Reporter read, concluded and did — every action with its
    reasoning and the articles behind it."""
    store = _news_store()
    if store is None:
        return {"reading": [], "conclusions": [], "actions": []}
    by_id = {}

    def articles(ids: list[str]) -> list[dict]:
        missing = [i for i in ids if i not in by_id]
        if missing:
            for a in store.articles(limit=500):
                by_id.setdefault(a["id"], _card(a))
        return [by_id[i] for i in ids if i in by_id]

    reading = [_card(a) for a in store.articles(symbol=symbol, limit=limit)]
    latest: dict[str, dict] = {}
    for a in store.assessments(limit=200, symbol=symbol):
        latest.setdefault(a["symbol"], a)
    conclusions = []
    for sym, a in sorted(latest.items(), key=lambda kv: kv[1]["as_of"], reverse=True)[:12]:
        words, tone = _mood(float(a["sentiment"]))
        conclusions.append({"symbol": sym, "when": a["as_of"], "sentiment": a["sentiment"], "mood": words,
                            "tone": tone, "confidence": a["confidence"], "why": a["why"],
                            "supporting": articles(a["article_ids"])})
    actions = []
    for act in store.actions(limit=limit, symbol=symbol):
        tone = {"buy": "good", "exit": "neutral", "caution": "bad"}.get(act["kind"], "neutral")
        actions.append({"when": act["ts"], "symbol": act["symbol"], "kind": act["kind"],
                        "headline": f"{_ACTION_WORDS.get(act['kind'], act['kind'])}: {act['symbol']}",
                        "reason": act["reason"], "result": act["result"], "tone": tone,
                        "risk_check": (act["details"].get("order") or {}).get("reason"),
                        "supporting": articles(act["details"].get("articles") or [])})
    return {"reading": reading, "conclusions": conclusions, "actions": actions}


# --------------------------------------------------------------- providers
def provider_usage() -> list[dict]:
    """Today's free-tier usage per provider:model vs our cap, and resting state."""
    cfg = data._cfg()
    agents = cfg.get("agents")
    provs = (agents.get("providers").as_dict() if agents is not None and agents.get("providers") is not None else {})
    state = load_json(cfg.resolve_path(cfg.state_dir) / "llm_usage.json", default=None) or {}
    today = datetime.now(timezone.utc).date().isoformat()
    counts = state.get("counts", {}) if state.get("date") == today else {}
    rest = state.get("rest") or {}
    out = []
    for name, p in provs.items():
        if not p.get("enabled", True):
            continue
        for model, cap in (p.get("daily_requests") or {}).items():
            label = f"{name}:{model}"
            until = _parse_ts(rest.get(label))
            out.append({"label": friendly_model(label), "used": int(counts.get(label, 0)), "cap": int(cap),
                        "resting_until": rest.get(label) if until and until > datetime.now(timezone.utc) else None})
    return out


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
    feed = news_feed(limit=5)
    news_strip = [a for a in feed["actions"] if a["kind"] != "considered"][:3] or feed["actions"][:3]
    return {"as_of": activity["as_of"], "agents": agents, "watch": watch_status(), "news": news_strip,
            "providers": provider_usage(),
            "latest_debate": _debate(debates[-1]) if debates else None,
            "team": {k: activity[k] for k in ("budget", "supervisor", "kill_switch", "sleeve")}}


def profile(name: str) -> dict | None:
    spec = specs.by_name(name)
    if spec is None:
        return None
    rows = _audit_rows()
    kill = data.load_agent_activity(limit=1)["kill_switch"]
    diary = [humanize(r) for r in reversed(rows) if r.get("agent") == name][:100]
    extra = {"watch": watch_status(), "watch_log": _watch_log()} if name == "watchman" else {}
    if name == "reporter":
        extra = {"news": news_feed()}
    return {**_persona(spec), **extra, "stats": _stats(rows, name, kill), "looked_at": _looked_at(rows, name),
            "diary": diary, "lessons": _lessons(name),
            "debates": [_debate(p) for p in reversed(_posts("debate", 20)) if _took_part(name, p)]}


def _took_part(name: str, post: dict) -> bool:
    """The Judge sits every debate; the Tuner/Skeptic only when they answered."""
    if name == "optimization":
        return bool(post.get("advocate"))
    if name == "validation":
        return bool(post.get("skeptic"))
    return name == "governance"   # the Watchman and Reporter never debate
