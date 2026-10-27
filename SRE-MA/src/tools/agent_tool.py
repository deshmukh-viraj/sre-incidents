"""
tools/agent_tools.py
--------------------------

sandboxed tool set for the DeepAgent/ReAct investigation harness.
"""

import json
import urllib.request
import os
from langchain_core.tools import tool
from src.tools.prometheus_tool import _instant_query
from src.tools.loki_tool import query_loki, extract_log_summary
from src.tools.kg_tool import (
    query_knowledge_graph,
    query_recent_resolved_incident,
    SERVICE_METRICS,
    METRIC_LABEL_NOTES,
)
from src.tools.sre_tool import lookup_runbook
from src.graph.policy import ALLOWED_WRITE_TOOLS

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# metric families that are simulator-app
_APP_METRIC_PREFIXES = (
    "http_", "circuit_", "db_", "payment_", "fraud_", "slo_", "net_conntrack_"
)

METRIC_CATALOG = {
    "http_requests_total": "{service,method,status_code}",
    "http_request_duration_seconds": "{service,endpoint} (hist)",
    "db_connection_pool_active": "{db='postgres_primary'}",
    "db_connection_pool_max": "{db='postgres_primary'}",
    "db_query_duration_seconds": "{db,operation} (hist)",
    "circuit_breaker_state": "{service,upstream}",
    "payment_transactions_total": "{transaction_type,status,currency}",
    "payment_amount_processed": "{transaction_type}",
    "fraud_score": "{decision} (hist)",
    "fraud_model_prediction_latency_seconds": "{model_version} (hist)",
    "process_memory_rss_bytes": "{service}",
    "process_cpu_seconds": "{service}",
    "compliance_events_total": "{regulation,event_type,severity}",
    "slo_error_budget_remaining_ratio": "{service,slo_name}",
    "net_conntrack_dialer_conn_failed_total": "{upstream}",
}

def validate_expr(expr: str) -> tuple[bool, str]:
    """Validate PromQL expression. Returns (is_valid, error_message)."""
    #check balanced brackets
    brackets = {'(': ')', '[': ']', '{': '}'}
    stack = []
    for ch in expr:
        if ch in brackets:
            stack.append(brackets[ch])
        elif ch in brackets.values():
            if not stack or stack.pop() != ch:
                return False, "Unbalanced brackets"
    if stack:
        return False, "Unbalanced brackets"
    
    #check rate()/increase() have range selector
    #find rate() and increase() calls and check if they contain a range selector [...]
    import re
    rate_pattern = r'(rate|increase)\s*\([^)]*\)'
    for match in re.finditer(rate_pattern, expr):
        call = match.group(0)
        #check if there's a [...] inside the parentheses
        inner = call[call.find('(')+1:call.rfind(')')]
        if '[' not in inner or ']' not in inner:
            return False, f"{call} missing range selector (e.g., [5m])"
    
    #check metric exists in catalog
    if not any(m in expr for m in METRIC_CATALOG):
        return False, f"Unknown metric. Allowed: {list(METRIC_CATALOG.keys())}"
    
    return True, ""


@tool
def query_prometheus_metric(expr: str) -> str:
    """Run ONE instant PromQL query against live Prometheus and return its numeric value.

    Use for: testing a single hypothesis per call (p99, error rate, pool, breaker state...).
    Rules enforced by the tool:
    - expr must use an EXACT metric from list_metrics/METRIC_CATALOG — never guess or
      invent names, never prefix service names (`http_requests_total`, not `payment_...`)
    - brackets must balance; every rate()/increase() MUST include a range selector
      (e.g. [5m]) — rate(x[5m]) is valid, rate(x) is rejected
    - pool/db metrics use db='postgres_primary' (NOT service=); request metrics use
      service='payment_gateway' and label status_code for non-200 diagnosis
      (e.g. http_requests_total{service="payment_gateway",status_code="429"})

    Returns the scalar value, or a structured ERROR explaining what to fix.
    On ERROR: call list_metrics(service) before retrying.
    """
    valid, err = validate_expr(expr)
    if not valid:
        return f"ERROR: {err}. Allowed metrics: {list(METRIC_CATALOG.keys())}. Convention: generic names + labels (e.g. db='postgres_primary'). Never use service prefixes."
    try:
        val = _instant_query(expr)
        if val is None:
            return f"ERROR: Query '{expr}' returned no data. Use exact metric from list_metrics and verify labels (e.g. db='postgres_primary', not service=)."
        return f"{val}"
    except Exception as e:
        return f"query failed: {e}"

@tool
def query_service_logs(service: str) -> str:
    """Return up to 6 summarized recent ERROR log lines for a Prometheus-scraped service (example: payment_gateway).

    Use for: extracting concrete failure fingerprints (timeouts, 429s, pool exhaustion,
    exception types) that metrics alone cannot show.
    Skip when: the alert state says logs_ok=False (no logs available — state it and
    move on to another hypothesis instead of burning a round here).
    Accepts either a bare service name or a full LogQL selector like {service="x",level="error"}.
    Returns log summaries or the literal 'no error logs' (which is itself useful: it
    rules out log-visible failure modes).
    """
    try:
        logql = service if service.startswith("{") else f'{{service="{service}"}}'
        logs = extract_log_summary(query_loki(logql))
        return "\n".join(logs[:6]) or "no error logs"
    except Exception as e:
        return f"log query failed: {e}"

