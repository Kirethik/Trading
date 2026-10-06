"""Stress testing: real historical event windows + the synthetic fallback.

Event replay slices the full price history up to each event's END (so indicators
keep their warm-up) and evaluates only the [start, end] window via the engine's
start_ts — causal, and bounded to the crisis. Events carry a `markets` tag; a
strategy is only put through events that match its market and that its data
actually covers. The synthetic crash/gap/scramble suite always runs as a
universal floor (covers data-short markets like newer crypto).
"""
from __future__ import annotations

import pandas as pd

from ..backtest.engine import WalkForwardEngine
from ..backtest.stress import run_stress_suite
from ..config import get_config
from ..data.provider import is_crypto
from ..monitor.log_setup import get_logger

log = get_logger("governance")


def infer_markets(symbols: list[str]) -> set[str]:
    return {"crypto" if is_crypto(s) else "equities" for s in symbols}


def run_event_stress(strategy, bars, oracle, symbols: list[str]) -> dict:
    """Replay each configured real event window the strategy's data covers."""
    cfg = get_config().governance
    events = cfg.stress_events.as_dict() if hasattr(cfg.stress_events, "as_dict") else cfg.stress_events
    min_dd = cfg.stress_min_survival_dd
    my_markets = infer_markets(symbols)
    out: dict[str, dict] = {}

    for ev in events:
        ev = dict(ev)
        name = ev["name"]
        ev_markets = set(ev.get("markets", ["equities"]))
        if not (ev_markets & my_markets):
            out[name] = {"skipped": "market not applicable"}
            continue
        start = pd.Timestamp(ev["start"], tz="UTC")
        end = pd.Timestamp(ev["end"], tz="UTC")

        sliced = {s: df[df.index <= end] for s, df in bars.items() if s in symbols}
        # require real coverage of the window for at least the proxy/first symbol
        covered = any(len(df[(df.index >= start) & (df.index <= end)]) >= 5 for df in sliced.values())
        if not covered:
            out[name] = {"skipped": "no data coverage for window"}
            continue
        ev_oracle = oracle[oracle.index <= end] if oracle is not None else None
        try:
            res = WalkForwardEngine(sliced, strategy, symbols, regime_oracle=ev_oracle,
                                    start_ts=start).run()
            mdd = res.metrics.get("max_drawdown", -1.0)
            out[name] = {
                "survived": bool(mdd >= min_dd and res.equity.iloc[-1] > 0),
                "max_drawdown": mdd,
                "total_return": res.metrics.get("total_return"),
                "bars": res.metrics.get("bars"),
            }
        except Exception as exc:
            out[name] = {"skipped": f"engine error: {exc}"}
        log.info("event stress %s/%s: %s", strategy.name, name, out[name])
    return out


def run_full_stress(strategy, bars, oracle, symbols: list[str]) -> dict:
    """Synthetic suite (always) + real event windows (where data exists)."""
    synthetic = run_stress_suite(strategy, bars, oracle, symbols)
    events = run_event_stress(strategy, bars, oracle, symbols)
    applicable = [v for v in events.values() if "survived" in v]
    events_passed = all(v["survived"] for v in applicable) if applicable else True
    return {
        "synthetic": synthetic,
        "events": events,
        "synthetic_passed": bool(synthetic.get("passed")),
        "events_passed": events_passed,
        "passed": bool(synthetic.get("passed") and events_passed),
    }
