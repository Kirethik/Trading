"""Command-line entry point.

  python -m regime_trader train          fit + save the regime model (BIC table)
  python -m regime_trader backtest       walk-forward backtest of one strategy + benchmarks
  python -m regime_trader compare        §5 bracket leaderboard + §6 ensemble validation
  python -m regime_trader stress-test    crash / gap / scrambled-regime suite
  python -m regime_trader dry-run        one full live cycle, no orders sent
  python -m regime_trader live           paper trading loop (live needs typed phrase)
  python -m regime_trader dashboard      live terminal dashboard
  python -m regime_trader omni-deploy    §13 omnivore cohort cycle (paper)
  python -m regime_trader omni-report    cohort leaderboard from the ledger
  python -m regime_trader omni-prune     retire losing cohort members
  python -m regime_trader broker-check   Alpaca round-trip: bracket -> tighten -> cancel
  python -m regime_trader release-lock   manually release the hard drawdown lock
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import pandas as pd
from rich.console import Console
from rich.table import Table

from .config import get_config
from .monitor.log_setup import setup_logging
from .utils import utc_now

console = Console()


# ---------------------------------------------------------------- helpers
def _setup():
    cfg = get_config()
    setup_logging(cfg.resolve_path(cfg.monitoring.log_dir))
    return cfg


def _load_bars(refresh: bool = False):
    from .data.provider import DataProvider
    from .strategy.catalog import all_required_symbols

    provider = DataProvider()
    symbols = all_required_symbols()
    console.print(f"[dim]loading bars for {len(symbols)} symbols...[/dim]")
    bars = provider.get_universe_bars(symbols, refresh=refresh)
    if not bars:
        console.print("[red]no data available[/red]")
        sys.exit(1)
    return bars


def _oracle(bars, use_cache: bool = True):
    from .backtest.oracle import compute_regime_oracle

    cfg = get_config()
    proxy = cfg.universe.market_proxy
    console.print("[dim]computing walk-forward regime oracle (cached after first run)...[/dim]")
    return compute_regime_oracle(bars[proxy], use_cache=use_cache)


def _print_metrics(name: str, metrics: dict):
    t = Table(title=f"Metrics — {name}", header_style="bold")
    t.add_column("metric")
    t.add_column("value", justify="right")
    order = ["total_return", "cagr", "sharpe", "sortino", "calmar", "max_drawdown",
             "max_drawdown_duration_bars", "ann_vol", "win_rate", "profit_factor",
             "n_round_trips", "n_fills", "avg_gross_exposure", "bars"]
    pct = {"total_return", "cagr", "max_drawdown", "ann_vol", "win_rate"}
    for k in order:
        if k not in metrics:
            continue
        v = metrics[k]
        t.add_row(k, f"{v:.2%}" if k in pct and isinstance(v, float) else
                  (f"{v:.3f}" if isinstance(v, float) else str(v)))
    console.print(t)


def _print_df(df: pd.DataFrame, title: str):
    if df is None or len(df) == 0:
        console.print(f"[dim]{title}: empty[/dim]")
        return
    t = Table(title=title, header_style="bold")
    for col in df.columns:
        t.add_column(str(col), justify="right")
    for _, row in df.iterrows():
        t.add_row(*[f"{v:.3f}" if isinstance(v, float) else str(v) for v in row])
    console.print(t)


# ------------------------------------------------------------- commands
def cmd_train(args):
    _setup()
    from .live.orchestrator import Orchestrator

    orch = Orchestrator(dry_run=True)
    orch.load_or_train_model(retrain=True)
    m = orch.model
    console.print(f"[green]model trained:[/green] {m.n_states} states (BIC-selected)")
    t = Table(title="BIC by state count (lower is better)", header_style="bold")
    t.add_column("states"); t.add_column("BIC", justify="right")
    for k, v in sorted(m.bic_table.items()):
        marker = " <- selected" if k == m.n_states else ""
        t.add_row(str(k), f"{v:,.1f}{marker}")
    console.print(t)
    for i in range(m.n_states):
        console.print(f"  state {i}: {m.labels[i]:<18} vol_rank={m.vol_rank[i]:.2f} "
                      f"exp_vol={m.exp_vol[i]:.1%} exp_ret={m.exp_ret[i]:+.1%}")


def cmd_backtest(args):
    cfg = _setup()
    from .backtest.benchmarks import run_benchmarks
    from .backtest.engine import WalkForwardEngine
    from .strategy.catalog import build_challengers, strategy_universe

    bars = _load_bars(refresh=args.refresh)
    oracle = _oracle(bars, use_cache=not args.refresh)
    strategies = {s.name: s for s in build_challengers()}
    if args.strategy not in strategies:
        console.print(f"[red]unknown strategy[/red] — choose from: {', '.join(strategies)}")
        sys.exit(1)
    strat = strategies[args.strategy]
    symbols = [s for s in strategy_universe(strat) if s in bars]

    res = WalkForwardEngine(bars, strat, symbols, regime_oracle=oracle).run()
    outdir = cfg.resolve_path(cfg.runs_dir) / f"backtest_{args.strategy}_{utc_now():%Y%m%d_%H%M%S}"
    res.export(outdir)
    _print_metrics(strat.name, res.metrics)
    _print_df(res.per_regime, "Per-regime performance (decision-time attribution)")
    _print_df(res.conf_buckets, "Performance by model-confidence bucket")

    if not args.no_benchmarks:
        console.print("[dim]running benchmarks (incl. random-with-same-risk-rules)...[/dim]")
        bench = run_benchmarks(bars, oracle, symbols, n_random_seeds=args.seeds)
        for name, m in bench.items():
            if name == "random_same_risk_rules":
                console.print(f"[bold]random baseline[/bold] ({m['seeds']} seeds): "
                              f"sharpe {m['sharpe_mean']:.2f}±{m['sharpe_std']:.2f}, "
                              f"cagr {m['cagr_mean']:.2%}±{m['cagr_std']:.2%}")
            else:
                _print_metrics(name, m)
    console.print(f"[green]exported to[/green] {outdir}")


def cmd_compare(args):
    cfg = _setup()
    from .backtest.leaderboard import run_bracket

    bars = _load_bars(refresh=args.refresh)
    oracle = _oracle(bars)
    board, _ = run_bracket(bars, oracle, validate=not args.no_validate)
    show = board[[c for c in ["strategy", "sharpe", "sortino", "calmar", "cagr", "max_drawdown",
                              "win_rate", "profit_factor", "validated"] if c in board.columns]]
    _print_df(show, "§5 Strategy Bracket — ranked by Sharpe (identical engine, risk, bars)")
    if "ensemble_kept" in board.columns:
        kept = board.loc[board["strategy"] == "ensemble", "ensemble_kept"]
        if len(kept):
            console.print(f"[bold]ensemble verdict:[/bold] "
                          f"{'KEPT — beats best single' if bool(kept.iloc[0]) else 'discarded — does not beat best single'}")
    console.print(f"[green]full results in[/green] {cfg.resolve_path(cfg.runs_dir) / 'bracket'}")


def cmd_stress(args):
    _setup()
    from .backtest.stress import run_stress_suite
    from .strategy.catalog import build_challengers, strategy_universe

    bars = _load_bars()
    oracle = _oracle(bars)
    strategies = {s.name: s for s in build_challengers()}
    strat = strategies.get(args.strategy)
    if strat is None:
        console.print(f"[red]unknown strategy[/red] — choose from: {', '.join(strategies)}")
        sys.exit(1)
    report = run_stress_suite(strat, bars, oracle, [s for s in strategy_universe(strat) if s in bars])
    console.print_json(json.dumps(report, indent=2, default=str))
    console.print("[green]STRESS SUITE PASSED[/green]" if report.get("passed")
                  else "[red]STRESS SUITE FAILED[/red]")
    if not report.get("passed"):
        sys.exit(2)


def cmd_dry_run(args):
    _setup()
    from .broker.alpaca import AlpacaBroker
    from .live.orchestrator import Orchestrator

    broker = None
    try:
        broker = AlpacaBroker(paper=True)
        broker.health_check()
    except Exception as exc:
        console.print(f"[yellow]broker unavailable ({exc}); dry-run continues without it[/yellow]")
        broker = None
    orch = Orchestrator(broker=broker, dry_run=True)
    orch.boot()
    summary = orch.run_once()
    console.print_json(json.dumps(summary, indent=2, default=str))
    console.print("[green]dry run complete — no orders were sent[/green]")


def cmd_live(args):
    cfg = _setup()
    from .broker.alpaca import AlpacaBroker
    from .live.orchestrator import Orchestrator

    if args.live:
        phrase = cfg.broker.live_confirmation_phrase
        console.print(f"[bold red]LIVE trading requested.[/bold red] Type the phrase {phrase!r} to proceed:")
        typed = input("> ").strip()
        broker = AlpacaBroker(paper=False, live_confirmation=typed)  # raises if wrong
    else:
        broker = AlpacaBroker(paper=True)
    orch = Orchestrator(broker=broker, dry_run=False)
    orch.run_loop(interval_minutes=args.interval)


def cmd_dashboard(args):
    _setup()
    from .monitor.dashboard import run_dashboard

    run_dashboard()


def cmd_omni_deploy(args):
    _setup()
    from .broker.alpaca import AlpacaBroker
    from .omnivore.deploy import OmnivoreDeployer

    broker = None
    if args.orders:
        broker = AlpacaBroker(paper=True)
        broker.health_check()
    dep = OmnivoreDeployer(broker=broker, dry_run=not args.orders)
    console.print(f"[bold]omnivore cohort:[/bold] {len(dep.cohort)} strategies "
                  f"({'PAPER ORDERS ON' if args.orders else 'ledger-only dry run'})")
    if args.loop:
        console.print(f"looping every {args.interval} min — Ctrl+C to stop")
        try:
            while True:
                summary = dep.run_cycle()
                console.print(str(summary))
                time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            console.print("[dim]omnivore loop stopped; ledger saved[/dim]")
    else:
        summary = dep.run_cycle()
        console.print_json(json.dumps(summary, default=str))


def cmd_omni_report(args):
    _setup()
    from .omnivore.deploy import OmnivoreDeployer

    dep = OmnivoreDeployer(dry_run=True)
    _print_df(dep.report(), "§13 Omnivore cohort — live forward-test ranking")


def cmd_omni_prune(args):
    _setup()
    from .omnivore.deploy import OmnivoreDeployer

    dep = OmnivoreDeployer(dry_run=not args.orders)
    retired = dep.prune(min_observations=args.min_obs)
    console.print(f"retired: {retired or 'none'}")


def cmd_broker_check(args):
    _setup()
    from .broker.alpaca import AlpacaBroker

    broker = AlpacaBroker(paper=True)
    info = broker.health_check()
    console.print_json(json.dumps(info, default=str))
    if not args.round_trip:
        return
    # full §11 round trip: bracket entry -> tighten stop -> cancel -> clean
    from .data.provider import DataProvider

    symbol = "SPY"
    px = float(DataProvider().get_bars(symbol).iloc[-1]["close"])
    console.print(f"round-trip on {symbol} @ ~{px:.2f} (paper)")
    order = broker.submit_entry(symbol, qty=1, limit_price=round(px * 0.90, 2),
                                stop_loss=round(px * 0.80, 2), take_profit=round(px * 1.20, 2),
                                signal_id=f"roundtrip-{int(time.time())}")
    console.print(f"bracket submitted: {order.id} (deep limit; will not fill)")
    time.sleep(2)
    moved = broker.tighten_stop(symbol, round(px * 0.82, 2))
    console.print(f"stop tighten attempted: {'modified' if moved else 'no resting stop yet (entry unfilled — expected)'}")
    broker._call(broker.client.cancel_order_by_id, order.id)
    time.sleep(2)
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    remaining = broker._call(broker.client.get_orders,
                             GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
    console.print(f"open {symbol} orders after cancel: {len(remaining)}")
    console.print("[green]round trip complete, state clean[/green]" if not remaining
                  else "[yellow]orders still open — inspect manually[/yellow]")


def cmd_release_lock(args):
    _setup()
    from .risk.manager import RiskManager

    released = RiskManager().manual_release()
    console.print("[green]lock released[/green]" if released else "no lock was engaged")


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="regime_trader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("train").set_defaults(func=cmd_train)

    b = sub.add_parser("backtest")
    b.add_argument("--strategy", default="regime-hmm")
    b.add_argument("--refresh", action="store_true", help="force data refetch")
    b.add_argument("--no-benchmarks", action="store_true")
    b.add_argument("--seeds", type=int, default=None, help="random-benchmark seeds")
    b.set_defaults(func=cmd_backtest)

    c = sub.add_parser("compare")
    c.add_argument("--refresh", action="store_true")
    c.add_argument("--no-validate", action="store_true",
                   help="skip per-strategy look-ahead/stress gates (faster)")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("stress-test")
    s.add_argument("--strategy", default="regime-hmm")
    s.set_defaults(func=cmd_stress)

    sub.add_parser("dry-run").set_defaults(func=cmd_dry_run)

    lv = sub.add_parser("live")
    lv.add_argument("--live", action="store_true", help="REAL money (typed confirmation required)")
    lv.add_argument("--interval", type=int, default=30, help="loop interval, minutes")
    lv.set_defaults(func=cmd_live)

    sub.add_parser("dashboard").set_defaults(func=cmd_dashboard)

    od = sub.add_parser("omni-deploy")
    od.add_argument("--orders", action="store_true", help="actually submit paper orders")
    od.add_argument("--loop", action="store_true")
    od.add_argument("--interval", type=int, default=60, help="loop interval, minutes")
    od.set_defaults(func=cmd_omni_deploy)

    sub.add_parser("omni-report").set_defaults(func=cmd_omni_report)

    op = sub.add_parser("omni-prune")
    op.add_argument("--orders", action="store_true")
    op.add_argument("--min-obs", type=int, default=20)
    op.set_defaults(func=cmd_omni_prune)

    bc = sub.add_parser("broker-check")
    bc.add_argument("--round-trip", action="store_true", help="submit/modify/cancel a paper bracket")
    bc.set_defaults(func=cmd_broker_check)

    sub.add_parser("release-lock").set_defaults(func=cmd_release_lock)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
