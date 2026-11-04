"""unit tests for the deterministic routing + diagnosis layer (frozen thresholds)."""

from typing import Any

from src.graph.state import AgentState
from src.graph.routing import (
    classify_severity,
    classify_blast_radius,
    requires_human_approval,
    deterministic_diagnosis,
    route_after_diagnosis,
    _apply_residual_check,
    infer_service,
    P99_LATENCY_WARNING, ERROR_RATE_WARNING, DB_POOL_WARNING,
)


def _state(**signals: Any) -> AgentState:
    log_patterns = signals.pop("log_patterns", {})
    runbook_id = signals.pop("runbook_id", None)
    return AgentState(
        runbook_id=runbook_id,
        raw_signals={"log_patterns": log_patterns, **signals},
    )


# --- classify_severity: alert label is the paging contract ---

def test_severity_label_wins_over_metrics():
    # warning label + SEV1-level p99: label decides (INC-SLOBur regression)
    s = _state(severity="warning", p99_latency_s=4.0)
    assert classify_severity(s) == "SEV2"


def test_severity_metric_fallback_critical():
    assert classify_severity(_state(p99_latency_s=3.5)) == "SEV1"
    assert classify_severity(_state(error_rate=0.2)) == "SEV1"
    assert classify_severity(_state(circuit_breaker_state=2.0)) == "SEV1"


def test_severity_metric_fallback_warning_and_default():
    assert classify_severity(_state(p99_latency_s=P99_LATENCY_WARNING)) == "SEV2"
    assert classify_severity(_state(db_pool_utilization=DB_POOL_WARNING)) == "SEV2"
    assert classify_severity(_state(p99_latency_s=0.1)) == "SEV3"


# --- deterministic diagnosis: runbook-gated patterns ---

def test_rb001_throttling_confirmed():
    s = _state(runbook_id="RB-001", p99_latency_s=2.5, error_rate=0.12,
               throttle_error_rate=0.09, http_429_count=40)
    h = deterministic_diagnosis(s)
    assert h["supporting_runbook"] == "RB-001"
    assert "throttling" in h["hypothesis"].lower()
    assert h["confidence"] >= 0.8


def test_rb001_without_throttling_is_ambiguous():
    s = _state(runbook_id="RB-001", p99_latency_s=2.5, error_rate=0.12, throttle_error_rate=0.01)
    h = deterministic_diagnosis(s)
    assert "ambiguous" in h["hypothesis"].lower()
    assert h["confidence"] == 0.50


def test_rb004_pool_exhaustion():
    s = _state(runbook_id="RB-004", db_pool_utilization=0.97, log_patterns={"slow_queries": 0})
    h = deterministic_diagnosis(s)
    assert "connection pool" in h["hypothesis"].lower()


def test_no_match_returns_none():
    assert deterministic_diagnosis(_state(p99_latency_s=0.2)) is None
    # runbook label present but confirming signals absent -> None
    assert deterministic_diagnosis(_state(runbook_id="RB-006", payment_decline_rate=0.01)) is None


# --- residual gate: matched runbook must explain every live anomaly ---

def test_residual_gate_caps_confidence_on_unexplained_db_pressure():
    result = {"supporting_runbook": "RB-001", "confidence": 0.88, "evidence": []}
    state = _state(runbook_id="RB-001", p99_latency_s=2.5, db_pool_utilization=0.9)
    out = _apply_residual_check(result, state)
    assert out["residual_gate_failed"] is True
    assert out["confidence"] <= 0.65
    assert any("DB pressure" in e for e in out["evidence"])


def test_residual_gate_passes_when_explained():
    result = {"supporting_runbook": "RB-001", "confidence": 0.88, "evidence": []}
    state = _state(runbook_id="RB-001", p99_latency_s=2.5, error_rate=0.1, throttle_error_rate=0.09)
    out = _apply_residual_check(result, state)
    assert "residual_gate_failed" not in out


# --- route_after_diagnosis ---

def test_high_confidence_goes_to_remediator_even_after_loops():
    state = {"hypotheses": [{"confidence": 0.9}], "diagnosis_mode": "deterministic", "diagnosis_loops": 3}
    assert route_after_diagnosis(state) == "remediator"


def test_loop_budget_escalates():
    state = {"hypotheses": [{"confidence": 0.3}], "diagnosis_mode": "llm", "diagnosis_loops": 3}
    assert route_after_diagnosis(state) == "escalate"


def test_no_hypothesis_tries_llm_then_escalates():
    assert route_after_diagnosis({"hypotheses": [], "diagnosis_mode": None, "diagnosis_loops": 0}) == "llm_diagnoser"
    assert route_after_diagnosis({"hypotheses": [], "diagnosis_mode": "llm", "diagnosis_loops": 0}) == "escalate"


# --- blast radius / approval ---

def test_cluster_and_irreversible_need_human():
    assert requires_human_approval({"tool": "kubectl_scale", "params": {"service": "x"}, "reversible": True})
    assert requires_human_approval({"tool": "revoke_api_key", "params": {}, "reversible": False})


def test_blast_radius_classification():
    assert classify_blast_radius({"tool": "set_feature_flag", "params": {}}) == "pod"
    assert classify_blast_radius({"tool": "rolling_restart", "params": {"service": "payment_gateway"}}) == "service"
    assert classify_blast_radius({"tool": "rolling_restart", "params": {"service": "postgres_primary"}}) == "cluster"


def test_infer_service_mapping():
    assert infer_service("DBConnectionPoolExhaustion") == "account_ledger"
    assert infer_service("FraudModelDrift") == "fraud_detector"
    assert infer_service("TotallyUnknown") == "payment_gateway"  # documented default
