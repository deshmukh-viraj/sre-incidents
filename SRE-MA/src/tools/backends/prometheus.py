"""
tools/backends/prometheus.py
----------------------------
prometheus adapter. all promql lives here every expression is either a
capability template below or an agent-authored string validated at the tool
boundary (src/tools/agent_tool.py) before it reaches instant().
"""

import urllib.parse
import json, re
from typing import Dict, List, Optional, Tuple

from src.tools.backends.base import BackendConfig, MetricsBackend, bounded_get, clip

# standard promql templates for the fintech simulator (keyed by service and window)
METRIC_TEMPLATES = {
    "p99_latency": 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) by (le))',
    "p50_latency": 'histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) by (le))',
    "error_rate": 'sum(rate(http_requests_total{{service="{service}",status_code=~"5.."}}[{window}])) / sum(rate(http_requests_total{{service="{service}"}}[{window}]))',
    "throttle_error_rate": 'sum(rate(http_requests_total{{service="{service}",status_code=~"429|504"}}[{window}])) / sum(rate(http_requests_total{{service="{service}"}}[{window}]))',
    "rps": 'sum(rate(http_requests_total{{service="{service}"}}[1m]))',
    "circuit_breaker_state": 'circuit_breaker_state{{service="{service}"}}',
    "slo_budget_remaining": 'min(slo_error_budget_remaining_ratio{{service="{service}"}})',
    "db_pool_utilization": 'db_connection_pool_active{{db="postgres_primary"}} / db_connection_pool_max{{db="postgres_primary"}}',
    "service_memory_bytes": 'process_memory_rss_bytes{{service="{service}"}}',
    "payment_decline_rate": 'sum(rate({metric}{{status="declined"}}[{window}])) / sum(rate({metric}[{window}]))',
    "fraud_model_latency_p99": 'histogram_quantile(0.99, rate({metric}[{window}]))',
    "service_up": "up",
}

#openTelemetry demo templates (uses service_name instead of service)
OTELDEM_METRIC_TEMPLATES = {
    "p99_latency": 'histogram_quantile(0.99, sum(rate(http_server_request_duration_seconds_bucket{{service_name="{service}"}}[{window}])) by (le))',
    "p50_latency": 'histogram_quantile(0.50, sum(rate(http_server_request_duration_seconds_bucket{{service_name="{service}"}}[{window}])) by (le))',
    "error_rate": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}",http_response_status_code=~"5.."}}[{window}])) / sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[{window}]))',
    "throttle_error_rate": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}",http_response_status_code=~"429|504"}}[{window}])) / sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[{window}]))',
    "rps": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[5m]))',
    "flag_variant_evaluations": 'sum by (feature_flag_key, feature_flag_result_variant) (increase(feature_flag_evaluation_success_total{{service_name="{service}"}}[{window}]))',
    "service_up": "up",
}

#default metric names for parameterized templates
DEFAULT_METRIC = {
    "payment_decline_rate": "payment_transactions_total",
    "fraud_model_latency_p99": "fraud_model_prediction_latency_seconds_bucket",
}


