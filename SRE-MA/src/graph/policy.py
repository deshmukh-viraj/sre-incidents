"""
graph/policy.py
--------------

policy-as-code gate between planning and execution.
the agent proposes, this module disposes.

"""

import json
from typing import Literal
from src.graph.routing import DELTA_EFFECT_SECONDS, DIAGNOSIS_CONFIDENCE
from src.tools.backends.registry import service_tier

ALLOWED_WRITE_TOOLS = set(DELTA_EFFECT_SECONDS.keys())

ALLOWED_WRITE_TOOLS |= {"escalate", "escalate_with_reason"}
APPROVED_KUBECTL_ACTIONS = frozenset({"kubectl_patch_deployment", "kubectl_restart_deployment", "kubectl_scale"})
ALLOWED_WRITE_TOOLS |= set(APPROVED_KUBECTL_ACTIONS)
CONFIDENCE_FLOOR_FOR_AUTO = 0.50 # below this, even pod-blast need human

#spec: autonomous execution only for reversible, pod/service-level actions,
#capped at 2 actions per incident. cluster-level or not-known-reversible
#actions route to the human gate; plan length is capped with recorded rejections.
MAX_ACTIONS_PER_INCIDENT = 2

# goal act-rule: only stateless_app targets are auto-eligible; stateful_data and
# monitoring always need a human, and a target missing from the environment's
# service_catalog defaults to the human gate. informational ops are not gated.
AUTO_ELIGIBLE_TIERS = {"stateless_app"}
SERVICE_MUTATING_TOOLS = {
    "set_feature_flag", "rolling_restart", "rollback_deployment",
    "execute_kg_suggestion", "kubectl_patch_deployment",
    "kubectl_restart_deployment", "kubectl_scale",
}
MIN_CITATIONS_FOR_AUTO = 2


def _citations_for_target(state, target) -> list:
    """distinct diagnosis-evidence lines implicating the action target.
    the autonomy rule needs >=2 tool-result citations for the target; raw-signal
    breach lines count when the target IS the alerted service (its own signals)."""
    hyps = state.get("hypotheses") or []
    if not hyps or not target:
        return []
    best = max(hyps, key=lambda h: h.get("confidence", 0))
    primary = (state.get("affected_services") or [""])[0]
    out = set()
    for e in best.get("evidence") or []:
        e = str(e)
        if not e:
            continue
        if target in e or (target == primary and ("threshold" in e or "->" in e)):
            out.add(e)
    return sorted(out)


REVERSIBLE_WRITE_TOOLS = {
    "set_feature_flag",
    "rolling_restart",
    "capture_diagnostics",
    "notify",
    "execute_kg_suggestion",
    "kubectl_patch_deployment",
    "kubectl_restart_deployment",
    "kubectl_scale",
}  # rollback_deployment deliberately absent: reverting is itself the mitigation

def idem_key(incident_id: str, action: dict) -> str:
    return f"{incident_id}: {action.get('tool')}:" \
            f"{json.dumps(action.get('params', {}), sort_keys=True)}"

