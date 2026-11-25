"""
tools/agent_tool.py
-------------------
Diagnostic and remediation tools used by investigation agents.
Provides read tools for metrics, logs, traces, and topology,
plus completion tools to propose remediation or escalate.
"""

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import tool

from src.graph.policy import ALLOWED_WRITE_TOOLS
from src.tools.backends.registry import get_backend
from src.tools.kg_tool import (
    SERVICE_METRICS,
    find_active_upstream_incident,
    label_notes_for,
    query_knowledge_graph,
)
from src.tools.prometheus_tool import _instant_query, _series_query
from src.tools.promql_planner import build_query
from src.tools.sre_tool import lookup_runbook
from src.tools.telemetry_profiler import profile_service_telemetry

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

OTEL_DEMO_CATALOG = {
    "http_server_request_duration_seconds": "{service_name,http_route,http_response_status_code} (hist/cnt)",
    "http_client_request_duration_seconds": "{service_name,server_address,http_response_status_code} (hist/cnt)",
    "rpc_server_call_duration_seconds": "{service_name,rpc_service,rpc_method,rpc_response_status_code} (hist/cnt)",
    "rpc_client_call_duration_seconds": "{service_name,rpc_service,rpc_method,rpc_response_status_code} (hist/cnt)",
    "feature_flag_evaluation_success_total": "{service_name,feature_flag_key,feature_flag_result_variant}",
    "feature_flag_evaluation_error_total": "{service_name,feature_flag_key,feature_flag_result_error}",
    "flagd_replica_follow_refetches": "{service_name}",
    "traces_span_metrics_calls_total": "{service_name,span_name,span_status}",
    "traces_span_metrics_duration_milliseconds": "{service_name,span_name} (hist)",
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
    **OTEL_DEMO_CATALOG,
}

_RATE_PATTERN = re.compile(r"(rate|increase)\s*\([^)]*\)")


def catalog_for_style(style: str) -> dict:
    if style == "oteldemo":
        return {k: v for k, v in METRIC_CATALOG.items() if k in OTEL_DEMO_CATALOG}
    return {k: v for k, v in METRIC_CATALOG.items() if k not in OTEL_DEMO_CATALOG}


def catalog_for_active_environment() -> dict:
    backend = get_backend("metrics")
    style = getattr(getattr(backend, "cfg", None), "label_style", "fintech") if backend else "fintech"
    return catalog_for_style(style)


# Per-investigation cache to prevent duplicate queries
_investigation_cache = threading.local()


def reset_investigation_cache():
    _investigation_cache.results = {}
    _investigation_cache.discovered_metric_names = set()


def _cached_read(key: str, run_fn):
    cache = getattr(_investigation_cache, "results", None)
    if cache is None:
        return run_fn()
    if key in cache:
        return (f"[DUPLICATE] You already ran this exact query in this investigation. "
                f"Recorded result: {str(cache[key])[:180]}, move to a different hypothesis.")
    result = run_fn()
    cache[key] = result
    return result


def validate_expr(expr: str, check_catalog: bool = True) -> Tuple[bool, str]:
    brackets = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for ch in expr:
        if ch in brackets:
            stack.append(brackets[ch])
        elif ch in brackets.values():
            if not stack or stack.pop() != ch:
                return False, "Unbalanced brackets"
    if stack:
        return False, "Unbalanced brackets"

    for match in _RATE_PATTERN.finditer(expr):
        call = match.group(0)
        inner = call[call.find("(") + 1:call.rfind(")")]
        if "[" not in inner or "]" not in inner:
            return False, f"{call} missing range selector (e.g., [5m])"

    if check_catalog and not any(m in expr for m in METRIC_CATALOG):
        return False, f"Unknown metric. Allowed: {list(METRIC_CATALOG.keys())}"

    return True, ""


def _fmt_series(row: dict) -> str:
    labels = dict(row.get("labels") or {})
    name = labels.pop("__name__", "")
    lbl = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    rendered = f"{name}{{{lbl}}}" if lbl else name
    return f"{rendered} -> {row['value']}"


