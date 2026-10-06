"""Background job runner for the dashboard's SAFE controls.

Only commands in the allowlist below can be started, and every one is a
research / report action — none place orders. Jobs run as detached
``python -m regime_trader ...`` subprocesses (the same entrypoint cron uses),
with stdout+stderr teed to a per-job log the UI can stream.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from pathlib import Path

from ..config import PROJECT_ROOT, get_config
from ..utils import new_id, utc_now_iso

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_RUNNING = 4

# command-key -> human label, shown in the UI control panel.
COMMANDS = {
    "govern-daily-notrade": "Run daily governance (no orders)",
    "govern-validate": "Validate one strategy",
    "govern-optimize": "Run optimization proposals",
    "refresh-data": "Refresh market-data cache",
    "notion-sync": "Sync reports to Notion",
}

_jobs: dict[str, dict] = {}
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def _build_argv(command: str, params: dict) -> tuple[list[str], str]:
    """Map a control command + params to the argv tail after `-m regime_trader`.

    Raises ValueError on any command outside the allowlist or bad params. The
    set here deliberately excludes every order-placing command.
    """
    p = params or {}
    if command == "govern-daily-notrade":
        argv = ["govern-daily", "--no-trade"]
        if p.get("deep"):
            argv = ["govern-daily", "--deep", "--no-trade"]
        return argv, COMMANDS[command]
    if command == "govern-validate":
        strat = str(p.get("strategy", ""))
        if not _SAFE_TOKEN.match(strat):
            raise ValueError("invalid or missing strategy")
        argv = ["govern-validate", "--strategy", strat]
        if p.get("deep"):
            argv.append("--deep")
        return argv, f"Validate {strat}"
    if command == "govern-optimize":
        argv = ["govern-optimize"]
        strat = p.get("strategy")
        if strat:
            if not _SAFE_TOKEN.match(str(strat)):
                raise ValueError("invalid strategy")
            argv += ["--strategy", str(strat)]
        return argv, COMMANDS[command]
    if command == "refresh-data":
        # forces a bars + oracle refetch; no orders, no broker
        return ["backtest", "--strategy", "regime-hmm", "--refresh", "--no-benchmarks"], COMMANDS[command]
    if command == "notion-sync":
        argv = ["notion-sync"]
        date = p.get("date")
        if date:
            if not _DATE.match(str(date)):
                raise ValueError("invalid date (want YYYY-MM-DD)")
            argv += ["--date", str(date)]
        return argv, COMMANDS[command]
    raise ValueError(f"command not allowed: {command}")


def _running_count() -> int:
    return sum(1 for j in _jobs.values() if j["status"] == "running")


def _job_log_dir() -> Path:
    cfg = get_config()
    d = cfg.resolve_path(cfg.get("web", {}).get("job_log_dir", "logs/jobs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def start_job(command: str, params: dict | None = None) -> dict:
    argv, label = _build_argv(command, params or {})
    with _lock:
        if _running_count() >= _MAX_RUNNING:
            raise RuntimeError(f"too many jobs running (max {_MAX_RUNNING}); wait for one to finish")
        job_id = new_id("job")
        logfile = _job_log_dir() / f"{job_id}.log"
        full = [sys.executable, "-m", "regime_trader", *argv]
        fh = open(logfile, "w", encoding="utf-8")
        fh.write(f"$ {' '.join(['python', '-m', 'regime_trader', *argv])}\n\n")
        fh.flush()
        try:
            proc = subprocess.Popen(full, cwd=str(PROJECT_ROOT), stdout=fh,
                                    stderr=subprocess.STDOUT, text=True)
        except Exception:
            fh.close()
            raise
        job = {"id": job_id, "command": command, "label": label,
               "argv": argv, "status": "running", "pid": proc.pid,
               "started": utc_now_iso(), "ended": None, "returncode": None,
               "logfile": str(logfile)}
        _jobs[job_id] = job
        _procs[job_id] = proc

    def _waiter():
        rc = proc.wait()
        try:
            fh.close()
        except Exception:
            pass
        with _lock:
            job["status"] = "succeeded" if rc == 0 else "failed"
            job["returncode"] = rc
            job["ended"] = utc_now_iso()

    threading.Thread(target=_waiter, name=f"job-{job_id}", daemon=True).start()
    return _public(job)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "logfile"}


def list_jobs() -> list[dict]:
    with _lock:
        return [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)]


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def job_log(job_id: str, n: int = 400) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        logfile = job["logfile"] if job else None
    if not job:
        return {"id": job_id, "lines": [], "error": "unknown job"}
    p = Path(logfile)
    if not p.exists():
        return {"id": job_id, "lines": [], "status": job["status"]}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"id": job_id, "lines": [], "error": str(exc)}
    return {"id": job_id, "status": job["status"], "returncode": job["returncode"],
            "lines": lines[-n:]}
