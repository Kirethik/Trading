"""Deterministic news rules: assessed news -> actions. Pure; no I/O, no model.

The model only ever *describes* news (sentiment, confidence, why). Whether
anything happens is decided here, by fixed and fully tested gates, so a
manipulative or mistaken article can't talk the system into a trade.

  caution  strongly bad news (medium/high confidence) -> more careful
  buy      strongly good, high-confidence, corroborated news -> small agent-sleeve buy
  considered  strong good news that failed a gate (shown in the UI, no trade)
  exit     a news-driven holding reached its time limit or the news turned bad
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

_ORDER = {"caution": 0, "exit": 1, "buy": 2, "considered": 3}


@dataclass(frozen=True)
class Assessment:
    id: int | None
    symbol: str
    sentiment: float          # -2 (very bad) .. +2 (very good)
    confidence: str           # low | medium | high
    impact: str               # risk | opportunity | neutral
    why: str
    article_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    articles: int             # articles behind the assessment
    sources: int              # independent outlets/sources among them
    point_in_time: bool       # at least one Alpaca/SEC (time-stamped) item


@dataclass(frozen=True)
class Context:
    now: datetime
    allowed: frozenset[str]
    above_sma50: dict[str, bool]
    evidence: dict[str, Evidence]
    open_cautions: frozenset[str]
    paused: bool
    holdings: dict[str, dict]           # news-driven holdings: symbol -> {"opened", "weight"}
    buys_today: int
    sleeve_held: frozenset[str] = frozenset()   # anything the agents hold, for any reason


@dataclass(frozen=True)
class Action:
    kind: str
    symbol: str
    reason: str                         # plain English, shown to the owner
    assessment_id: int | None = None
    weight: float = 0.0                 # target share of the agent sleeve (buy)
    gates: tuple[str, ...] = field(default_factory=tuple)
    sell_ok: bool = False               # caution: strong enough evidence to also sell


def _days_held(opened: datetime, now: datetime) -> int:
    return int(np.busday_count(opened.date(), now.date()))


def _buy_gates(a: Assessment, ctx: Context, s: dict) -> list[str]:
    """Every reason NOT to buy, in plain English (empty list = all clear)."""
    failed = []
    ev = ctx.evidence.get(a.symbol, Evidence(0, 0, False))
    if a.confidence != "high":
        failed.append(f"the reading is only {a.confidence}-confidence (needs high)")
    if a.symbol not in ctx.allowed:
        failed.append(f"{a.symbol} is not in the trading universe")
    if ev.sources < int(s["min_sources"]):
        failed.append(f"only {ev.sources} independent source{'s' if ev.sources != 1 else ''} "
                      f"(needs {int(s['min_sources'])})")
    if not ev.point_in_time:
        failed.append("no time-stamped source (Alpaca/SEC) backs it up")
    if not ctx.above_sma50.get(a.symbol, False):
        failed.append(f"{a.symbol} is trading below its 50-day average")
    if a.symbol in ctx.open_cautions:
        failed.append(f"there is an active caution on {a.symbol}")
    if ctx.paused:
        failed.append("agent buying is paused by the Watchman")
    if a.symbol in ctx.holdings:
        failed.append(f"the agents already hold {a.symbol} on news")
    elif a.symbol in ctx.sleeve_held:
        failed.append(f"the agents already hold {a.symbol} for another reason")
    if ctx.buys_today >= int(s["max_news_buys_per_day"]):
        failed.append(f"the daily limit of {int(s['max_news_buys_per_day'])} news buys was reached")
    exposure = sum(float(h.get("weight", 0)) for h in ctx.holdings.values())
    if exposure + float(s["buy_weight"]) > float(s["max_news_exposure"]) + 1e-9:
        failed.append(f"it would push news-driven holdings past {float(s['max_news_exposure']):.0%} of the sleeve")
    return failed


def decide(assessments: dict[str, Assessment], ctx: Context, settings: dict) -> list[Action]:
    s = settings
    out: list[Action] = []
    for sym in sorted(assessments):
        a = assessments[sym]
        if (a.sentiment <= float(s["caution_threshold"]) and a.confidence in ("medium", "high")
                and sym not in ctx.open_cautions):
            ev = ctx.evidence.get(sym, Evidence(0, 0, False))
            # selling needs the same rigour as buying; otherwise only flag + block buys
            sell_ok = a.confidence == "high" and ev.point_in_time
            note = "" if sell_ok else " (flag only: not enough time-stamped, high-confidence evidence to sell)"
            out.append(Action("caution", sym, f"Strongly negative news ({a.sentiment:+.1f}/2, "
                              f"{a.confidence} confidence): {a.why}{note}", a.id, sell_ok=sell_ok))
        elif a.sentiment >= float(s["buy_threshold"]) and a.impact == "opportunity":
            failed = _buy_gates(a, ctx, s)
            if failed:
                out.append(Action("considered", sym, "Good news, but I did not buy because "
                                  + "; ".join(failed) + ".", a.id, gates=tuple(failed)))
            else:
                ev = ctx.evidence[sym]
                out.append(Action("buy", sym, f"Strongly positive, corroborated news ({a.sentiment:+.1f}/2 from "
                                  f"{ev.sources} sources): {a.why}", a.id, weight=float(s["buy_weight"])))

    for sym in sorted(ctx.holdings):
        held = _days_held(ctx.holdings[sym]["opened"], ctx.now)
        a = assessments.get(sym)
        if a is not None and a.sentiment <= float(s["exit_sentiment"]):
            out.append(Action("exit", sym, f"The news turned negative ({a.sentiment:+.1f}/2): {a.why}", a.id))
        elif held >= int(s["hold_days"]):
            out.append(Action("exit", sym, f"News-driven positions are held for at most {int(s['hold_days'])} "
                              f"trading days; this one reached {held}."))
    return sorted(out, key=lambda x: (_ORDER[x.kind], x.symbol))