@tool
def query_prometheus_metric(expr: str) -> str:
    """Run ONE instant PromQL query against live Prometheus and return its numeric value."""
    valid, err = validate_expr(expr, check_catalog=False)
    if not valid:
        return f"ERROR: {err}. Allowed metrics: {list(METRIC_CATALOG.keys())}. Call list_metrics(service) for label conventions."
    try:
        val = _cached_read(f"metric:{expr}", lambda: _instant_query(expr))
        if val is None:
            return f"ERROR: Query '{expr}' returned no data. Use exact metric from list_metrics."
        return f"{val}"
    except Exception as e:
        return f"query failed: {e}"


@tool
def query_prometheus_series(expr: str) -> str:
    """Run ONE instant PromQL query and return its per-series values WITH labels."""
    valid, err = validate_expr(expr, check_catalog=False)
    if not valid:
        return f"ERROR: {err}. Allowed metrics: {list(METRIC_CATALOG.keys())}. Call list_metrics(service) for label conventions."
    try:
        rows = _cached_read(f"series:{expr}", lambda: _series_query(expr))
        if isinstance(rows, str) and rows.startswith("[DUPLICATE]"):
            return rows
        if not rows:
            return f"ERROR: Query '{expr}' returned no data. Use exact metric from list_metrics."
        return f"n_series={len(rows)} (value-desc)\n" + "\n".join(_fmt_series(r) for r in rows)
    except Exception as e:
        return f"query failed: {e}"


@tool
def discover_prometheus_telemetry(service: str, service_label: str = "", service_value: str = "", max_metrics: int = 80) -> str:
    """Retry live Prometheus discovery using an explicit service-label mapping."""
    try:
        backend = get_backend("metrics")
        if backend is None:
            return json.dumps({"status": "backend_unavailable", "reason": "Metrics backend is disabled"})
        result = backend.discover_metrics(
            service=service, service_label=service_label, service_value=service_value,
            max_metrics=max(1, min(int(max_metrics), 120))
        )
        _investigation_cache.discovered_metric_names = set(result.get("metric_names", []))
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"discovery failed: {str(e)}"


@tool
def execute_prometheus_plan(
    service: str,
    metric: str,
    operation: str,
    filters: dict = None,
    group_by: list = None,
    window: str = "5m",
    quantile: float = 0.99,
) -> str:
    """Validate and execute a structured Prometheus query plan."""
    if not service or not service.strip():
        return json.dumps({"status": "PLAN_REJECTED", "reason": "Service is required."})

    try:
        backend = get_backend("metrics")
        if backend is None:
            return json.dumps({"status": "BACKEND_UNAVAILABLE", "reason": "Metrics backend is disabled."})

        discovery = backend.discover_service(service.strip())
        if discovery.get("status") != "discovered":
            return json.dumps({
                "status": "PLAN_REJECTED",
                "reason": "Service telemetry discovery is incomplete.",
                "discovery_status": discovery.get("status"),
                "available_labels": discovery.get("label_names", []),
            })

        matchers = discovery.get("service_matchers") or []
        if not matchers:
            return json.dumps({"status": "PLAN_REJECTED", "reason": "No verified service matcher was discovered."})

        primary = matchers[0]
        s_lbl, s_val = primary.get("label"), primary.get("value")
        if not s_lbl or not s_val:
            return json.dumps({"status": "PLAN_REJECTED", "reason": "Discovered service matcher is incomplete."})

        effective_filters = {item["label"]: item["value"] for item in matchers[1:] if item.get("label") and item.get("value")}
        effective_filters.update(filters or {})

        meta = discovery.get("metric_metadata", {}).get(metric, {})
        if isinstance(meta, list):
            meta = meta[0] if meta else {}
        metric_type = str(meta.get("type", "unknown")).lower()

        if operation in {"rate", "increase", "sum_rate", "avg_rate"}:
            if metric_type != "counter" and not (metric_type == "histogram" and metric.endswith("_bucket")):
                return json.dumps({
                    "status": "PLAN_REJECTED",
                    "reason": "Counter operation requires verified counter metadata or a histogram bucket.",
                    "metric": metric, "metric_type": metric_type,
                })

        if operation == "histogram_quantile" and metric_type != "histogram":
            return json.dumps({
                "status": "PLAN_REJECTED",
                "reason": "Histogram metadata was not verified.",
                "metric": metric, "metric_type": metric_type,
            })

        plan = build_query(
            metric=metric, operation=operation,
            discovered_metrics=discovery.get("metric_names", []),
            discovered_labels=discovery.get("label_names", []),
            service_label=s_lbl, service_value=s_val,
            filters=effective_filters, group_by=group_by or [],
            window=window, quantile=quantile,
        )
        result = backend.execute_query(plan["promql"], limit=50)
        return json.dumps({
            "status": result["status"],
            "service": service,
            "query_plan": plan,
            "result_type": result.get("result_type"),
            "samples": result.get("samples", []),
            "error": result.get("error"),
        }, indent=2, default=str)

    except (ValueError, TypeError, KeyError) as exc:
        return json.dumps({"status": "PLAN_REJECTED", "reason": str(exc)})
    except Exception as exc:
        return json.dumps({"status": "BACKEND_UNAVAILABLE", "reason": str(exc)})


