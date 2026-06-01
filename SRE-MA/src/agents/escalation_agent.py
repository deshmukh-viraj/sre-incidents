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
    Called when diagnosis confidence remains below DIAGNOSIS_CONFIDENCE after
    all deterministic + LLM attempts have been exhausted.
    Creates an escalation record and notifies on-call via Slack.
    """
    print(f"\n[escalate] Escalating {state['incident_id']} — diagnosis confidence too low")

    hypotheses = state.get("hypotheses", [])
    best_guess = hypotheses[0].get("hypothesis", "Unknown") if hypotheses else "Unknown"
    max_conf = max((h.get("confidence", 0) for h in hypotheses), default=0)
    loops= state.get("diagnosis_loops", 0)
    tokens_used = state.get("total_tokens_used", 0)

    escalation = (
        f"[ESCALATION] Incident {state['incident_id']} — {state.get('severity', 'unknown')} severity\n"
        f"Alert: {state.get('alert_name', 'unknown')}\n"
        f"Best hypothesis: {best_guess} (confidence={max_conf:.2f})\n"
        f"Diagnosis loops exhausted: {loops}\n"
        f"Tokens consumed: {tokens_used}\n"
        f"Manual investigation required."
    )

    print(escalation)

    notify_slack(
        message=escalation,
        channel="incidents",
        severity="critical",
        incident_id=state["incident_id"],
    )

    return {
        "resolution_status": ResolutionStatus.ESCALATED.value,
        "escalation_message": escalation,
        "resolved_at": datetime.datetime.utcnow().isoformat(),
    }
