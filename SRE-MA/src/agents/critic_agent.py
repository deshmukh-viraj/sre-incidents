"""
agents/critic_agent.py
--------------------------

performs deterministic reflection between the DeepAgent harness and planner.
Runs fully code-based validation checks over the agent state without making any
llm calls, ensuring fast execution and reproducible evaluation runs
"""

from src.graph.state import AgentState
from src.graph.routing import requires_human_approval, classify_blast_radius
from src.graph.policy import ALLOWED_WRITE_TOOLS, REVERSIBLE_WRITE_TOOLS
from src.tools.kg_tool import get_dependencies

import json
import os

def _known_runbook_ids() -> set:
    """runbook ids this deployment actually ships (runbooks.json), plus the
    KG-memory marker — new runbooks validate instead of being rejected."""
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "..", "runbooks", "runbooks.json")
        with open(path) as fh:
            return set(json.load(fh).keys())
    except Exception:
        return set()

KNOWN_RB = _known_runbook_ids() | {"KG-Memory"}

def _has_tool_results(state: AgentState) -> bool:
    """did the investigation actually query anything? scratchpad entries written
    by deep_investigation carry tool results with a 'tool: ' prefix."""
    return any(str(e).startswith("tool: ") for e in state.get("investigation_scratchpad", []))