@tool
def query_service_logs(
    service: str,
    start: str = "",
    end: str = "",
    text: str = "",
    level: str = "",
) -> str:
    """Query logs through the active environment's configured LogsBackend."""
    if not service or not service.strip():
        return "signal unavailable: service is required"

    svc = service.strip()
    if svc.startswith("{"):
        match = re.search(r'service="([^"]+)"', svc)
        if match:
            svc = match.group(1)

    try:
        def _run():
            backend = get_backend("logs")
            if backend is None:
                return {"available": False, "reason": "logs backend disabled"}

            lookback = 15
            start_ns = end_ns = None
            if start:
                from datetime import datetime, timezone
                s_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
                start_ns, end_ns = int(s_dt.timestamp() * 1e9), int(e_dt.timestamp() * 1e9)
                lookback = max(1, min(60, int((e_dt - s_dt).total_seconds() / 60)))

            selector = backend._selector(service=svc, text=text or None, level=level or None)
            records = backend.query(selector, limit=200, lookback_minutes=lookback, start_ns=start_ns, end_ns=end_ns)
            return {
                "available": bool(records),
                "backend": backend.cfg.adapter,
                "records": records,
                "reason": None if records else "no log lines matched",
            }

        result = _cached_read(f"logs:{svc}:{start}:{end}:{text}:{level}", _run)
    except Exception as exc:
        return f"signal unavailable: log backend error: {exc}"

    if isinstance(result, str):
        return result
    if not result.get("available"):
        return f"signal unavailable (backend={result.get('backend', 'unknown')}): {result.get('reason', 'no matching logs')}"

    rows = [f"backend={result['backend']} records={len(result['records'])}"]
    for r in result["records"][:20]:
        msg = str(r.get("message", r.get("body", r)))[:200]
        rows.append(f"[{r.get('level', '?')}] {r.get('service', svc)}: {msg}")
    return "\n".join(rows)


@tool
def query_traces(service: str, lookback_minutes: int = 10, min_duration_ms: int = 0) -> str:
    """Return recent trace summaries for a service (id, root service, duration, start)."""
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
        return spans
    if not spans:
        return f"signal unavailable: no traces matched service='{svc}' in the last {lookback_minutes}m"

    rows = [f"traces for {svc} (lookback={lookback_minutes}m, n={len(spans)}):"]
    for s in spans:
        rows.append(f"  {s.get('trace_id')} root={s.get('root_service')} name={s.get('root_trace_name')} dur={s.get('duration_ms')}ms")
    return "\n".join(rows)


@tool
def get_recent_changes(service: str = "") -> str:
    """Return WHAT CHANGED recently around a service."""
    svc = (service or "").strip()
    sections = []

    try:
        expr = "sum by (feature_flag_key, feature_flag_result_variant) (increase(feature_flag_evaluation_success_total[10m]))"
        rows = _cached_read(f"flagmix:{expr}", lambda: _series_query(expr))
        if isinstance(rows, str) and rows.startswith("[DUPLICATE]"):
            sections.append(f"[Flag variants] {rows}")
        elif rows:
            sections.append(f"[Flag variants, last 10m evaluations, value-desc] {'; '.join(_fmt_series(r) for r in rows)}")
        else:
            sections.append("[Flag variants] signal unavailable: no flag-evaluation series in the last 10m (or metrics backend unreachable)")
    except Exception as e:
        sections.append(f"[Flag variants] signal unavailable: {e}")

    if svc:
        try:
            recent = find_active_upstream_incident(svc, window_min=60)
            if recent:
                sections.append(f"[Recent incident] upstream {recent.get('upstream_service')} ({recent.get('incident_id')}): {recent.get('root_cause')}")
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
    """Return the service's dependency topology, past incidents AND curated PromQL queries."""
    try:
        return _cached_read(f"topology:{service}", lambda: query_knowledge_graph([service], ""))
    except Exception as e:
        return f"kg query failed: {e}"


