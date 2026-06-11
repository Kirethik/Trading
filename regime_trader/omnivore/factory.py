"""§13 Omnivorous strategy generation: many diverse edges, all markets.

Parameterized variants of every strategy family are stamped out across the
equity universe AND the Alpaca crypto universe. No restriction on count
beyond the config cap — the philosophy is deploy first, observe, prune.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..strategy.allocation import DualMomentum, TAARotation
from ..strategy.base import Strategy
from ..strategy.meanrev import BollingerReversion, RSI2Pullback
from ..strategy.trend import (
    CrossSectionalMomentum,
    DonchianBreakout,
    SMATrendFilter,
    TimeSeriesMomentum,
)
from ..strategy.vol import VolTargeting


@dataclass
class OmniStrategy:
    id: str
    strategy: Strategy
    symbols: list[str]
    market: str                      # "stocks" | "crypto" | "etf"
    enabled: bool = True
    meta: dict = field(default_factory=dict)


def generate_cohort() -> list[OmniStrategy]:
    cfg = get_config()
    stocks = list(cfg.universe.equities)
    etfs = list(cfg.universe.etf_rotation)
    crypto = list(cfg.omnivore.crypto_universe)

    cohort: list[OmniStrategy] = []

    def add(slug: str, strategy: Strategy, symbols: list[str], market: str, **meta):
        cohort.append(OmniStrategy(id=slug, strategy=strategy, symbols=symbols, market=market, meta=meta))

    # --- time-series momentum across lookbacks, both markets ---------------
    for lb, skip in [(63, 0), (126, 21), (252, 21)]:
        add(f"tsmom{lb}-{skip}-stk", TimeSeriesMomentum(lb, skip), stocks, "stocks", lookback=lb, skip=skip)
        add(f"tsmom{lb}-{skip}-cry", TimeSeriesMomentum(lb, skip), crypto, "crypto", lookback=lb, skip=skip)

    # --- SMA trend filters --------------------------------------------------
    for w in (50, 100, 200):
        add(f"sma{w}-stk", SMATrendFilter(w), stocks, "stocks", window=w)
        add(f"sma{w}-cry", SMATrendFilter(w), crypto, "crypto", window=w)

    # --- Donchian breakouts -------------------------------------------------
    for ew, xw in [(20, 10), (55, 20)]:
        add(f"don{ew}-{xw}-stk", DonchianBreakout(ew, xw), stocks, "stocks", entry=ew, exit=xw)
        add(f"don{ew}-{xw}-cry", DonchianBreakout(ew, xw), crypto, "crypto", entry=ew, exit=xw)

    # --- mean reversion -----------------------------------------------------
    for er, xr in [(10.0, 70.0), (5.0, 65.0)]:
        add(f"rsi2-{int(er)}-{int(xr)}-stk", RSI2Pullback(er, xr), stocks, "stocks", entry=er, exit=xr)
    add("rsi2-10-70-cry", RSI2Pullback(10.0, 70.0, trend_window=100), crypto, "crypto")
    for ns in (2.0, 2.5):
        add(f"boll20-{ns}-stk", BollingerReversion(20, ns), stocks, "stocks", n_std=ns)
    add("boll20-2.0-cry", BollingerReversion(20, 2.0, trend_window=100), crypto, "crypto")

    # --- cross-sectional momentum -------------------------------------------
    for k in (2, 3):
        add(f"xsmom-top{k}-stk", CrossSectionalMomentum(k), stocks, "stocks", top_k=k)
    add("xsmom-top2-cry", CrossSectionalMomentum(2, lookback=90, skip=7), crypto, "crypto")

    # --- volatility targeting -----------------------------------------------
    for tv in (0.08, 0.12):
        add(f"voltarget-{int(tv * 100)}-stk", VolTargeting(tv), stocks, "stocks", target_vol=tv)
    add("voltarget-20-cry", VolTargeting(0.20, max_gross=1.0), crypto, "crypto", target_vol=0.20)

    # --- allocation models on the ETF set ------------------------------------
    add("dualmom-etf", DualMomentum(), etfs, "etf")
    add("taa-top3-etf", TAARotation(3), etfs, "etf")

    return cohort[: cfg.omnivore.max_strategies]
