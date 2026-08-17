"""
agents/detector_agent.py
------------------------
detector agent: first node in the LangGraph pipeline.

responsibility:
    - collect prometheus metrics + loki log patterns via collect_all_signals
    - infer affected service from alert name if not explicitly set
    - classify severity from numeric signals only (no LLM)

writes to state:
    raw_signals, severity, affected_services, resolution_status
"""

from src.graph.state import AgentState, ResolutionStatus
from src.graph.routing import classify_severity
from src.tools.sre_tool import collect_all_signals
from src.graph.routing import infer_service

#node 1: detector

def detector_node(state: AgentState) -> dict:
    """
    grab the metrics and figure out how bad it is.
    add prometheus and loki stuff to raw_signals.
    no ai used here.
    """
    print(f"\n[detector] Starting detection for incident {state['incident_id']}")

    raw = state.get("raw_signals", {})
    service = raw.get("service") or infer_service(raw.get("alert_name", ""))

    # collect Prometheus metrics and Loki log patterns
    signals = collect_all_signals(service)
    for key, value in signals.items():
        if key not in raw or raw[key] is None:
            raw[key] = value

    # guess severity just from the numbers
    severity = classify_severity({**state, "raw_signals": raw})

    print(f"[detector] Severity: {severity} | service: {service}")
    print(f"[detector] p99={raw.get('p99_latency_s')} error_rate={raw.get('error_rate')}")

    is_shadow = bool(state.get("shadow_execution", False) or raw.get("shadow_execution", False))
    raw["shadow_execution"] = is_shadow

    return {
        "raw_signals": raw,
        "severity": severity,
        "affected_services": [service],
        "resolution_status": ResolutionStatus.INVESTIGATING.value,
        "shadow_execution": is_shadow,
    }
