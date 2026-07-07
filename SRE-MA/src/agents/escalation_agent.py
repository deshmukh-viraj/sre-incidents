"""
agents/escalation_agent.py
---------------------------
Escalation agent: called when diagnosis confidence is too low after all attempts.

Responsibility:
    - build a structured escalation report (loops, tokens, best hypothesis)
    - notify the on-call team via Slack
    - mark the incident as ESCALATED in state

Writes to state:
    resolution_status, escalation_message, resolved_at
"""

import datetime

from src.graph.state import AgentState, ResolutionStatus
from src.tools.sre_tool import notify_slack


# Node 8: escalate

def escalate_node(state: AgentState) -> dict:
    """
    call this when we've tried everything and still don't know what's wrong.
    sends a slack message to wake up the on-call person.
    """
    print(f"\n[escalate] Escalating {state['incident_id']}, evaluating escalation reason....")

    incident_id = state['incident_id']
    severity = state.get('severity', 'unknown')
    alert_name = state.get('alert_name', 'unknown')

    #determine the reason for escalation
    if state.get('diagnosis_mode') == "llm" and state.get("diagnosis_loops", 0):
        reason = "DIAGNOSIS_EXHAUSTED"
        hypotheses = state.get("hypotheses", [])
        best_guess = hypotheses[0].get("hypothesis", "Unknown") if hypotheses else "Unknown"
        max_conf = max((h.get("confidence", 0) for h in hypotheses), default=0)
        
        message = (
            f" *AI diagnosis failed*\n"
            f"Incident: '{incident_id}' | Severity: {severity}\n"
            f"Alert: '{alert_name}'\n"
            f"Best Guess: {best_guess} (Conf: {max_conf:.0%})\n"
            f"Action Required: Manual root cause analysis needed"
        )
    
    elif state.get("verified") == False and state.get("action_taken"):
        reason = "REMEDIATION_FAILED"
        action_taken = state.get("action_taken")
        current_p99 = state.get("current_metrics", {}).get("p99_latency_s", "N/A")

        message = (
            f" *AI Remediation Failed*\n"
            f"Incident: {incident_id} | Severity: {severity}"
            f"Alert: {alert_name}\n"
            f"Action Attempted: {action_taken}\n"
            f"Current P99: {current_p99}s (still breaching)\n"
            f"Action Required: Manual fix or revert required"
        )
    
    else:
        reason = "UNKNOWN"
        message = f"Incident '{incident_id}' required manual investigation"

    print(f"\n[escalate] {reason}: {incident_id}")
    print(message)

    notify_slack(
        message=message,
        channel="incidents",
        severity="critical",
        incident_id=incident_id
    )

    return {
        "resolution_status": ResolutionStatus.ESCALATED.value,
        "escalation_message": message,
        "resolved_at": datetime.datetime.utcnow().isoformat(),
    }
