"""
tools/agent_tools.py
--------------------------

sandboxed tool set for the DeepAgent/ReAct investigation harness.
"""

import json
import re
import threading
import urllib.request
import os
from langchain_core.tools import tool
from src.tools.prometheus_tool import _instant_query, prometheus_url
from src.tools.loki_tool import extract_log_summary
from src.tools.log_tool import query_logs
from src.tools.backends.registry import get_backend
from src.tools.kg_tool import (
    query_knowledge_graph,
    query_recent_resolved_incident,
    find_active_upstream_incident,
    SERVICE_METRICS,
    label_notes_for,
)
from src.tools.sre_tool import lookup_runbook
from src.graph.policy import ALLOWED_WRITE_TOOLS

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# metric families that are simulator-app
_APP_METRIC_PREFIXES = (
    "http_", "circuit_", "db_", "payment_", "fraud_", "slo_", "net_conntrack_",
    # OTel demo (AWS) OTLP-native families
    "rpc_",
)

# labels/notes for the remote OTel demo stack (AWS) vs the fintech sim:
# server-side HTTP metrics use service_name (not service=) and
OTEL_DEMO_CATALOG = {
    "http_server_request_duration_seconds": "{service_name,http_route,http_response_status_code} (hist/cnt)",
    "http_client_request_duration_seconds": "{service_name,server_address,http_response_status_code} (hist/cnt)",
    "rpc_server_call_duration_seconds": "{service_name,rpc_service,rpc_method,rpc_response_status_code} (hist/cnt)",
    "rpc_client_call_duration_seconds": "{service_name,rpc_service,rpc_method,rpc_response_status_code} (hist/cnt)",
    "feature_flag_evaluation_success_total": "{service_name,feature_flag_key,feature_flag_result_variant}",
    "feature_flag_evaluation_error_total": "{service_name,feature_flag_key,feature_flag_result_error}",
    "flagd_replica_follow_refetches": "{service_name}",
}

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
# merge in the remote-demo families so validate_expr accepts them too
METRIC_CATALOG.update(OTEL_DEMO_CATALOG)

#per-investigation duplicate-read cache: an identical read query inside one
#investigation returns the recorded result instead of re-hitting the backend.
#deep_investigation resets it per run; outside a run the cache is inert.
_investigation_cache = threading.local()


def reset_investigation_cache():
    _investigation_cache.results = {}


def _cached_read(key: str, run):
    cache = getattr(_investigation_cache, "results", None)
    if cache is None:
        return run()
    if key in cache:
        print(f"[tool-cache] DUPLICATE read suppressed: {key[:80]}")
        return (f"[DUPLICATE] You already ran this exact query in this investigation. "
                f"Recorded result: {str(cache[key])[:180]} — move to a different hypothesis.")
    result = run()
    cache[key] = result
    return result