def critic_node(state: AgentState) -> dict:
    raw = state.get("raw_signals", {})
    prefixes = {str(k).split("_")[0] for k in raw.keys()} | set((raw.get("log_patterns") or {}).keys())
    issues, concerns = [], []

    # faithfullness pass over whatever hypotheses exist
    hyps = state.get("hypotheses") or []
    validated = []
    for h in hyps:
        conf = min(max(float(h.get("confidence", 0)), 0.0), 1.0)
        rb = h.get("supporting_runbook")
        if rb and rb not in KNOWN_RB:
            issues.append(f"unknown runbook ref: {rb}"); rb = None
        ev = [e for e in h.get("evidence", []) if e]
        if ev and not [e for e in ev if any(p in str(e).lower() for p in prefixes)]:
            issues.append(
                f"evidence ungrounded: none of {len(ev)} citation(s) reference a collected signal key "
                f"(e.g. {str(ev[0])[:60]!r}); available keys start with: {sorted(prefixes)[:10]}")
            conf = max(conf - 0.15, 0.0)
        validated.append({**h, "confidence": conf, "supporting_runbook": rb})

    severity = state.get("severity", "SEV3")
    service = (state.get("affected_services") or ["unknown"])[0]
    tgt = state.get("proposed_target") or service
    tool = state.get("proposed_tool")
    act = state.get("llm_suggested_action")
    reject_count = state.get("critic_reject_count", 0)

    #validate proposal grounding & presence — name WHICH check failed and WHY
    is_rejected = False
    force_approval = False
    agent_escalated = state.get("escalate_reason") == "agent_escalated"
    if severity in ("SEV1", "SEV2") and (not tool or not act):
        if agent_escalated:
            #the specialist deliberately closed its investigation with an escalation
            #request re-running burns the full budget to return the same conclusion
            #a second full deep_investigation started 15:12:15 only
            # to get re-rejected at 15:13:10
            return {
                "critic_reject_count": reject_count,
                "requires_retry": False,
                "escalate_reason": "agent_escalated",
                "action_plan": [],
                "diagnosis_issues": issues,
                "requires_approval": True,
                "critique": (
                    f"empty proposal on {severity}: investigation concluded with a deliberate "
                    f"escalation request -> escalating with the recorded findings "
                    f"(hypotheses stored: {len(state.get('hypotheses') or [])})"),
            }
        missing = " and ".join(p for v, p in ((tool, "proposed_tool"), (act, "action text")) if not v)
        concerns.append(f"empty proposal on {severity}: missing {missing} "
                        f"(hypotheses stored: {len(hyps)})")
        is_rejected = True
    elif tool:
        if tool not in ALLOWED_WRITE_TOOLS:
            concerns.append(f"ungrounded proposal: tool '{tool}' not in allowed write tools "
                            f"{sorted(ALLOWED_WRITE_TOOLS)}")
            is_rejected = True
        elif tgt != service:
            deps = get_dependencies(service) or []
            if tgt in deps:
                pass  # target is a declared dependency — binding verified
            elif deps:
                # KG answered and the target is genuinely unrelated
                concerns.append(f"ungrounded proposal: target '{tgt}' is neither '{service}' "
                                f"nor in its dependencies ({deps})")
                is_rejected = True
            else:
                # dependency graph unavailable (KG down): target binding cannot
                # be verified either way — a human confirms instead of the
                # critic wrongly rejecting a possibly-correct target
                concerns.append(f"target-binding unverifiable (dependency graph unavailable): "
                                f"target '{tgt}' — routing to human approval")
                force_approval = True
        elif state.get("diagnosis_mode") == "deep_agents" and not _has_tool_results(state):
            #spec: autonomous execution requires mandatory tool-result citations.
            #a proposal with zero recorded tool results is not auto-executed —
            #a human reviews it instead (no rejection loop, no budget burn).
            concerns.append("uncited proposal: no tool-result evidence in the investigation "
                            "scratchpad — routing to human approval")
            force_approval = True

    if is_rejected:
        reject_count += 1
        # ladder: reject #1 -> cheap repair (router), #2 -> deterministic plan or
        # full re-investigation, #3 -> human. one string-matched rejection costs
        # a whole deep-investigation round trip — that bound moves to #3.
        escalate_reason = "critic_rejected_after_retry" if reject_count >= 3 else None
        critique = "; ".join(concerns)
        print(f"[critic] REJECT #{reject_count} ({severity}): {critique} | "
              f"diagnosis_issues={issues or 'none'}")
        return {
            "critic_reject_count": reject_count,
            "requires_retry": reject_count < 3,
            "escalate_reason": escalate_reason,
            "action_plan": [],
            "critique": critique,
            "diagnosis_issues": issues,
            "requires_approval": True,
        }

    print("[critic] grounding check passed")

    # grounded proposal: recompute blast and materialize one ActionItem (remediator early-exits)
    action_plan = []
    if tool and act:
        blast = classify_blast_radius({"tool": tool, "params": {"service": tgt}})
        # receipts of the terminal tools prove nothing — a proposal that only
        # "recorded" itself must not pass as a tool-result citation
        citations = [str(e)[6:160] for e in state.get("investigation_scratchpad", [])
                     if str(e).startswith("tool: ") and "proposal recorded" not in str(e)][-3:]
        params = {"service": tgt}
        if tool == "set_feature_flag" and state.get("proposed_flag"):
            params["flag"] = state["proposed_flag"]
            params["variant"] = state.get("proposed_variant") or "off"
        action = {"action": act, "tool": tool, "params": params, "blast_radius": blast,
                  "reversible": tool in REVERSIBLE_WRITE_TOOLS, "requires_approval": False,
                  "executed": False, "result": None}
        if citations:
            action["citations"] = citations
        action["requires_approval"] = requires_human_approval(action)
        action_plan = [action]

    return {
        "hypotheses": validated or state.get("hypotheses", []),
        "action_plan": action_plan,
        "requires_approval": any(a.get("requires_approval") for a in action_plan) or bool(issues) or force_approval,
        "diagnosis_issues": issues,
        "critique": "; ".join(concerns) or "no concerns",
        "root_cause": validated[0]["hypothesis"] if validated else state.get("root_cause"),
        "diagnosis_summary": state.get("diagnosis_summary") or (validated[0]["hypothesis"] if validated else None),
        "evidence_summary": state.get("evidence_summary") or ("\n".join(validated[0].get("evidence", [])) if validated else None),
    }