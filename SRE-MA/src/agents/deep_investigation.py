"""
agents/deep_investigation.py
--------------------------------

DeepAgent/react harness adapter for novel incidents investigation
Owns the thinking -> tool -> observe loop, we own governance

added - tool call caching to prevent duplicate queries
      - stagnation breaker to force conclusion after 2 max rounds
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

import re
from src.agents.utils import _get_llm
from src.tools.agent_tool import ALL_TOOLS, READ_TOOLS, propose_remediation, escalate_with_reason
from src.tools.kg_tool import SERVICE_METRICS
from src.graph.routing import (
    P99_LATENCY_WARNING, ERROR_RATE_WARNING, THROTTLE_RATE_HIGH,
    DB_POOL_WARNING, CIRCUIT_BREAKER_OPEN, DECLINE_RATE_HIGH,
    classify_blast_radius,
)
from src.agents.utils import calculate_cost

MAX_ROUNDS, MAX_TOKEN = 4, 20_000
STAGNATION_THRESHOLD = 2

INVESTIGATOR_PROMPT = (
    "YOU ARE AN EXPERT SRE INVESTIGATING AN INCIDENT WITH NO KNOWN RUNBOOK MATCH "
    "EACH TOOL CALL MUST TEST ONE HYPOTHESIS; NEVER INVENT METRICS VALUES "
    "RULE 1: start with query_topology(primary service) for dependencies, past incidents "
    "and exact PromQL. Then call list_metrics(service) before any prometheus query — "
    "use the exact queries it returns, never guess metric names or labels. "
    f"Within {MAX_ROUNDS} tool rounds call propose_remediation with the safest fix "
    "(blast_radius pod|service|cluster), or state 'escalate' if evidence is weak. "
    "TERMINATION IS MANDATORY: you MUST end by EITHER calling propose_remediation "
    "OR your final message being exactly 'escalate'. Never end with only a summary. "
    "IF YOU SEE A MESSAGE SAYING YOU ALREADY QUERIED SOMETHING, MOVE TO A DIFFERENT HYPOTHESIS IMMEDIATELY"
)

# breach checks reused by the calm-first policy below
_BREACH_CHECKS = [
    ("circuit_breaker_state", CIRCUIT_BREAKER_OPEN),
    ("db_pool_utilization", DB_POOL_WARNING),
    ("p99_latency_s", P99_LATENCY_WARNING),
    ("error_rate", ERROR_RATE_WARNING),
    ("throttle_error_rate", THROTTLE_RATE_HIGH),
    ("payment_decline_rate", DECLINE_RATE_HIGH),
]

def _breaches(state) -> list:
    """live signals breaching warning thresholds (missing signal = not breaching)"""
    s = state.get("raw_signals", {})
    out = []
    for key, thr in _BREACH_CHECKS:
        v = s.get(key)
        if v is not None and v >= thr:
            out.append(f"{key}={v:.3f} exceeds threshold {thr}")
    lp = s.get("log_patterns", {}) or {}
    if (lp.get("auth_failures") or 0) >= 50:
        out.append(f"auth_failures={lp.get('auth_failures')} (>=50)")
    if lp.get("bulk_data_export"):
        out.append("bulk_data_export=True")
    return out

_harness = None

def get_harness(use_readonly: bool = True):
    """create the react agent harness
    use_readonly: if true, only use read only tools (safe for investigation)
                  if false, use all tools
    """
    global _harness
    if _harness is None:
        _harness = create_react_agent(
            model=_get_llm(temperature=0.1),
            tools=READ_TOOLS if use_readonly else ALL_TOOLS,
            prompt=INVESTIGATOR_PROMPT
        )
    return _harness


def _cache_key(tool_name: str, args: dict) -> str:
    """generate a cache key for tool calls"""
    return f"{tool_name}: {hash(frozenset(args.items()))}"


def deep_investigation_node(state) -> dict:
    """
    deep investigation node for novel/low-confidence incidents.
    replaces llm_diagnoser on the pending_llm path
    returns structured proposal for critic/policy validation
    Intercepts duplicate tool calls and serves caches results and
    forces conclusion after STAGNATION_THRESHOLD stagnat rounds
    """
    print(f"\n[deep_investigation] Starting investigation for {state.get('incident_id')}")
    print(f"[deep_investigation] Alert: {state.get('alert_name')} | Severity: {state.get('severity')}")
    print(f"[deep_investigation] Services: {state.get('affected_services')}")

    tool_call_cache = {}

    raw = state.get("raw_signals", {})
    sig = "\n".join(
        f" {k}: {v}" for k, v in raw.items()
        if v is not None and not k.endswith("summaries") and k != "log_patterns") or "none"

    error_logs = raw.get("error_log_summaries", [])[:6]
    error_text = "\n".join(error_logs)

    #extract downstream services from error logs (hypothesis seeds)
    downstream_services = set()
    for log in error_logs:
        lower = log.lower()
        for svc in ["card_rails", "postgres", "postgres_primary", "fraud_model", "redis", "kafka", "auth_service", "notification_service"]:
            if svc in lower:
                downstream_services.add(svc)

    affected_services = state.get('affected_services') or ['unknown']
    primary_service = affected_services[0]
    all_services = [primary_service] + list(downstream_services)

    hypothesis_hint = ""
    if downstream_services:
        hypothesis_hint = (
            f"\n\nHYPOTHESIS SEEDS: Error logs mention these downstream services: {', '.join(sorted(downstream_services))}. "
            f"You MUST query these services specifically. Do not limit investigation to {primary_service} only."
        )

    seeds = "\n".join(SERVICE_METRICS.get(primary_service, [])[:6])
    seed_text = f"\nSEED METRICS (run these first):\n{seeds}" if seeds else ""

    ctx = (
        f"ALERT: {state.get('alert_name')} | SEVERITY: {state.get('severity')}\n"
        f"SERVICES: {', '.join(all_services)} (primary: {primary_service})\n"
        f"METRICS: \n{sig}\nERROR LOGS:\n{error_text}{hypothesis_hint}{seed_text}\nInvestigate now"
    )

    try:
        result = get_harness().invoke(
            {"messages": [HumanMessage(content=ctx)]},
            config={"recursion_limit": 2 * MAX_ROUNDS + 2}
        )
    except Exception as e:
        print(f"[deep_investigation] ERROR: {e}")
        return {
            "errors": state.get("errors", []) + [f"deep_investigation failed: {e}"],
            "diagnosis_mode": "pending_llm",
            "diagnosis_loops": state.get('diagnosis_loops', 0) + 1
        }

    msgs = result["messages"]

    #heck terminal call -> forced finalization -> deterministic fallback synthesizer
    terminal = next((tc for m in msgs for tc in (getattr(m, "tool_calls", None) or []) if tc['name'] in ("propose_remediation", "escalate_with_reason")), None)
    if terminal is None:
        try:
            fin_llm = _get_llm(temperature=0.0).bind_tools([propose_remediation, escalate_with_reason], tool_choice="required")
            fin_msg = fin_llm.invoke(msgs + [HumanMessage(content="Investigation limit reached. You MUST call propose_remediation or escalate_with_reason now.")])
            msgs.append(fin_msg)
            terminal = next((tc for tc in (getattr(fin_msg, "tool_calls", None) or []) if tc['name'] in ("propose_remediation", "escalate_with_reason")), None)
        except Exception as e:
            print(f"[deep_investigation] Forced finalization failed: {e}")

    if terminal is None:
        top_text = next((str(m.content) for m in msgs if "[Known Fixes" in str(m.content)), "") or "\n".join(state.get("investigation_scratchpad", []))
        fixes = re.findall(r'-\s*(.*?)\s*\(success_rate=(\d+)%', top_text)
        keywords = [w for w in re.findall(r'\w+', f"{state.get('alert_name', '')} {' '.join(_breaches(state))}".lower()) if len(w) > 3]
        relevant = [(fix, int(rate)/100.0) for fix, rate in fixes if any(k in fix.lower() for k in keywords)]
        if relevant:
            best_fix, rate = max(relevant, key=lambda x: x[1])
            tool = "rollback_deployment" if "rollback" in best_fix.lower() else ("set_feature_flag" if "flag" in best_fix.lower() else "restart_service")
            blast = classify_blast_radius({"tool": tool, "params": {"service": primary_service}})
            terminal = {"name": "propose_remediation", "args": {"action": best_fix, "tool": tool, "target_service": primary_service, "blast_radius": blast, "confidence": 0.70 if rate >= 0.90 else 0.55}}
        else:
            terminal = {"name": "escalate_with_reason", "args": {"reason": "no_relevant_known_fix"}}

    proposal = terminal["args"] if terminal and terminal["name"] == "propose_remediation" else None
    text_escalated = terminal is not None and terminal["name"] == "escalate_with_reason"
    if text_escalated:
        print(f"[deep_investigation] Agent concluded: escalate ({terminal['args'].get('reason')})")

    #cost accounting
    model_name = next((getattr(m, "response_metadata", {}).get("model_name") or getattr(m, "response_metadata", {}).get("model") for m in reversed(msgs) if getattr(m, "response_metadata", None)), None)
    input_tokens = sum((getattr(m, "usage_metadata", None) or {}).get("input_tokens", 0) for m in msgs)
    output_tokens = sum((getattr(m, "usage_metadata", None) or {}).get("output_tokens", 0) for m in msgs)
    cost = calculate_cost(input_tokens, output_tokens, model_name)

    # detect stagnation: all tool calls in this run were cache hits
    tool_call_in_response = [tc for m in msgs for tc in (getattr(m, "tool_calls", None) or [])]
    fresh_calls = []
    for tc in tool_call_in_response:
        key = _cache_key(tc['name'], tc.get('args', {}))
        if key in tool_call_cache:
            print(f"[deep_investigation] DUPLICATE TOOL CALL HAPPENED: {tc['name']}")
        else:
            tool_call_cache[key] = True
            fresh_calls.append(tc)

    stagnant = len(tool_call_in_response) > 0 and len(fresh_calls) == 0
    if stagnant:
        print(f"[deep_investigation] STAGNATION: all {len(tool_call_in_response)} tool calls were duplicates")

    base_return = {
        "investigation_scratchpad": [
            f"{m.type}: {str(m.content)[:200]}" for m in msgs
        ],
        "investigation_steps": sum(1 for m in msgs if m.type == "ai"),
        "diagnosis_mode": "deep_agents",
        "total_input_tokens": state.get("total_input_tokens", 0) + input_tokens,
        "total_output_tokens": state.get("total_output_tokens", 0) + output_tokens,
        "total_tokens_used": state.get("total_tokens_used", 0) + input_tokens + output_tokens,
        "token_cost_usd": state.get("token_cost_usd", 0.0) + cost,
        "model_used": model_name or state.get("model_used") or "deep-agent-harness",
    }

    # calm-first policy: novel incidents must not page humans by default.
    # - no live breach at all -> close as false positive (notify info, no human)
    # - breach found but no proposal + SEV2/SEV3 -> safe mitigation first; escalate
    #   only if verification then fails (execute -> escalate_execution)
    # - SEV1 with no proposal falls through to critic SEV1 fallback -> human (exceptional)
    # ponytail: false-positive closes get honest labels from the existing 5-gate
    # attribution (metrics already healthy -> lead<0 -> natural_calm); upgrade path
    # is an explicit alertmanager silence instead of in-memory close.
    if proposal is None and not text_escalated:
        service = primary_service
        found = _breaches(state)

        if not found:
            healthy_ev = [
                f"{key}={state.get('raw_signals', {}).get(key)} within threshold {thr}"
                for key, thr in _BREACH_CHECKS
                if state.get("raw_signals", {}).get(key) is not None
            ][:4]
            print(f"[deep_investigation] ALL CLEAR: no live breach — calming alert (false-positive close, no human paged)")
            return {**base_return,
                "investigation_complete": True,
                "hypotheses": [{
                    "hypothesis": f"No active anomaly on {service} — all signals within thresholds; {state.get('alert_name')} is likely transient or a false positive",
                    "evidence": healthy_ev or ["no breaching signal found during live investigation"],
                    "confidence": 0.85,
                    "alternative": "slow-burn issue below thresholds",
                    "supporting_runbook": None,
                }],
                "action_plan": [{
                    "action": f"Alert calmed: {state.get('alert_name')} investigated, no real anomaly — closed as false positive",
                    "tool": "notify",
                    "params": {"channel": "incidents", "severity": "info", "message": f"{state.get('alert_name')} on {service}: investigation found no breach; no human action needed"},
                    "blast_radius": "pod",
                    "reversible": True,
                    "requires_approval": False,
                    "executed": False,
                    "result": None,
                }],
            }

        if state.get("severity") in ("SEV2", "SEV3"):
            print(f"[deep_investigation] Anomaly confirmed, no proposal — safe mitigation before escalation: {found[0]}")
            return {**base_return,
                "investigation_complete": True,
                "hypotheses": [{
                    "hypothesis": f"Investigation confirmed anomaly on {service}: {found[0]}; root cause not identified — capturing diagnostics and monitoring",
                    "evidence": found,
                    "confidence": 0.60,
                    "alternative": "transient spike that will self-recover",
                    "supporting_runbook": None,
                }],
                "action_plan": [{
                    "action": f"Capture diagnostics on {service} and monitor — safe mitigation to calm {state.get('alert_name')}",
                    "tool": "capture_diagnostics",
                    "params": {"service": service},
                    "blast_radius": "pod",
                    "reversible": True,
                    "requires_approval": False,
                    "executed": False,
                    "result": None,
                }],
            }

    print(f"[deep_investigation] Completed. Proposal: {proposal.get('action') if proposal else ('escalate' if text_escalated else 'None')} | In: {input_tokens} Out: {output_tokens} Cost: ${cost:.6f}")

    return {**base_return,
        "investigation_complete": proposal is not None or text_escalated or stagnant,
        "llm_suggested_action": proposal and proposal.get("action"),
        "proposed_tool": proposal and proposal.get("tool"),
        "proposed_target": proposal and proposal.get("target_service"),
        "proposed_blast_radius": proposal and proposal.get("blast_radius"),
    }