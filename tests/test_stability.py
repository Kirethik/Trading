"""Regime stability: confirmation, transition, flicker -> uncertainty."""
from regime_trader.regime.stability import StabilityTracker


def test_change_needs_confirmation():
    t = StabilityTracker(confirm_bars=3, flicker_window=10, flicker_max_switches=4)
    for _ in range(5):
        s = t.update(0)
    assert s.confirmed_state == 0
    s = t.update(1)
    assert s.confirmed_state == 0 and s.in_transition
    s = t.update(1)
    assert s.confirmed_state == 0 and s.in_transition
    s = t.update(1)  # third consecutive bar -> confirmed
    assert s.confirmed_state == 1 and not s.in_transition


def test_interrupted_candidate_resets():
    t = StabilityTracker(confirm_bars=3, flicker_window=10, flicker_max_switches=4)
    for _ in range(5):
        t.update(0)
    t.update(1)
    t.update(1)
    s = t.update(0)  # back to confirmed before the third bar
    assert s.confirmed_state == 0 and not s.in_transition
    s = t.update(1)  # candidate must start over
    assert s.confirmed_state == 0 and s.in_transition


def test_flicker_forces_uncertainty():
    t = StabilityTracker(confirm_bars=2, flicker_window=10, flicker_max_switches=4)
    s = None
    for i in range(12):  # alternate every bar: 9 switches in any 10-bar window
        s = t.update(i % 2)
    assert s.flickering and s.uncertainty_mode


def test_stable_sequence_never_flickers():
    t = StabilityTracker(confirm_bars=3, flicker_window=10, flicker_max_switches=4)
    s = None
    for _ in range(30):
        s = t.update(2)
    assert not s.flickering and s.consecutive_bars >= 29


def test_serialization_round_trip():
    t = StabilityTracker(confirm_bars=3, flicker_window=10, flicker_max_switches=4)
    seq = [0, 0, 0, 1, 1, 0, 2, 2]
    for x in seq:
        t.update(x)
    d = t.to_dict()
    t2 = StabilityTracker.from_dict(d, 3, 10, 4)
    # both trackers must agree on every future update
    for x in [2, 2, 1, 1, 1, 0]:
        a, b = t.update(x), t2.update(x)
        assert (a.confirmed_state, a.in_transition, a.flickering) == \
               (b.confirmed_state, b.in_transition, b.flickering)
