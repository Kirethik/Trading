"""ETH candidate grid: coarse, robust parameter sets across strategy families.

Design choice against overfitting: a DELIBERATELY SMALL, log-spaced grid (a few
values per family) rather than a dense sweep. Window lengths are scaled per
timeframe so a "trend" means a comparable calendar span on daily vs 4h bars
(4h has 6 bars/day). Every candidate carries `family` + `params` meta so the
parameter-sensitivity stage can compare a finalist to its grid neighbours.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..strategy.base import Strategy
from ..strategy.extended import (
    ADXTrend,
    EMACrossover,
    Ensemble,
    KeltnerBreakout,
    MACDTrend,
    MultiFactor,
    RegimeGated,
)
from ..strategy.meanrev import BollingerReversion, RSI2Pullback
from ..strategy.trend import DonchianBreakout, SMATrendFilter, TimeSeriesMomentum
from ..strategy.vol import VolTargeting


@dataclass
class Candidate:
    cid: str
    strategy: Strategy
    family: str
    timeframe: str
    params: dict = field(default_factory=dict)
    needs_regime: bool = False   # True -> backtest must run with the ETH regime oracle


def diversified_members(timeframe: str) -> list[Strategy]:
    """A weakly-correlated member set for the Ensemble: trend + breakout +
    mean-reversion + multi-factor. Diversity (not count) is what reduces variance."""
    w = _WINDOWS[timeframe]
    return [
        SMATrendFilter(w["slow"][1]),                       # slow trend
        SMATrendFilter(w["mid"][0]),                        # mid trend
        TimeSeriesMomentum(*w["mom"][1]),                   # absolute momentum
        DonchianBreakout(*w["don"][1]),                     # breakout
        KeltnerBreakout(w["fast"][1], 14, 2.0),             # ATR breakout
        RSI2Pullback(10.0, 70.0, trend_window=w["trend"][0]),  # mean reversion (trend-gated)
        MultiFactor(trend_window=w["trend"][0], mom_lookback=w["mom"][1][0], mom_skip=w["mom"][1][1]),
    ]


def advanced_candidates(timeframe: str) -> list[Candidate]:
    """The Sharpe-lifting layer: ensembles, volatility targeting, and HMM
    regime-gating. Regime-gated variants set needs_regime so the harness runs
    them with the causal ETH oracle."""
    mem = diversified_members(timeframe)
    tag = _tf_tag(timeframe)
    out = [
        Candidate(f"ensemble-{tag}", Ensemble(mem), "ensemble", timeframe, {"members": len(mem)}),
        Candidate(f"ensemble-vt40-{tag}", Ensemble(mem, target_vol=0.40), "ensemble", timeframe,
                  {"target_vol": 0.40}),
        Candidate(f"ensemble-vt60-{tag}", Ensemble(mem, target_vol=0.60), "ensemble", timeframe,
                  {"target_vol": 0.60}),
        Candidate(f"rg-ensemble-{tag}", RegimeGated(Ensemble(mem), max_vol_rank=0.66),
                  "regime-ensemble", timeframe, {"max_vol_rank": 0.66}, needs_regime=True),
        Candidate(f"rg-ensemble-vt60-{tag}", RegimeGated(Ensemble(mem, target_vol=0.60), max_vol_rank=0.66),
                  "regime-ensemble", timeframe, {"target_vol": 0.60}, needs_regime=True),
    ]
    # regime-gated SMA trend across several windows AND gate thresholds so the
    # parameter-sensitivity (plateau) check has neighbours — i.e. we can prove the
    # winner is a robust plateau, not a lone tuned spike.
    sma_windows = ([100, 150, 200] if timeframe == "1Day" else [300, 450, 600])
    for win in sma_windows:
        out.append(Candidate(f"rg-sma{win}-{tag}", RegimeGated(SMATrendFilter(win), max_vol_rank=0.66),
                             "regime-trend", timeframe, {"window": win, "max_vol_rank": 0.66},
                             needs_regime=True))
    for mvr in (0.55, 0.75):  # gate-threshold neighbours of the 0.66 default
        out.append(Candidate(f"rg-sma{sma_windows[-1]}-g{int(mvr*100)}-{tag}",
                             RegimeGated(SMATrendFilter(sma_windows[-1]), max_vol_rank=mvr),
                             "regime-trend", timeframe, {"window": sma_windows[-1], "max_vol_rank": mvr},
                             needs_regime=True))
    return out


# bar-count window sets per timeframe (≈ comparable calendar spans)
_WINDOWS = {
    "1Day": {"fast": [10, 20], "mid": [50, 100], "slow": [100, 200],
             "mom": [(30, 0), (60, 7), (120, 14)], "don": [(20, 10), (55, 20)],
             "trend": [100, 200]},
    "4Hour": {"fast": [30, 60], "mid": [150, 300], "slow": [300, 600],
              "mom": [(90, 0), (180, 12), (360, 24)], "don": [(60, 30), (165, 60)],
              "trend": [300, 600]},
}


def build_candidates(timeframe: str) -> list[Candidate]:
    """The full candidate set for one timeframe."""
    w = _WINDOWS[timeframe]
    out: list[Candidate] = []

    def add(cid, strat, family, **params):
        out.append(Candidate(f"{cid}-{_tf_tag(timeframe)}", strat, family, timeframe, params))

    # --- trend: time-series momentum ---------------------------------------
    for lb, sk in w["mom"]:
        add(f"tsmom{lb}-{sk}", TimeSeriesMomentum(lb, sk), "tsmom", lookback=lb, skip=sk)
    # --- trend: SMA filter --------------------------------------------------
    for win in sorted(set(w["mid"] + w["slow"])):
        add(f"sma{win}", SMATrendFilter(win), "sma", window=win)
    # --- trend: EMA crossover ----------------------------------------------
    for f, s in [(w["fast"][0], w["mid"][0]), (w["fast"][1], w["mid"][1]), (w["fast"][1], w["slow"][1])]:
        add(f"emax{f}-{s}", EMACrossover(f, s), "ema-cross", fast=f, slow=s)
    # --- trend: MACD --------------------------------------------------------
    for tw in w["trend"]:
        add(f"macd-{tw}", MACDTrend(trend_window=tw), "macd", trend_window=tw)
    # --- trend: ADX-gated ---------------------------------------------------
    for tw in w["trend"]:
        for amin in (20.0, 25.0):
            add(f"adx{int(amin)}-{tw}", ADXTrend(trend_window=tw, adx_min=amin), "adx",
                trend_window=tw, adx_min=amin)
    # --- breakout: Donchian -------------------------------------------------
    for ew, xw in w["don"]:
        add(f"don{ew}-{xw}", DonchianBreakout(ew, xw), "donchian", entry=ew, exit=xw)
    # --- breakout: Keltner (ATR channel) -----------------------------------
    for ew, mult in [(w["fast"][1], 2.0), (w["fast"][1], 2.5), (w["mid"][0], 2.0)]:
        add(f"kelt{ew}-{mult}", KeltnerBreakout(ew, 14, mult), "keltner", ema=ew, mult=mult)
    # --- mean reversion -----------------------------------------------------
    tw0 = w["trend"][0]
    add("rsi2-10-70", RSI2Pullback(10.0, 70.0, trend_window=tw0), "rsi2", entry=10, exit=70)
    add("rsi2-5-65", RSI2Pullback(5.0, 65.0, trend_window=tw0), "rsi2", entry=5, exit=65)
    for ns in (2.0, 2.5):
        add(f"boll-{ns}", BollingerReversion(20 if timeframe == "1Day" else 60, ns, trend_window=tw0),
            "bollinger", n_std=ns)
    # --- volatility targeting (crypto-scale targets) ------------------------
    for tv in (0.50, 0.80):
        add(f"voltgt{int(tv*100)}", VolTargeting(tv, max_gross=1.0), "vol-target", target_vol=tv)
    # --- multi-factor blend -------------------------------------------------
    add("multifac", MultiFactor(trend_window=tw0, mom_lookback=w["mom"][1][0], mom_skip=w["mom"][1][1]),
        "multi-factor", trend_window=tw0)
    add("multifac-vt", MultiFactor(trend_window=tw0, mom_lookback=w["mom"][1][0],
                                   mom_skip=w["mom"][1][1], target_vol=0.60),
        "multi-factor", trend_window=tw0, target_vol=0.60)

    return out + advanced_candidates(timeframe)


def _tf_tag(tf: str) -> str:
    return {"1Day": "1d", "4Hour": "4h"}.get(tf, tf.lower())