def policy_validator_node(state) -> dict:
    rejections, validated = [], []
    executed = set(state.get("executed_action_keys", []))
    max_conf = max((h.get("confidence", 0) for h in state.get("hypotheses", [])), default=0.0)
    requires_approval = state.get("requires_approval", False)

    # verifier verdict gate: a diagnosis the adversarial verifier could NOT uphold
    # (unverifiable/ungrounded, or contradicted after the reroute cap) never
    # auto-executes — it goes to the human gate with the plan prepared.
    verifier_res = state.get("verifier_result") or {}
    verifier_blocked = bool(verifier_res) and verifier_res.get("verdict") not in (None, "upheld")
    if verifier_blocked:
        print(f"[policy] verifier verdict={verifier_res.get('verdict')} "
              f"grounded={verifier_res.get('grounded')} -> human gate")

    for a in state.get("action_plan", []):
        key = idem_key(state["incident_id"], a)
        if a.get("tool") not in ALLOWED_WRITE_TOOLS:
            rejections.append({"action": a.get("action"), "reason": f"tool '{a.get('tool')}' not in allowed list"})
            continue
        if key in executed:
            rejections.append({"action": a.get("action"), "reason": "idempotency check: already executed"})
            continue
        if str(a.get("blast_radius", "")) == "cluster" or a.get("tool") not in REVERSIBLE_WRITE_TOOLS:
            # cluster-level or not-known-reversible: never auto-execute, human decides
            a = {**a, "requires_approval": True}
        if max_conf < CONFIDENCE_FLOOR_FOR_AUTO:
            a = {**a, "requires_approval": True}

        # act-rule gates for service-mutating actions (checked in order, all reasons logged)
        if a.get("tool") in SERVICE_MUTATING_TOOLS:
            target = (a.get("params") or {}).get("service") or ""
            tier = service_tier(target)
            if tier not in AUTO_ELIGIBLE_TIERS:
                print(f"[policy] tier('{target}')={tier or 'not in catalog'} -> human gate")
                a = {**a, "requires_approval": True}
            elif state.get("diagnosis_mode") == "deep_agents":
                # LLM-sourced diagnoses must cite >=2 tool results implicating the target
                n_cites = len(_citations_for_target(state, target))
                if n_cites < MIN_CITATIONS_FOR_AUTO:
                    print(f"[policy] only {n_cites} citation(s) implicate '{target}' "
                          f"(need {MIN_CITATIONS_FOR_AUTO}) -> human gate")
                    a = {**a, "requires_approval": True}
        if verifier_blocked:
            a = {**a, "requires_approval": True}
        validated.append(a)

    if len(validated) > MAX_ACTIONS_PER_INCIDENT:
        for extra in validated[MAX_ACTIONS_PER_INCIDENT:]:
            rejections.append({"action": extra.get("action"),
                               "reason": f"max {MAX_ACTIONS_PER_INCIDENT} actions per incident"})
        validated = validated[:MAX_ACTIONS_PER_INCIDENT]
    
    if validated and any(a.get("requires_approval") for a in validated):
       requires_approval = True
    return {"action_plan": validated, "requires_approval": requires_approval, 
            "policy_rejections": rejections, "plan_viable": bool(validated)}
    

def route_after_policy(state) -> Literal["human_gate", "execute", "escalate"]:
    #budget-exhausted mid-remediation -> escalate (handoff)
    if state.get("budget_exhausted"):
        return "escalate"
    if not state.get("plan_viable", True):
        return "escalate"
    return "human_gate" if state.get("requires_approval") else "execute"


def route_after_critic(state) -> Literal["repair_proposal", "remediator", "deep_investigation", "escalate"]:
    """the critic-rejection ladder instead of 'loop-or-escalate':
    reject -> cheap one-call repair -> deterministic runbook plan when the best
    hypothesis confidence is high -> full re-investigation -> escalate. The
    agent's own escalation request or the hard reject ceiling (escalate_reason
    set by the critic) always escalates first, and a double rejection must never
    send an empty plan to the remediator (INC-SLOBur-FF52)."""
    # P9: budget middleware fired -> single escalation path (B7 handoff)
    if state.get("budget_exhausted"):
        return "escalate"
    if state.get("escalate_reason"):
        return "escalate"
    if not state.get("requires_retry"):
        return "remediator"  # accepted proposal -> normal path (unchanged)
    critique = state.get("critique") or ""
    if "empty proposal" in critique or "ungrounded" in critique:
        if not state.get("repair_attempts"):
            return "repair_proposal"  # one LLM call, no tool rounds
        conf = max((h.get("confidence", 0) for h in (state.get("hypotheses") or [])), default=0)
        if conf >= DIAGNOSIS_CONFIDENCE:
            return "remediator"  # deterministic plan from an already-high-confidence diagnosis
        return "deep_investigation"  # full re-investigation within the usual loop budget
    return "remediator"
    