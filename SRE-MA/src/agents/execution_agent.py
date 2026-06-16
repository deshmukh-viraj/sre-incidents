"""
agents/execution_agent.py
--------------------------
Execution agents: human approval gate and action executor.

    human_gate_node  — Node 6: pauses workflow; waits for /approve call in production.
                       In simulation mode (SIM_AUTO_APPROVE=true) auto-approves after timeout.
    execute_node     — Node 7: runs each action in the plan, appends incident to KG.

Writes to state:
    human_approved, approval_timeout (human_gate)
    action_plan, resolution_status, resolved_at, mttr_seconds, resolution_notes (execute)
"""

import os
import datetime
import time

from src.graph.state import AgentState, ResolutionStatus
from src.tools.sre_tool import execute_remediation, notify_slack
from src.tools.kg_tool import append_past_incident


# Node 6: Human Gate 

def human_gate_node(state: AgentState) -> dict:
    """
    save the action plan and chill until human says yes.
    """
    incident_id = state['incident_id']
    print(f"\n[human_gate] Waiting for approval on {incident_id}")
    print("[human_gate] Actions requiring approval:")

    actions_text = ""
    for a in state.get("action_plan", []):
        if a.get("requires_approval"):
            print(f"  pending: {a['action'][:60]}")
            actions_text += f"- {a['action']}\n"

    # Send a detailed Slack message to ask for approval
    message = (
        f" *Approval Required for {incident_id}*\n"
        f"*Alert*: {state.get('alert_name', 'Unknown')}\n"
        f"*Diagnosis*: {state.get('diagnosis_summary') or 'Unknown'}\n"
        f"*Proposed Actions*:\n{actions_text}\n"
        f"Please approve via the API or dashboard."
    )
    notify_slack(message=message, channel="incidents", severity="warning", incident_id=incident_id)

    timeout = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "30"))

    if os.getenv("SIM_AUTO_APPROVE", "false").lower() == "true":
        print(f"[human_gate] SIM_AUTO_APPROVE=true -> auto-approving in {min(timeout, 5)}s")
        time.sleep(min(timeout, 5))
        return {"human_approved": True, "approval_timeout": False}

    return {
        "human_approved": False, 
        "approval_timeout": False,
        "resolution_status": ResolutionStatus.WAITING_FOR_APPROVAL.value
    }


def _verify_recovery(service: str, metric_name: str = "p99_latency_s",
                     threshold: float = 5.0, timeout: int = 15, polls: int = 3) -> bool:
    """
    poll prometheus a few times to make sure things are actually getting better.
    returns true if metric is good now.
    """
    from src.tools.prometheus_tool import collect_incident_signals

    interval = max(1, timeout // polls)
    for i in range(polls):
        time.sleep(interval)
        try:
            signals = collect_incident_signals(service)
            current = signals.get(metric_name)
            if current is not None and current < threshold:
                print(f"[execute] Verification poll {i+1}/{polls}: {metric_name}={current:.3f} < {threshold} ")
                return True
            print(f"[execute] Verification poll {i+1}/{polls}: {metric_name}={current:.3f} >= {threshold} ")
        except Exception as e:
            print(f"[execute] Verification poll {i+1}/{polls} failed: {e}")
    return False


# Node 7: Execute

def execute_node(state: AgentState) -> dict:
    """
    run the action plan here.
    records outcome in neo4j graph.
    also checks if the metric got better before marking as resolved, so we dont lie.
    """
    print(f"\n[execute] Executing action plan for {state['incident_id']}")

    action_plan = state.get("action_plan", [])
    start_ts = datetime.datetime.fromisoformat(state["started_at"])
    mttr = int((datetime.datetime.utcnow() - start_ts).total_seconds())
    approved_by = state.get("approved_by")
    all_success = True
    updated_plan = []

    for action in action_plan:
        result = execute_remediation(
            action=action["action"],
            tool=action["tool"],
            params=action["params"],
           
        )
        if not result.get("success"):
            all_success = False
        updated_plan.append({
            **action,
            "executed": True,
            "result": result.get("result") or result.get("error"),
        })

    # Post-execution verification: confirm the metric actually improved
    service = state.get("raw_signals", {}).get("service", "payment_gateway")
    verified = False
    if all_success:
        print(f"[execute] Verifying recovery for {service}...")
        verified = _verify_recovery(service)

    # record outcome in knowledge graph for future incident correlation
    append_past_incident(
        incident_id = state["incident_id"],
        alert_name = state.get("alert_name", "unknown"),
        root_cause = state.get("root_cause") or "unknown",
        action_taken = "; ".join(a["action"] for a in action_plan),
        mttr_seconds = mttr,
        success = all_success and verified,
    )

    if all_success and verified:
        status = ResolutionStatus.RESOLVED.value
        print(f"[execute] Done: MTTR={mttr}s  success=True  verified=True")
    elif all_success:
        status = ResolutionStatus.EXECUTED_UNVERIFIED.value
        print(f"[execute] Done: MTTR={mttr}s  success=True  verified=False — metric did not improve")
    else:
        status = ResolutionStatus.FAILED.value
        print(f"[execute] Done: MTTR={mttr}s  success=False")

    return {
        "action_plan": updated_plan,
        "resolution_status": status,
        "resolved_at": datetime.datetime.utcnow().isoformat(),
        "mttr_seconds":  mttr,
        "resolution_notes":  f"{len(action_plan)} action(s) executed. MTTR: {mttr}s. Verified: {verified}",
    }

