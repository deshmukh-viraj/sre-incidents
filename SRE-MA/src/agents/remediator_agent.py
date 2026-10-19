"""
agents/remediator_agent.py
--------------------------
remediator agent: builds and validates an action plan from the diagnosed root cause.
"""

from typing import Optional
import json
import os
import re
from src.graph.state import AgentState
from src.graph.routing import classify_blast_radius, requires_human_approval, DIAGNOSIS_CONFIDENCE


#node: remediator 

def remediator_node(state: AgentState) -> dict:
    if state.get("action_plan"):
        return {}

    # critic retry: rebuild the plan from the proposal the harness actually made
    if state.get("requires_retry"):
        plan = _extract_from_scratchpad(state)
        if plan is None:
            plan = _llm_extract_proposal(state)
        if plan:
            print(f"\n[remediator] Retry: rebuilt plan from investigation proposal ({plan[0]['tool']})")
            return {
                "action_plan": plan,
                "requires_approval": any(a.get("requires_approval") for a in plan),
                "requires_retry": False,
            }
        
    print(f"\n[remediator] Building action plan for {state['incident_id']}")

    consensus_rb = _check_correlated_rb_consensus(state)
    if state.get("correlated_alerts"):
        if consensus_rb:
            print(f"[remediator] Correlated runbook consensus achieved: {consensus_rb}")
        else:
            print(f"[remediator] Correlated alerts disagree on runbook")

    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"action_plan": [], "requires_approval": False}

    best = max(hypotheses, key=lambda h:h.get("confidence", 0))
    runbook = consensus_rb or state.get("runbook_id") or best.get("supporting_runbook")
    llm_action = state.get("llm_suggested_action")

    #if confidence is below threshold and no LLM action was proposed, do not execute Path A
    if best.get("confidence", 0) < DIAGNOSIS_CONFIDENCE and not llm_action:
        print(f"[remediator] Low confidence ({best.get('confidence', 0):.2f} < {DIAGNOSIS_CONFIDENCE}) and no LLM proposal -> no auto runbook")
        return {"action_plan": [], "requires_approval": True}

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

    # SEV2 -> capture diagnostics + notify
    if severity == "SEV2":
        return [{
            "action": "Notify on-call and capture diagnostics",
            "tool": "notify",
            "params": {"channel": "incidents", "severity": "warning", "message": f"SEV2 incident on {service} requires investigation"},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
            "executed": False,
            "result": None,
        }, {
            "action": "Capture thread dump and heap diagnostics",
            "tool": "capture_diagnostics",
            "params": {"service": service},
            "blast_radius": "pod",
            "reversible": True,
            "requires_approval": False,
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


def _check_correlated_rb_consensus(state: AgentState) -> Optional[str]:
    """
    if this incident has correlated alerts,,and they all share the same
    runbook label, that a strong deterministric signal, one root cause, 
    one fix, no need for extra reasoning
    """
    incident_id = state["incident_id"]
    corr = state.get("correlated_alerts", [])
    if not corr:
        return state.get("runbook_id")
    
    rb_seen = {state.get("runbook_id")}
    for entry in corr:
        rb = entry.get("runbook_id")
        if rb:
            rb_seen.add(rb)
    
    if len(rb_seen) == 1:
        return rb_seen.pop()
    return None


#task 3.4: extraction helpers for the critic retry path

def _extract_from_scratchpad(state: AgentState) -> Optional[list]:
    """find the last propose_remediation call in the scratchpad and copy its args into an action plan."""
    entries = [str(e) for e in state.get("investigation_scratchpad", []) if "propose_remediation" in str(e)]
    if not entries:
        return None
    blob = entries[-1]
    args = dict(re.findall(r"(\w+)\s*=\s*['\"]([^'\"]+)['\"]", blob))
    tool = args.get("tool")
    if not tool:
        return None
    service = args.get("target_service") or (state.get("affected_services") or ["unknown"])[0]
    action = {
        "action": args.get("action", tool),
        "tool": tool,
        "params": {"service": service},
        "blast_radius": args.get("blast_radius") or classify_blast_radius({"tool": tool, "params": {"service": service}}),
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }
    action["requires_approval"] = requires_human_approval(action)
    return [action]


def _llm_extract_proposal(state: AgentState) -> Optional[list]:
    """no proposal in scratchpad -> one LLM call with scratchpad + critique to synthesize it."""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from src.agents.utils import _get_llm
        from src.agents.schemas import RemediationProposal
    except ImportError:
        return None

    scratchpad = "\n".join(str(e) for e in state.get("investigation_scratchpad", [])[-20:])
    critique = state.get("critique") or "no critique"
    try:
        llm = _get_llm(temperature=0.0, simple=True).with_structured_output(RemediationProposal)  # P1-5: extraction is a simple node
        # every LLM call must start with a system message (prompt-channel fix from trace INC-SLOBur-0F86)
        resp = llm.invoke([
            SystemMessage(content="Extract the remediation proposal from the investigation scratchpad. Return the tool call the investigator intended."),
            HumanMessage(content=f"CRITIC: {critique}\n\nSCRATCHPAD:\n{scratchpad}"),
        ])
    except Exception as e:
        print(f"[remediator] LLM proposal extraction failed: {e}")
        return None

    tool = getattr(resp, "tool", None)
    if not tool:
        return None
    service = getattr(resp, "target_service", None) or (state.get("affected_services") or ["unknown"])[0]
    action = {
        "action": getattr(resp, "action", None) or tool,
        "tool": tool,
        "params": {"service": service},
        "blast_radius": getattr(resp, "blast_radius", None) or classify_blast_radius({"tool": tool, "params": {"service": service}}),
        "reversible": True,
        "requires_approval": False,
        "executed": False,
        "result": None,
    }
    action["requires_approval"] = requires_human_approval(action)
    return [action]