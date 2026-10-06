"""Volatility-regime model: Gaussian HMM with BIC-selected state count and
strictly causal (filtered/forward-only) inference.

Look-ahead policy — the most important invariant in this file:
hmmlearn's `predict` / `predict_proba` run forward-BACKWARD smoothing, which
lets the regime at time T see the future. They are never used here. Inference
goes through `filtered_probabilities`, a hand-rolled forward recursion, so the
regime at T is identical whether the series ends at T or years later. The
automated proof lives in tests/test_no_lookahead.py.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from scipy.stats import multivariate_normal

from ..config import get_config
from ..monitor.log_setup import get_logger

log = get_logger("regime")


@dataclass
class RegimeView:
    """Causal snapshot of the regime at one bar — rich metadata for downstream."""

    state: int                      # most-likely filtered state (raw, unconfirmed)
    label: str                      # human-readable, derived from return/vol
    prob: float                     # filtered probability of `state`
    probs: list[float] = field(default_factory=list)
    confidence: float = 0.0         # max filtered probability
    vol_rank: float = 0.0           # 0 = calmest state, 1 = most turbulent
    expected_vol: float = 0.0       # annualized, from emission params
    expected_ret: float = 0.0       # annualized, from emission params
    n_states: int = 0

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "label": self.label,
            "prob": round(self.prob, 4),
            "confidence": round(self.confidence, 4),
            "vol_rank": round(self.vol_rank, 4),
            "expected_vol": round(self.expected_vol, 4),
            "expected_ret": round(self.expected_ret, 4),
            "n_states": self.n_states,
        }


def _state_labels(exp_ret: np.ndarray, vol_rank: np.ndarray) -> list[str]:
    """Readable names: vol tercile + return sign. Cosmetic only — strategy
    decisions key off vol_rank, never off these labels."""
    labels = []
    for r, v in zip(exp_ret, vol_rank):
        vol_word = "calm" if v < 1 / 3 else ("moderate" if v < 2 / 3 else "turbulent")
        ret_word = "bull" if r > 0.02 else ("bear" if r < -0.02 else "flat")
        labels.append(f"{vol_word}-{ret_word}")
    return labels


class RegimeModel:
    """Fit on a training window; infer causally ever after."""

    def __init__(
        self,
        n_states_min: int | None = None,
        n_states_max: int | None = None,
        random_seed: int | None = None,
    ):
        cfg = get_config().regime
        self.n_min = n_states_min if n_states_min is not None else cfg.n_states_min
        self.n_max = n_states_max if n_states_max is not None else cfg.n_states_max
        self.seed = random_seed if random_seed is not None else cfg.random_seed
        self.restarts = cfg.random_restarts
        self.n_iter = cfg.em_iterations
        self.cov_type = cfg.covariance_type

        self.model: GaussianHMM | None = None
        self.n_states: int = 0
        self.feature_names: list[str] = []
        self.scale_mean: np.ndarray | None = None
        self.scale_std: np.ndarray | None = None
        # per-state metadata, computed at fit time
        self.exp_ret: np.ndarray | None = None      # annualized
        self.exp_vol: np.ndarray | None = None      # annualized
        self.vol_rank: np.ndarray | None = None     # 0..1
        self.labels: list[str] = []
        self.bic_table: dict[int, float] = {}

    # ------------------------------------------------------------- fitting
    def fit(self, X: pd.DataFrame) -> "RegimeModel":
        """BIC-selected GaussianHMM across [n_min, n_max] states.

        The scaler is fit on THIS window only — full-sample standardization
        would leak future statistics into the past.
        """
        self.feature_names = list(X.columns)
        arr = X.to_numpy(dtype=float)
        self.scale_mean = arr.mean(axis=0)
        self.scale_std = arr.std(axis=0) + 1e-12
        Z = (arr - self.scale_mean) / self.scale_std

        best_bic, best_model, best_k = np.inf, None, 0
        self.bic_table = {}
        for k in range(self.n_min, self.n_max + 1):
            model = self._fit_one(Z, k)
            if model is None:
                continue
            bic = model.bic(Z)
            self.bic_table[k] = float(bic)
            # strict inequality: ties go to the simpler model
            if bic < best_bic - 1e-9:
                best_bic, best_model, best_k = bic, model, k
        if best_model is None:
            raise RuntimeError("HMM fitting failed for every candidate state count")

        self.model, self.n_states = best_model, best_k
        self._compute_state_meta()
        log.info(
            "regime model fit: %d states selected by BIC", best_k,
            extra={"ctx": {"bic": {k: round(v, 1) for k, v in self.bic_table.items()}}},
        )
        return self

    def _fit_one(self, Z: np.ndarray, k: int) -> GaussianHMM | None:
        best_ll, best = -np.inf, None
        for r in range(self.restarts):
            try:
                m = GaussianHMM(
                    n_components=k,
                    covariance_type=self.cov_type,
                    n_iter=self.n_iter,
                    random_state=self.seed + 1000 * r,
                    min_covar=1e-3,
                )
                m.fit(Z)
                ll = m.score(Z)
                if np.isfinite(ll) and ll > best_ll:
                    best_ll, best = ll, m
            except Exception as exc:
                log.warning("HMM fit failed (k=%d, restart=%d): %s", k, r, exc)
        return best

    def _compute_state_meta(self) -> None:
        assert self.model is not None
        means = self.model.means_ * self.scale_std + self.scale_mean  # un-scale
        i_ret = self.feature_names.index("log_return")
        i_vol = self.feature_names.index("log_realized_vol")
        self.exp_ret = means[:, i_ret] * 252.0
        self.exp_vol = np.exp(means[:, i_vol])
        order = self.exp_vol.argsort().argsort()  # rank of each state by vol
        self.vol_rank = (
            order / (self.n_states - 1) if self.n_states > 1 else np.zeros(self.n_states)
        )
        self.labels = _state_labels(self.exp_ret, self.vol_rank)

    # ----------------------------------------------------------- inference
    def _log_emissions(self, Z: np.ndarray) -> np.ndarray:
        """log P(obs_t | state_k) for every t, k — no hmmlearn private API."""
        m = self.model
        T, K = Z.shape[0], self.n_states
        logB = np.empty((T, K))
        # hmmlearn 0.3.x exposes covars_ already expanded to (K, n_feat, n_feat)
        # for full/diag/tied, so the full matrix is used directly for every
        # covariance_type — np.diag() would have silently dropped the
        # off-diagonal terms of a 'tied' covariance.
        for k in range(K):
            cov = m.covars_[k]
            logB[:, k] = multivariate_normal.logpdf(Z, mean=m.means_[k], cov=cov, allow_singular=True)
        return logB

    def filtered_probabilities(self, X: pd.DataFrame) -> np.ndarray:
        """P(state_t | obs_1..t) for every t — the forward recursion.

        Strictly causal: row t depends only on rows <= t. Appending future
        rows to X cannot change earlier rows of the output.
        """
        if self.model is None:
            raise RuntimeError("model not fitted")
        Z = (X[self.feature_names].to_numpy(dtype=float) - self.scale_mean) / self.scale_std
        logB = self._log_emissions(Z)
        logA = np.log(self.model.transmat_ + 1e-300)
        log_pi = np.log(self.model.startprob_ + 1e-300)

        T, K = logB.shape
        filtered = np.empty((T, K))
        log_alpha = log_pi + logB[0]
        log_alpha -= _logsumexp(log_alpha)
        filtered[0] = np.exp(log_alpha)
        for t in range(1, T):
            log_alpha = _logsumexp_rows(log_alpha[:, None] + logA) + logB[t]
            log_alpha -= _logsumexp(log_alpha)
            filtered[t] = np.exp(log_alpha)
        return filtered

    def view_at(self, probs_row: np.ndarray) -> RegimeView:
        """Package one bar's filtered probabilities into rich metadata."""
        s = int(probs_row.argmax())
        return RegimeView(
            state=s,
            label=self.labels[s],
            prob=float(probs_row[s]),
            probs=[float(p) for p in probs_row],
            confidence=float(probs_row.max()),
            vol_rank=float(self.vol_rank[s]),
            expected_vol=float(self.exp_vol[s]),
            expected_ret=float(self.exp_ret[s]),
            n_states=self.n_states,
        )

    def views(self, X: pd.DataFrame) -> list[RegimeView]:
        return [self.view_at(row) for row in self.filtered_probabilities(X)]

    # --------------------------------------------------------- persistence
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self.__dict__, fh)

    @classmethod
    def load(cls, path: str | Path) -> "RegimeModel":
        obj = cls.__new__(cls)
        with open(path, "rb") as fh:
            obj.__dict__.update(pickle.load(fh))
        return obj


def _logsumexp(v: np.ndarray) -> float:
    m = v.max()
    return m + np.log(np.exp(v - m).sum())


def _logsumexp_rows(mat: np.ndarray) -> np.ndarray:
    m = mat.max(axis=0)
    return m + np.log(np.exp(mat - m).sum(axis=0))
