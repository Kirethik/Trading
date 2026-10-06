"""The always-on agent supervisor (runs under systemd on EC2).

Hosts interval jobs for the always-on agents (live oversight, intraday intel
— registered by later sub-projects). Trading never depends on this process:
agents are not in the orchestrator's order path, so if it dies, trading
continues and systemd restarts it.

Safety: refuses to start on a non-paper account or without GOOGLE_API_KEY;
the kill-switch file pauses all jobs while the heartbeat keeps flowing.
"""
from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import get_config
from ..monitor.log_setup import get_logger, setup_logging
from ..utils import atomic_write_json, utc_now_iso
from . import guards

log = get_logger("agents")

TICK_SECONDS = 1.0


@dataclass(frozen=True)
class Job:
    name: str
    interval_seconds: float
    fn: Callable[[], None]


class Supervisor:
    def __init__(self, heartbeat_path: str | Path, heartbeat_seconds: float,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 alert: Callable[[str], None] | None = None):
        self.heartbeat_path = Path(heartbeat_path)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.clock, self.sleep = clock, sleep
        self.alert = alert or (lambda msg: None)
        self._jobs: list[Job] = []
        self._next_due: dict[str, float] = {}
        self._stats: dict[str, dict] = {}
        self._stop = False
        self._last_beat = float("-inf")
        self._status = ""

    @classmethod
    def from_config(cls) -> "Supervisor":
        from ..monitor.alerts import AlertKind, AlertManager

        cfg = get_config()
        setup_logging(cfg.resolve_path(cfg.monitoring.log_dir))
        s = cfg.agents.supervisor
        alerts = AlertManager()
        return cls(cfg.resolve_path(s.heartbeat_file), s.heartbeat_seconds,
                   alert=lambda msg: alerts.send(AlertKind.AGENT, msg))

    def register(self, job: Job) -> None:
        self._jobs.append(job)
        self._next_due[job.name] = float("-inf")
        self._stats[job.name] = {"runs": 0, "last_run": None, "last_error": None}

    # ------------------------------------------------------------- loop
    def tick(self) -> None:
        now = self.clock()
        paused = guards.kill_switch_engaged()
        ran = False
        if not paused:
            for job in self._jobs:
                if now >= self._next_due[job.name]:
                    self._run_job(job)
                    self._next_due[job.name] = now + job.interval_seconds
                    ran = True
        status = "paused" if paused else "running"
        if ran or status != self._status or now - self._last_beat >= self.heartbeat_seconds:
            self._beat(status)
            self._status, self._last_beat = status, now

    def _run_job(self, job: Job) -> None:
        stats = self._stats[job.name]
        try:
            job.fn()
            error = None
        except Exception as exc:  # one bad agent must never take down the others
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("supervisor job %s failed", job.name)
            self.alert(f"agent job {job.name} failed: {error}")
        self._stats[job.name] = {"runs": stats["runs"] + 1, "last_run": utc_now_iso(),
                                 "last_error": error}

    def _beat(self, status: str) -> None:
        atomic_write_json(self.heartbeat_path, {
            "ts": utc_now_iso(), "pid": os.getpid(), "status": status, "jobs": self._stats,
        })

    def _on_signal(self, signum, frame) -> None:
        log.info("supervisor received signal %s; stopping", signum)
        self._stop = True

    def run(self, install_signals: bool = True, max_ticks: int | None = None) -> None:
        guards.assert_paper_only()
        if not get_config().google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set; the agent supervisor needs it (.env)")
        if install_signals:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        log.info("agent supervisor started with %d job(s)", len(self._jobs))
        self.alert("agent supervisor started")
        ticks = 0
        try:
            while not self._stop and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
                self.sleep(TICK_SECONDS)
        finally:
            self._beat("stopped")
            log.info("agent supervisor stopped")
            self.alert("agent supervisor stopped")
