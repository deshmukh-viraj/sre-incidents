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
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from src.graph.state import AgentState
from src.agents.utils import _get_llm

load_dotenv()


#node: communicator

def communicator_node(state: AgentState) -> dict:
    """
    write updates for the status page, slack, and pagerduty all at once.
    runs parallel to the diagnoser so we don't waste time waiting.
    """
    print(f"\n[communicator] Generating communications for {state['incident_id']}")

    llm = _get_llm(temperature=0.1)
    severity = state.get("severity", "SEV3")
    alert = state.get("alert_name", "unknown_alert")
    services = ", ".join(state.get("affected_services", ["unknown"]))
    summary= state.get("incident_summary", "Incident under investigation")
    root = state.get("root_cause", "Root cause still being determined")
    status = state.get("resolution_status", "investigating")
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""Generate 3 incident communications. Return ONLY JSON.

Incident details:
- ID: {state['incident_id']}
- Time: {ts}
- Alert: {alert}
- Severity: {severity}
- Affected services: {services}
- Summary: {summary}
- Root Cause: {root}
- Status: {status}

Return this exact JSON structure:
{{
  "status_page_update": "2-3 sentence customer-facing update. No internal details, no jargon. Say what users experience and when to expect resolution.",
  "war_room_summary": "Technical Slack message for engineers. Include: severity, alert name, affected services, root cause, current actions, what to watch.",
  "escalation_message": "PagerDuty escalation for SEV1/SEV2. Include: incident ID, severity, impact, who is needed. Leave null if SEV3 or lower."
}}"""

    try:
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw  = resp.content.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw)

        usage = resp.response_metadata.get("token_usage", {})
        new_tokens = usage.get("total_tokens", 0)
        cost = new_tokens * 0.000015

        return {
            "status_page_update": parsed.get("status_page_update"),
            "war_room_summary": parsed.get("war_room_summary"),
            "escalation_message": parsed.get("escalation_message") if severity in ("SEV1", "SEV2") else None,
            "total_tokens_used": state.get("total_tokens_used", 0) + new_tokens,
            "token_cost_usd": state.get("token_cost_usd", 0.0) + cost,
        }

    except Exception as e:
        print(f"[communicator] ERROR: {e}")
        return {
            "status_page_update": f"We are investigating an issue affecting {services}. Updates to follow.",
            "war_room_summary": f"[{severity}] {alert} — {summary}",
            "escalation_message": None,
            "errors":  state.get("errors", []) + [f"communicator failed: {str(e)}"],
        }
