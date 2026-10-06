"""The Watchman's rules: pure, deterministic checks over a snapshot of the
trading system. No model, no I/O — every finding here is reproducible, and
the LLM is only ever asked to *explain* one, never to detect it.

Severity: "warn" = tell the human; "critical" = tell the human AND protect
(pause agent buying, trim the agent sleeve). Nothing here touches the main
system's positions or any risk limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

WARN, CRITICAL = "warn", "critical"


@dataclass(frozen=True)
class Finding:
    code: str            # stable id, used for de-duplication
    severity: str        # warn | critical
    title: str           # plain English, no jargon
    detail: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    market_open: bool | None                 # None = broker unreachable
    account_equity: float | None
    broker_positions: dict[str, dict] | None  # symbol -> {"qty", "market_value"}
    dashboard: dict                          # state/dashboard.json (regime orchestrator)
    session: dict                            # state/session.json
    omnivore: dict                           # state/omnivore_ledger.json
    agent_ledger: dict                       # state/agent_ledger.json
    breaker_locked: bool
    recent_outcomes: list[str]               # newest-last agent call outcomes
    budget: dict                             # today's spend snapshot
    market_open_since: datetime | None = None  # first tick this session the market was open
    news_cautions: dict | None = None          # symbol -> reason (Reporter cautions)


def _norm(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def _marks(s: Snapshot) -> dict[str, float]:
    """Per-share prices from the broker's own valuation (market_value / qty)."""
    out = {}
    for sym, p in (s.broker_positions or {}).items():
        try:
            qty = float(p.get("qty", 0))
            if qty:
                out[_norm(sym)] = abs(float(p.get("market_value", 0.0)) / qty)
        except (TypeError, ValueError):
            continue
    return out


def _sleeve_value(s: Snapshot) -> float:
    marks = _marks(s)
    total = 0.0
    for sym, p in ((s.agent_ledger or {}).get("positions") or {}).items():
        px = marks.get(_norm(sym)) or float(p.get("price", p.get("entry", 0)) or 0)
        total += float(p.get("qty", 0)) * px
    return total


