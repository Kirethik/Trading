"""Walk-forward regime oracle: the causal regime series every run shares.

For each walk-forward step, an HMM is trained ONLY on bars before the test
window (BIC-selected state count), then filtered probabilities are produced
for the test bars. Concatenating steps yields a regime view at every bar that
never saw its own future — the regime history used by the engine, the
per-regime breakdowns, and the dashboard.

Cached on disk keyed by (data span, parameters) because refitting ~30 HMMs
is the slow part of a backtest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from ..config import get_config
from ..data import features as F
from ..monitor.log_setup import get_logger
from ..regime.hmm import RegimeModel

log = get_logger("regime")

ORACLE_COLUMNS = [
    "state", "prob", "confidence", "vol_rank", "expected_vol", "expected_ret",
    "n_states", "label", "refit_id",
]


def compute_regime_oracle(
    market_df: pd.DataFrame,
    train_bars: int | None = None,
    step_bars: int | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    cfg = get_config()
    train_bars = train_bars or cfg.backtest.train_bars
    step_bars = step_bars or cfg.backtest.step_bars

    X = F.regime_features(market_df, cfg.regime.vol_window)
    cache_file = _cache_path(X, train_bars, step_bars)
    if use_cache and cache_file.exists():
        cached = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        cached.index = pd.DatetimeIndex(cached.index, tz="UTC")
        return cached

    rows: list[pd.DataFrame] = []
    refit_id = 0
    i = train_bars
    while i < len(X):
        train = X.iloc[i - train_bars : i]
        test_end = min(i + step_bars, len(X))
        model = RegimeModel().fit(train)
        # filter over everything up to test_end (causal recursion), but only
        # CONSUME rows for this step's out-of-sample slice [i, test_end)
        probs = model.filtered_probabilities(X.iloc[:test_end])
        views = [model.view_at(p) for p in probs[i:test_end]]
        chunk = pd.DataFrame([v.to_dict() for v in views], index=X.index[i:test_end])
        chunk["refit_id"] = refit_id
        rows.append(chunk)
        refit_id += 1
        i = test_end
    if not rows:
        raise RuntimeError(f"not enough bars ({len(X)}) for train window {train_bars}")
    oracle = pd.concat(rows)
    oracle.to_csv(cache_file)
    log.info("regime oracle computed: %d bars, %d refits", len(oracle), refit_id)
    return oracle


def _cache_path(X: pd.DataFrame, train_bars: int, step_bars: int) -> Path:
    cfg = get_config()
    key = {
        "start": str(X.index[0]),
        "end": str(X.index[-1]),
        "n": len(X),
        "train": train_bars,
        "step": step_bars,
        "seed": cfg.regime.random_seed,
        "k": [cfg.regime.n_states_min, cfg.regime.n_states_max],
        "cols": list(X.columns),
        "sum": float(X.sum().sum()),  # cheap content fingerprint
    }
    h = hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    cache_dir = cfg.resolve_path(cfg.data.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"oracle_{h}.csv"
