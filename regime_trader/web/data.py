"""Read-only loaders for the dashboard.

Everything here just parses on-disk artifacts the governance cycle already
writes (``runs/governance/<date>/`` + ``state/*.json`` + ``logs/``). No trading
stack is imported and nothing is recomputed — so the web process stays light
and can never place an order. The one exception is `live_portfolio`, which
makes a single read-only market-data call (latest trade quotes) to mark the
omnivore book to market.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from ..config import get_config
from ..monitor.log_setup import get_logger
from ..utils import load_json, utc_now_iso

log = get_logger("main")

_LOG_CHANNELS = {"main", "trades", "alerts", "regime", "governance"}
_SUMMARY_KV = re.compile(r"^-\s+\*\*(?P<k>[^:*]+):\*\*\s+(?P<v>.*?)\s*$")

# one structured trades-log record: "<asctime> <LEVEL> rt.trades: <msg> | <json>"
_TRADE_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
    r"(?P<level>\w+)\s+rt\.trades:\s+(?P<msg>.*?)(?:\s+\|\s+(?P<json>\{.*\}))?$"
)
_REGIME_FIT = re.compile(r"regime model fit:\s+(?P<states>\d+) states selected by BIC")
_RISK_CAP = re.compile(r"risk cap\s+[\d.]+%")   # collapse per-run numeric variants
_PCT_NUM = re.compile(r"[-\d.]+%")

# friendly names for the symbols in the interactive universe
_SYMBOL_LABELS = {
    "ETH/USD": "Ethereum", "BTC/USD": "Bitcoin", "SOL/USD": "Solana",
    "ADA/USD": "Cardano", "LTC/USD": "Litecoin", "DOGE/USD": "Dogecoin",
    "AVAX/USD": "Avalanche", "LINK/USD": "Chainlink",
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "TSLA": "Tesla",
    "SPY": "S&P 500 ETF", "QQQ": "Nasdaq 100 ETF", "VTI": "Total Market ETF",
}


def _cfg():
    return get_config()


def _gov_dir() -> Path:
    cfg = _cfg()
    return cfg.resolve_path(cfg.runs_dir) / "governance"


def _state_dir() -> Path:
    cfg = _cfg()
    return cfg.resolve_path(cfg.state_dir)


def _log_dir() -> Path:
    cfg = _cfg()
    return cfg.resolve_path(cfg.monitoring.log_dir)


def _tail_lines(path: Path, max_bytes: int = 400_000) -> list[str]:
    """Read only the tail of a (possibly large, rotating) log file.

    Returns whole lines; the first line is dropped when the file was truncated
    mid-line so callers never see a partial record. Keeps the dashboard light
    even when ``trades.log`` is several megabytes.
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and len(lines) > 1:
        lines = lines[1:]
    return lines


# ---------------------------------------------------------------- run discovery
def available_run_dates() -> list[str]:
    d = _gov_dir()
    if not d.exists():
        return []
    out = [p.name for p in d.iterdir() if p.is_dir() and (p / "summary.md").exists()]
    return sorted(out, reverse=True)


def latest_run_date() -> str | None:
    dates = available_run_dates()
    return dates[0] if dates else None


def _run_dir(date: str | None) -> Path | None:
    date = date or latest_run_date()
    if not date:
        return None
    d = _gov_dir() / date
    return d if d.exists() else None


# --------------------------------------------------------------- core artifacts
def load_registry() -> dict:
    cfg = _cfg()
    return load_json(cfg.resolve_path(cfg.governance.registry_file), default={}) or {}


def load_decisions_ledger(limit: int | None = None) -> list:
    cfg = _cfg()
    led = load_json(cfg.resolve_path(cfg.governance.decisions_file), default=[]) or []
    if limit and len(led) > limit:
        return led[-limit:]
    return led


