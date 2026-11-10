"""self-check for the deterministic change-point detector (Bet 2)."""

import time

from src.tools.changepoint import detect_changepoint


def _series(n=30, step_s=30, values=None):
    now = time.time()
    return [(now - (n - 1 - i) * step_s, v) for i, v in enumerate(values)]


def test_flat_series_yields_no_hint():
    assert detect_changepoint(_series(values=[1.0] * 30)) is None


def test_short_series_yields_no_hint():
    assert detect_changepoint(_series(values=[0.1, 5.0, 0.1, 5.0])) is None


def test_level_shift_up_is_detected_with_onset():
    vals = [0.05] * 18 + [0.90] * 12  # step at index 18
    cp = detect_changepoint(_series(values=vals))
    assert cp is not None
    assert cp["direction"] == "up"
    assert cp["sigma"] >= 3.0
    assert cp["minutes_ago"] <= 12 * 0.5 + 1  # onset in the last 12 samples


def test_level_shift_down_detected_with_direction():
    vals = [0.90] * 18 + [0.05] * 12
    cp = detect_changepoint(_series(values=vals))
    assert cp is not None
    assert cp["direction"] == "down"


def test_noisy_flat_series_stays_silent():
    import random

    rng = random.Random(7)
    vals = [0.5 + rng.uniform(-0.05, 0.05) for _ in range(30)]
    assert detect_changepoint(_series(values=vals)) is None
