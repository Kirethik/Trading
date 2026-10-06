"""Head-to-head: the YouTube 'market structure + supply/demand pullback' method
(mechanized) vs our validated rg-sma150, on the same ETH data, costs and engine.

Answers one question honestly: do the 'better' demand-zone entries actually beat a
plain regime-gated trend filter AFTER counting every zone that failed?
"""
from __future__ import annotations

import numpy as np

from regime_trader.backtest.oracle import compute_regime_oracle
from regime_trader.research.eth_research import (
    HOLDOUT_START,
    SELECTION_END,
    _ppy,
    buy_hold_metrics,
    load_eth_bars,
    metrics_on,
    run_backtest,
)
from regime_trader.strategy.extended import RegimeGated
from regime_trader.strategy.structure import StructureZonePullback
from regime_trader.strategy.trend import SMATrendFilter

TF = "1Day"
PPY = _ppy(TF)
bars = load_eth_bars(TF)
oracle = compute_regime_oracle(bars["ETH/USD"])

STRATS = [
    ("rg-sma150 (ours)",        RegimeGated(SMATrendFilter(150), max_vol_rank=0.66), True),
    ("structure-zone (video)",  StructureZonePullback(),                            False),
    ("rg-structure (video+gate)", RegimeGated(StructureZonePullback(), max_vol_rank=0.66), True),
]

COLS = ["sharpe", "cagr", "max_drawdown", "win_rate", "profit_factor",
        "avg_trade", "n_round_trips"]


def realized_rr(res) -> float:
    """Average winning round-trip return / average losing round-trip return."""
    t = res.trades
    if t is None or not len(t) or "pnl" not in t.columns:
        return float("nan")
    pnl = t["pnl"].to_numpy(dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    if not len(wins) or not len(losses):
        return float("nan")
    return float(wins.mean() / abs(losses.mean()))


def fmt(m: dict) -> str:
    if "error" in m:
        return "  (too short)"
    return (f"  Sharpe {m['sharpe']:5.2f} | CAGR {100*m['cagr']:6.2f}% | "
            f"MaxDD {100*m['max_drawdown']:6.2f}% | Win {100*m.get('win_rate',0):4.1f}% | "
            f"PF {m.get('profit_factor',0):4.2f} | AvgTrade ${m.get('avg_trade',0):7.2f} | "
            f"RT {m.get('n_round_trips',0):3d}")


print("=" * 100)
print(f"ETH/USD daily | full 2017->2026 + 2025-26 holdout | costs: realistic crypto | ppy={PPY}")
print("=" * 100)

bh = buy_hold_metrics(bars, "2017-08-17", "2026-12-31", PPY)
print(f"\nBuy & Hold ETH:\n{fmt(bh)}")

for label, strat, needs_regime in STRATS:
    res = run_backtest(bars, strat, TF, oracle=oracle if needs_regime else None)
    full = metrics_on(res.equity, res.trades, None, None, PPY)
    sel = metrics_on(res.equity, res.trades, None, SELECTION_END, PPY)
    oos = metrics_on(res.equity, res.trades, HOLDOUT_START, None, PPY)
    exp = res.metrics.get("avg_gross_exposure", 0.0)
    rr = realized_rr(res)
    print(f"\n{label}   [avg exposure {100*exp:.1f}% | realized avg-win/avg-loss "
          f"{rr:.2f}]")
    print(f"  FULL   {fmt(full)}")
    print(f"  SELECT {fmt(sel)}")
    print(f"  HOLDOUT{fmt(oos)}")

print("\n" + "=" * 100)