class PrometheusBackend(MetricsBackend):

    def _api_data(self, endpoint: str, params=()):
        """call a prometheus metadata API and preserve backend error"""
        query = urllib.parse.urlencode(params)
        url = f"{self.cfg.url.rstrip('/')}/api/v1/{endpoint}"

        if query:
            url = f"{url}?{query}"
        payload = bounded_get(url, self.cfg)
        if payload.get("status") != "success":
            raise RuntimeError(
                f"Prometheus {endpoint} failed: {payload.get('error')}"
            )
        return payload.get("data") or []

    def label_names(self, matchers=(), limit=300):
        params = [("limit", str(limit))]
        params.extend(("match[]", matcher) for matcher in matchers)
        return self._api_data("labels", params)
        
    def label_values(self, label_name, matchers=(), limit=200):
        params = [("limit", str(limit))]
        params.extend(("match[]", matcher) for matcher in matchers)

        endpoint = (
            f"label/{urllib.parse.quote(label_name, safe='')}/values"
        )
        return self._api_data(endpoint, params)

    def metadata(self, metric=None, limit=500):
        params= [
            ("limit", str(limit)),
            ("limit_per_metric", "1"),
        ]
        if metric:
            params.append(("metric", metric))
        return self._api_data("metadata", params)
        
    def series(self, matchers, limit=100):
        if not matchers:
            raise ValueError("at least one series matcher is required")
        params = [("limit", str(limit))]
        params.extend(("match[]", matcher) for matcher in matchers)

        return self._api_data("series", params)
    
    def _query_url(self, endpoint: str, params: dict) -> str:
        """constructs a fully encoded URL for a Prometheus API endpoint."""
        query_string = urllib.parse.urlencode(params)
        return f"{self.cfg.url}/api/v1/{endpoint}?{query_string}"

    def instant(self, expr: str) -> Optional[float]:
        """runs an instant query and returns a single float value, or none if empty/ambiguous."""
        url = self._query_url("query", {"query": expr})
        try:
            data = bounded_get(url, self.cfg)
        except Exception:
            return None

        results = data.get("data", {}).get("result", [])
        if not results:
            print(f"[prometheus] no data for: {expr[:80]}")
            return None

        # if multiple series match, only accept if all series have the identical value
        if len(results) > 1:
            values = {r["value"][1] for r in results}
            if len(values) == 1:
                return float(results[0]["value"][1])
            print(f"[prometheus] AMBIGUOUS: {len(results)} series for: {expr[:80]}")
            return None

        val = float(results[0]["value"][1])
        print(f"[prometheus {expr[:60]} -> {val:.4f}]")
        return val

    def labeled_instant(self, expr: str, cap: int = 8) -> Optional[List[dict]]:
        """runs an instant query returning per-series labels and values, sorted descending."""
        url = self._query_url("query", {"query": expr})
        try:
            data = bounded_get(url, self.cfg)
        except Exception:
            return None

        results = data.get("data", {}).get("result", [])
        if not results:
            print(f"[prometheus] no data for: {expr[:80]}")
            return None

        rows = [
            {"labels": dict(r.get("metric") or {}), "value": float(r["value"][1])}
            for r in results if r.get("value")
        ]
        rows.sort(key=lambda x: x["value"], reverse=True)
        return rows[:cap]

    def range(self, expr: str, start_s: float, end_s: float, step_s: int) -> List[Tuple[float, float]]:
        """queries time series points across a time range [(timestamp, value)]"""
        url = self._query_url("query_range", {
            "query": expr,
            "start": f"{start_s:.3f}",
            "end": f"{end_s:.3f}",
            "step": f"{step_s}s",
        })
        data = bounded_get(url, self.cfg)
        if data.get("status") != "success":
            raise RuntimeError(f"prometheus query_range failed: {data.get('error')}")

        results = data.get("data", {}).get("result", [])
        points = []
        for ts, val in results[0].get("values", []) if results else []:
            try:
                points.append((float(ts), float(val)))
            except (TypeError, ValueError):
                continue

        return clip(points, self.cfg)

    def metric_names(self, prefixes: Optional[Tuple[str, ...]] = None, matchers=(), limit: int = 500) -> List[str]:
        """return live metric names, optionally scoped to matching series"""
        params = [("limit", str(limit))]
        params.extend(("match[]", matcher) for matcher in matchers)

        names = self._api_data("label/__name__/values", params)
        if prefixes:
            names = [n for n in names if n.startswith(prefixes)]
        return names[:limit]

    def template(self, capability: str, **params) -> str:
        """renders a promql template for the requested capability based on the active label style."""
        style = getattr(self.cfg, "label_style", "fintech")
        templates = OTELDEM_METRIC_TEMPLATES if style == "oteldemo" else METRIC_TEMPLATES

        if capability not in templates:
            raise KeyError(f"unknown metric capability '{capability}' for label_style '{style}'. known: {sorted(templates)}")

        metric = params.get("metric") or DEFAULT_METRIC.get(capability, "")
        return templates[capability].format(
            service=params.get("service", ""),
            window=params.get("window", "5m"),
            metric=metric,
        )

    def label_guidance(self) -> str:
        """returns instructions on the label conventions used by this environment for prompts."""
        if getattr(self.cfg, "label_style", "fintech") == "oteldemo":
            return (
                'labels: ALL metrics key on service_name="<service>" (NEVER service=). '
                "http families are http_server_request_duration_seconds_{bucket,count} "
                "with http_response_status_code; rpc families are "
                "rpc_server_call_duration_seconds_* with rpc_response_status_code; "
                "flag evidence is feature_flag_evaluation_success_total"
                "{service_name,feature_flag_key,feature_flag_result_variant}. "
                "Every query MUST filter to the claim's target service; a query "
                "matching multiple series is rejected as ambiguous."
            )
        return (
            'labels: request/histogram metrics key on service="<service>"; '
            'db pool metrics key on db="postgres_primary"; '
            "payment metrics use payment_transactions_total. "
            "Every query MUST filter to the claim's target service; a query "
            "matching multiple series is rejected as ambiguous."
        )
    
    def discover_service(self, service:str, service_label: str = "", service_value: str = "", max_metrics: int = 80,):
        """
        Discover a service's live metrics and diamensional labels.
        does not assume HTTP, RPC, fintech or Otel-specific metric names.
        """
        labels = self.label_names(limit=500)

        def normalize(value):
            value = str(value).lower().rsplit("/", 1)[-1]
            return re.sub(r"[^a-z0-9]", "", value)
        
        matched = []
        candidate_values = {}
        #explicit mapping allows investigator to retry discovery
        # wwhen the service label does not follow a conventional name.
        if service_label or service_value:
            if not service_label or not service_value:
                return {"status": "invalid_service_matcher", "service": service}
            if service_label not in labels:
                return {"status": "unknown_label", "service": service, "available_labels": labels}

            values = self.label_values(service_label, limit=500)

            if service_value not in values:
                return {
                        "status": "unknown_label_value", 
                        "service": service, 
                        "label": service_label,
                        "available_values": values[:50]} 

            matched.append((service_label, service_value))

        else:
            # discover likely service-identifying labels from prometheus
            candidates = [label for label in labels if any(term in label.lower() for term in ("service", "app", "workload", "component", "deployment")) or label.lower() in ("job", "name")]
            for label in candidates[:12]:
                values = self.label_values(label, limit=200)
                candidate_values[label] = values[:20]
                for value in values:
                    if normalize(value) == normalize(service):
                        matched.append((label, value))

        if not matched:
            return {
                "status": "service_label_unmatched",
                "service": service,
                "available_labels": labels,
                "candidate_values": candidate_values,
                "metric_names": [],
            }

        selectors = [
            "{" + f"{label}={json.dumps(value)}" + "}" for label, value in matched
        ]
        metric_names = sorted(set(name for sel in selectors for name in self.metric_names(matchers=[sel], limit=max_metrics)))[:max_metrics]
        service_labels = self.label_names(matchers=selectors, limit=300)
        matching_series = self.series(matchers=selectors, limit=100)
        all_metadata = self.metadata(limit=500)
        rel_metadata = {name: all_metadata.get(name, []) for name in metric_names if name in all_metadata}
        return {
            "status": ("discovered" if metric_names else "service_matched_no_metrics"),
            "service": service,
            "service_matchers": [{"label": label, "value": value} for label, value in matched],
            "metric_names": metric_names,
            "label_names": service_labels,
            "metric_metadata": rel_metadata,
            "matching_series": matching_series[:50]
        }
    
    def execute_query(self, expr:str, limit:int=50) -> dict:
        """
        Execute one instant PromQL query and preserve its outcome.

        Distinguishes successful data, empty results, query errors and backend failures.
        """
        params = urllib.parse.urlencode({"query": expr})
        url = f"{self.cfg.url.rstrip('/')}/api/v1/query?{params}"

        try:
            payload = bounded_get(url, self.cfg)
        except Execution as e:
            return {"status": "backend_unavailable", "error": str(e), "samples": [],}

        if payload.get("status") != "success":
            return {"status": "QUERY_ERROR", "error": payload.get("error", "Prometheus bhai failed"), "samples": []}

        data = payload.get("data") or {}
        result_type = data.get("resultType")
        results = data.get("result") or []

        if not results:
            return {"status": "EMPTY", "result_type": result_type, "samples": [],}
        
        samples = []
        for item in results[:max(1, min(limit, self.cfg.max_rows))]:
            if result_type == "vector":
                value = item.get("value")
                if not value or len(value) < 2:
                    continue

                samples.append({
                    "labels": item.get("metric", {}),
                    "timestamp": value[0],
                    "value": value[1]
                })

            elif result_type == "matrix":
                for ts, value in item.get("values", []):
                    samples.append({
                        "labels": item.get("metric", {}),
                        "timestamp": ts,
                        "value": value
                    })
            else:
                samples.append({"result": item})
        
        return {
            "status": "data" if samples else "empty",
            "result_type": result_type,
            "samples": samples,
        }