"""
agents/critic_agent.py
--------------------------

performs deterministic reflection between the DeepAgent harness and planner.
Runs fully code-based validation checks over the agent state without making any
llm calls, ensuring fast execution and reproducible evaluation runs
"""

from src.graph.state import AgentState
from src.graph.routing import requires_human_approval, classify_blast_radius
from src.graph.policy import ALLOWED_WRITE_TOOLS
from src.tools.kg_tool import get_dependencies

KNOWN_RB = {"RB-001", "RB-002", "RB-003", "RB-004", "RN-005", "RB-006", "KG-Memory"}

def critic_node(state: AgentState) -> dict:
    raw = state.get("raw_signals", {})
    prefixes = {str(k).split("_")[0] for k in raw.keys()} | set((raw.get("log_patterns") or {}).keys())
    issues, concerns = [], []

    # faithfullness pass over whatever hypotheses exist
    validated = []
    for h in state.get("hypotheses", []):
        conf = min(max(float(h.get("confidence", 0)), 0.0), 1.0)
        rb = h.get("supporting_runbook")
        if rb and rb not in KNOWN_RB:
            issues.append(f"unknown runbook ref: {rb}"); rb = None
        ev = [e for e in h.get("evidence", []) if e]
        if ev and not any (p in str(e).lower() for e in ev for p in prefixes):
            issues.append("evidence not grounded in collected singals")
            conf = max(conf - 0.15, 0.0)
        validated.append({**h, "confidence": conf, "supporting_runbook": rb})

    severity = state.get("severity", "SEV3")
    service = (state.get("affected_services") or ["unknown"])[0]
    tgt = state.get("proposed_target") or service
    tool = state.get("proposed_tool")
    act = state.get("llm_suggested_action")
    reject_count = state.get("critic_reject_count", 0)

    # validate proposal grounding & presence
    is_rejected = False
    if severity in ("SEV1", "SEV2") and (not tool or not act):
        concerns.append(f"empty proposal on {severity}")
        is_rejected = True
    elif tool and (tool not in ALLOWED_WRITE_TOOLS or (tgt != service and tgt not in (get_dependencies(service) or []))):
        concerns.append(f"ungrounded proposal: tool={tool}, target={tgt}")
        is_rejected = True

    if is_rejected:
        reject_count += 1
        escalate_reason = "critic_rejected_twice" if reject_count >= 2 else None
        return {
            "critic_reject_count": reject_count,
            "requires_retry": reject_count < 2,
            "escalate_reason": escalate_reason,
            "action_plan": [],
            "critique": "; ".join(concerns),
            "diagnosis_issues": issues,
            "requires_approval": True,
        }

    # grounded proposal: recompute blast and materialize one ActionItem (remediator early-exits)
    action_plan = []
    if tool and act:
        blast = classify_blast_radius({"tool": tool, "params": {"service": tgt}})
        action = {"action": act, "tool": tool, "params": {"service": tgt}, "blast_radius": blast,
                  "reversible": True, "requires_approval": False, "executed": False, "result": None}
        action["requires_approval"] = requires_human_approval(action)
        action_plan = [action]

    return {
        "hypotheses": validated or state.get("hypotheses", []),
        "action_plan": action_plan,
        "requires_approval": any(a.get("requires_approval") for a in action_plan) or bool(issues),
        "diagnosis_issues": issues,
        "critique": "; ".join(concerns) or "no concerns",
        "root_cause": validated[0]["hypothesis"] if validated else state.get("root_cause"),
        "diagnosis_summary": state.get("diagnosis_summary") or (validated[0]["hypothesis"] if validated else None),
    }