"""UI strategy catalog: maps friendly product names to engine strategy builders.

The frontend shows "Trend Following / Momentum / Breakout / Mean Reversion /
Multi-Factor / AI Enhanced". This module turns a (key, params) selection into a
concrete `Strategy` instance and tells the backtest service whether it needs the
regime oracle. It also exposes the asset universe and per-asset annualization /
sizing so crypto (24/7) and equities are scored correctly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..strategy.base import Strategy
from ..strategy.extended import Ensemble, KeltnerBreakout, MultiFactor, RegimeGated
from ..strategy.meanrev import BollingerReversion, RSI2Pullback
from ..strategy.trend import DonchianBreakout, SMATrendFilter, TimeSeriesMomentum
from ..strategy.vol import VolTargeting
from ..research.candidates import diversified_members


# --------------------------------------------------------------------- assets
ASSETS = [
    {"symbol": "ETH/USD", "label": "Ethereum", "cls": "crypto"},
    {"symbol": "BTC/USD", "label": "Bitcoin", "cls": "crypto"},
    {"symbol": "SOL/USD", "label": "Solana", "cls": "crypto"},
    {"symbol": "ADA/USD", "label": "Cardano", "cls": "crypto"},
    {"symbol": "AAPL", "label": "Apple", "cls": "stock"},
    {"symbol": "MSFT", "label": "Microsoft", "cls": "stock"},
    {"symbol": "NVDA", "label": "NVIDIA", "cls": "stock"},
    {"symbol": "TSLA", "label": "Tesla", "cls": "stock"},
    {"symbol": "SPY", "label": "S&P 500 ETF", "cls": "etf"},
    {"symbol": "QQQ", "label": "Nasdaq 100 ETF", "cls": "etf"},
    {"symbol": "VTI", "label": "Total Market ETF", "cls": "etf"},
]
_ASSET_BY_SYMBOL = {a["symbol"]: a for a in ASSETS}


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def asset_class(symbol: str) -> str:
    a = _ASSET_BY_SYMBOL.get(symbol)
    return a["cls"] if a else ("crypto" if is_crypto(symbol) else "stock")


def ppy_for(symbol: str, timeframe: str) -> int:
    """Annualization basis. Crypto is 24/7 (365 daily / 2190 on 4h); equities 252."""
    if is_crypto(symbol):
        return 2190 if timeframe == "4Hour" else 365
    return 252


def max_single_for(symbol: str) -> float:
    """A single-asset crypto sleeve can be fully allocated; equities keep the 20% cap."""
    return 1.0 if is_crypto(symbol) else 0.20


# ------------------------------------------------------------------ strategies
@dataclass
class StrategyDef:
    key: str
    label: str
    family: str
    description: str
    params: list[dict]                 # [{name,label,type,default,min,max,step}]
    build: Callable[[dict, str], Strategy]
    needs_regime: bool = False


def _p(name, label, default, lo, hi, step=1, type_="int"):
    return {"name": name, "label": label, "type": type_, "default": default,
            "min": lo, "max": hi, "step": step}


def _trend(params, symbol):
    return SMATrendFilter(
        int(params.get("window", 150)),
        band=float(params.get("band", 0.01)),
        target_vol=float(params.get("target_vol", 0.15)),
        sizing_mode="vol_target",
        stop_atr_mult=6.0,
    )


def _momentum(params, symbol):
    return TimeSeriesMomentum(int(params.get("lookback", 90)), int(params.get("skip", 7)))


def _breakout(params, symbol):
    return DonchianBreakout(int(params.get("entry_window", 55)), int(params.get("exit_window", 20)))


def _mean_reversion(params, symbol):
    return RSI2Pullback(float(params.get("entry_rsi", 10.0)), float(params.get("exit_rsi", 70.0)),
                        trend_window=int(params.get("trend_window", 100)))


def _multifactor(params, symbol):
    return MultiFactor(trend_window=int(params.get("trend_window", 100)),
                       mom_lookback=int(params.get("mom_lookback", 90)),
                       mom_skip=int(params.get("mom_skip", 7)))


def _ai_enhanced(params, symbol):
    """The research winner: a trend filter gated by the HMM volatility regime —
    stand aside in turbulent regimes. SMA window + gate are tunable."""
    base = SMATrendFilter(
        int(params.get("window", 150)),
        band=float(params.get("band", 0.01)),
        target_vol=float(params.get("target_vol", 0.15)),
        sizing_mode="vol_target",
        stop_atr_mult=6.0,
    )
    return RegimeGated(base, max_vol_rank=float(params.get("max_vol_rank", 0.66)))


def _ai_ensemble(params, symbol):
    """Regime-gated diversified ensemble (higher in-sample, more complex)."""
    tf = "1Day"
    return RegimeGated(Ensemble(diversified_members(tf)), max_vol_rank=float(params.get("max_vol_rank", 0.66)))


def _volatility(params, symbol):
    return VolTargeting(float(params.get("target_vol", 0.5)), max_gross=1.0)


def _keltner(params, symbol):
    return KeltnerBreakout(int(params.get("ema_window", 20)), 14, float(params.get("mult", 2.0)))


CATALOG: list[StrategyDef] = [
    StrategyDef("trend-following", "Trend Following", "trend",
                "Hold while price is above its long moving average; flat below. "
                "Vol-target sizing + wide 6-ATR stop lets winners run.",
                [_p("window", "SMA window", 150, 20, 300, 5),
                 _p("target_vol", "Vol target", 0.15, 0.05, 0.50, 0.05, "float"),
                 _p("band", "SMA band %", 0.01, 0.0, 0.05, 0.005, "float")], _trend),
    StrategyDef("momentum", "Momentum", "momentum",
                "Long when recent absolute momentum is positive (12-1 style).",
                [_p("lookback", "Lookback (bars)", 90, 20, 300, 5),
                 _p("skip", "Skip (bars)", 7, 0, 30, 1)], _momentum),
    StrategyDef("breakout", "Breakout", "breakout",
                "Turtle-style: enter on an N-bar high breakout, exit on an M-bar low.",
                [_p("entry_window", "Entry window", 55, 10, 200, 5),
                 _p("exit_window", "Exit window", 20, 5, 100, 5)], _breakout),
    StrategyDef("mean-reversion", "Mean Reversion", "mean-reversion",
                "Buy short-term RSI(2) dips while the long trend is intact.",
                [_p("entry_rsi", "Entry RSI", 10.0, 2.0, 30.0, 1.0, "float"),
                 _p("exit_rsi", "Exit RSI", 70.0, 50.0, 90.0, 1.0, "float"),
                 _p("trend_window", "Trend SMA", 100, 50, 250, 10)], _mean_reversion),
    StrategyDef("multi-factor", "Multi-Factor", "multi-factor",
                "Blend of trend, absolute momentum, and MACD agreement.",
                [_p("trend_window", "Trend SMA", 100, 50, 250, 10),
                 _p("mom_lookback", "Momentum lookback", 90, 20, 250, 5)], _multifactor),
    StrategyDef("ai-enhanced", "AI Enhanced", "regime",
                "Trend filter gated by an HMM volatility-regime model — stands aside "
                "in turbulent regimes. Vol-target sizing + wide 6-ATR stop.",
                [_p("window", "SMA window", 150, 50, 250, 10),
                 _p("max_vol_rank", "Regime gate (vol-rank ≤)", 0.66, 0.4, 0.9, 0.01, "float"),
                 _p("target_vol", "Vol target", 0.15, 0.05, 0.50, 0.05, "float"),
                 _p("band", "SMA band %", 0.01, 0.0, 0.05, 0.005, "float")],
                _ai_enhanced, needs_regime=True),
    StrategyDef("ai-ensemble", "AI Ensemble", "regime",
                "Regime-gated blend of seven diversified edges (more complex).",
                [_p("max_vol_rank", "Regime gate (vol-rank ≤)", 0.66, 0.4, 0.9, 0.01, "float")],
                _ai_ensemble, needs_regime=True),
    StrategyDef("volatility", "Volatility Target", "volatility",
                "Scale exposure toward a target annualized volatility.",
                [_p("target_vol", "Target vol", 0.5, 0.1, 1.0, 0.05, "float")], _volatility),
    StrategyDef("keltner", "Keltner Breakout", "breakout",
                "Volatility-adaptive channel breakout (EMA ± ATR band).",
                [_p("ema_window", "EMA window", 20, 10, 100, 5),
                 _p("mult", "ATR mult", 2.0, 1.0, 4.0, 0.25, "float")], _keltner),
]
_BY_KEY = {s.key: s for s in CATALOG}


def list_strategies() -> list[dict]:
    return [{"key": s.key, "label": s.label, "family": s.family,
             "description": s.description, "params": s.params, "needs_regime": s.needs_regime}
            for s in CATALOG]


def list_assets() -> list[dict]:
    return [{"symbol": a["symbol"], "label": a["label"], "class": a["cls"]} for a in ASSETS]


def build_strategy(key: str, params: dict | None, symbol: str) -> tuple[Strategy, bool]:
    """Returns (strategy_instance, needs_regime). Raises KeyError on unknown key."""
    s = _BY_KEY[key]
    return s.build(params or {}, symbol), s.needs_regime
