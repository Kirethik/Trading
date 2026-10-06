"""The daily governed research cycle — the spec's Continuous Improvement Loop.

Order (spec §Continuous Improvement Loop):
  1. Data Collection      -> refresh bars + regime oracle
  2. Strategy Research     -> enumerate governed universe
  3. Optimization Team     -> propose param improvements (daily light / deep full)
  4-6. Validation Team     -> independently audit every strategy + overfitting,
                              produce approval/rejection reports
  7-8. Governance (Judge)  -> review findings, make final decisions
  + capital reallocation on the Alpaca PAPER account (through the risk veto)
  + reports for every strategist.

Tiered: cheap gates daily on ALL strategies; the full mandatory pipeline
(OOS, walk-forward, 10k Monte Carlo, sweeps, real+synthetic stress) on the
configured weekly day or with --deep. Equity orders only fire when the Alpaca
clock says the market is open; otherwise state updates and orders defer.
"""
from __future__ import annotations

from pathlib import Path

from ..config import get_config
from ..monitor.alerts import AlertKind, AlertManager
from ..monitor.log_setup import get_logger, setup_logging
from ..utils import utc_now
from . import optimization, reporting, validation
from .governance import RETIRE, Governance
from .registry import DEPLOYED, GovernanceRegistry

log = get_logger("governance")

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


