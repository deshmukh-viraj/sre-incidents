"""
agents/detector_agent.py
------------------------
detector agent: first node in the LangGraph pipeline.

grabs all the important metrics for a service iin one go.
return dict so the detector agent has an easy time.
these are "seed calls" deterministic baseline metric, fetched before any LLM reasoning
"""

from src.graph.state import AgentState, ResolutionStatus
from src.graph.routing import classify_severity
from src.tools.sre_tool import collect_all_signals
from src.graph.routing import infer_service
from src.tools.backends.registry import get_backend
from src.tools.changepoint import detect_changepoint
import datetime as dt
import time

# Bet 2: deterministic onset detection over the last CHANGEPOINT_WINDOW_S of the
# two most diagnostic capabilities. Best-effort — any failure skips the hint and
# never breaks detection.
CHANGEPOINT_WINDOW_S = 1800
CHANGEPOINT_CAPABILITIES = ("p99_latency", "error_rate")


def _detect_changepoints(service: str) -> list:
    out = []
    try:
        mb = get_backend("metrics")
        if mb is None:
            return out
        now = float(time.time())
        for cap in CHANGEPOINT_CAPABILITIES:
            try:
                expr = mb.template(cap, service=service)
                series = mb.range(expr, now - CHANGEPOINT_WINDOW_S, now, 30)
                cp = detect_changepoint(series)
                if cp:
                    cp["signal"] = cap
                    out.append(cp)
            except Exception as e:
                print(f"[detector] changepoint skip {cap}: {e}")
    except Exception as e:
        print(f"[detector] changepoint detection unavailable: {e}")
    return out

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
    print(f"[detector] db_pool={raw.get('db_pool_utilization')} circuit_breaker={raw.get('circuit_breaker_state')}")

    is_shadow = bool(state.get("shadow_execution", False) or raw.get("shadow_execution", False))
    raw["shadow_execution"] = is_shadow

    # Bet 2: deterministic onset hints feed the investigation as evidence anchors
    raw["changepoints"] = _detect_changepoints(service)

    return {
        "raw_signals": raw,
        "severity": severity,
        "affected_services": [service],
        "resolution_status": ResolutionStatus.INVESTIGATING.value,
        "shadow_execution": is_shadow,
    }
