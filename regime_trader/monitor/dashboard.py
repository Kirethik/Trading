"""Live terminal dashboard (rich): regime, portfolio, positions, signals,
color-coded risk bars, system health, and the omnivore multi-market cohort.

Reads the snapshots the orchestrator/deployer write (state/dashboard.json,
state/omnivore_ledger.json) so it can run in its own terminal, attach to a
running system, or replay the last known state when nothing is live.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import get_config
from ..utils import load_json


def _bar(value: float, cap: float, width: int = 20) -> Text:
    """Color-coded utilization bar: green -> yellow -> red as value nears cap."""
    frac = 0.0 if cap <= 0 else max(0.0, min(value / cap, 1.0))
    filled = int(frac * width)
    color = "green" if frac < 0.6 else ("yellow" if frac < 0.85 else "red")
    bar = Text("█" * filled + "░" * (width - filled), style=color)
    bar.append(f" {value:.2f}/{cap:.2f}", style="dim")
    return bar


def _regime_panel(snap: dict) -> Panel:
    reg, stab = snap.get("regime", {}), snap.get("stability", {})
    body = Table.grid(padding=(0, 1))
    label = reg.get("label", "unknown")
    color = "green" if "calm" in label else ("yellow" if "moderate" in label else "red")
    body.add_row("Regime", Text(label.upper(), style=f"bold {color}"))
    body.add_row("Confidence", _bar(reg.get("confidence", 0.0), 1.0))
    body.add_row("Vol rank", _bar(reg.get("vol_rank", 0.0), 1.0))
    body.add_row("Expected vol", f"{reg.get('expected_vol', 0):.1%}")
    body.add_row("States (BIC)", str(reg.get("n_states", "?")))
    flick = stab.get("flickering", False)
    body.add_row("Stability", Text(
        f"{stab.get('consecutive_bars', 0)} bars"
        + (" | TRANSITION" if stab.get("in_transition") else "")
        + (" | FLICKER" if flick else "")
        + (" | UNCERTAINTY" if stab.get("uncertainty_mode") else ""),
        style="red" if flick else "white",
    ))
    return Panel(body, title="Volatility Regime", border_style=color)


def _portfolio_panel(snap: dict) -> Panel:
    pf, risk = snap.get("portfolio", {}), snap.get("risk", {})
    body = Table.grid(padding=(0, 1))
    body.add_row("Equity", f"${pf.get('equity', 0):,.2f}")
    daily = pf.get("daily_pnl_pct", 0.0)
    body.add_row("Daily P&L", Text(f"{daily:+.2%}", style="green" if daily >= 0 else "red"))
    body.add_row("Weekly P&L", f"{pf.get('weekly_pnl_pct', 0):+.2%}")
    body.add_row("Peak DD", Text(f"{pf.get('peak_drawdown_pct', 0):+.2%}",
                                 style="red" if pf.get("peak_drawdown_pct", 0) < -0.05 else "white"))
    body.add_row("Gross exposure", _bar(pf.get("gross_exposure", 0.0), risk.get("max_gross", 1.5)))
    mode = risk.get("breaker_mode", "normal")
    style = {"normal": "green", "reduce": "yellow", "halt": "red", "locked": "bold white on red"}.get(mode, "white")
    body.add_row("Breakers", Text(mode.upper(), style=style))
    if risk.get("breaker_reasons"):
        body.add_row("", Text("; ".join(risk["breaker_reasons"]), style="dim red"))
    return Panel(body, title="Portfolio & Risk", border_style="cyan")


def _positions_panel(snap: dict) -> Panel:
    t = Table(expand=True, header_style="bold")
    for col in ("Symbol", "Qty", "Entry", "Price", "Stop", "P&L%", "Strategy"):
        t.add_column(col, justify="right" if col != "Symbol" else "left")
    for sym, p in (snap.get("positions") or {}).items():
        px, entry = p.get("price") or 0, p.get("entry") or 1
        pnl = px / entry - 1 if entry else 0
        t.add_row(sym, f"{p.get('qty', 0):.4g}", f"{entry:.2f}", f"{px:.2f}",
                  f"{p.get('stop', 0):.2f}",
                  Text(f"{pnl:+.1%}", style="green" if pnl >= 0 else "red"),
                  p.get("strategy", ""))
    return Panel(t, title=f"Open Positions ({len(snap.get('positions') or {})})", border_style="blue")


def _activity_panel(snap: dict) -> Panel:
    lines = []
    for a in (snap.get("recent_actions") or [])[-6:]:
        lines.append(Text(str(a), style="dim"))
    for al in (snap.get("recent_alerts") or [])[-4:]:
        lines.append(Text(f"⚠ [{al.get('kind')}] {al.get('message', '')[:90]}", style="yellow"))
    return Panel(Group(*lines) if lines else Text("no recent activity", style="dim"),
                 title="Signals & Alerts", border_style="magenta")


def _omnivore_panel(ledger: dict) -> Panel:
    rows = []
    for sid, e in ledger.items():
        hist = e.get("equity_history", [])
        eq = hist[-1][1] if hist else e.get("allocated", 0)
        rows.append((sid, e.get("market", "?"), e.get("enabled", True),
                     eq, eq - e.get("allocated", 0), e.get("trades", 0), len(e.get("positions", {}))))
    rows.sort(key=lambda r: r[4], reverse=True)
    t = Table(expand=True, header_style="bold")
    for col in ("Strategy", "Mkt", "On", "Equity", "P&L", "Trades", "Pos"):
        t.add_column(col, justify="right" if col not in ("Strategy", "Mkt") else "left")
    shown = rows[:8] + ([("…", "", "", "", "", "", "")] if len(rows) > 16 else []) + rows[-8:] if len(rows) > 16 else rows
    for r in shown:
        if r[0] == "…":
            t.add_row("…", "", "", "", "", "", "")
            continue
        pnl_style = "green" if r[4] >= 0 else "red"
        t.add_row(r[0], r[1], "✓" if r[2] else Text("✗", style="red"),
                  f"{r[3]:,.0f}", Text(f"{r[4]:+,.0f}", style=pnl_style), str(r[5]), str(r[6]))
    n_on = sum(1 for r in rows if r[2])
    total_pnl = sum(r[4] for r in rows)
    title = f"Omnivore Cohort ({n_on}/{len(rows)} live, total P&L {total_pnl:+,.0f})"
    return Panel(t, title=title, border_style="green" if total_pnl >= 0 else "red")


def _health_panel(snap: dict) -> Panel:
    ts = snap.get("ts")
    age_txt, style = "no data yet", "red"
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            age_txt = f"last cycle {age / 60:.1f} min ago"
            style = "green" if age < 3600 * 2 else ("yellow" if age < 3600 * 13 else "red")
        except ValueError:
            pass
    mode = "DRY-RUN" if snap.get("dry_run", True) else "TRADING"
    return Panel(Text(f"{mode} | {age_txt}", style=style), title="System Health", border_style=style)


def build_view() -> Layout:
    cfg = get_config()
    snap = load_json(cfg.resolve_path(cfg.state_dir) / "dashboard.json", default={}) or {}
    ledger = load_json(cfg.resolve_path(cfg.omnivore.ledger_file), default={}) or {}

    layout = Layout()
    layout.split_column(
        Layout(name="top", size=12),
        Layout(name="middle", ratio=2),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(Layout(_regime_panel(snap)), Layout(_portfolio_panel(snap)),
                            Layout(_health_panel(snap)))
    layout["middle"].split_row(Layout(_positions_panel(snap)), Layout(_activity_panel(snap)))
    layout["bottom"].update(_omnivore_panel(ledger))
    return layout


def run_dashboard() -> None:
    cfg = get_config()
    refresh = cfg.monitoring.dashboard_refresh_seconds
    console = Console()
    with Live(build_view(), console=console, screen=True, redirect_stderr=False) as live:
        try:
            while True:
                time.sleep(refresh)
                live.update(build_view())
        except KeyboardInterrupt:
            pass
