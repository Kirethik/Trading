"""FastAPI dashboard app — `uvicorn regime_trader.web.app:app`.

Read-only views + interactive charts + SAFE controls, behind a password gate.
All `/api/*` data/control routes require a valid session cookie; `/health` and
`/api/session` and `/login` are open. The app reads on-disk artifacts only and
shells safe commands out as background jobs — it never places an order.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_config
from . import auth, data, jobs
from .backtest_api import router as v1_router

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Regime-Trader Dashboard", docs_url=None, redoc_url=None)

# allow the Next.js dev server (and the nginx-proxied UI) to call the API
_cfg = get_config()
_origins = _cfg.get("webapp", {}).get("cors_origins", [
    "http://localhost:3000", "http://localhost:3001",
])
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(v1_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ------------------------------------------------------------- oracle pre-warm
# The HMM regime oracle (~30 refits, 25-30s cold) is the dominant cost of the
# first interactive backtest of the day. It is shared by EVERY strategy/param
# for a given data span, so warm the default views in a background thread at
# startup; the user's first backtest then reuses the cached oracle instead of
# waiting. Best-effort — failures never block the app.
def _warm_default_oracles() -> None:
    import threading

    def _run() -> None:
        from datetime import datetime, timezone

        from ..backtest.oracle import compute_regime_oracle
        from ..monitor.log_setup import get_logger
        from .backtest_api import _load_bars

        log = get_logger("web")
        today = datetime.now(timezone.utc).date().isoformat()
        views = [("ETH/USD", "1Day", "2021-01-01"), ("BTC/USD", "1Day", "2021-01-01")]
        for symbol, timeframe, start in views:
            try:
                bars = _load_bars(symbol, timeframe, start, today)
                compute_regime_oracle(bars[symbol])
                log.info("oracle pre-warmed: %s %s", symbol, timeframe)
            except Exception as exc:  # noqa: BLE001 — best-effort warm
                log.warning("oracle pre-warm failed (%s %s): %s", symbol, timeframe, exc)

    threading.Thread(target=_run, name="oracle-warm", daemon=True).start()


@app.on_event("startup")
def _on_startup() -> None:
    _warm_default_oracles()


# ----------------------------------------------------------------- auth guard
def require_session(request: Request) -> None:
    if not auth.is_authed(request):
        raise HTTPException(status_code=401, detail="authentication required")


# ----------------------------------------------------------------- open routes
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/session")
def session(request: Request) -> dict:
    cfg = get_config()
    return {"authed": auth.is_authed(request),
            "password_set": auth.password_configured(),
            "title": cfg.get("web", {}).get("title", "Regime-Trader")}


@app.post("/login")
async def login(request: Request, response: Response):
    # accept JSON {password} or form-encoded password
    pw = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        pw = (body or {}).get("password")
    else:
        form = await request.form()
        pw = form.get("password")
    if not auth.verify_password(pw):
        raise HTTPException(status_code=401, detail="invalid password")
    cfg = get_config()
    max_age = int(float(cfg.get("web", {}).get("session_hours", 12)) * 3600)
    response.set_cookie(auth.SESSION_COOKIE, auth.issue_cookie(), max_age=max_age,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


# --------------------------------------------------------------- read routes
@app.get("/api/overview")
def api_overview(_: None = Depends(require_session)) -> dict:
    return data.overview()


@app.get("/api/runs")
def api_runs(_: None = Depends(require_session)) -> dict:
    return {"dates": data.available_run_dates(), "latest": data.latest_run_date()}


@app.get("/api/leaderboard")
def api_leaderboard(date: str | None = None, _: None = Depends(require_session)) -> dict:
    return data.load_leaderboard(date)


@app.get("/api/registry")
def api_registry(_: None = Depends(require_session)) -> dict:
    return data.load_registry()


@app.get("/api/decisions")
def api_decisions(limit: int = 500, _: None = Depends(require_session)) -> dict:
    return {"decisions": data.load_decisions_ledger(limit=limit)}


@app.get("/api/agents/activity")
def api_agents_activity(limit: int = 200, _: None = Depends(require_session)) -> dict:
    return data.load_agent_activity(limit=limit)


@app.get("/api/strategies")
def api_strategies(date: str | None = None, _: None = Depends(require_session)) -> dict:
    return {"date": date or data.latest_run_date(), "keys": data.list_strategies(date)}


@app.get("/api/strategy/{key}")
def api_strategy(key: str, date: str | None = None, _: None = Depends(require_session)) -> dict:
    rep = data.load_strategy(key, date)
    if not rep:
        raise HTTPException(status_code=404, detail=f"no report for {key}")
    return rep


@app.get("/api/omnivore")
def api_omnivore(_: None = Depends(require_session)) -> dict:
    return data.load_omnivore()


@app.get("/api/portfolio")
def api_portfolio(_: None = Depends(require_session)) -> dict:
    return data.live_portfolio()


@app.get("/api/trades")
def api_trades(limit: int = 200, _: None = Depends(require_session)) -> dict:
    return data.executed_trades(limit=min(max(limit, 1), 1000))


@app.get("/api/watchlist")
def api_watchlist(_: None = Depends(require_session)) -> dict:
    return data.watchlist()


@app.get("/api/learning")
def api_learning(_: None = Depends(require_session)) -> dict:
    return data.learning()


@app.get("/api/logs/tail")
def api_logs(name: str = "governance", n: int = 200, _: None = Depends(require_session)) -> dict:
    return data.tail_log(name, n=min(max(n, 1), 2000))


# ------------------------------------------------------------- control routes
@app.get("/api/commands")
def api_commands(_: None = Depends(require_session)) -> dict:
    return {"commands": jobs.COMMANDS}


@app.post("/api/run")
async def api_run(request: Request, _: None = Depends(require_session)):
    body = await request.json()
    command = (body or {}).get("command")
    params = (body or {}).get("params", {})
    try:
        job = jobs.start_job(command, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return job


@app.get("/api/jobs")
def api_jobs(_: None = Depends(require_session)) -> dict:
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, _: None = Depends(require_session)) -> dict:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    return job


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, n: int = 400, _: None = Depends(require_session)) -> dict:
    return jobs.job_log(job_id, n=min(max(n, 1), 5000))
