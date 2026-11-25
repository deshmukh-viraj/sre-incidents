"""Build safe PromQL from a structured query plan.

The model says what it wants. Python checks the plan and writes the query.
Raw model-written PromQL is never accepted.
"""

import json
import re

IDENTIFIER = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
WINDOW = re.compile(r"^[1-9][0-9]*(s|m|h)$")
SUPPORTED_OPERATIONS = {"instant", "rate", "increase", "sum_rate", "avg_rate", "histogram_quantile"}


def _escape_label_value(value):
    """Quote a label value the way PromQL expects."""
    return json.dumps(str(value))


def _require(ok, message):
    """Raise a ValueError when a plan check fails."""
    if not ok:
        raise ValueError(message)


def build_query(
    *,
    metric,
    operation,
    discovered_metrics,
    discovered_labels,
    service_label,
    service_value,
    filters=None,
    group_by=None,
    window="5m",
    quantile=0.99,
):
    """Turn a checked plan into PromQL. Metrics and labels must come from live discovery, not the model."""
    filters = filters or {}
    group_by = group_by or []

    _require(operation in SUPPORTED_OPERATIONS, f"Operation '{operation}' isn't supported.")
    _require(IDENTIFIER.fullmatch(metric), "That metric name isn't a valid PromQL identifier.")
    _require(metric in discovered_metrics, f"Metric '{metric}' wasn't found in live discovery.")
    if operation != "instant":
        _require(WINDOW.fullmatch(window), "That range window isn't valid (use s, m, or h).")
    _require(IDENTIFIER.fullmatch(service_label), "That service label isn't a valid PromQL identifier.")
    _require(service_label in discovered_labels, f"Service label '{service_label}' wasn't seen in discovery.")
    _require(service_value, "A service value is required.")

    matchers = {service_label: str(service_value)}
    for label, value in filters.items():
        _require(IDENTIFIER.fullmatch(label), f"Label '{label}' isn't a valid PromQL identifier.")
        _require(label in discovered_labels, f"Label '{label}' wasn't seen in discovery.")
        _require(label not in matchers, f"Label '{label}' is used twice (service and filter).")
        matchers[label] = str(value)

    for label in group_by:
        _require(IDENTIFIER.fullmatch(label), f"Grouping label '{label}' isn't a valid PromQL identifier.")
        _require(label in discovered_labels, f"Grouping label '{label}' wasn't seen in discovery.")

    matcher_text = ",".join(f"{label}={_escape_label_value(value)}" for label, value in sorted(matchers.items()))
    selector = f"{metric}{{{matcher_text}}}"

    templates = {
        "instant": "{selector}",
        "rate": "rate({selector}[{window}])",
        "increase": "increase({selector}[{window}])",
        "sum_rate": "sum(rate({selector}[{window}]))",
        "avg_rate": "avg(rate({selector}[{window}]))",
    }
    if operation == "histogram_quantile":
        _require(0 < float(quantile) < 1, "Quantile must be between 0 and 1.")
        _require("le" in discovered_labels, "The 'le' bucket label wasn't found in discovery.")
        _require(metric.endswith("_bucket"), "Histogram quantile needs a _bucket metric.")
        labels = list(dict.fromkeys(["le"] + group_by))
        expression = f"histogram_quantile({float(quantile)}, sum by ({', '.join(labels)}) (rate({selector}[{window}])))"
    elif operation in templates:
        expression = templates[operation].format(selector=selector, window=window)
    else: 
        raise ValueError(f"Unsupported operation: {operation}")
    
    return {
        "metric": metric,
        "operation": operation,
        "promql": expression,
        "service_label": service_label,
        "service_value": service_value,
        "filters": filters,
        "group_by": group_by,
        "window": window,
    }