@tool
def profile_prometheus_telemetry(service: str, service_label: str = "", service_value: str = "") -> str:
    """Discover and profile live prometheus telemetry for a service."""
    if not service or not service.strip():
        return json.dumps({"status": "invalid_input", "reason": "Service name is required"})

    try:
        backend = get_backend("metrics")
        if backend is None:
            return json.dumps({"status": "backend_unavailable", "service": service})

        discovery = backend.discover_service(service=service.strip(), service_value=service_value, service_label=service_label)
        _investigation_cache.discovered_metric_names = set(discovery.get("metric_names", []))
        profile = profile_service_telemetry(discovery)
        return json.dumps({"service": service, "discovery": discovery, "profile": profile}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"status": "profiling_failed", "service": service, "reason": str(e)})


@tool
def list_metrics(service: str) -> str:
    """Discover live Prometheus metrics available for a service."""
    svc = (service or "").strip()
    if not svc:
        return "ERROR: service name is required"

    notes = label_notes_for(svc)
    known = SERVICE_METRICS.get(svc, [])
    try:
        backend = get_backend("metrics")
        if backend is None:
            result = {"status": "backend_unavailable", "reason": "Metrics backend disabled"}
        else:
            discover = getattr(backend, "discover_service", None)
            if not callable(discover):
                return "ERROR: Active metrics backend does not support dynamic service discovery"
            result = discover(service=svc)
            _investigation_cache.discovered_metric_names = set(result.get("metric_names", []))
    except Exception as e:
        result = {"status": "backend_unavailable", "reason": str(e)}

    return json.dumps({**result, "known_metric_hints": known, "label_notes": notes}, indent=2, default=str)


@tool
def lookup_runbook_chunk(query: str) -> str:
    """Return the 2 most relevant runbook chunks for a query."""
    try:
        return lookup_runbook(query=query, k=2).get("context", "no runbook match")
    except Exception as e:
        return f"runbook lookup failed: {e}"


@tool
def propose_remediation(action: str, tool: str, target_service: str, blast_radius: str, flag: str = "", variant: str = "") -> str:
    """PROPOSE (never execute) a concrete remediation. This ENDS the investigation."""
    if tool not in ALLOWED_WRITE_TOOLS:
        return f"REJECTED: tool '{tool}' is not a valid remediation tool. Call propose_remediation again with tool set to one of: {sorted(ALLOWED_WRITE_TOOLS)}"
    if tool == "set_feature_flag" and (not flag or not variant):
        return ("REJECTED: set_feature_flag needs the flag key and the variant to restore, named from your evidence. "
                "Look at feature_flag_evaluation_success_total labels or list_metrics before proposing. Never guess a flag name or variant.")
    return "proposal recorded, investigation ended."


@tool
def escalate_with_reason(reason: str) -> str:
    """Hand the incident to the on-call human with a specific reason. TERMINAL call."""
    return f"escalated: {reason}"


READ_TOOLS = [
    execute_prometheus_plan,
    profile_prometheus_telemetry,
    discover_prometheus_telemetry,
    query_prometheus_metric,
    query_prometheus_series,
    query_service_logs,
    query_traces,
    get_recent_changes,
    query_topology,
    list_metrics,
    lookup_runbook_chunk,
]

INVESTIGATION_TOOLS = [
    t for t in READ_TOOLS if t.name not in {"query_prometheus_metric", "query_prometheus_series"}
]
INVESTIGATON_TOOLS = INVESTIGATION_TOOLS 

ALL_TOOLS = READ_TOOLS + [propose_remediation, escalate_with_reason]