def _parse(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ checks
def check_broker(s: Snapshot, cfg: dict) -> list[Finding]:
    if s.market_open is None:
        return [Finding("broker_unreachable", WARN, "I can't reach the broker right now",
                        "Account checks are skipped until the connection comes back.")]
    return []


def check_breakers(s: Snapshot, cfg: dict) -> list[Finding]:
    out = []
    if s.breaker_locked:
        out.append(Finding("hard_lock", CRITICAL, "The emergency stop is ON — all buying has halted",
                           "The account fell far enough from its peak to trip the hard lock. "
                           "A person must review and release it; I will not.", {}))
    mode = (s.dashboard.get("risk") or {}).get("breaker_mode", "normal")
    if mode not in ("normal", None, "") and not s.breaker_locked:
        reasons = "; ".join((s.dashboard.get("risk") or {}).get("reasons", []))
        out.append(Finding("breaker_" + str(mode), WARN, f"A safety brake is active ({mode})",
                           reasons or "Losses crossed a circuit-breaker tier.", {"mode": mode}))
    return out


def check_daily_loss(s: Snapshot, cfg: dict) -> list[Finding]:
    pnl = (s.dashboard.get("portfolio") or {}).get("daily_pnl_pct")
    limit = float(cfg.get("big_loss_pct", -0.03))
    if isinstance(pnl, (int, float)) and pnl <= limit:
        return [Finding("big_daily_loss", WARN, f"The main system is down {pnl:.1%} today",
                        f"That is past the {limit:.0%} warning line.", {"daily_pnl_pct": pnl})]
    return []


def check_exposure(s: Snapshot, cfg: dict) -> list[Finding]:
    """Whole-account exposure from the broker's own numbers — this sees the
    main system, the strategy cohort AND the agent sleeve together."""
    if not s.broker_positions or not s.account_equity:
        return []
    gross = sum(abs(float(p.get("market_value", 0.0))) for p in s.broker_positions.values())
    ratio = gross / float(s.account_equity)
    limit = float(cfg.get("max_gross_exposure", 1.5))
    if ratio > limit:
        share = _sleeve_value(s) / float(s.account_equity)
        return [Finding("combined_exposure", CRITICAL,
                        f"Too much money is invested at once ({ratio:.0%} of the account)",
                        f"The limit is {limit:.0%}. The main system and the agents together crossed it.",
                        {"ratio": round(ratio, 4), "sleeve_share": round(share, 4)})]
    if ratio > 0.9 * limit:
        return [Finding("exposure_high", WARN, f"Investment level is close to the limit ({ratio:.0%})",
                        f"The limit is {limit:.0%}.", {"ratio": round(ratio, 4)})]
    return []


def check_sleeve_loss(s: Snapshot, cfg: dict) -> list[Finding]:
    led = s.agent_ledger or {}
    day = led.get("day") or {}
    start, cash = day.get("start_equity"), led.get("cash")
    # only today's baseline counts: the ledger rolls its day on the next order,
    # so a stale start_equity would otherwise flag (and trim) every day forever
    if not start or cash is None or day.get("date") != s.now.date().isoformat():
        return []
    pnl = (float(cash) + _sleeve_value(s)) / float(start) - 1.0
    limit = float(cfg.get("sleeve_loss_pct", -0.04))
    if pnl <= limit:
        return [Finding("sleeve_loss", CRITICAL, f"The agents' own trades are down {pnl:.1%} today",
                        f"Past the {limit:.0%} line, so I am pausing agent buying and trimming "
                        "their positions.", {"sleeve_pnl_pct": round(pnl, 4)})]
    return []


def _tracked_qty(s: Snapshot) -> dict[str, float]:
    out: dict[str, float] = {}

    def add(sym: str, qty: object) -> None:
        try:
            out[_norm(sym)] = out.get(_norm(sym), 0.0) + float(qty)
        except (TypeError, ValueError):
            pass

    for sym, p in (s.session.get("positions") or {}).items():
        add(sym, p.get("qty", 0))
    for entry in (s.omnivore or {}).values():
        if isinstance(entry, dict):
            for sym, p in (entry.get("positions") or {}).items():
                add(sym, p.get("qty", 0))
    for sym, p in ((s.agent_ledger or {}).get("positions") or {}).items():
        add(sym, p.get("qty", 0))
    return out


def check_reconciliation(s: Snapshot, cfg: dict) -> list[Finding]:
    """What our books say we hold vs what the broker says we hold."""
    if s.broker_positions is None:
        return []
    tol = float(cfg.get("reconcile_tolerance", 0.02))
    tracked = _tracked_qty(s)
    broker = {_norm(k): float(v.get("qty", 0)) for k, v in s.broker_positions.items()}
    off = {}
    for sym in set(tracked) | set(broker):
        t, b = tracked.get(sym, 0.0), broker.get(sym, 0.0)
        if abs(t - b) > max(tol * max(abs(t), abs(b)), 1e-6):
            off[sym] = {"our_books": round(t, 6), "broker": round(b, 6)}
    if not off:
        return []
    names = ", ".join(sorted(off)[:5])
    return [Finding("books_mismatch", WARN, f"Our records don't match the broker for {len(off)} holding(s)",
                    f"Affected: {names}. Usually an order that hasn't filled yet or a stop "
                    "that fired at the broker.", {"symbols": off})]


def check_loop_alive(s: Snapshot, cfg: dict) -> list[Finding]:
    """A trading loop that was running recently but has gone quiet."""
    if not s.market_open:
        return []
    stale = timedelta(minutes=float(cfg.get("stale_minutes", 150)))
    last = _parse(s.session.get("last_cycle")) or _parse(s.dashboard.get("ts"))
    if last is None or last.tzinfo is None or s.now - last > timedelta(days=7):
        return []   # never deployed / long retired: not a live loop
    # count silence from this session's open, so the overnight gap isn't "silence"
    ref = max(last, s.market_open_since) if s.market_open_since else last
    age = s.now - ref
    if age > stale:
        return [Finding("loop_silent", WARN, "The main trading loop has gone quiet",
                        f"Its last check-in was {int(age.total_seconds() // 60)} minutes ago "
                        "while the market is open.", {"minutes": int(age.total_seconds() // 60)})]
    return []


def check_agent_health(s: Snapshot, cfg: dict) -> list[Finding]:
    out = []
    streak = int(cfg.get("failure_streak", 5))
    tail = [o for o in s.recent_outcomes if o != "blocked"][-streak:]
    if len(tail) == streak and all(o != "ok" for o in tail):
        out.append(Finding("agents_failing", WARN, f"The AI helpers failed their last {streak} tasks",
                           "Usually the AI service is busy. The normal rules keep deciding meanwhile."))
    spent, cap = float(s.budget.get("spent_usd", 0)), float(s.budget.get("daily_usd", 0) or 0)
    if cap and spent >= float(cfg.get("budget_warn_pct", 0.8)) * cap:
        out.append(Finding("budget_high", WARN, f"Today's AI spending is at {spent / cap:.0%} of the limit",
                           f"${spent:.2f} of ${cap:.2f}.", {"spent": spent, "cap": cap}))
    return out


def check_news_risk(s: Snapshot, cfg: dict) -> list[Finding]:
    if not s.news_cautions:
        return []
    names = ", ".join(sorted(s.news_cautions))
    return [Finding("news_caution", WARN, f"Bad news is being watched for {names}",
                    "The Reporter found strongly negative news; agents won't buy these until it clears.",
                    {"symbols": {k: str(v)[:200] for k, v in s.news_cautions.items()}})]


CHECKS = (check_news_risk, check_broker, check_breakers, check_daily_loss, check_exposure, check_sleeve_loss,
          check_reconciliation, check_loop_alive, check_agent_health)


def run_checks(s: Snapshot, cfg: dict) -> list[Finding]:
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(s, cfg))
    return findings
