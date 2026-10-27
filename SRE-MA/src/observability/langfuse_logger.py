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
from langfuse import Langfuse, propagate_attributes
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
    trace_id: Optional[str] = None,
) -> CallbackHandler:
    """
    returns a callback handler scoped to one incident.
    if trace_id is provided, links callback events to the existing trace.
    """
    tags = ["sre-agent", alert_name]
    if scenario_name:
        tags.append(f"scenario: {scenario_name}")

    trace_context = {"trace_id": trace_id} if trace_id else None
    return CallbackHandler(trace_context=trace_context)


def get_trace_id(handler: CallbackHandler) -> Optional[str]:
    """
    pulls the trace_id generated for thi incident's invoke call, so we can attach
    feedback later using langfuse SDK.
    """
    try:
        if hasattr(handler, "last_trace_id") and handler.last_trace_id:
            return handler.last_trace_id
        if hasattr(handler, "get_trace_id"):
            return handler.get_trace_id()
        client = _get_client()
        return client.get_current_trace_id()
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
    sets trace-level userId and sessionId for proper Langfuse tracking
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

    #dont log final incident resolution metrics if the incident is still opem
    if str(resolution_status).lower() in ("investigating", "waiting_for_approval", "open"):
        return

    #determine if it was success (only RESOLVED counts, ESCALATED/FAILED do not count)
    is_resolved = 1.0 if resolution_status and resolution_status.upper() == "RESOLVED" else 0.0

    #escalation with evidence tracking  
    has_evidence = bool(
        result.get("root_cause") is not None or
        result.get("investigation_steps", 0) > 0 or
        result.get("evidence_summary") is not None or
        result.get("llm_suggested_action") is not None or
        result.get("diagnosis_summary") is not None or
        len(result.get("hypotheses", [])) > 0 or
        len(result.get("action_plan", [])) > 0
    )

    #determine escalation level
    if is_resolved == 1.0:
        escalation_class = "resolved"
    elif has_evidence:
        escalation_class = "escalated_with_evidence"
    else:
        escalation_class = "escalated_without_evidence"

    #log ERROR/WARNING for escalations
    if escalation_class == "escalated_without_evidence":
        client.log_level = "ERROR"
        level = "ERROR"
    elif escalation_class == "escalated_with_evidence":
        client.log_level = "WARNING"
        level = "WARNING"
    else:
        level = "INFO"

    def _score(name: str, value: Any, data_type: str = "NUMERIC"):
        try:
            client.create_score(trace_id=trace_id, name=name, value=value, data_type=data_type)
        except Exception:
            client.score_current_trace(name=name, value=value, data_type=data_type)

    try:
        # mttr only exists for genuinely resolved incidents; check business/agent MTTR keys
        mttr = result.get("business_mttr_seconds") or result.get("agent_mttr_seconds") or result.get("mttr_seconds")
        if resolution_status.upper() == "RESOLVED" and mttr is not None:
            _score("mttr_seconds", float(mttr), "NUMERIC")
        cost_usd = float(result.get("token_cost_usd") or 0)
        _score("token_cost_usd", cost_usd, "NUMERIC")
        _score("cost", cost_usd, "NUMERIC")
        _score("max_confidence", float(confidence or 0), "NUMERIC")
        _score("diagnosis_loops", float(result.get("diagnosis_loops") or 0), "NUMERIC")
        _score("resolution_status", is_resolved, "NUMERIC")

        #log escalation class for all runs so resolved updates stale in-flight scores
        _score("escalation_class", escalation_class, "CATEGORICAL")
            
        #log ground truth comparison — only when the scenario has a known oracle;
        # novel scenarios (no expected runbook) must not score a fake 0
        if scenario_name:
            expected_runbook = SCENARIO_EXPECTED_RUNBOOK.get(scenario_name)
            if expected_runbook and diagnosed_runbooks:
                is_correct = 1.0 if expected_runbook in diagnosed_runbooks else 0.0
                _score("ground_truth_match", is_correct, "NUMERIC")

        #set trace-level fields: userid, sessionid at trace level, tags preserved
        incident_id = result.get("incident_id", "unknown")
        service = result.get("raw_signals", {}).get("service", "unknown")
        alert_name = result.get("alert_name", "unknown")
        
        #update current span with trace-level attributes
        client.update_current_span(
            metadata={
                "alert_name": alert_name,
                "service": service,
                "severity": result.get("severity", "unknown"),
                "diagnosis_mode": result.get("diagnosis_mode", "unknown"),
                "diagnosed_runbook": diagnosed_runbooks,
                "resolution_status": resolution_status,
                "verified": result.get("verified", False),
                "escalation_class": escalation_class,
                "token_cost_usd": cost_usd,
                "total_tokens_used": result.get("total_tokens_used", 0),
            },
        )
        #set trace-level user, session, tags
        with propagate_attributes(
            user_id=incident_id,
            session_id=service,
            tags=["sre-agent", alert_name, f"severity:{result.get('severity', 'unknown')}", f"escalation_class:{escalation_class}"]
            ):  
            
            #tag the client itself for later updates
            client.tags = tags = ["sre-agent", alert_name]
        
        print(f"[Langfuse] logged metrics for {trace_id} | status: {resolution_status} | class: {escalation_class} | level: {level}")
    
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
        
        client.create_score(
            trace_id=trace_id,
            name="human_approval",
            value=1.0 if approved else 0.0,
            comment=notes or f"Action {'approved' if approved else 'rejected'} by {approver}",
            data_type="BOOLEAN",
            metadata={"approver": approver}
            
        )
        print(f"[langfuse] Logged human feedback on {trace_id}: approved: {approver}")
    except Exception as e:
        print(f"[langfuse] Failed to log human feedback: {e}")