def validate_expr(expr: str, check_catalog: bool = True) -> tuple[bool, str]:
    """Validate PromQL expression. Returns (is_valid, error_message).
    check_catalog=False skips the metric-membership check (syntax only) — used
    by the verifier, where unknown metrics are honest no-data results and the
    read-only dispatcher is the enforcement point."""
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
    
    #check metric exists in catalog (skippable for the verifier's syntax-only pass)
    if check_catalog and not any(m in expr for m in METRIC_CATALOG):
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
    - label conventions differ per environment — call list_metrics(service) for the
      exact label names this environment uses; never guess labels

    Returns the scalar value, or a structured ERROR explaining what to fix.
    On ERROR: call list_metrics(service) before retrying.
    """
    valid, err = validate_expr(expr)
    if not valid:
        return f"ERROR: {err}. Allowed metrics: {list(METRIC_CATALOG.keys())}. Call list_metrics(service) for this environment's label conventions. Never use service prefixes."
    try:
        val = _cached_read(f"metric:{expr}", lambda: _instant_query(expr))
        if val is None:
            return f"ERROR: Query '{expr}' returned no data. Use exact metric from list_metrics and verify the labels this environment uses (list_metrics lists them)."
        return f"{val}"
    except Exception as e:
        return f"query failed: {e}"

@tool
def query_service_logs(service: str, start: str = "", end: str = "", text: str = "", level: str = "") -> str:
    """Return TOP log patterns (normalized, with counts) + up to 6 sample lines for a service.

    Use for: extracting concrete failure fingerprints (timeouts, 500s, exceptions,
    queue lag, DNS/network errors) that metrics alone cannot show. Backend is set
    by the LOG_BACKEND env (local Loki, or the OTel demo's OpenSearch via tunnel).

    Args:
        service: plain service name (e.g. 'cart', 'payment_gateway'). Legacy
            LogQL selectors {service="x"} are still accepted.
        start/end: ISO8601. Omit both -> last 15 min. Passing the alert's fired
            time in `start` with no `end` widens to alert ±15 min.
        text: optional substring filter (e.g. 'timeout').
        level: DEBUG|INFO|WARN|ERROR|FATAL (opensearch: treated as minimum severity).

    HONEST OUTPUT: an unreachable backend, a timeout, or an empty window is
    reported as 'signal unavailable: <reason>' — that means the signal could not
    be obtained (or nothing matched the filters/level), NEVER 'no problem found'.
    """
    if not service or not service.strip():
        return "signal unavailable: service is required"
    svc = service.strip()
    if svc.startswith("{"):
        m = re.search(r'service="([^"]+)"', svc)
        if m:
            svc = m.group(1)
    try:
        def _run():
            return query_logs(
                svc,
                start=start or None,
                end=end or None,
                text=text or None,
                level=level or None,
            )
        res = _cached_read(f"logs:{svc}:{start}:{end}:{text}:{level}", _run)
    except Exception as e:
        return f"signal unavailable: log backend error: {e}"
    if not res.get("available"):
        return (f"signal unavailable (backend={res.get('backend')}, window={res.get('window', {}).get('mode')}): "
                f"{res.get('reason') or 'no log lines matched'}")
    rows = [f"backend={res['backend']} window={res['window']['mode']} lines_scanned={res['lines_scanned']}/{res['limit']}"]
    for p in res["top_patterns"]:
        rows.append(f"x{p['count']:>3} [{p['level'][:8]}] {p['pattern']}")
    if res.get("sample_lines"):
        rows.append("SAMPLES:")
        rows.extend(f"  {s}" for s in res["sample_lines"])
    return "\n".join(rows)

@tool
def query_traces(service: str, lookback_minutes: int = 10, min_duration_ms: int = 0) -> str:
    """Return recent trace summaries for a service (id, root service, duration, start).

    Use for: seeing the ACTUAL cross-service call path behind a symptom — which
    downstream hop is slow or erroring, instead of inferring it from metric
    coincidence. Best after metrics point at a suspect hop.

    Args:
        service: plain service name (e.g. 'checkout', 'payment_gateway').
        lookback_minutes: how far back to search (default 10).
        min_duration_ms: optional floor, e.g. 500 to surface only slow traces.

    HONEST OUTPUT: a disabled traces backend (this environment pack has none)
    or an unreachable one is reported as 'signal unavailable: <reason>' — that
    means the signal could not be obtained, NEVER 'no problem found'.
    """
    svc = (service or "").strip()
    if not svc:
        return "signal unavailable: service is required"
    try:
        tempo = get_backend("traces")
        if tempo is None:
            return "signal unavailable: no traces backend in this environment pack (traces disabled)"
        spans = _cached_read(
            f"traces:{svc}:{lookback_minutes}:{min_duration_ms}",
            lambda: tempo.search(
                svc,
                lookback_minutes=max(1, min(int(lookback_minutes), 60)),
                min_duration_ms=int(min_duration_ms) if min_duration_ms else None,
            ),
        )
    except Exception as e:
        return f"signal unavailable: traces backend error: {e}"
    if isinstance(spans, str):
        # _cached_read returns a [DUPLICATE] marker string on repeat queries
        return spans
    if not spans:
        return f"signal unavailable: no traces matched service='{svc}' in the last {lookback_minutes}m"
    rows = [f"traces for {svc} (lookback={lookback_minutes}m, n={len(spans)}):"]
    for s in spans:
        rows.append(
            f"  {s.get('trace_id')} root={s.get('root_service')} "
            f"name={s.get('root_trace_name')} dur={s.get('duration_ms')}ms"
        )
    return "\n".join(rows)

@tool
def get_recent_changes(service: str = "") -> str:
    """Return WHAT CHANGED recently around a service: current feature-flag variant
    mix (from live flag-evaluation metrics) and recent resolved incidents touching
    it or its upstream (KG).

    Use for: the FIRST question of any investigation — most incidents are caused
    by a change. If a flag's variant mix shifted, that shift is your prime
    suspect; if a nearby upstream incident is active, start there.

    HONEST OUTPUT: each source that does not exist in this environment (e.g. no
    flag metrics in the fintech sim, no k8s events backend) is reported as
    unavailable — never as 'nothing changed'.
    """
    svc = (service or "").strip()
    sections: list = []

    # current flag variant mix: per flag, which variant is actually evaluating now
    try:
        expr = ("sum by (feature_flag_key, feature_flag_result_variant) "
                "(increase(feature_flag_evaluation_success_total[10m]))")
        val = _cached_read(f"flagmix:{expr}", lambda: _instant_query(expr))
        if isinstance(val, str) and val.startswith("[DUPLICATE]"):
            sections.append(f"[Flag variants] {val}")
        elif val:
            sections.append(f"[Flag variants, last 10m evaluations] {val}")
        else:
            sections.append("[Flag variants] signal unavailable: no flag-evaluation metrics in this environment")
    except Exception as e:
        sections.append(f"[Flag variants] signal unavailable: {e}")

    # recent incident activity on or upstream of the service (KG; static-topology
    # fallback covers Neo4j-outage regimes)
    if svc:
        try:
            recent = find_active_upstream_incident(svc, window_min=60)
            if recent:
                sections.append(
                    f"[Recent incident] upstream {recent.get('upstream_service')} "
                    f"({recent.get('incident_id')}): {recent.get('root_cause')}"
                )
            else:
                sections.append("[Recent incident] none found, or the knowledge graph is unreachable (could not verify)")
        except Exception as e:
            sections.append(f"[Recent incident] signal unavailable: {e}")
    else:
        sections.append("[Recent incident] unavailable: pass a service name")

    sections.append("[K8s events / deploys] signal unavailable: no kubernetes events source in this environment")
    return "\n".join(sections)

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
        return _cached_read(f"topology:{service}", lambda: query_knowledge_graph([service], ""))
    except Exception as e:
        return f"kg query failes: {e}"

@tool
def list_metrics(service: str) -> str:
    """Return the exact, live Prometheus metric names and curated PromQL for a service
    (the authoritative catalog — query these names, do NOT guess names).

    Use as a FALLBACK whenever query_prometheus_metric errors with 'Unknown metric'
    or 'returned no data' — this lists what actually exists in the sim cluster.
    Also returns the label gotchas for this service's environment that cause
    most rejected queries.
    Returns 'no curated queries for <service>' for services that are not scraped.
    """
    try:
        def _run():
            metrics = get_backend("metrics")
            names = metrics.metric_names(prefixes=_APP_METRIC_PREFIXES) if metrics else None
            live = names if names is not None else None
            return live
        live = _cached_read(f"list_metrics:{service}", _run)
    except Exception:
        live = None  # fall through to verified static map

    notes = label_notes_for(service)
    known = SERVICE_METRICS.get(service, [])
    if live is not None:
        header = f"Live metric families in Prometheus: {', '.join(sorted(set(live)))}"
    else:
        header = "Prometheus unreachable — using verified static list"
    if known:
        return (
            f"{header}\n"
            f"Exact PromQL for {service}: {', '.join(known)}\n"
            f"{notes}"
        )
    return (
        f"{header}\n"
        f"No curated queries for '{service}'  it may not be a scraped service "
        f"(scraped: {', '.join(sorted(SERVICE_METRICS))}). {notes}"
    )

@tool
def lookup_runbook_chunk(query: str) -> str:
    """return the 2 most relevant runbook chunks for a query"""
    try:
        return lookup_runbook(query=query, k=2).get("context", "no rubbook match")
    except Exception as e:
        return f"runbook lookup failed: {e}"

@tool
def propose_remediation(action: str, tool: str, target_service: str, blast_radius: str,
                        flag: str = "", variant: str = "") -> str:
    """PROPOSE (never execute) a concrete remediation. This ENDS the investigation —
    call it once your diagnosis is complete, with the evidence-based finding in `action`.

    Args:
        action: full free-text diagnosis + fix rationale (evidence incl. the numbers
            you observed; keep the raw-signal numbers coherent with the live values).
            This text is also the human's record of what was done and why — state
            the diagnosis, the fix, its expected effect, and what to watch after
            it lands (same readability as an escalation reason).
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
        flag: for set_feature_flag ONLY — the feature-flag key your evidence names
            (e.g. from feature_flag_evaluation_success_total{feature_flag_key=...}).
        variant: for set_feature_flag ONLY — the variant to restore, named from your
            evidence (e.g. the last healthy variant the flag metric shows).

    The proposal is validated by a critic (grounding + policy) before any execution.
    """
    if tool not in ALLOWED_WRITE_TOOLS:
        return (f"REJECTED: tool '{tool}' is not a valid remediation tool. "
                f"Call propose_remediation again with tool set to one of: {sorted(ALLOWED_WRITE_TOOLS)}")
    if tool == "set_feature_flag" and (not flag or not variant):
        return ("REJECTED: set_feature_flag needs the flag key and the variant to restore, "
                "named from your evidence (e.g. flag='<key your metric evidence names>', "
                "variant='<the healthy variant it stopped evaluating to>'). "
                "Look at feature_flag_evaluation_success_total labels or list_metrics before proposing. "
                "Never guess a flag name or variant.")
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
    query_traces,
    get_recent_changes,
    query_topology,
    list_metrics,
    lookup_runbook_chunk,
]

ALL_TOOLS = READ_TOOLS + [propose_remediation, escalate_with_reason]