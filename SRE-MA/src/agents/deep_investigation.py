"""
agents/deep_investigation.py
--------------------------------

DeepAgent/react harness adapter for novel incidents investigation
Owns the thinking -> tool -> observe loop, we own governance
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from src.agents.utils import _get_llm
from src.tools.agent_tool import ALL_TOOLS

MAX_ROUNDS, MAX_TOKEN = 4, 20_000

INVESTIGATOR_PROMPT = (
    "You are an expert SRE investigating an incident with no known runbook match. "
    "Each tool call must test ONE hypothesis; never invent metrics values "
    f"Within {MAX_ROUNDS} tool rounds call propose_remediation with the safest fix "
    "(blast_radius pod|service|cluster), or state 'escalate' if evidence is weak. "
)

_harness = None

def get_harness():
    global _harness
    if _harness is None:
        _harness = create_react_agent(
            model=_get_llm(temperature=0.1),
            tools=ALL_TOOLS,
            prompt=INVESTIGATOR_PROMPT
        )
    return _harness


def deep_investigation_node(state) -> dict:
    """
    deep investigation node for novel/low-confidence incidents.
    replaces llm_diagnoser on the pending_llm path
    returns structured proposal for critic/policy validation
    """

    raw = state.get("raw_signals", {})
    sig = "\n".join(
        f" {k}: {v}" for k, v in raw.items()
        if v is not None and not k.endswith("summaries") and k != "log_patterns") or "none"
    
    ctx = (
        f"ALERT: {state.get('alert_name')} | SEVERITY: {state.get('severity')}\n"
        f"SERVICE: {(state.get('affected_services') or ['unknown'])[0]}\n"
        f"METRICS: \n{sig}\nERROR LOGS:\n" + "\n".join(raw.get("error_log_summaries", [])[:6]) + "\nInvestigate now"
    )

    try:
        result = get_harness().invoke(
            {"messages": [HumanMessage(content=ctx)]}, 
            config={"recursion_limit": 2 * MAX_ROUNDS + 2}
        )
    except Exception as e:
        return {
            "errors": state.get("errors", []) + [f"deep_investigations failed: {e}"],
            "diagnosis_mode": "pending_llm",
            "diagnosis_loops": state.get('diagnosis_loops', 0) + 1
        }

    msgs = result["messages"] 

    #extract proposal from tool calls
    proposal = next((tc['args'] for m in msgs for tc in (getattr(m, "tool_calls", None) or []) if tc['name'] == "propose_remediation"), None)

    #token counting
    tokns = sum((getattr(m, "usage_metadata", None) or {}).get("total_tokens", 0) for m in msgs) 

    return {
        "investigatioon_scratchpad": [
            f"{m.type}: {str(m.content)[:200]}" for m in msgs
        ],
        "investigation_steps": sum(1 for m in msgs if m.type == "ai"),
        "investigation_complete": proposal is not None,
        "llm_suggested_action": proposal and proposal.get("action"),
        "proposed_tool": proposal and proposal.get("tool"),
        "proposed_target": proposal and proposal.get("target_service"),
        "proposed_blast_radius": proposal and proposal.get("blast_radius"),
        "diagnosis_mode": "deep_agents",
        "total_tokens_used": state.get("total_tokens_used", 0) + tokns,
        "token_cost_usd": state.get("token_cost_usd", 0.0) + tokns * 0.002,
        "model_used": state.get("model_used") or "deep-agent-harness",
    }