def load_run_decisions(date: str | None = None) -> list:
    """The day's decision slice, as written by `reporting.write_run`."""
    rd = _run_dir(date)
    if rd is None:
        return []
    return load_json(rd / "governance_decisions.json", default=[]) or []


def parse_summary(date: str | None = None) -> dict:
    rd = _run_dir(date)
    if rd is None:
        return {}
    f = rd / "summary.md"
    if not f.exists():
        return {}
    meta: dict = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        m = _SUMMARY_KV.match(line)
        if m:
            meta[m.group("k").strip()] = m.group("v").strip()
            continue
        if line.startswith("- strategies evaluated:"):
            meta["strategies_evaluated"] = line.split(":", 1)[1].strip()
        elif line.startswith("- PASS:"):
            meta["pass_reject"] = line.split(":", 1)[1].strip()
    return meta


def load_leaderboard(date: str | None = None) -> dict:
    rd = _run_dir(date)
    if rd is None:
        return {"date": None, "rows": []}
    csv = rd / "master_leaderboard.csv"
    if not csv.exists():
        return {"date": rd.name, "rows": []}
    try:
        df = pd.read_csv(csv)
    except Exception:
        return {"date": rd.name, "rows": []}
    df = df.where(pd.notnull(df), None)
    return {"date": rd.name, "rows": df.to_dict("records")}


def list_strategies(date: str | None = None) -> list[str]:
    rd = _run_dir(date)
    if rd is None:
        return []
    sd = rd / "strategies"
    if not sd.exists():
        return []
    return sorted(p.stem for p in sd.glob("*.json"))


def load_strategy(key: str, date: str | None = None) -> dict:
    rd = _run_dir(date)
    if rd is None:
        return {}
    f = rd / "strategies" / f"{key}.json"
    data = load_json(f, default=None)
    if data is None:
        return {}
    # attach live equity history (if this strategy trades in the omnivore cohort)
    omni = _omnivore_ledger().get(key)
    if omni:
        data["equity_history"] = omni.get("equity_history", [])
        data["live"] = {
            "realized_pnl": omni.get("realized_pnl"),
            "trades": omni.get("trades"),
            "positions": omni.get("positions", {}),
            "allocated": omni.get("allocated"),
            "enabled": omni.get("enabled"),
        }
    return data


# ------------------------------------------------------------------- omnivore
def _omnivore_ledger() -> dict:
    cfg = _cfg()
    return load_json(cfg.resolve_path(cfg.omnivore.ledger_file), default={}) or {}


def load_omnivore() -> dict:
    led = _omnivore_ledger()
    rows = []
    equity = {}
    for sid, e in led.items():
        hist = e.get("equity_history", [])
        allocated = e.get("allocated", 0.0) or 0.0
        current = hist[-1][1] if hist else allocated
        # slice Sharpe over the live equity history (matches deploy.report)
        sharpe = 0.0
        if len(hist) > 10:
            eq = pd.Series([h[1] for h in hist], dtype=float)
            rets = eq.pct_change().dropna()
            if rets.std() > 0:
                sharpe = float(rets.mean() / rets.std() * (252 ** 0.5))
        rows.append({
            "strategy": sid,
            "market": e.get("market", "?"),
            "enabled": e.get("enabled", True),
            "equity": round(float(current), 2),
            "total_pnl": round(float(current) - allocated, 2),
            "pnl_pct": round((float(current) / allocated - 1.0) if allocated else 0.0, 4),
            "realized_pnl": round(e.get("realized_pnl", 0.0) or 0.0, 2),
            "sharpe": round(sharpe, 2),
            "trades": e.get("trades", 0),
            "open_positions": len(e.get("positions", {})),
            "observations": len(hist),
        })
        equity[sid] = hist
    rows.sort(key=lambda r: r["pnl_pct"], reverse=True)
    return {"rows": rows, "equity": equity}


