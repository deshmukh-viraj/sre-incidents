"""
Regression suite for the t_clear measurement fix.

Every test is a synthetic Prometheus series -- no Prometheus, no docker, no LLM.
The point: prove the LABEL CHANGES because of the code change, on the exact
scenarios that produced the false positive.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.graph.clear_time import (
    ClearEvidence,
    build_clear_evidence,
    find_true_clear_time,
    analyse_pre_action,
)
from src.graph.attribution_gates import ( 
    AGENT_REMEDIATED,
    ACTION_FAILED,
    AMBIGUOUS,
    NATURAL_CALM,
    evaluate_gate3,
)

STEP = 5
THRESH = 2.0          # seconds of latency; healthy == below
PEAK = 10.0
GREEN = 0.4


def series(spans):
    """spans = [(duration_sec, value), ...] starting at t=0."""
    out, t = [], 0.0
    for dur, val in spans:
        n = int(dur // STEP)
        for _ in range(n):
            out.append((t, val))
            t += STEP
    return out


def fetch_from(samples):
    def _fetch(expr, start, end, step, **kw):
        return [(ts, v) for ts, v in samples if start <= ts <= end]

    return _fetch


# ---------------------------------------------------------------------------
# the old bug, reproduced
# ---------------------------------------------------------------------------

def test_polling_architecture_awards_false_credit():
    """Simulate the OLD behaviour: t_clear = the poll time, always +60s."""
    t_action_end = 300.0
    t_clear_polled = t_action_end + 60          # agent slept 60s then looked, saw green
    tolerance = 60.0
    # the old inequality
    assert t_clear_polled <= t_action_end + tolerance
    # -> Gate 3 passes NO MATTER WHAT the metric actually did. That is the bug.


# ---------------------------------------------------------------------------
# scenario C == incident 5972: agent acts DURING natural_calming downslope
# ---------------------------------------------------------------------------

def test_scenario_C_agent_acts_on_downslope_is_natural_calm():
    # peak 10s for 200s, then a clean glide down starting at t=200
    s = series(
        [(200, PEAK), (20, 7.5), (20, 5.0), (20, 3.0), (20, 1.2), (200, GREEN)]
    )
    # agent executes at t=240 -- squarely inside the decline
    t_action_start, t_action_end, t_now = 240.0, 250.0, 400.0

    ev = build_clear_evidence(
        "latency", THRESH, t_action_start, t_action_end, t_now,
        step=STEP, fetch=fetch_from(s),
    )
    assert ev.already_healing is True, ev.to_dict()

    g3 = evaluate_gate3(ev, t_action_end)
    assert g3.passed is False
    assert g3.label_hint == NATURAL_CALM
    print("\n[C] ", g3.reason)


# ---------------------------------------------------------------------------
# the 30-second spike you predicted would still slip through polling
# ---------------------------------------------------------------------------

def test_short_spike_recovery_before_action_is_caught():
    """30s spike, recovers at t=30. Agent acts at t=100, polls at t=160.

    Old code: t_clear=160 <= 110+60 -> PASSES (false positive).
    New code: t_clear_true=30, lead=-80s -> natural_calm.
    """
    s = series([(30, PEAK), (400, GREEN)])
    t_action_start, t_action_end, t_now = 100.0, 110.0, 170.0

    ev = build_clear_evidence(
        "latency", THRESH, t_action_start, t_action_end, t_now,
        step=STEP, fetch=fetch_from(s),
    )
    g3 = evaluate_gate3(ev, t_action_end)

    assert ev.t_clear_true == 30.0, ev.to_dict()
    assert g3.lead_seconds == -80.0
    assert g3.label_hint == NATURAL_CALM
    assert ev.per_action_healthy is True
    print("\n[short-spike] ", g3.reason)


# ---------------------------------------------------------------------------
# the true positive must still pass -- a fix that breaks this is useless
# ---------------------------------------------------------------------------

def test_true_positive_still_earns_credit():
    """Flat 10s at peak, agent acts at 300-310, metric drops right after."""
    s = series([(310, PEAK), (200, GREEN)])
    t_action_start, t_action_end, t_now = 300.0, 310.0, 420.0

    ev = build_clear_evidence(
        "latency", THRESH, t_action_start, t_action_end, t_now,
        step=STEP, fetch=fetch_from(s),
    )
    g3 = evaluate_gate3(ev, t_action_end)

    assert ev.already_healing is False
    assert ev.per_action_healthy is False
    assert g3.passed is True
    assert g3.label_hint == AGENT_REMEDIATED
    assert 0 <= g3.lead_seconds <= 60
    print("\n[true-positive] ", g3.reason)


def test_never_recovers_is_action_failed():
    s = series([(600, PEAK)])
    ev = build_clear_evidence(
        "latency", THRESH, 300, 310, 600, step=STEP, fetch=fetch_from(s)
    )
    g3 = evaluate_gate3(ev, 310)
    assert ev.t_clear_true is None
    assert g3.label_hint == ACTION_FAILED


def test_late_recovery_is_ambiguous():
    """Recovers 200s after we acted -- too late to credit honestly."""
    s = series([(510, PEAK), (200, GREEN)])
    ev = build_clear_evidence(
        "latency", THRESH, 300, 310, 720, step=STEP, fetch=fetch_from(s)
    )
    g3 = evaluate_gate3(ev, 310)
    assert g3.lead_seconds == 200.0
    assert g3.label_hint == AMBIGUOUS


# ---------------------------------------------------------------------------
# robustness of the sustained-run rule
# ---------------------------------------------------------------------------

def test_single_green_scrape_does_not_declare_recovery():
    """One lucky dip inside the peak must not count as a clear."""
    s = series([(100, PEAK), (5, GREEN), (100, PEAK), (100, GREEN)])
    ev = find_true_clear_time(s, THRESH, consecutive=3)
    assert ev.t_clear_true == 205.0, ev.to_dict()


def test_flapping_series_reports_first_sustained_run():
    s = series([(50, PEAK), (10, GREEN), (10, PEAK), (60, GREEN)])
    ev = find_true_clear_time(s, THRESH, consecutive=3)
    assert ev.t_clear_true == 70.0


def test_above_comparison_for_success_rate_metrics():
    """success_rate: healthy == ABOVE threshold."""
    s = series([(60, 0.10), (60, 0.99)])
    ev = find_true_clear_time(s, 0.95, consecutive=3, comparison="above")
    assert ev.t_clear_true == 60.0


def test_empty_series_is_not_a_recovery():
    ev = find_true_clear_time([], THRESH)
    assert ev.t_clear_true is None
    assert ev.samples_seen == 0
    assert "no samples" in ev.notes[0]


def test_pre_action_trend_detects_decline():
    pre = series([(20, 10.0), (20, 7.0), (20, 4.0)])
    healthy, trend, healing = analyse_pre_action(pre, THRESH)
    assert healing is True
    assert trend > 0.5
    assert healthy is False   # 4.0 still above 2.0 threshold


def test_pre_action_trend_ignores_flat_peak():
    pre = series([(60, PEAK)])
    healthy, trend, healing = analyse_pre_action(pre, THRESH)
    assert healing is False
    assert abs(trend) < 1e-9


def test_noisy_flat_peak_is_not_mistaken_for_healing():
    """Jittery but non-declining peak must not read as already_healing."""
    s = [(i * 5.0, 10.0 + (0.4 if i % 2 else -0.4)) for i in range(24)]
    healthy, trend, healing = analyse_pre_action(s, THRESH)
    assert healing is False


def test_insufficient_samples_falls_back_to_t_clear_polled():
    """When range query has 1 sample (below consecutive=3) but live verification succeeded, fall back to t_clear_polled."""
    s = [(100.0, 10.0), (105.0, 0.4)]  # Only 1 healthy sample, consecutive=3 needed
    ev = build_clear_evidence(
        "latency", THRESH, 100.0, 105.0, 110.0,
        step=STEP, fetch=fetch_from(s), t_clear_polled=105.0
    )
    g3 = evaluate_gate3(ev, 105.0)
    assert ev.t_clear_true == 105.0
    assert g3.passed is True
    assert g3.label_hint == AGENT_REMEDIATED

