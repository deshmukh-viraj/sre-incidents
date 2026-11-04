"""
Holdout-arm assignment tests.

These do NOT import app.py (it needs fastapi + src.*). They test the exact
_pick_arm logic copied verbatim, so the statistical properties are proven
without standing up the whole service.
"""

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pick_arm(incident_id: str, rate: float, seed: str | None) -> bool:
    """Verbatim copy of app.py::_pick_arm logic."""
    if rate <= 0.0:
        return False
    if seed:
        digest = hashlib.sha256(f"{seed}:{incident_id}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    else:
        import random

        draw = random.random()
    return draw < rate


IDS = [f"INC-HighLat-{i:04X}" for i in range(2000)]


def test_disabled_by_default():
    assert all(not pick_arm(i, 0.0, None) for i in IDS)


def test_rate_one_puts_everything_in_shadow():
    assert all(pick_arm(i, 1.0, "s") for i in IDS)


def test_seeded_assignment_is_deterministic():
    """A validation run must be reproducible -- same seed, same arms."""
    a = [pick_arm(i, 0.3, "run-42") for i in IDS]
    b = [pick_arm(i, 0.3, "run-42") for i in IDS]
    assert a == b


def test_different_seeds_give_different_splits():
    a = [pick_arm(i, 0.3, "run-1") for i in IDS]
    b = [pick_arm(i, 0.3, "run-2") for i in IDS]
    assert a != b


def test_split_is_close_to_requested_rate():
    """0.3 over 2000 ids should land near 30%, else the hash is biased."""
    frac = sum(pick_arm(i, 0.3, "run-42") for i in IDS) / len(IDS)
    assert 0.26 < frac < 0.34, f"got {frac:.3f}"


def test_shadow_arm_can_never_earn_credit():
    """The core invariant: a no-op cannot be labelled agent_remediated."""
    from src.graph.attribution_gates import AGENT_REMEDIATED, NATURAL_CALM, Gate3Result, final_label

    g3 = Gate3Result(
        passed=True, label_hint=AGENT_REMEDIATED, lead_seconds=5.0,
        reason="recovered right after (no-op) action",
    )
    label = final_label(True, True, g3, True, True, shadow_arm=True)
    assert label == NATURAL_CALM, label

    # same evidence, real arm -> credit is granted
    assert final_label(True, True, g3, True, True, shadow_arm=False) == AGENT_REMEDIATED