def live_portfolio() -> dict:
    """Live mark-to-market view of the omnivore paper book.

    Totals invested capital, cash, position market value and P&L across all
    strategies, plus a flat per-position table priced with the latest trade
    quote. If the quote fetch fails (no credentials / network), positions
    fall back to their entry price and `live` is reported False.
    """
    led = _omnivore_ledger()
    symbols = sorted({sym for e in led.values() for sym in e.get("positions", {})})

    prices: dict[str, float] = {}
    live = False
    if symbols:
        try:
            from ..data.provider import DataProvider

            prices = DataProvider().get_latest_prices(symbols)
            live = bool(prices)
        except Exception as exc:
            log.warning("live price fetch failed: %s", exc)

    strategies = []
    positions = []
    t_invested = t_cash = t_mv = t_realized = t_unrealized = 0.0
    enabled_count = 0

    for sid, e in led.items():
        is_enabled = bool(e.get("enabled", True))
        allocated = float(e.get("allocated", 0.0) or 0.0)
        cash = float(e.get("cash", 0.0) or 0.0)
        realized = float(e.get("realized_pnl", 0.0) or 0.0)

        mv = 0.0
        for sym, p in e.get("positions", {}).items():
            qty = float(p.get("qty", 0.0) or 0.0)
            entry = float(p.get("entry", 0.0) or 0.0)
            price = prices.get(sym, entry)
            value = qty * price
            cost = qty * entry
            upnl = value - cost
            mv += value
            if is_enabled:
                t_unrealized += upnl
            positions.append({
                "strategy": sid,
                "symbol": sym,
                "qty": round(qty, 6),
                "entry": round(entry, 4),
                "price": round(price, 4),
                "value": round(value, 2),
                "unrealized_pnl": round(upnl, 2),
                "unrealized_pnl_pct": round((price / entry - 1.0) if entry else 0.0, 4),
                "stale": sym not in prices,
            })

        equity = cash + mv
        strategies.append({
            "strategy": sid,
            "market": e.get("market", "?"),
            "enabled": is_enabled,
            "allocated": round(allocated, 2),
            "cash": round(cash, 2),
            "market_value": round(mv, 2),
            "equity": round(equity, 2),
            "total_pnl": round(equity - allocated, 2),
            "pnl_pct": round((equity / allocated - 1.0) if allocated else 0.0, 4),
            "realized_pnl": round(realized, 2),
            "open_positions": len(e.get("positions", {})),
        })

        # Totals reflect only the active book — capital in retired/benched
        # strategies is not counted, so returns aren't diluted by frozen slices.
        if is_enabled:
            t_invested += allocated
            t_cash += cash
            t_mv += mv
            t_realized += realized
            enabled_count += 1

    t_equity = t_cash + t_mv
    totals = {
        "total_invested": round(t_invested, 2),
        "total_cash": round(t_cash, 2),
        "total_market_value": round(t_mv, 2),
        "total_equity": round(t_equity, 2),
        "total_pnl": round(t_equity - t_invested, 2),
        "total_pnl_pct": round((t_equity / t_invested - 1.0) if t_invested else 0.0, 4),
        "total_realized_pnl": round(t_realized, 2),
        "total_unrealized_pnl": round(t_unrealized, 2),
        "strategies": len(led),
        "enabled": enabled_count,
        "open_positions": len(positions),
        "symbols": len(symbols),
        "symbols_priced": sum(1 for s in symbols if s in prices),
    }

    strategies.sort(key=lambda r: r["total_pnl"], reverse=True)
    positions.sort(key=lambda r: r["value"], reverse=True)
    return {"as_of": utc_now_iso(), "live": live, "totals": totals,
            "strategies": strategies, "positions": positions}


