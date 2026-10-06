"""Daily USD spending cap for agent model calls.

Shared by the cron jobs and the supervisor, so every read-modify-write is
done under an exclusive file lock. An unknown model is priced at the most
expensive configured rate (fail conservative).
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import get_config
from ..utils import atomic_write_json, load_json, utc_now

_PER = 1_000_000


class Budget:
    def __init__(self, path: str | Path, daily_usd: float, pricing: dict):
        self.path = Path(path)
        self.daily_usd = float(daily_usd)
        self.pricing = {m: (float(p["input"]), float(p["output"])) for m, p in pricing.items()}
        if not self.pricing:
            raise ValueError("agents.pricing is empty: the spending cap could never trip")

    @classmethod
    def from_config(cls) -> "Budget":
        a = get_config().agents
        return cls(get_config().resolve_path(a.budget_file), a.daily_budget_usd,
                   a.pricing.as_dict())

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        rate_in, rate_out = self.pricing.get(model) or max(self.pricing.values())
        return (input_tokens * rate_in + output_tokens * rate_out) / _PER

    def _today_state(self) -> dict:
        today = utc_now().date().isoformat()
        state = load_json(self.path, default=None) or {}
        if state.get("date") != today:
            return {"date": today, "spent_usd": 0.0, "calls": 0, "by_model": {}}
        return state

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        usd = self.cost(model, input_tokens, output_tokens)
        with self._locked():
            state = self._today_state()
            by_model = dict(state["by_model"])
            by_model[model] = round(by_model.get(model, 0.0) + usd, 6)
            atomic_write_json(self.path, {
                "date": state["date"],
                "spent_usd": round(state["spent_usd"] + usd, 6),
                "calls": state["calls"] + 1,
                "by_model": by_model,
                "daily_usd": self.daily_usd,
            })
        return usd

    def spent_today(self) -> float:
        return float(self._today_state()["spent_usd"])

    def snapshot(self) -> dict:
        return {**self._today_state(), "daily_usd": self.daily_usd}

    def exhausted(self) -> bool:
        return self.spent_today() >= self.daily_usd
