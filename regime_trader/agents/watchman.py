"""The Watchman — always-on live oversight (runs inside the agent supervisor).

Every tick: snapshot the system -> run the deterministic rules
(watch_checks) -> report NEW / WORSENED / long-unresolved findings (warnings
only after they persist for two ticks, so transient blips don't page you),
protect on critical ones, and announce when a problem has really cleared
(two clean ticks).

Authority ("protect, don't trade", fully automatic, always warns):
  * pause the other agents' BUYING — re-armed every tick while anything
    critical is active, auto-expires once it isn't,
  * sell-only trim of the agents' own sleeve, ONCE per problem per day,
    retried until the market accepts it, through the RiskManager veto (using
    the sleeve's private lock — never the main system's breaker.lock).
It never touches the main system's or the cohort's positions, never changes
a risk limit, and never engages or releases the hard lock.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import atomic_write_json, load_json, utc_now
from . import guards, runtime, specs
from .audit import AuditLog
from .blackboard import Blackboard
from .watch_checks import CRITICAL, Finding, Snapshot, run_checks

log = get_logger("agents")

EXPLAIN_SCHEMA = {"headline": "string", "explanation": "string", "suggestion": "string"}
_RANK = {"warn": 1, "critical": 2}
CONFIRM_TICKS = 2      # a warning must persist this many ticks before it alerts
CLEAR_TICKS = 2        # a finding must be absent this many ticks before "resolved"
MIN_SLEEVE_SHARE = 0.02  # combined_exposure trims only if agents hold >= 2% of the account


def _settings() -> dict:
    w = get_config().agents.get("watchman")
    out = w.as_dict() if w is not None else {}
    out.setdefault("max_gross_exposure", float(get_config().risk.max_gross_exposure))
    return out


def _safe_load(path: Path) -> dict:
    """A torn or corrupt state file must never take the Watchman down."""
    try:
        data = load_json(path, default=None)
    except (OSError, ValueError) as exc:
        log.warning("watchman: unreadable %s (%s); treating as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _ts(value: object, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default


class Watchman:
    def __init__(self, broker=None, *, state_path: str | Path | None = None,
                 board: Blackboard | None = None, audit: AuditLog | None = None,
                 explain: Callable[[Finding, dict], dict | None] | None = None,
                 executor=None, alert: Callable[[str], None] | None = None,
                 settings: dict | None = None, clock: Callable[[], Any] = utc_now):
        cfg = get_config()
        self.broker = broker
        self.settings = settings or _settings()
        self.state_path = Path(state_path or cfg.resolve_path(cfg.state_dir) / "watchman.json")
        self.board = board
        self.audit = audit
        self.explain = explain or self._explain_with_model
        self.executor = executor
        self.alert = alert or _default_alert
        self.clock = clock

    # ---------------------------------------------------------- snapshot
    def snapshot(self, memory: dict | None = None) -> Snapshot:
        cfg = get_config()
        state = cfg.resolve_path(cfg.state_dir)
        market_open = equity = positions = None
        if self.broker is not None:
            try:
                market_open = bool(self.broker.is_market_open())
                equity = float(self.broker.account_equity()[0])
                positions = self.broker.positions()
            except Exception as exc:  # broker hiccup -> a finding, never a crash
                log.warning("watchman: broker unavailable (%s)", exc)
                market_open = equity = positions = None
        try:
            audit = self.audit or AuditLog.from_config()
            outcomes = [r.get("outcome") for r in audit.tail(60)
                        if r.get("outcome") and r.get("agent") != "watchman"]
        except OSError:
            outcomes = []
        budget = _safe_load(cfg.resolve_path(cfg.agents.budget_file))
        if budget.get("date") != self.clock().date().isoformat():
            budget = {}
        budget = {**budget, "daily_usd": float(cfg.agents.daily_budget_usd)}
        open_since = (memory or {}).get("market_open_since")
        try:
            from ..news.store import NewsStore
            rep = cfg.agents.get("reporter")
            hours = float(rep.get("caution_hours", 24)) if rep is not None else 24.0
            cautions = {s: a.get("reason", "") for s, a in
                        NewsStore.from_config().open_cautions(self.clock(), hours).items()}
        except Exception:
            cautions = {}
        return Snapshot(
            now=self.clock(), market_open=market_open, account_equity=equity,
            broker_positions=positions,
            dashboard=_safe_load(state / "dashboard.json"),
            session=_safe_load(state / "session.json"),
            omnivore=_safe_load(cfg.resolve_path(cfg.omnivore.ledger_file)),
            agent_ledger=_safe_load(cfg.resolve_path(cfg.agents.execution.ledger_file)),
            breaker_locked=cfg.resolve_path(cfg.risk.lock_file).exists(),
            recent_outcomes=outcomes, budget=budget,
            market_open_since=_ts(open_since, None) if open_since else None,
            news_cautions=cautions,
        )

    # -------------------------------------------------------------- tick
    def tick(self, snapshot: Snapshot | None = None) -> list[dict]:
        """One oversight pass. Returns the events it posted (for tests/logs)."""
        if guards.kill_switch_engaged():
            return []  # the human's master off switch silences the Watchman too
        memory = _safe_load(self.state_path)
        snap = snapshot or self.snapshot(memory)
        now = self.clock()
        memory = self._track_session(memory, snap, now)
        active: dict = dict(memory.get("active", {}))
        findings = {f.code: f for f in run_checks(snap, self.settings)}
        realert = timedelta(hours=float(self.settings.get("realert_hours", 6)))
        events: list[dict] = []

        def save() -> None:
            self._save({**memory, "active": active, "last_tick": now.isoformat(timespec="seconds"),
                        "status": "issues" if any(a.get("alerted") for a in active.values()) else "all clear"})

        for code, f in findings.items():
            prev = dict(active.get(code) or {})
            seen = int(prev.get("seen", 0)) + 1
            entry = {**prev, "severity": f.severity, "title": f.title, "seen": seen, "clear": 0,
                     "first_seen": prev.get("first_seen", now.isoformat(timespec="seconds"))}
            confirmed = f.severity == CRITICAL or seen >= CONFIRM_TICKS
            worse = bool(prev.get("alerted")) and _RANK[f.severity] > _RANK.get(prev.get("severity"), 0)
            stale = bool(prev.get("alerted")) and now - _ts(prev.get("last_alert"), now) >= realert
            actions: list[str] = []
            newsworthy = False
            if f.severity == CRITICAL and self._should_trim(f, now, entry):
                trim_actions, pending, sold = self._trim(f, now)
                actions += trim_actions
                # a retry that is still waiting for the market isn't news
                newsworthy = bool(trim_actions) and (not prev.get("trim_pending") or sold)
                entry["trim_tag"] = now.date().isoformat() if not pending else None
                entry["trim_pending"] = pending
            if confirmed and (not prev.get("alerted") or worse or stale or newsworthy):
                if f.severity == CRITICAL and not prev.get("alerted"):
                    actions.insert(0, self._pause_text())
                status = "new" if not prev.get("alerted") else "ongoing"
                events.append(self._report(f, status, actions))
                entry.update(alerted=True, last_alert=now.isoformat(timespec="seconds"))
            active[code] = entry
            save()  # persist per finding: a crash later can't cause a re-alert storm

        for code in [c for c in active if c not in findings]:
            entry = {**active[code], "clear": int(active[code].get("clear", 0)) + 1, "seen": 0}
            if entry["clear"] < CLEAR_TICKS:
                active[code] = entry
                continue
            gone = active.pop(code)
            if gone.get("alerted"):
                events.append(self._resolved(code, gone))

        if any(a.get("severity") == CRITICAL for c, a in active.items() if c in findings):
            self._pause(findings)   # re-armed every tick while a critical persists
        save()
        return events

    # ----------------------------------------------------------- actions
    def _track_session(self, memory: dict, snap: Snapshot, now: datetime) -> dict:
        """Remember when the market opened this session (for loop_silent)."""
        if snap.market_open and not memory.get("market_open_since"):
            return {**memory, "market_open_since": now.isoformat(timespec="seconds")}
        if snap.market_open is False and memory.get("market_open_since"):
            return {k: v for k, v in memory.items() if k != "market_open_since"}
        return memory

    def _pause_text(self) -> str:
        return f"Paused the other agents' buying (renewed every {int(self.settings.get('interval_seconds', 300)) // 60} min while this lasts)"

    def _pause(self, findings: dict) -> None:
        crit = [f.title for f in findings.values() if f.severity == CRITICAL]
        try:
            guards.pause_agents(float(self.settings.get("pause_hours", 4)), "Watchman: " + "; ".join(crit))
        except Exception as exc:
            log.warning("watchman could not write the pause (%s)", exc)

    def _should_trim(self, f: Finding, now: datetime, entry: dict) -> bool:
        if self.executor is None:
            return False
        if f.code == "combined_exposure" and float(f.data.get("sleeve_share", 0)) < MIN_SLEEVE_SHARE:
            return False   # the agents hold too little to make a difference: warn + pause only
        if f.code not in ("combined_exposure", "sleeve_loss"):
            return False
        return entry.get("trim_tag") != now.date().isoformat() or bool(entry.get("trim_pending"))

    def _trim(self, f: Finding, now: datetime) -> tuple[list[str], bool, bool]:
        """Returns (actions, still_pending, sold_something).
        Pending = the market didn't take (all of) it yet; retried next tick."""
        frac = float(self.settings.get("trim_fraction", 0.5))
        try:
            results = self.executor.trim_all(frac, f"Watchman protective trim: {f.title}",
                                             tag=f"{now.date().isoformat()}-{f.code}")
        except Exception as exc:
            log.warning("watchman trim failed: %s", exc)
            return [f"Tried to trim the agents' positions but it failed ({type(exc).__name__}); will retry"], True, False
        if not results:
            return [], False, False
        sold = [r["symbol"] for r in results if r.get("status") in ("submitted", "duplicate")]
        deferred = [r["symbol"] for r in results if r.get("status") == "deferred"]
        actions = []
        if sold:
            actions.append(f"Sold {frac:.0%} of the agents' {', '.join(sold)}")
        if deferred:
            actions.append(f"Will sell {frac:.0%} of the agents' {', '.join(deferred)} as soon as the market opens")
        other = [f"{r['symbol']}: {r.get('status')}" for r in results
                 if r.get("status") not in ("submitted", "duplicate", "deferred")]
        if other:
            actions.append("Could not trim " + ", ".join(other))
        return actions, bool(deferred), bool(sold)

    # ------------------------------------------------------- reporting
    def _report(self, f: Finding, status: str, actions: list[str]) -> dict:
        text = None
        if self._explanations_left():
            try:
                text = self.explain(f, {"actions_taken": actions, "status": status})
            except Exception as exc:
                log.warning("watchman explanation failed: %s", exc)
        if not isinstance(text, dict) or not text.get("headline"):
            text = {"headline": f.title, "explanation": f.detail,
                    "suggestion": "No action needed from you unless this keeps happening."}
        event = {"code": f.code, "severity": f.severity, "status": status, "title": f.title,
                 "detail": f.detail, "headline": text["headline"],
                 "explanation": text.get("explanation", ""), "suggestion": text.get("suggestion", ""),
                 "actions": actions, "data": f.data}
        self._post(event)
        acted = f" | I did: {'; '.join(actions)}" if actions else ""
        self._send(f"[Watchman] {f.severity.upper()}: {text['headline']}{acted}")
        return event

    def _resolved(self, code: str, gone: dict) -> dict:
        event = {"code": code, "severity": "info", "status": "resolved", "title": gone.get("title", code),
                 "headline": f"All clear: {gone.get('title', code)}", "explanation": "", "suggestion": "",
                 "actions": [], "data": {}}
        self._post(event)
        self._send(f"[Watchman] RESOLVED: {gone.get('title', code)}")
        return event

    def _send(self, message: str) -> None:
        try:
            self.alert(message)
        except Exception as exc:  # a webhook outage must not stop oversight
            log.warning("watchman alert failed: %s", exc)

    def _post(self, event: dict) -> None:
        try:
            (self.board or Blackboard.from_config()).post("watch", "watchman", event)
        except Exception as exc:
            log.warning("watchman could not record event: %s", exc)

    def _save(self, state: dict) -> None:
        try:
            atomic_write_json(self.state_path, state)
        except OSError as exc:
            log.warning("watchman could not save state: %s", exc)

    def _explanations_left(self) -> bool:
        cap = int(self.settings.get("max_explanations_per_hour", 6))
        try:
            recent = (self.board or Blackboard.from_config()).latest("watch", cap * 2)
        except Exception:
            return False
        hour_ago = self.clock() - timedelta(hours=1)
        explained = [p for p in recent if p.get("payload", {}).get("status") != "resolved"
                     and _ts(p.get("ts"), hour_ago) > hour_ago]
        return len(explained) < cap

    @staticmethod
    def _explain_with_model(f: Finding, context: dict) -> dict | None:
        spec = specs.by_name("watchman")
        if spec is None:
            return None
        return runtime.run_agent(
            spec, "A rule just flagged this situation in the trading system. Explain it for a "
                  "non-expert in plain English: a short headline, two or three sentences on what "
                  "it means and why it matters, and one concrete suggestion. Do not invent numbers.",
            {"finding": {"title": f.title, "detail": f.detail, "severity": f.severity, "data": f.data},
             **context},
            EXPLAIN_SCHEMA, subject=f.code)


def _default_alert(message: str) -> None:
    from ..monitor.alerts import AlertKind, AlertManager
    AlertManager().send(AlertKind.AGENT, message, dedupe_key=f"watch:{message[:60]}")


def from_config() -> Watchman | None:
    """Production wiring: paper broker + agent-sleeve executor. None if disabled."""
    w = get_config().agents.get("watchman")
    if w is not None and not w.get("enabled", True):
        return None
    broker = executor = None
    try:
        from ..broker.alpaca import AlpacaBroker
        from .execution import executor_from_config

        broker = AlpacaBroker(paper=True)
        executor = executor_from_config("watchman", broker)
    except Exception as exc:  # the Watchman still runs its file-based checks
        log.warning("watchman starting without broker (%s)", exc)
    return Watchman(broker, executor=executor)