# ------------------------------------------------------------- executed trades
def executed_trades(limit: int = 200) -> dict:
    """Structured order/trade feed parsed from ``logs/trades.log``.

    The execution path logs three record kinds: ``omnivore entry`` / ``omnivore
    exit`` fills and ``risk decision`` order intents (carrying the requested vs
    final weight and the risk layer's verdict). This turns the raw log tail into
    one newest-first table so the UI shows exactly what the system traded and
    what it attempted but the risk layer sized down or blocked.
    """
    lines = _tail_lines(_log_dir() / "trades.log")
    rows: list[dict] = []
    dec_ok = dec_blocked = fills = 0
    symbols: set[str] = set()

    for line in lines:
        m = _TRADE_LINE.match(line)
        if not m:
            continue
        msg = m.group("msg").strip()
        ctx: dict = {}
        if m.group("json"):
            try:
                ctx = json.loads(m.group("json"))
            except ValueError:
                ctx = {}
        sym = ctx.get("symbol", "")

        if msg.startswith("omnivore entry") or msg.startswith("omnivore exit"):
            kind = "entry" if msg.startswith("omnivore entry") else "exit"
            qty = float(ctx.get("qty", 0.0) or 0.0)
            price = float(ctx.get("price", 0.0) or 0.0)
            detail = f"{round(qty, 4)} @ ${round(price, 2)}"
            if ctx.get("reason"):
                detail += f" ({ctx['reason']})"
            if ctx.get("dry_run"):
                detail += " · paper"
            rows.append({
                "ts": ctx.get("ts") or m.group("ts"), "strategy": ctx.get("strategy", ""),
                "symbol": sym, "side": "buy" if kind == "entry" else "sell", "kind": kind,
                "requested": None, "final": None, "approved": True, "reason": detail,
            })
            fills += 1
        elif msg.startswith("risk decision"):
            ok = bool(ctx.get("approved"))
            rows.append({
                "ts": ctx.get("ts") or m.group("ts"), "strategy": ctx.get("strategy", ""),
                "symbol": sym, "side": ctx.get("side", ""), "kind": "decision",
                "requested": ctx.get("requested"), "final": ctx.get("final"),
                "approved": ok, "reason": "; ".join(ctx.get("reasons", []) or []),
            })
            dec_ok += ok
            dec_blocked += not ok
        else:
            continue
        if sym:
            symbols.add(sym)

    rows.reverse()  # newest first
    return {
        "as_of": utc_now_iso(),
        "rows": rows[:limit],
        "summary": {"approved": dec_ok, "blocked": dec_blocked, "fills": fills,
                    "symbols": sorted(symbols)},
    }


# --------------------------------------------------------- watchlist / universe
def watchlist() -> dict:
    """Symbols and strategies currently under consideration.

    Universe = the interactive asset list (config ``webapp.assets``) plus the
    omnivore crypto universe. Each symbol is flagged with whether the live paper
    book holds it and which strategies hold it. Strategies under consideration
    come from the governance registry.
    """
    cfg = _cfg()
    universe: dict[str, str] = {}
    assets = cfg.get("webapp", {}).get("assets", {})
    assets = assets.as_dict() if hasattr(assets, "as_dict") else (assets or {})
    for grp, cls in (("crypto", "crypto"), ("stocks", "stock"), ("etfs", "etf")):
        for sym in assets.get(grp, []) or []:
            universe.setdefault(sym, cls)
    for sym in cfg.get("omnivore", {}).get("crypto_universe", []) or []:
        universe.setdefault(sym, "crypto")

    led = _omnivore_ledger()
    held: dict[str, dict] = {}
    for sid, e in led.items():
        for sym, p in e.get("positions", {}).items():
            h = held.setdefault(sym, {"qty": 0.0, "strategies": []})
            h["qty"] += float(p.get("qty", 0.0) or 0.0)
            h["strategies"].append(sid)
            universe.setdefault(sym, "crypto" if "/" in sym else "stock")

    symbols = []
    for sym, cls in universe.items():
        h = held.get(sym)
        symbols.append({
            "symbol": sym, "label": _SYMBOL_LABELS.get(sym, sym), "cls": cls,
            "held": h is not None, "qty": round(h["qty"], 6) if h else 0.0,
            "held_by": ", ".join(sorted(set(h["strategies"]))) if h else "",
        })
    symbols.sort(key=lambda r: (not r["held"], r["cls"], r["symbol"]))

    reg = load_registry()
    strategies = [{
        "key": key, "status": e.get("status", "unknown"), "market": e.get("market", "?"),
        "source": e.get("source", "?"),
        "allocation": round(float(e.get("allocation", 0.0) or 0.0), 2),
        "last_validated": e.get("last_validated", ""),
    } for key, e in reg.items()]
    strategies.sort(key=lambda r: r["allocation"], reverse=True)

    return {"as_of": utc_now_iso(), "symbols": symbols, "strategies": strategies,
            "n_symbols": len(symbols), "n_held": len(held), "n_strategies": len(strategies)}


