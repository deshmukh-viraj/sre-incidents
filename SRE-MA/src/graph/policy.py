"""
graph/policy.py
--------------

policy-as-code gate between planning and execution. 
the agent proposes, this module disposes.

"""

import json
from typing import Literal
from src.graph.routing import DELTA_EFFECT_SECONDS

ALLOWED_WRITE_TOOLS = set(DELTA_EFFECT_SECONDS.keys())

ALLOWED_WRITE_TOOLS |= {"escalate", "escalate_with_reason"}
APPROVED_KUBECTL_ACTIONS = frozenset({"kubectl_patch_deployment", "kubectl_restart_deployment", "kubectl_scale"})
ALLOWED_WRITE_TOOLS |= set(APPROVED_KUBECTL_ACTIONS)
CONFIDENCE_FLOOR_FOR_AUTO = 0.50 # below this, even pod-blast need human

def idem_key(incident_id: str, action: dict) -> str:
    return f"{incident_id}: {action.get('tool')}:" \
            f"{json.dumps(action.get('params', {}), sort_keys=True)}"

def policy_validator_node(state) -> dict:
    rejections, validated = [], []
    executed = set(state.get("executed_action_keys", []))
    max_conf = max((h.get("confidence", 0) for h in state.get("hypotheses", [])), default=0.0)
    requires_approval = state.get("requires_approval", False)

    for a in state.get("action_plan", []):
        key = idem_key(state["incident_id"], a)
        if a.get("tool") not in ALLOWED_WRITE_TOOLS:
            rejections.append({"action": a.get("action"), "reason": f"tool '{a.get('tool')}' not in allowed list"})
            continue
        if key in executed:
            rejections.append({"action": a.get("action"), "reason": "idempotency check: already executed"})
            continue
        if max_conf < CONFIDENCE_FLOOR_FOR_AUTO:
            a = {**a, "requires_approval": True}
        validated.append(a)
    
    if validated and any(a.get("requires_approval") for a in validated):
       requires_approval = True
    return {"action_plan": validated, "requires_approval": requires_approval, 
            "policy_rejections": rejections, "plan_viable": bool(validated)}
    

def route_after_policy(state) -> Literal["human_gate", "execute", "escalate"]:
    if not state.get("plan_viable", True):
        return "escalate"
    return "human_gate" if state.get("requires_approval") else "execute"


def route_after_critic(state) -> Literal["remediator", "deep_investigation", "escalate"]:
    """P0-2: ungrounded/empty proposals go back to investigation with the critique; plan-quality fixes go to the remediator."""
    if not state.get("requires_retry"):
        return "remediator"  # accepted proposal -> normal path (unchanged)
    if state.get("critic_reject_count", 0) >= 2:
        return "escalate"  # retry limit: no infinite loops
    critique = state.get("critique") or ""
    if "empty proposal" in critique or "ungrounded" in critique:
        return "deep_investigation"
    return "remediator"
    