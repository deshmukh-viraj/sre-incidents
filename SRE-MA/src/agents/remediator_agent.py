"""
agents/remediator_agent.py
--------------------------
remediator agent: builds and validates an action plan from the diagnosed root cause.
"""

import json
import os
from src.graph.state import AgentState
from src.graph.routing import classify_blast_radius, requires_human_approval


#node: remediator 

def remediator_node(state: AgentState) -> dict:
    print(f"\n[remediator] Building action plan for {state['incident_id']}")

    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"action_plan": [], "requires_approval": False}

    best = max(hypotheses, key=lambda h:h.get("confidence", 0))
    runbook = best.get("supporting_runbook") or state.get("runbook_id")
    llm_action = state.get("llm_suggested_action")

    #path A: known resolution
    action_plan = _build_action_plan(runbook, state)
    is_fallback = (not action_plan or (len(action_plan) == 1 and action_plan[0].get("tool")=="notify"))

    if not is_fallback:
        print(f"[remediator] Path A runbook {runbook}")

    #path B: novel alert -> safe mitifation
    elif llm_action:
        print(f"[remediator] Path B - KG action: {llm_action[:60]}")
        _destructive = ["delete", "drop", 'purge', 'revoke', 'terminate', 'wipe']
        is_reversible = not any(w in llm_action.lower() for w in _destructive)

        action_plan = [{
            "action": f"KG suggestion: {llm_action}",
            "tool": "execute_kg_suggestion",
            "params": {"command": llm_action, 
            "service": state.get("affected_services", ["unknown"])[0]}, 
            "blast_radius": "service",
            "reversible": is_reversible,
            "requires_approval": True,
            "executed": False,
            "result": None,
        }]

    # path C: safe fallback based on severity + signal pattern
    else:
        print("[remediator] Path C -> safe mitigation")
        action_plan = _build_safe_mitigation(state)

    #safety layer: fill blast_radius / approval if not already set by Path A
    needs_approval = False
    for action in action_plan:
        if not action.get("blast_radius"):
            action["blast_radius"] = classify_blast_radius(action)
        if action.get("requires_approval") is None:
            action["requires_approval"] = requires_human_approval(action)
        if action.get("requires_approval"):
            needs_approval = True

    for a in action_plan:
        print(f"  -> {a['action'][:60]}  blast={a['blast_radius']} approval={a['requires_approval']}")

    return {"action_plan": action_plan, "requires_approval": needs_approval}


def _build_action_plan(runbook_id: str, state: AgentState) -> list:
    """load action plan from json."""
    raw = state.get("raw_signals", {})
    plans_path = os.path.join(os.path.dirname(__file__), "..", "..", "runbooks", "runbooks.json")
    
    try:
        with open(plans_path, "r") as f:
            plans = json.load(f)
    except FileNotFoundError:
        print(f"[remediator] WARNING: runbooks.json not found at {plans_path} falling back to safe actions")
        plans = {}

    plan = plans.get(runbook_id)
    if plan:

        plan_str = json.dumps(plan)
        if "{attacking_ip}" in plan_str:
            plan_str = plan_str.replace("{attacking_ip}", raw.get("attacking_ip", "unknown"))
        if "{compromised_key}" in plan_str:
            plan_str = plan_str.replace("{compromised_key}", raw.get("compromised_key", "unknown"))
        return json.loads(plan_str)

    return [{
        "action": "Escalate to on-call engineer",
        "tool": "notify",
        "params": {"channel": "incidents", "severity": "critical"},
        "blast_radius": "pod",
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }]


def _build_safe_mitigation(state: AgentState) -> list:
    severity = state.get("severity", "SEV3")
    signals = state.get("raw_signals", {})
    service = state.get("affected_services", ["unknown"])[0]

    # SEV1 + high latency -> rate-limit to stop cascade
    if severity == "SEV1" and (signals.get("p99_latency_s") or 0) > 3.0:
        return [{
            "action": "Enable rate-limiting to prevent cascading failure",
            "tool": "set_feature_flag",
            "params": {"flag": "rate_limiting_enabled", "value": True, "service": service},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }]

    # SEV1 unknown -> rollback to last good deployment
    if severity == "SEV1":
        return [{
            "action": "Rollback to last good deployment",
            "tool": "rollback_deployment",
            "params": {"revision": "previous", "service": service},
            "blast_radius": "service",
            "reversible": True,
            "requires_approval": True,
            "executed": False,
            "result": None,
        }]

    # default -> capture diagnostics for human investigation
    return [{
        "action": "Capture thread dump and heap diagnostics",
        "tool": "capture_diagnostics",
        "params": {"service": service},
        "blast_radius": "pod",
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }]