# ----------------------------------------------------------------- learning
def learning() -> dict:
    """What the system is currently learning / enforcing.

    Three concrete signals:
      * regime model — the most recent HMM fit (state count chosen by BIC) and
        how often each state-count has been selected across recent fits.
      * risk lessons — the most frequent reasons the risk layer gave for sizing
        down or blocking orders, normalized so per-run numeric variants group.
      * strategy notes — the latest governance rationale per strategy.
    """
    regime_lines = _tail_lines(_log_dir() / "regime.log", max_bytes=200_000)
    fit_hist: dict[str, int] = {}
    latest_fit = None
    for line in regime_lines:
        m = _REGIME_FIT.search(line)
        if not m:
            continue
        states = m.group("states")
        fit_hist[states] = fit_hist.get(states, 0) + 1
        bic = {}
        if "|" in line:
            try:
                bic = json.loads(line.split("|", 1)[1].strip()).get("bic", {})
            except ValueError:
                bic = {}
        latest_fit = {"states": int(states), "bic": bic, "ts": line[:19]}

    reason_counts: dict[str, int] = {}
    for line in _tail_lines(_log_dir() / "trades.log"):
        if "risk decision" not in line or "|" not in line:
            continue
        try:
            ctx = json.loads(line.split("|", 1)[1].strip())
        except ValueError:
            continue
        for r in ctx.get("reasons", []) or []:
            norm = _PCT_NUM.sub("", _RISK_CAP.sub("risk cap", r)).strip(" :")
            reason_counts[norm] = reason_counts.get(norm, 0) + 1
    risk_reasons = sorted(
        ({"reason": k, "count": v} for k, v in reason_counts.items()),
        key=lambda r: r["count"], reverse=True)[:12]

    latest: dict[str, dict] = {}
    for d in load_decisions_ledger():
        key = d.get("key")
        if key:
            latest[key] = d
    strategy_insights = [{
        "key": k, "decision": d.get("decision", ""),
        "rationale": (d.get("rationale", "") or "")[:240], "ts": d.get("ts", ""),
    } for k, d in latest.items()]
    strategy_insights.sort(key=lambda r: r["ts"], reverse=True)

    return {"as_of": utc_now_iso(), "regime": latest_fit, "regime_fit_histogram": fit_hist,
            "risk_reasons": risk_reasons, "strategy_insights": strategy_insights[:30]}


# -------------------------------------------------------------------- overview
def breaker_status() -> dict:
    lock = _state_dir() / "breaker.lock"
    if not lock.exists():
        return {"locked": False, "detail": None}
    try:
        detail = lock.read_text(encoding="utf-8")[:500]
    except Exception:
        detail = None
    return {"locked": True, "detail": detail}


