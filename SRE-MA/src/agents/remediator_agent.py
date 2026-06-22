"""
agents/remediator_agent.py
--------------------------
Remediator agent: builds and validates an action plan from the diagnosed root cause.

Resolution paths:
    A. Known runbook match  -> deterministic action list from _build_action_plan
    B. Novel alert + KG suggestion -> KG-based action (requires approval)
    C. No runbook, no KG   -> safe mitigation tier based on severity

Writes to state:
    action_plan, requires_approval
"""

from src.graph.state import AgentState
from src.graph.routing import classify_blast_radius, requires_human_approval


# Node: Remediator 

def remediator_node(state: AgentState) -> dict:
    """
    make the action plan.
    if runbook matches, use it. otherwise ask llm for help.
    """
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

    # Path C: safe fallback based on severity + signal pattern
    else:
        print("[remediator] Path C — safe mitigation")
        action_plan = _build_safe_mitigation(state)

    # safety layer: fill blast_radius / approval if not already set by Path A
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


# private helpers

def _build_action_plan(runbook_id: str, state: AgentState) -> list:
    """
    hardcoded plans for known runbooks.
    """
    service = state.get("affected_services", ["unknown"])[0]
    raw = state.get("raw_signals", {})

    plans = {
        "RB-001": [
            {
                "action": "Enable card_rails request queuing",
                "tool": "set_feature_flag",
                "params": {"flag": "card_rails_queue_enabled", "value": True, "service": "payment_gateway"},
                "blast_radius": "pod",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result": None,
            }
        ],
        "RB-002": [
            {
                "action": "Rolling restart of account_ledger to clear circuit breaker",
                "tool": "rolling_restart",
                "params": {"service": "account_ledger"},
                "blast_radius": "service",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result":  None,
            }
        ],
        "RB-003": [
            {
                "action": "Block attacking IP at api_gateway",
                "tool": "block_ip",
                "params": {"service": "api_gateway", "ip": raw.get("attacking_ip", "unknown")},
                "blast_radius": "pod",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result":  None,
            },
            {
                "action": "Revoke compromised API key",
                "tool": "revoke_api_key",
                "params": {"key_id": raw.get("compromised_key", "unknown")},
                "blast_radius": "service",
                "reversible": False,
                "requires_approval": False,
                "executed": False,
                "result": None,
            },
        ],
        "RB-004": [
            {
                "action": "Rolling restart of account_ledger pods to release leaked connections",
                "tool": "rolling_restart",
                "params": {"service": "account_ledger"},
                "blast_radius": "service",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result": None,
            }
        ],
        "RB-005": [
            {
                "action": "Export audit evidence logs for compliance team",
                "tool": "export_logs",
                "params": {"service": "all", "duration": "24h"},
                "blast_radius": "pod",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result": None,
            }
        ],
        "RB-006": [
            {
                "action": "Restart feature pipeline to force fresh data pull",
                "tool": "restart_service",
                "params": {"service": "feature_pipeline"},
                "blast_radius": "service",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result":  None,
            },
            {
                "action": "Enable fraud model fallback rule-based scorer",
                "tool": "set_feature_flag",
                "params": {"flag": "fraud_model_fallback_enabled", "value": True},
                "blast_radius": "pod",
                "reversible": True,
                "requires_approval": False,
                "executed": False,
                "result": None,
            },
        ],
    }

    return plans.get(runbook_id, [
        {
            "action": "Escalate to on-call engineer",
            "tool": "notify",
            "params": {"channel": "incidents", "severity": "critical"},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }
    ])


def _build_safe_mitigation(state: AgentState) -> list:
    """
    path C no runbook, no KG action.
    three tiers based on severity and signal pattern.
    """
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