@tool
def query_topology(service: str) -> str:
    """Return the service's dependency topology, past incidents AND the authoritative
    curated PromQL queries from the knowledge graph (live; beats any catalog).

    IMPORTANT: this is the recommended FIRST call of an investigation:
    - DEPENDS_ON list tells you which downstream services must also be probed
    - the PromQL returned here is pre-validated, use it directly for
      query_prometheus_metric instead of writing your own
    - past incidents frequently name the exact root cause + the remediation that worked
    Only fall back to list_metrics when a topology query returns empty or errors.
    """
    try:
        return query_knowledge_graph([service], "")
    except Exception as e:
        return f"kg query failes: {e}"

@tool
def list_metrics(service: str) -> str:
    """Return the exact, live Prometheus metric names and curated PromQL for a service
    (the authoritative catalog — query these names, do NOT guess names).

    Use as a FALLBACK whenever query_prometheus_metric errors with 'Unknown metric'
    or 'returned no data' — this lists what actually exists in the sim cluster.
    Also returns METRIC_LABEL_NOTES: the label gotchas (e.g. pool metrics are keyed by
    db='postgres_primary', card_rails appears only under circuit_breaker_state while
    most metrics use service=...) that cause most rejected queries.
    Returns 'no curated queries for <service>' for services that are not scraped.
    """
    try:
        url = f"{PROMETHEUS_URL}/api/v1/label/__name__/values"
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            names = json.loads(resp.read().decode()).get("data", [])
        live = [n for n in names if n.startswith(_APP_METRIC_PREFIXES)]
    except Exception:
        live = None  # fall through to verified static map

    known = SERVICE_METRICS.get(service, [])
    if live is not None:
        header = f"Live metric families in Prometheus: {', '.join(sorted(set(live)))}"
    else:
        header = "Prometheus unreachable — using verified static list"
    if known:
        return (
            f"{header}\n"
            f"Exact PromQL for {service}: {', '.join(known)}\n"
            f"{METRIC_LABEL_NOTES}"
        )
    return (
        f"{header}\n"
        f"No curated queries for '{service}'  it may not be a scraped service "
        f"(scraped: {', '.join(sorted(SERVICE_METRICS))}). {METRIC_LABEL_NOTES}"
    )

@tool
def lookup_runbook_chunk(query: str) -> str:
    """return the 2 most relevant runbook chunks for a query"""
    try:
        return lookup_runbook(query=query, k=2).get("context", "no rubbook match")
    except Exception as e:
        return f"runbook lookup failed: {e}"

@tool
def propose_remediation(action: str, tool: str, target_service: str, blast_radius: str) -> str:
    """PROPOSE (never execute) a concrete remediation. This ENDS the investigation —
    call it once your diagnosis is complete, with the evidence-based finding in `action`.

    Args:
        action: full free-text diagnosis + fix rationale (evidence incl. the numbers
            you observed; keep the raw-signal numbers coherent with the live values).
        tool: MUST be one of the whitelisted write tools:
            'set_feature_flag', 'rollback_deployment', 'rolling_restart',
            'capture_diagnostics', 'notify', 'execute_kg_suggestion',
            'kubectl_patch_deployment', 'kubectl_restart_deployment', 'kubectl_scale'.
            Anything else is REJECTED here in the loop — you immediately get the
            allowed list back and can fix it in the same investigation instead of
            paying a full re-run later.
        target_service: the service you are affecting — the alerted service itself or
            one of its DEPENDS_ON services (use query_topology to get the list).
        blast_radius: 'pod' | 'service' | 'cluster' (your honest estimate of impact scope).

    The proposal is validated by a critic (grounding + policy) before any execution.
    """
    if tool not in ALLOWED_WRITE_TOOLS:
        return (f"REJECTED: tool '{tool}' is not a valid remediation tool. "
                f"Call propose_remediation again with tool set to one of: {sorted(ALLOWED_WRITE_TOOLS)}")
    return "proposal recorded, investigation ended."


@tool
def escalate_with_reason(reason: str) -> str:
    """Hand the incident to the on-call human with a specific reason. TERMINAL call —
    ends the investigation (use instead of ending with only a summary message).

    Use when: evidence is inconclusive, a required signal is unavailable (e.g.
    logs_ok=False), or the residual risk of acting outweighs acting (e.g. would
    restart bank-critical traffic on an unconfirmed root cause).

    The reason text is the on-call's ONLY context: include the diagnosis so far, the
    evidence (signal keys + observed live values), what was ruled out, and exactly
    what you need the human to check. It is replayed verbatim into the escalation.
    """
    return f"escalated: {reason}"


READ_TOOLS = [
    query_prometheus_metric,
    query_service_logs,
    query_topology,
    list_metrics,
]
INVESTIGATION_TOOLS = READ_TOOLS

ALL_TOOLS = READ_TOOLS + [propose_remediation, escalate_with_reason]