"""Interactive backtest API — /api/v1/* endpoints.

Mounted onto the existing FastAPI app in app.py. All routes require the same
session cookie as the legacy /api/* routes.

  GET  /api/v1/assets            list tradeable assets
  GET  /api/v1/strategies        list strategies + param schemas
  GET  /api/v1/price             OHLCV slice for chart
  POST /api/v1/backtest          run (cache-first) + return full payload
  GET  /api/v1/backtest/{hash}   retrieve a previously cached run
  POST /api/v1/compare           multi-run summary for comparison matrix
  GET  /api/v1/runs              recent run history (DB if available)
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..backtest.engine import WalkForwardEngine
from ..backtest.metrics import compute_metrics
from ..backtest.oracle import compute_regime_oracle
from ..data.provider import DataProvider
from ..risk.manager import RiskManager
from . import cache as Cache
from . import db as DB
from .analytics import (
    assemble_payload,
    equity_series_json,
    price_series_json,
)
from .strategies_registry import (
    ASSETS,
    build_strategy,
    is_crypto,
    list_assets,
    list_strategies,
    max_single_for,
    ppy_for,
)

router = APIRouter(prefix="/api/v1")

# shared thread pool for CPU-bound backtest work (GIL released by numpy/pandas)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bt")

# Binance symbol map for deep-history crypto data
_BINANCE_MAP = {
    "ETH/USD": "ETHUSDT",
    "BTC/USD": "BTCUSDT",
    "SOL/USD": "SOLUSDT",
    "ADA/USD": "ADAUSDT",
}
# realistic all-in cost per side (slippage + fee + spread) in basis points
_COST_BPS = 20.0
# max trailing bars per timeframe to bound O(n^2) indicator recompute
_MAX_LOOKBACK = {"1Day": 750, "4Hour": 2000}


# ------------------------------------------------------------------ auth reuse
def _require_session(request: Request) -> None:
    from . import auth
    if not auth.is_authed(request):
        raise HTTPException(status_code=401, detail="authentication required")


# ------------------------------------------------------------------ request model
class BacktestRequest(BaseModel):
    strategy: str
    symbol: str = "ETH/USD"
    timeframe: str = "1Day"
    amount: float = 10_000.0
    start_date: str | None = None
    end_date: str | None = None
    params: dict[str, Any] | None = None
    force_refresh: bool = False


class CompareRequest(BaseModel):
    runs: list[str]          # list of param_hashes


# ------------------------------------------------------------------ helpers
def _load_bars(symbol: str, timeframe: str, start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
    """Load OHLCV from local CSV cache (DataProvider) or Binance for deep crypto."""
    dp = DataProvider(allow_network=False)
    try:
        df = dp.get_bars(symbol, start=start or "2017-01-01", end=end, timeframe=timeframe)
        if len(df) >= 30:
            return {symbol: df}
    except Exception:
        pass

    # for crypto, fall back to a live Binance fetch if cache is empty/missing
    if is_crypto(symbol):
        binance_sym = _BINANCE_MAP.get(symbol)
        if binance_sym is None:
            raise HTTPException(400, f"No data source configured for {symbol}")
        try:
            from ..data.external import fetch_binance_klines, write_cache
            df = fetch_binance_klines(timeframe=timeframe, binance_symbol=binance_sym,
                                      start=start or "2017-01-01", end=end)
            write_cache(symbol, timeframe, df)
            return {symbol: df}
        except Exception as exc:
            raise HTTPException(503, f"Could not fetch data for {symbol}: {exc}") from exc

    raise HTTPException(404, f"No data available for {symbol}")


def _make_risk_manager(symbol: str) -> RiskManager:
    rm = RiskManager(lock_file=Path(tempfile.mkdtemp(prefix="rt_api_")) / "breaker.lock")
    rm.max_single = max_single_for(symbol)
    return rm


def _buy_hold_equity(bars: pd.DataFrame, capital: float,
                     start: str | None, end: str | None) -> pd.Series:
    close = bars["close"]
    if start:
        close = close[close.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        close = close[close.index <= pd.Timestamp(end, tz="UTC")]
    if len(close) < 2:
        return pd.Series(dtype=float)
    return capital * (close / close.iloc[0])


def _run_sync(req: BacktestRequest) -> tuple[str, dict]:
    """CPU-bound backtest executed in the thread pool."""
    symbol = req.symbol
    timeframe = req.timeframe
    ppy = ppy_for(symbol, timeframe)

    bars = _load_bars(symbol, timeframe, req.start_date, req.end_date)
    strategy, needs_regime = build_strategy(req.strategy, req.params or {}, symbol)

    oracle = None
    if needs_regime:
        oracle = compute_regime_oracle(bars[symbol])

    eng = WalkForwardEngine(
        bars, strategy, [symbol],
        regime_oracle=oracle,
        risk_manager=_make_risk_manager(symbol),
        initial_capital=req.amount,
        periods_per_year=ppy,
        max_lookback=_MAX_LOOKBACK.get(timeframe, 1000),
    )
    eng.slippage = _COST_BPS / 10_000.0
    result = eng.run()

    # benchmark: buy & hold over the same window
    bh_equity = _buy_hold_equity(bars[symbol], req.amount, req.start_date, req.end_date)
    bh_metrics = compute_metrics(bh_equity, periods_per_year=ppy) if len(bh_equity) > 5 else {}

    payload = assemble_payload(
        result, bars, symbol, timeframe, ppy,
        initial_capital=req.amount,
        start_date=req.start_date,
        end_date=req.end_date,
    )
    payload["benchmark"] = {
        "equity": equity_series_json(bh_equity, req.amount),
        "metrics": bh_metrics,
    }
    payload["strategy"] = req.strategy
    payload["symbol"] = symbol
    payload["timeframe"] = timeframe
    payload["amount"] = req.amount

    run_id = str(uuid.uuid4())
    return run_id, payload


# ------------------------------------------------------------------ routes
@router.get("/assets")
def api_assets(_: None = Depends(_require_session)) -> dict:
    return {"assets": list_assets()}


@router.get("/strategies")
def api_strategies(_: None = Depends(_require_session)) -> dict:
    return {"strategies": list_strategies()}


@router.get("/price")
def api_price(
    symbol: str = "ETH/USD",
    timeframe: str = "1Day",
    start: str | None = None,
    end: str | None = None,
    _: None = Depends(_require_session),
) -> dict:
    try:
        bars = _load_bars(symbol, timeframe, start, end)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    df = bars[symbol]
    return {"symbol": symbol, "timeframe": timeframe, "bars": price_series_json(df)}


@router.post("/backtest")
async def api_backtest(req: BacktestRequest, _: None = Depends(_require_session)) -> dict:
    # canonical cache key from all request parameters
    cache_key_params = {
        "strategy": req.strategy,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "amount": req.amount,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "params": req.params or {},
    }
    param_hash = Cache.make_hash(cache_key_params)

    if not req.force_refresh:
        cached = Cache.cache_get(param_hash)
        if cached:
            cached["from_cache"] = True
            cached["param_hash"] = param_hash
            return cached

    loop = asyncio.get_event_loop()
    try:
        run_id, payload = await loop.run_in_executor(_executor, _run_sync, req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Backtest failed: {exc}") from exc

    payload["run_id"] = run_id
    payload["param_hash"] = param_hash
    payload["from_cache"] = False

    Cache.cache_set(param_hash, payload)
    asyncio.create_task(
        DB.persist_run(param_hash, cache_key_params, payload.get("metrics", {}))
    )

    return payload


@router.get("/backtest/{param_hash}")
async def api_backtest_get(param_hash: str, _: None = Depends(_require_session)) -> dict:
    cached = Cache.cache_get(param_hash)
    if cached:
        cached["from_cache"] = True
        cached["param_hash"] = param_hash
        return cached
    raise HTTPException(404, "Run not found (may have expired from cache)")


@router.post("/compare")
async def api_compare(req: CompareRequest, _: None = Depends(_require_session)) -> dict:
    rows = []
    for h in req.runs:
        cached = Cache.cache_get(h)
        if cached is None:
            continue
        m = cached.get("metrics", {})
        rows.append({
            "param_hash": h,
            "strategy": cached.get("strategy", ""),
            "symbol": cached.get("symbol", ""),
            "timeframe": cached.get("timeframe", "1Day"),
            "amount": cached.get("amount"),
            "sharpe": m.get("sharpe"),
            "cagr": m.get("cagr"),
            "max_drawdown": m.get("max_drawdown"),
            "calmar": m.get("calmar"),
            "profit_factor": m.get("profit_factor"),
            "win_rate": m.get("win_rate"),
            "total_return": m.get("total_return"),
            "sortino": m.get("sortino"),
            "recovery_factor": m.get("recovery_factor"),
            "n_round_trips": m.get("n_round_trips"),
        })
    return {"rows": rows}


@router.get("/runs")
async def api_runs(limit: int = 50, _: None = Depends(_require_session)) -> dict:
    rows = await DB.list_runs(limit=min(limit, 200))
    return {"runs": rows, "db_available": DB._db_url() is not None}


@router.get("/cache/status")
def api_cache_status(_: None = Depends(_require_session)) -> dict:
    return {"redis_available": Cache.is_available()}
