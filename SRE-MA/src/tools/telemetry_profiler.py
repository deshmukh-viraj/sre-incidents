"""profile discovered Prometheus metrics into heuristic type and role classifications."""

import re
from collections import defaultdict

VALID_METRIC_TYPES = {"counter", "gauge", "histogram", "summary"}

ROLE_PATTERNS = {
    "latency": (r"duration", r"latency", r"response_time", r"processing_time", r"elapsed_time"),
    "errors": (r"error", r"failure", r"exception", r"fault"),
    "throughput": (r"request", r"transaction", r"operation", r"throughput", r"completed"),
    "saturation": (r"queue", r"pending", r"waiting", r"pool", r"inflight", r"active", r"concurrent"),
    "resource_usage": (r"cpu", r"memory", r"heap", r"disk", r"filesystem", r"network"),
    "retries": (r"retry", r"retries", r"redelivery", r"attempt"),
    "availability": (r"availability", r"health", r"ready", r"up"),
    "traffic": (r"traffic", r"bytes", r"bandwidth", r"received", r"transmitted"),
}


def _infer_metric_type(metric_name, metadata, series):
    metric_type = str(metadata.get("type", "unknown")).lower().strip()
    if metric_type in VALID_METRIC_TYPES:
        return metric_type, ["Metric type reported by Prometheus metadata"]

    labels = {label for item in series for label in item}
    name = metric_name.lower()
    if name.endswith("_bucket") and "le" in labels:
        return "histogram", ["Metric has _bucket suffix", "Observed histogram le label"]
    if name.endswith("_seconds_count"):
        return "unknown", ["Count suffix alone does not establish metric type"]
    if name.endswith("_total"):
        return "counter", ["Metric has conventional _total counter suffix"]
    return "unknown", []


def _infer_semantic_role(metric_name, metadata):
    text = f"{metric_name.lower()} {str(metadata.get('help', '')).lower()} {str(metadata.get('unit', '')).lower()}"
    scores, evidence = defaultdict(int), defaultdict(list)
    for role, patterns in ROLE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text):
                scores[role] += 1
                evidence[role].append(f"Matched semantic pattern: {pattern}")

    if not scores:
        return "unknown", []
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "unknown", ["Multiple semantic roles have equal evidence"]
    return ranked[0][0], evidence[ranked[0][0]]


def classify_metric(metric_name, metadata=None, series=None):
    """Profile one discovered metric without guessing unknown classifications."""
    if not isinstance(metric_name, str) or not metric_name.strip():
        raise ValueError("metric_name must be a non-empty string")

    if isinstance(metadata, list):
        metadata = metadata[0] if metadata else {}
    if not isinstance(metadata, dict):
        metadata = {}
    series = [item for item in series if isinstance(item, dict)] if isinstance(series, list) else []

    metric_type, type_evidence = _infer_metric_type(metric_name, metadata, series)
    semantic_role, role_evidence = _infer_semantic_role(metric_name, metadata)
    evidence = type_evidence + role_evidence
    if metadata.get("help"):
        evidence.append("Prometheus HELP description available")
    if series:
        evidence.append(f"Observed {len(series)} matching series sample(s)")

    if semantic_role == "unknown" or metric_type == "unknown":
        confidence = "low"
    elif metadata.get("type") and metadata.get("help"):
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "metric": metric_name,
        "metric_type": metric_type,
        "semantic_role": semantic_role,
        "unit": str(metadata.get("unit") or "unknown").lower(),
        "description": metadata.get("help", ""),
        "evidence": evidence,
        "confidence": confidence,
        "series_count": len(series),
    }


def profile_service_telemetry(discovery_result):
    """Profile every metric in a live discovery result, grouped by semantic role."""
    if not isinstance(discovery_result, dict):
        raise TypeError("discovery_result must be a dictionary")

    status = discovery_result.get("status")
    if status != "discovered":
        return {
            "status": "discovery_incomplete",
            "discovery_status": status,
            "service": discovery_result.get("service"),
            "profiles": [],
            "groups": {},
            "message": "Cannot profile live service telemetry because discovery did not return a discovered status.",
        }

    metric_names = discovery_result.get("metric_names", [])
    if not isinstance(metric_names, list):
        raise TypeError("metric_names must be a list")
    metadata_map = discovery_result.get("metric_metadata", {})
    if not isinstance(metadata_map, dict):
        metadata_map = {}
    matching_series = discovery_result.get("matching_series", [])
    if not isinstance(matching_series, list):
        matching_series = []

    series_by_metric = defaultdict(list)
    for item in matching_series:
        if isinstance(item, dict) and item.get("__name__"):
            series_by_metric[item["__name__"]].append(item)

    profiles = [
        classify_metric(name, metadata_map.get(name), series_by_metric.get(name, []))
        for name in metric_names
        if isinstance(name, str)
    ]
    groups = defaultdict(list)
    for profile in profiles:
        groups[profile["semantic_role"]].append(profile["metric"])

    return {
        "status": "profiled",
        "service": discovery_result.get("service"),
        "total_metrics": len(profiles),
        "profiles": profiles,
        "groups": dict(groups),
        "limitations": [
            "Semantic roles are heuristic candidates.",
            "Metric names and HELP descriptions do not prove causality.",
            "Missing metadata does not invalidate a metric.",
            "No health status or root cause is inferred.",
        ],
    }