def overview() -> dict:
    reg = load_registry()
    status_counts: dict[str, int] = {}
    capital = 0.0
    for e in reg.values():
        status_counts[e.get("status", "unknown")] = status_counts.get(e.get("status", "unknown"), 0) + 1
        capital += e.get("allocation", 0.0) or 0.0

    date = latest_run_date()
    lb = load_leaderboard(date)
    decision_counts: dict[str, int] = {}
    verdict_counts = {"PASS": 0, "REJECTED": 0}
    for r in lb["rows"]:
        dec = r.get("decision")
        if dec:
            decision_counts[dec] = decision_counts.get(dec, 0) + 1
        v = r.get("verdict")
        if v in verdict_counts:
            verdict_counts[v] += 1

    return {
        "latest_date": date,
        "available_dates": available_run_dates(),
        "summary": parse_summary(date),
        "status_counts": status_counts,
        "decision_counts": decision_counts,
        "verdict_counts": verdict_counts,
        "capital_allocated": round(capital, 2),
        "strategies": len(reg),
        "breaker": breaker_status(),
        "title": _cfg().get("web", {}).get("title", "Regime-Trader"),
    }


# ------------------------------------------------------------------------ logs
def tail_log(name: str, n: int = 200) -> dict:
    cfg = _cfg()
    log_dir = cfg.resolve_path(cfg.monitoring.log_dir)
    if name == "governance-run":
        runs = sorted(log_dir.glob("governance_*.log"))
        path = runs[-1] if runs else None
    elif name in _LOG_CHANNELS:
        path = log_dir / f"{name}.log"
    else:
        return {"name": name, "lines": [], "error": "unknown log"}
    if path is None or not path.exists():
        return {"name": name, "path": str(path) if path else None, "lines": []}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"name": name, "lines": [], "error": str(exc)}
    lines = text.splitlines()
    return {"name": name, "path": path.name, "lines": lines[-n:]}


# ------------------------------------------------------------ agent activity
_AGENT_DEFAULTS = {
    "audit_file": "agent_audit.jsonl", "budget_file": "agent_budget.json",
    "kill_switch_file": "agents.disabled", "heartbeat_file": "agent_supervisor.json",
    "ledger_file": "agent_ledger.json", "pause_file": "agents.paused",
}


def _agent_path(key: str, section: str | None = None) -> Path:
    """Configured agent artifact path, defaulting into state_dir."""
    cfg = _cfg()
    agents = cfg.get("agents")
    node = agents.get(section) if (agents is not None and section) else agents
    rel = node.get(key) if node is not None else None
    return cfg.resolve_path(rel) if rel else cfg.resolve_path(cfg.state_dir) / _AGENT_DEFAULTS[key]


def load_agent_activity(limit: int = 200) -> dict:
    """Agent audit tail (newest first), supervisor heartbeat, today's spend,
    kill-switch state and the agent sleeve book — all read from disk."""
    from ..agents.audit import AuditLog

    events = AuditLog(_agent_path("audit_file"), max_bytes=1).tail(max(1, min(limit, 1000)))
    hb = load_json(_agent_path("heartbeat_file", "supervisor"), default=None)
    if hb and hb.get("ts"):
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(hb["ts"])).total_seconds()
        hb = {**hb, "age_seconds": max(0.0, round(age, 1))}
    today = pd.Timestamp.now(tz="UTC").date().isoformat()
    budget = load_json(_agent_path("budget_file"), default=None) or {}
    if budget.get("date") != today:
        budget = {"date": today, "spent_usd": 0.0, "calls": 0, "by_model": {}}
    agents = _cfg().get("agents")
    budget = {**budget, "daily_usd": float(agents.get("daily_budget_usd", 0.0)) if agents else 0.0}
    ledger = load_json(_agent_path("ledger_file", "execution"), default=None) or {}
    return {
        "as_of": utc_now_iso(),
        "events": list(reversed(events)),
        "supervisor": hb,
        "budget": budget,
        "kill_switch": _agent_path("kill_switch_file").exists(),
        "sleeve": {"positions": ledger.get("positions", {}), "day": ledger.get("day")},
    }
