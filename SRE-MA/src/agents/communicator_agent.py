"""
agents/communicator_agent.py
------------
communicator agent: generates three stakeholder-appropriate messages in parallel
with the diagnoser — does NOT wait for root cause.

outputs:
    status_page_update —     customer-facing, no internal details
    war_room_summary — internal slack, full technical context
    escalation_message — pagerduty escalation (SEV1/SEV2 only)

writes to state:
    status_page_update, war_room_summary, escalation_message,
    total_token_used, total_cost_used
"""

import datetime
import json
import os
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from src.graph.state import AgentState
from src.agents.utils import _get_llm, calculate_cost, structured_invoke
from src.agents.schemas import CommunicatorOutput

load_dotenv()


def _comms_enabled() -> bool:
    """ flag (default 0; MULTI_AGENT=1 enables all upgrades)."""
    return os.getenv("MULTI_AGENT", "0") == "1" or os.getenv("COMMS_ENABLED", "0") == "1"


def _facts_block(state: AgentState) -> str:
    """ authoritative execution facts (receipts + handoff decision) for the
    agent-authored final report. Empty string when flag off or nothing to show."""
    if not _comms_enabled():
        return ""
    parts = []
    for r in state.get("receipts") or []:
        outcome = "OK" if r.get("success") else "FAILED"
        dry = " (dry-run)" if r.get("dry_run") else ""
        parts.append(f"- {r.get('action')} via {r.get('tool')}: {outcome}{dry}")
    pkg = state.get("handoff_to_human") or {}
    if pkg.get("presented"):
        parts.append(f"- Human handoff: cause={pkg.get('cause')}, decision={pkg.get('decision')}")
    return "\n".join(parts)


#node: communicator

def communicator_node(state: AgentState) -> dict:
    """
    write updates for the status page, slack, and pagerduty all at once.
    runs parallel to the diagnoser so we don't waste time waiting.
    """
    print(f"\n[communicator] Generating communications for {state['incident_id']}")

    severity = state.get("severity", "SEV3")
    alert = state.get("alert_name", "unknown_alert")
    services = ", ".join(state.get("affected_services", ["unknown"]))
    raw_summary = state.get("incident_summary")
    summary = raw_summary or state.get("diagnosis_summary") or f"{severity} incident on {services} ({alert})"
    root = state.get("root_cause", "Root cause still being determined")
    status = state.get("resolution_status", "investigating")

    #format incident start time accurately (not current time)
    alert_started_raw = state.get("alert_started_at")
    if alert_started_raw:
        try:
            dt_started = datetime.datetime.fromisoformat(alert_started_raw.replace("Z", "+00:00"))
            started_str = dt_started.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            started_str = str(alert_started_raw)
    else:
        started_str = "unknown"
    current_time_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    #observed metrics grounding to prevent hallucinated recovery claims
    p99_val = state.get("final_p99_latency_s") or state.get("raw_signals", {}).get("p99_latency_s")
    err_val = state.get("error_rate") or state.get("raw_signals", {}).get("error_rate")
    metrics_parts = []
    if p99_val is not None:
        metrics_parts.append(f"p99={p99_val:.3f}s")
    if err_val is not None:
        metrics_parts.append(f"error_rate={err_val:.4f} ({err_val*100:.1f}%)")
    metrics_str = ", ".join(metrics_parts) if metrics_parts else "metrics pending"

    is_resolved = str(status).lower() in ("resolved", "action_failed")

    #agent-authored final report grounded in receipts/handoff facts
    facts = _facts_block(state)
    facts_section = f"""
- Execution facts (authoritative, from the agent ledger):\n{facts}
""" if facts else ""
    facts_guideline = ("- Execution facts: when present, the war_room_summary MUST include "
                       "them verbatim in a 'Final report' section. Do not invent or contradict them.\n") if facts else ""

    prompt = f"""Generate 3 incident communications. Return ONLY JSON.

Incident details:
- ID: {state['incident_id']}
- Incident Started At: {started_str}
- Current Time: {current_time_str}
- Alert: {alert}
- Severity: {severity}
- Affected services: {services}
- Summary: {summary}
- Root Cause: {root}
- Current Status: {status}
- Current Observed Metrics: {metrics_str}{facts_section}

Return this exact JSON structure:
{{
  "status_page_update": "2-3 sentence customer-facing update. No internal details, no jargon. Say what users experience and when to expect resolution.",
  "war_room_summary": "Technical Slack message for engineers. Include: severity, alert name, affected services, root cause, current metrics, what to watch.",
  "escalation_message": "PagerDuty escalation for active SEV1/SEV2 incidents needing on-call responder attention. Set to null if SEV3 or lower, OR if incident is already resolved/closed (never page or escalate on-call for resolved incidents)."
}}

Guidelines:
- Start time: Always use '{started_str}' when citing when the incident began. Do not use '{current_time_str}' as the start time.
- Factual claims: Ground all recovery statements in the Current Observed Metrics. Do NOT overclaim recovery (e.g. do not say 'recovered to baseline' or 'throttling subsided' if metrics are still elevated above zero/baseline). Report observed numbers factually.
- Escalation: If Current Status is '{status}' and resolved, 'escalation_message' MUST be null.
{facts_guideline}"""

    try:
        result = _get_llm(temperature=0.1, simple=True).with_structured_output(
            CommunicatorOutput, include_raw=True).invoke([HumanMessage(content=prompt)])
        resp, raw_msg = result["parsed"], result["raw"]

        usage = raw_msg.usage_metadata or raw_msg.response_metadata.get("token_usage", {})
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        cost = calculate_cost(input_tokens, output_tokens)

        #never send escalation messages for resolved incidents or SEV3
        escalation_msg = None if is_resolved else (resp.escalation_message if severity in ("SEV1", "SEV2") else None)

        return {
            "status_page_update": resp.status_page_update,
            "war_room_summary": resp.war_room_summary,
            "escalation_message": escalation_msg,
            "total_tokens_used": state.get("total_tokens_used", 0) + input_tokens + output_tokens,
            "token_cost_usd": state.get("token_cost_usd", 0.0) + cost,
        }

    except Exception as e:
        print(f"[communicator] ERROR: {e}")
        fallback = f"[{severity}] {alert} — {summary}"
        if facts:
            fallback += f"\nFinal report:\n{facts}"
        return {
            "status_page_update": f"We are investigating an issue affecting {services}. Updates to follow.",
            "war_room_summary": fallback,
            "escalation_message": None if is_resolved else state.get("escalation_message"),
            "errors":  state.get("errors", []) + [f"communicator failed: {str(e)}"],
        }
