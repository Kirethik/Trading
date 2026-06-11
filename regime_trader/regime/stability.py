"""Regime stability: confirmation, transition damping, flicker -> uncertainty.

A raw regime change only *counts* once it has persisted `confirm_bars` bars.
While a change is pending the system is "in transition" (sizes reduced).
Too many raw switches inside a trailing window means the model is flickering,
which forces uncertainty mode (halved sizes, neutral leverage) downstream.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class StabilityState:
    raw_state: int
    confirmed_state: int
    consecutive_bars: int        # bars the confirmed regime has held
    in_transition: bool          # a candidate change is pending confirmation
    flickering: bool
    uncertainty_mode: bool       # flicker or low data -> downstream halves size
    raw_switches_in_window: int


class StabilityTracker:
    def __init__(self, confirm_bars: int = 3, flicker_window: int = 10, flicker_max_switches: int = 4):
        self.confirm_bars = confirm_bars
        self.flicker_window = flicker_window
        self.flicker_max = flicker_max_switches
        self._raw: deque[int] = deque(maxlen=flicker_window)
        self.confirmed: int | None = None
        self.candidate: int | None = None
        self.candidate_count = 0
        self.consecutive = 0

    def update(self, raw_state: int) -> StabilityState:
        self._raw.append(raw_state)

        if self.confirmed is None:  # first ever observation
            self.confirmed, self.consecutive = raw_state, 1
        elif raw_state == self.confirmed:
            self.candidate, self.candidate_count = None, 0
            self.consecutive += 1
        else:
            if raw_state == self.candidate:
                self.candidate_count += 1
            else:
                self.candidate, self.candidate_count = raw_state, 1
            if self.candidate_count >= self.confirm_bars:
                self.confirmed = self.candidate
                self.consecutive = self.candidate_count
                self.candidate, self.candidate_count = None, 0

        switches = sum(1 for a, b in zip(list(self._raw), list(self._raw)[1:]) if a != b)
        flickering = len(self._raw) >= self.flicker_window and switches > self.flicker_max
        return StabilityState(
            raw_state=raw_state,
            confirmed_state=self.confirmed,
            consecutive_bars=self.consecutive,
            in_transition=self.candidate is not None,
            flickering=flickering,
            uncertainty_mode=flickering,
            raw_switches_in_window=switches,
        )

    # --- persistence for kill-and-restart recovery ---
    def to_dict(self) -> dict:
        return {
            "raw": list(self._raw),
            "confirmed": self.confirmed,
            "candidate": self.candidate,
            "candidate_count": self.candidate_count,
            "consecutive": self.consecutive,
        }

    @classmethod
    def from_dict(cls, d: dict, confirm_bars: int, flicker_window: int, flicker_max: int) -> "StabilityTracker":
        t = cls(confirm_bars, flicker_window, flicker_max)
        for r in d.get("raw", []):
            t._raw.append(r)
        t.confirmed = d.get("confirmed")
        t.candidate = d.get("candidate")
        t.candidate_count = d.get("candidate_count", 0)
        t.consecutive = d.get("consecutive", 0)
        return t
