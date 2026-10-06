"""Command-line entry point.

  python -m regime_trader train          fit + save the regime model (BIC table)
  python -m regime_trader backtest       walk-forward backtest of one strategy + benchmarks
  python -m regime_trader compare        §5 bracket leaderboard + §6 ensemble validation
  python -m regime_trader stress-test    crash / gap / scrambled-regime suite
  python -m regime_trader dry-run        one full live cycle, no orders sent
  python -m regime_trader live           paper trading loop (live needs typed phrase)
  python -m regime_trader dashboard      live terminal dashboard
  python -m regime_trader research-eth   ETH/USD systematic research: sweep -> validate -> decide
  python -m regime_trader omni-deploy    §13 omnivore cohort cycle (paper)
  python -m regime_trader omni-report    cohort leaderboard from the ledger
  python -m regime_trader omni-prune     retire losing cohort members
  python -m regime_trader broker-check   Alpaca round-trip: bracket -> tighten -> cancel
  python -m regime_trader release-lock   manually release the hard drawdown lock
  python -m regime_trader govern-daily   governed daily cycle: research -> validate -> decide -> report
  python -m regime_trader govern-validate  independent validation pipeline for one strategy
  python -m regime_trader govern-optimize  optimization-team proposals (ad hoc)
  python -m regime_trader govern-report  show the latest (or dated) governance report
  python -m regime_trader notion-sync    mirror a governance run's reports into Notion
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


def cmd_omni_reconcile(args):
    _setup()
    from .omnivore.deploy import OmnivoreDeployer

    dep = OmnivoreDeployer(dry_run=True)  # ledger-only; no broker, no orders
    fixed = dep.reconcile_enabled()
    if not fixed:
        console.print("nothing to reconcile — every disabled strategy has a real trading record")
        return
    if args.dry_run:
        console.print(f"[yellow]would re-enable {len(fixed)} never-traded strategies[/yellow]: "
                      f"{', '.join(fixed)}")
    else:
        dep.save()
        console.print(f"[green]re-enabled {len(fixed)} never-traded strategies[/green]: "
                      f"{', '.join(fixed)}")


def cmd_warm_oracle(args):
    """Pre-compute & cache the HMM regime oracle for the interactive backtest's
    default views, so the first dashboard backtest of the day doesn't pay the
    ~25-30s refit. The oracle is shared across all strategies/params for a span."""
    _setup()
    from datetime import datetime, timezone

    from .backtest.oracle import compute_regime_oracle
    from .data.provider import DataProvider

    today = datetime.now(timezone.utc).date().isoformat()
    symbols = args.symbols or ["ETH/USD", "BTC/USD"]
    dp = DataProvider(allow_network=False)
    for symbol in symbols:
        try:
            df = dp.get_bars(symbol, start="2021-01-01", end=today, timeframe="1Day")
            oracle = compute_regime_oracle(df)
            console.print(f"[green]warmed[/green] {symbol} 1Day — {len(oracle)} oracle bars")
        except Exception as exc:  # noqa: BLE001 — best-effort warm, never fatal
            console.print(f"[yellow]warm failed[/yellow] {symbol}: {exc}")


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


def cmd_govern_daily(args):
    _setup()
    from .broker.alpaca import AlpacaBroker
    from .govern.daily import DailyGovernanceCycle

    broker = None
    if not args.no_trade:
        try:
            broker = AlpacaBroker(paper=True)
            broker.health_check()
        except Exception as exc:
            console.print(f"[yellow]broker unavailable ({exc}); continuing research-only[/yellow]")
            broker = None
    cycle = DailyGovernanceCycle(broker=broker, deep=args.deep, no_trade=args.no_trade)
    summary = cycle.run()
    console.print_json(json.dumps(summary, indent=2, default=str))


def cmd_agents_supervisor(args):
    _setup()
    from .agents import watchman
    from .agents.supervisor import Job, Supervisor
    from .config import get_config

    sup = Supervisor.from_config()
    wm = watchman.from_config()
    if wm is not None:
        interval = float(get_config().agents.watchman.get("interval_seconds", 300))
        sup.register(Job("watchman", interval, wm.tick))
    sup.run()


def cmd_research_eth(args):
    _setup()
    from .research.eth_research import run_research

    summary = run_research(deep=args.deep)
    d = summary["decision"]
    console.print_json(json.dumps(d, indent=2, default=str))
    color = "green" if d.get("verdict") == "SELECTED" else "yellow"
    console.print(f"[{color}]decision: {d.get('verdict')}[/{color}]  "
                  f"(artifacts in {get_config().research.out_dir})")


def cmd_govern_validate(args):
    _setup()
    from .govern import validation
    from .govern.registry import GovernanceRegistry

    bars = _load_bars(refresh=args.refresh)
    oracle = _oracle(bars, use_cache=not args.refresh)
    registry = GovernanceRegistry()
    governed = {gs.key: gs for gs in registry.universe}
    gs = governed.get(args.strategy)
    if gs is None:
        console.print(f"[red]unknown strategy[/red] — choose from: {', '.join(sorted(governed))}")
        sys.exit(1)
    rep = validation.validate(gs.strategy, bars, oracle, gs.symbols, deep=args.deep,
                              prior_sharpe=registry.prior_sharpe(gs.key))
    console.print_json(json.dumps(rep.__dict__, indent=2, default=str))
    color = "green" if rep.passed else "red"
    console.print(f"[{color}]verdict: {rep.verdict}[/{color}]")
    if not rep.passed:
        sys.exit(2)


def cmd_govern_optimize(args):
    _setup()
    from .govern import optimization
    from .govern.registry import GovernanceRegistry

    bars = _load_bars(refresh=args.refresh)
    oracle = _oracle(bars, use_cache=not args.refresh)
    registry = GovernanceRegistry()
    only = {args.strategy} if args.strategy else None
    proposals = optimization.run_optimization(registry.universe, bars, oracle, deep=True, only_keys=only)
    if not proposals:
        console.print("[dim]no improving proposals found[/dim]")
        return
    for prop in proposals.values():
        console.print_json(json.dumps({
            "key": prop.key, "base_sharpe": prop.base_sharpe,
            "proposed_sharpe": prop.proposed_sharpe, "changes": prop.changes,
            "improves": prop.improves,
        }, indent=2, default=str))


def cmd_govern_report(args):
    cfg = _setup()
    date = args.date or utc_now().strftime("%Y-%m-%d")
    outdir = cfg.resolve_path(cfg.runs_dir) / "governance" / date
    summary_file = outdir / "summary.md"
    if not summary_file.exists():
        console.print(f"[red]no report found for {date}[/red] (looked in {outdir})")
        sys.exit(1)
    console.print(summary_file.read_text())
    leaderboard = outdir / "master_leaderboard.md"
    if leaderboard.exists():
        console.print(leaderboard.read_text())


def cmd_notion_sync(args):
    _setup()
    from .web import notion_publisher

    date = args.date or utc_now().strftime("%Y-%m-%d")
    result = notion_publisher.sync_run(date)
    console.print_json(json.dumps(result, indent=2, default=str))
    if result["status"] == "ok":
        console.print(f"[green]notion-sync: mirrored {date}[/green]")
    elif result["status"] == "skipped":
        console.print(f"[dim]notion-sync: skipped ({result.get('reason')})[/dim]")
    else:
        console.print(f"[yellow]notion-sync: error ({result.get('error')})[/yellow]")


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

    orc = sub.add_parser("omni-reconcile")
    orc.add_argument("--dry-run", action="store_true", help="preview without writing the ledger")
    orc.set_defaults(func=cmd_omni_reconcile)

    wo = sub.add_parser("warm-oracle")
    wo.add_argument("--symbols", nargs="*", default=None,
                    help="symbols to warm (default: ETH/USD BTC/USD)")
    wo.set_defaults(func=cmd_warm_oracle)

    bc = sub.add_parser("broker-check")
    bc.add_argument("--round-trip", action="store_true", help="submit/modify/cancel a paper bracket")
    bc.set_defaults(func=cmd_broker_check)

    sub.add_parser("release-lock").set_defaults(func=cmd_release_lock)

    gd = sub.add_parser("govern-daily")
    gd.add_argument("--deep", action="store_true", default=None,
                    help="force deep validation (default: auto by weekday)")
    gd.add_argument("--no-trade", action="store_true", help="research/report only, no paper orders")
    gd.set_defaults(func=cmd_govern_daily)

    sub.add_parser("agents-supervisor",
                   help="always-on Gemini agent supervisor (paper only; run under systemd)"
                   ).set_defaults(func=cmd_agents_supervisor)

    re_ = sub.add_parser("research-eth")
    re_.add_argument("--deep", action="store_true", help="full 4h grid + 10k Monte-Carlo sims")
    re_.set_defaults(func=cmd_research_eth)

    gv = sub.add_parser("govern-validate")
    gv.add_argument("--strategy", required=True)
    gv.add_argument("--deep", action="store_true")
    gv.add_argument("--refresh", action="store_true")
    gv.set_defaults(func=cmd_govern_validate)

    go = sub.add_parser("govern-optimize")
    go.add_argument("--strategy", default=None, help="limit to one strategy key (default: whole universe)")
    go.add_argument("--refresh", action="store_true")
    go.set_defaults(func=cmd_govern_optimize)

    gr = sub.add_parser("govern-report")
    gr.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    gr.set_defaults(func=cmd_govern_report)

    ns = sub.add_parser("notion-sync")
    ns.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ns.set_defaults(func=cmd_notion_sync)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
