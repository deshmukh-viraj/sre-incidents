"""
obervability using langfuse

covers all the requirements
- aggregate metrics across many incidents (confidence, MTTR, cost)
- per-incidents reasoning traces
- human feedback attached to a specific incident's trace
- ground truth comparison in experiment
"""

import os
from typing import Optional, Dict, Any, List
from pathlib import Path
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from dotenv import load_dotenv


load_dotenv()

_client: Optional[Langfuse] = None


def _get_client() -> Langfuse:
    global _client
    if _client is None:
        _client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_BASE_URL"),
        )
    return _client


def get_langfuse_handler(
    session_id: str,
    alert_name: str = "unknown",
    scenario_name: Optional[str] = None,
) -> CallbackHandler:
    """
    returns a callback hanfdler scoped to one incident
    pass this into th graph invoke config
    """
    tags = ["sre-agent", alert_name]
    if scenario_name:
        tags.append(f"scenario: {scenario_name}")

    return CallbackHandler()


def get_trace_id(hanfdler: CallbackHandler) -> Optional[str]:
    """
    pulls the trace_id generated for thi incident's invoke call, so we can attach
    feedback later using langfuse SDK.
    """
    try:
        return hanfdler.get_trace_id()
    except Exception as e:
        print(f"[langfuse_logger] Warning: failed to capture trace_id: {e}")
        return None


#ground truth
SCENARIO_EXPECTED_RUNBOOK: Dict[str, str] = {
    "payment_latency_spike": "RB-001",
    "circuit_breaker_trip": "RB-002",
    "data_exfiltration_attempt": "RB-003",
    "db_connection_exhaustion": "RB-004",
    "compliance_audit_fail": "RB-005",
    "fraud_model_degradation": "RB-006",
}    

def log_incident_run(
    result: Dict[str, Any],
    trace_id: Optional[str],
    scenario_name: Optional[str] = None,
):
    """
    attaches per incident sre metrics as scores on the trace
    extracts data directly from the final agentstate dictionary
    """
    if not trace_id:
        print(f"[Langfuse] No trace_id found, skipping incident logging")
        return
    
    client = Langfuse()

    #extracts data safely from the state dictionary
    hypotheses = result.get("hypotheses") or []
    best_hyp = max(hypotheses, key=lambda h: h.get("confidence", 0)) if hypotheses else {}

    confidence = best_hyp.get("confidence", 0.0)
    diagnosed_runbooks = best_hyp.get("supporting_runbook") or result.get("root_cause") or "none"
    resolution_status = result.get("resolution_status", "UNKNOWN")

    #determine if it was success (only RESOLVED counts, ESCALATED/FAILED do not count)
    is_resolved = 1.0 if resolution_status and resolution_status.upper() == "RESOLVED" else 0.0

    try:
        #log numeric sre metrics using v4.x score_current_trace
        client.score_current_trace(name="mttr_seconds", value=float(result.get("mttr_seconds") or 0), data_type="NUMERIC")
        client.score_current_trace(name="token_cost_usd", value=float(result.get("token_cost_usd") or 0), data_type="NUMERIC")
        client.score_current_trace(name="max_confidence", value=float(confidence or 0), data_type="NUMERIC")
        client.score_current_trace(name="diagnosis_loops", value=float(result.get("diagnosis_loops") or 0), data_type="NUMERIC")
        client.score_current_trace(name="resolution_status", value=is_resolved, data_type="NUMERIC")

        # log grund truth comparison
        if scenario_name:
            expected_runbook = SCENARIO_EXPECTED_RUNBOOK.get(scenario_name, "unknown")
            if expected_runbook and diagnosed_runbooks:
                is_correct = 1.0 if expected_runbook in diagnosed_runbooks else 0.0
                client.score_current_trace(name="ground_truth_match", value=is_correct, data_type="NUMERIC")

        # update current span metadata (replaces old client.trace() call)
        client.update_current_span(
            metadata={
                "alert_name": result.get("alert_name", "unknown"),
                "service": result.get("raw_signals", {}).get("service", "unknown"),
                "severity": result.get("severity", "unknown"),
                "diagnosis_mode": result.get("diagnosis_mode", "unknown"),
                "diagnosed_runbook": diagnosed_runbooks,
                "resolution_status": resolution_status,
                "verified": result.get("verified", False)
            },
        )
        print(f"[Langfuse] logged metrics for {trace_id} | status: {resolution_status}")
    
    except Exception as e:
        print(f"[Langfuse] Failed to log incident scores: {e}")



def log_human_feedback(
    trace_id: Optional[str],
    approved: bool,
    notes: Optional[str] = None,
    approver: str ="unknown",
):
    """
    attaches a human's approve/reject decision to the trace.
    call by the fastapi /approve endpoint
    """
    if not trace_id:
        return
    
    client = _get_client()
    try:
        client.score(
            trace_id=trace_id, 
            name="human_approval",
            value=1.0 if approved else 0.0,
            comment=notes or f"Action {'approved' if approved else 'rejected'} by {approver}",
            data_type="BOOLEAN",
            user_id = approver
        )
        print(f"[langfuse] Logged human feedback on {trace_id}: approved: {approver}")
    except Exception as e:
        print(f"[langfuse] Failed to log human feedback: {e}")

