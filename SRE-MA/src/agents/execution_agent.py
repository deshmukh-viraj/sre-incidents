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
from src.tools.sre_tool import execute_remediation
from src.tools.kg_tool import append_past_incident


# Node 6: Human Gate 

def human_gate_node(state: AgentState) -> dict:
    """
    saves the pending action plan and waits for human approval.
    """
    print(f"\n[human_gate] Waiting for approval on {state['incident_id']}")
    print("[human_gate] Actions requiring approval:")

    for a in state.get("action_plan", []):
        if a.get("requires_approval"):
            print(f"  pending: {a['action'][:60]}")

    timeout = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "30"))

    if os.getenv("SIM_AUTO_APPROVE", "false").lower() == "true":
        print(f"[human_gate] SIM_AUTO_APPROVE=true -> auto-approving in {min(timeout, 5)}s")
        time.sleep(min(timeout, 5))
        return {"human_approved": True, "approval_timeout": False}

    return {"human_approved": False, "approval_timeout": False}


# bode 7: Execute

def execute_node(state: AgentState) -> dict:
    """
    executes the action plan.
    in simulation mode: calls execute_remediation (which may be a dry-run stub).
    in production: calls actual kubectl / API tools.
    appends the incident outcome to the Neo4j knowledge graph for future reference.
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
            approved_by=approved_by,
        )
        if not result.get("success"):
            all_success = False
        updated_plan.append({
            **action,
            "executed": True,
            "result": result.get("result") or result.get("error"),
        })

    # record outcome in knowledge graph for future incident correlation
    append_past_incident(
        incident_id = state["incident_id"],
        alert_name = state.get("alert_name", "unknown"),
        root_cause = state.get("root_cause") or "unknown",
        action_taken = "; ".join(a["action"] for a in action_plan),
        mttr_seconds = mttr,
        success = all_success,
    )

    print(f"[execute] Done: MTTR={mttr}s  success={all_success}")
    return {
        "action_plan": updated_plan,
        "resolution_status": ResolutionStatus.RESOLVED.value,
        "resolved_at": datetime.datetime.utcnow().isoformat(),
        "mttr_seconds":  mttr,
        "resolution_notes":  f"{len(action_plan)} action(s) executed. MTTR: {mttr}s",
    }
