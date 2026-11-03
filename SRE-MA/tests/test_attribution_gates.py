"""unit tests for the frozen Five-Gate Attribution Framework (behavior as-is)."""

from src.graph.clear_time import ClearEvidence
from src.graph.attribution_gates import evaluate_gate3, final_label, AGENT_REMEDIATED


def _ev(t_clear=None, already_healing=False, per_action_healthy=False,
        healthy_run_len=3, trend=None):
    return ClearEvidence(
        t_clear_true=t_clear,
        clear_source="range_backfill" if t_clear is not None else "never_cleared",
        healthy_run_len=healthy_run_len,
        already_healing=already_healing,
        per_action_healthy=per_action_healthy,
        per_action_trend_pct=trend,
    )


# --- gate 3: temporal window ---

def test_never_recovered_is_action_failed():
    g3 = evaluate_gate3(_ev(t_clear=None), t_action_end=100.0)
    assert g3.passed is False
    assert g3.label_hint == "ACTION_FAILED"
    assert g3.lead_seconds is None


def test_already_healing_revokes_credit():
    g3 = evaluate_gate3(_ev(t_clear=105.0, already_healing=True, trend=-0.5), t_action_end=100.0)
    assert g3.passed is False
    assert g3.label_hint == "NATURAL_CALM"


def test_healthy_before_action_revokes_credit():
    g3 = evaluate_gate3(_ev(t_clear=90.0, per_action_healthy=True), t_action_end=100.0)
    assert g3.passed is False
    assert g3.label_hint == "NATURAL_CALM"


def test_cleared_long_before_action_is_natural_calm():
    g3 = evaluate_gate3(_ev(t_clear=80.0), t_action_end=100.0, grace_sec=5.0)
    assert g3.passed is False
    assert g3.label_hint == "NATURAL_CALM"
    assert g3.lead_seconds is not None and g3.lead_seconds < -5.0


def test_cleared_too_long_after_is_ambiguous():
    g3 = evaluate_gate3(_ev(t_clear=100.0 + 301.0), t_action_end=100.0, tolerance_sec=300.0)
    assert g3.passed is False
    assert g3.label_hint == "AMBIGUOUS"


def test_in_window_recovery_passes():
    g3 = evaluate_gate3(_ev(t_clear=145.0, healthy_run_len=4), t_action_end=100.0, tolerance_sec=300.0)
    assert g3.passed is True
    assert g3.label_hint == "AGENT_REMEDIATED"
    assert g3.lead_seconds == 45.0


# --- final label: the 5-gate matrix ---

def _g3(passed=True, ok=True):
    from src.graph.attribution_gates import Gate3Result
    return Gate3Result(passed=passed, label_hint="X" if not passed else AGENT_REMEDIATED,
                       lead_seconds=10.0 if ok else None, reason="test")


def test_all_gates_pass_is_agent_remediated():
    assert final_label(True, True, _g3(), True, True) == "AGENT_REMEDIATED"


def test_no_claim_is_natural_calm():
    assert final_label(False, True, _g3(), True, True) == "NATURAL_CALM"


def test_target_not_bound_is_ambiguous():
    assert final_label(True, False, _g3(), True, True) == "AMBIGUOUS"


def test_shadow_arm_never_gets_credit():
    assert final_label(True, True, _g3(), True, True, shadow_arm=True) == "NATURAL_CALM"
    assert final_label(True, True, _g3(ok=False), True, True, shadow_arm=True) == "ACTION_FAILED"


def test_gate3_failure_hints_label():
    from src.graph.attribution_gates import Gate3Result, NATURAL_CALM
    g3 = Gate3Result(passed=False, label_hint=NATURAL_CALM, lead_seconds=-20.0, reason="r")
    assert final_label(True, True, g3, True, True) == "NATURAL_CALM"


def test_unverified_is_action_failed_and_flap_is_ambiguous():
    assert final_label(True, True, _g3(), False, True) == "ACTION_FAILED"
    assert final_label(True, True, _g3(), True, False) == "AMBIGUOUS"
