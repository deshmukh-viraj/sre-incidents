"""
agents/critic_agent.py
--------------------------

performs deterministic reflection between the DeepAgent harness and planner.
Runs fully code-based validation checks over the agent state without making any
llm calls, ensuring fast execution and reproducible evaluation runs
"""

from src.graph.state import AgentState
from src.graph.routing import requires_human_approval
from src.graph.policy import ALLOWED_WRITE_TOOLS
from src.tools.kg_tool import get_dependencies

KNOWN_RB = {"RB-001", "RB-002", "RB-003", "RB-004", "RN-005", "RB-006", "KG-Memory"}

def critic_node(state: AgentState) -> dict:
    raw = state.get("raw_signals", {})
    prefixes = {str(k).split("_")[0] for k in raw.keys()} | set((raw.get("log_patterns") or {}).keys())
    issues, concerns, validated = [], [], []

    # faithfullness pass over whatever hyptheses exist
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

    # capped systhesis if the harness proposed but never emitted a hypotheses
    tool = state.get("proposed_tool")
    if not validated and state.get("llm_suggested_action"):
        validated = [{
            "hypothesis": state["llm_suggested_action"],
            "evidence": [s for s in state.get("investigation_scratchpad", []) if s.startswith(("ai:", "tool:"))][:4],
            "confidence": 0.60,     #capped: agentic, unvalidated
            "alternative": None,
            "supporting_runbook": None,
        }]

    # topology sanity: target must be the incident service or a dependency
    tgt = state.get("proposed_target")
    service = (state.get("affected_services") or ["unknown"])[0]
    if tgt and tgt != service and tgt not in (get_dependencies(service) or []):
        concerns.append(f"target {tgt} outside incident topology")
    
    #materialize the plan
    action_pla = state.get("action_plan", [])
    if not action_pla and tool:
        if tool not in ALLOWED_WRITE_TOOLS:
            concerns.append(f"proposed tool '{tool}' not in write allowlist")
        else:
            a = {"action": state.get("llm_suggested_action", "investigate further"),
                "tool": tool, 
                "params": {"service": tgt or service},
                "blast_radius": state.get("proposed_blast_radius", "service"),
                "reversible": True, 
                "requires_approval": False,
                "executed": False, 
                "result": None }
            a["requires_approval"] = requires_human_approval(a)
            action_pla = [a]
    
    return {
        "hypotheses": validated or state.get("hypotheses", []),
        "action_plan": action_pla,
        "requires_approval": state.get("requires_approval", False) or bool(concerns) or bool(issues),
        "dignosis_issues": issues,
        "critique": ",".join(concerns) or "no concerns",
        "root_cause": validated[0]["hypothesis"] if validated else state.get("root_cause"),
        "diagnosis_summary": state.get("diagnosis_summary") or (validated[0]["hypothesis"] if validated else None),
    }