class DailyGovernanceCycle:
    def __init__(self, broker=None, deep: bool | None = None, no_trade: bool = False):
        self.cfg = get_config()
        setup_logging(self.cfg.resolve_path(self.cfg.monitoring.log_dir))
        self.gcfg = self.cfg.governance
        self.broker = broker
        self.no_trade = no_trade
        self._deep_override = deep
        self.alerts = AlertManager()

    def _is_deep(self, now) -> bool:
        if self._deep_override is not None:
            return self._deep_override
        target = _WEEKDAYS.get(str(self.gcfg.tiering.deep_validation_weekday).upper(), 5)
        return now.weekday() == target

    def run(self) -> dict:
        now = utc_now()
        deep = self._is_deep(now)
        tier = "DEEP" if deep else "cheap-daily"
        log.info("=== governance cycle start (%s) ===", tier)

        # 1. data collection
        bars, oracle = self._load_data()
        if bars is None:
            self.alerts.send(AlertKind.DATA_LOSS, "governance cycle aborted: no data")
            return {"error": "no data"}

        # 2. strategy research: the governed universe
        registry = GovernanceRegistry()
        governed = registry.universe

        # market-open gate for live paper orders
        can_trade = bool(self.broker is not None and not self.no_trade and self._market_open())

        # live-drift on currently deployed strategies (from the omnivore ledger)
        drift = self._live_drift(registry)

        # 3. optimization team: deep -> all; daily -> deployed laggards + the
        # focus cohort, which also gets LLM-proposed parameter sets (improver)
        focus_keys = self._focus_keys(governed)
        only = None if deep else ({k for k, d in drift.items() if d.get("degraded")} | focus_keys)
        proposals = optimization.run_optimization(governed, bars, oracle, deep,
                                                  only_keys=only, llm_keys=focus_keys)

        # 4-6. INDEPENDENT validation of every strategy
        reports = {}
        for gs in governed:
            try:
                rep = validation.validate(gs.strategy, bars, oracle, gs.symbols, deep=deep,
                                          prior_sharpe=registry.prior_sharpe(gs.key))
            except Exception as exc:
                log.warning("validation failed for %s: %s", gs.key, exc)
                continue
            reports[gs.key] = rep
            registry.record_validation(gs.key, rep.verdict, rep.metrics.get("sharpe"), deep)

        # 7-8. governance decisions (Judge authority; escalates borderline/conflict)
        gov = Governance(registry)
        decisions = gov.decide(reports, proposals, drift)

        # paper reallocation (through the risk veto, market permitting)
        applied = self._apply(gov, decisions, can_trade)

        # alerts on retirements / breakers
        for key, d in decisions.items():
            if d.decision == RETIRE:
                self.alerts.send(AlertKind.SYSTEM, f"strategy retired by governance: {key}")

        gov.append_ledger(decisions)
        registry.save()

        # agents learn: compare their past calls with what happened (once a day)
        lessons = self._reflect(registry)

        # reports for every strategist
        outdir = self._reports(reports, decisions, registry,
                               {"date": now.strftime("%Y-%m-%d"), "tier": tier,
                                "can_trade": can_trade, "proposals": len(proposals), **applied})
        summary = {"date": now.strftime("%Y-%m-%d"), "tier": tier, "strategies": len(reports),
                   "can_trade": can_trade, "proposals": len(proposals), "applied": applied,
                   "lessons": lessons, "report_dir": str(outdir)}
        log.info("=== governance cycle complete ===", extra={"ctx": summary})
        return summary

    # ------------------------------------------------------------ helpers
    def _reflect(self, registry) -> dict:
        r = self.gcfg.get("reflection")
        if r is not None and not r.get("enabled", True):
            return {}
        from . import llm_bridge, reflection

        if not llm_bridge.available():
            return {}
        try:
            return reflection.reflect_all(registry.state)
        except Exception as exc:  # learning is optional; never fail the cycle on it
            log.warning("agent reflection failed: %s", exc)
            return {}

    def _focus_keys(self, governed) -> set[str]:
        """Strategies whose symbols sit entirely inside the improver focus set
        (e.g. the single-asset ETH cohort). Empty when the improver is off."""
        icfg = self.gcfg.get("improver")
        if not icfg or not icfg.get("enabled", False):
            return set()
        focus = set(icfg.get("focus_symbols", []) or [])
        return {gs.key for gs in governed
                if focus and gs.symbols and set(gs.symbols) <= focus}

    def _load_data(self):
        from ..data.provider import DataProvider
        from ..strategy.catalog import all_required_symbols
        from ..backtest.oracle import compute_regime_oracle
        from ..omnivore.factory import generate_cohort

        provider = DataProvider()
        syms = set(all_required_symbols())
        for om in generate_cohort():          # include crypto/etf cohort symbols
            syms.update(om.symbols)
        try:
            bars = provider.get_universe_bars(sorted(syms), refresh=True)
        except Exception as exc:
            log.error("data refresh failed: %s", exc)
            return None, None
        if not bars:
            return None, None
        proxy = self.cfg.universe.market_proxy
        oracle = compute_regime_oracle(bars[proxy]) if proxy in bars else None
        return bars, oracle

    def _market_open(self) -> bool:
        try:
            return bool(self.broker.is_market_open())
        except Exception as exc:
            log.warning("market-clock check failed (%s); treating as closed", exc)
            return False

    def _live_drift(self, registry) -> dict:
        """Read the omnivore ledger's live slice performance for deployed keys."""
        from ..omnivore.deploy import OmnivoreDeployer

        drift: dict[str, dict] = {}
        try:
            rep = OmnivoreDeployer(dry_run=True).report()
        except Exception as exc:
            log.warning("could not read omnivore ledger for drift: %s", exc)
            return drift
        if rep is None or len(rep) == 0:
            return drift
        min_obs = self.gcfg.drift.min_live_observations
        degrade = self.gcfg.drift.degrade_sharpe
        for row in rep.itertuples():
            if row.observations >= min_obs and row.sharpe < degrade:
                drift[row.strategy] = {"degraded": True, "severe": row.pnl_pct < -0.10,
                                       "live_sharpe": row.sharpe, "pnl_pct": row.pnl_pct}
        return drift

    def _apply(self, gov, decisions, can_trade) -> dict:
        if self.no_trade:
            return gov.apply(decisions, None)          # research-only: registry recommendations
        from ..omnivore.deploy import OmnivoreDeployer

        deployer = OmnivoreDeployer(broker=self.broker, dry_run=not can_trade)
        applied = gov.apply(decisions, deployer)
        if can_trade:
            try:
                deployer.run_cycle()                   # rebalance toward new allocations
            except Exception as exc:
                log.warning("paper rebalance cycle failed: %s", exc)
        else:
            log.info("market closed: allocations updated, orders deferred to next open")
        return applied

    def _reports(self, reports, decisions, registry, meta) -> Path:
        outdir = self.cfg.resolve_path(self.cfg.runs_dir) / "governance" / meta["date"]
        reporting.write_run(outdir, reports, decisions, registry, meta)
        board = reporting.master_frame(reports, decisions, registry)
        reporting.print_console(board, f"Governance — {meta['date']} ({meta['tier']})")
        return outdir
