"""
tools/backends/prometheus.py
----------------------------
prometheus adapter. all PromQL lives here — every expression is either a
capability template below or an agent-authored string validated at the tool
boundary (src/tools/agent_tool.py) before it reaches instant().
"""

import urllib.parse
from typing import List, Optional, Tuple

from src.tools.backends.base import BackendConfig, MetricsBackend, bounded_get, clip

# per-backend query templates: capability name -> promql.
# params: {service}, {window}; db-pool capabilities are keyed by db label on purpose.
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

# oteldemo (OpenTelemetry demo) conventions: service_name= (NOT service=),
# http_server_request_duration_seconds families, http_response_status_code.
# Mirrors the instance's own alert-rule query shapes so gate math measures
# what the alerts measure.
OTELDEM_METRIC_TEMPLATES = {
    "p99_latency": 'histogram_quantile(0.99, sum(rate(http_server_request_duration_seconds_bucket{{service_name="{service}"}}[{window}])) by (le))',
    "p50_latency": 'histogram_quantile(0.50, sum(rate(http_server_request_duration_seconds_bucket{{service_name="{service}"}}[{window}])) by (le))',
    "error_rate": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}",http_response_status_code=~"5.."}}[{window}])) / sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[{window}]))',
    "throttle_error_rate": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}",http_response_status_code=~"429|504"}}[{window}])) / sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[{window}]))',
    "rps": 'sum(rate(http_server_request_duration_seconds_count{{service_name="{service}"}}[1m]))',
    "flag_variant_evaluations": 'sum by (feature_flag_key, feature_flag_result_variant) (increase(feature_flag_evaluation_success_total{{service_name="{service}"}}[{window}]))',
    "service_up": "up",
}

# default metric family per parameterized template (overridable via the config pack)
DEFAULT_METRIC = {
    "payment_decline_rate": "payment_transactions_total",
    "fraud_model_latency_p99": "fraud_model_prediction_latency_seconds_bucket",
}


class PrometheusBackend(MetricsBackend):

    def instant(self, expr: str) -> Optional[float]:
        url = (
            f"{self.cfg.url}/api/v1/query?"
            f"{urllib.parse.urlencode({'query': expr})}"
        )
        try:
            data = bounded_get(url, self.cfg)
        except Exception:
            return None
        results = data.get("data", {}).get("result", [])
        if not results:
            print(f"[prometheus] no data for: {expr[:80]}")
            return None
        value = float(results[0]["value"][1])
        print(f"[prometheus {expr[:60]} -> {value:.4f}]")
        return value

    def range(
        self, expr: str, start_s: float, end_s: float, step_s: int
    ) -> List[Tuple[float, float]]:
        params = urllib.parse.urlencode(
            {"query": expr, "start": f"{start_s:.3f}", "end": f"{end_s:.3f}", "step": f"{step_s}s"}
        )
        data = bounded_get(f"{self.cfg.url}/api/v1/query_range?{params}", self.cfg)
        if data.get("status") != "success":
            raise RuntimeError(f"prometheus query_range failed: {data.get('error')}")
        results = data.get("data", {}).get("result", [])
        out = []
        for ts, val in results[0].get("values", []) if results else []:
            try:
                out.append((float(ts), float(val)))
            except (TypeError, ValueError):
                continue
        return clip(out, self.cfg)

    def metric_names(self, prefixes: Optional[Tuple[str, ...]] = None) -> Optional[List[str]]:
        try:
            data = bounded_get(f"{self.cfg.url}/api/v1/label/__name__/values", self.cfg)
            names = data.get("data", [])
        except Exception:
            return None
        if prefixes:
            names = [n for n in names if n.startswith(prefixes)]
        return clip(names, self.cfg)

    def template(self, capability: str, **params) -> str:
        templates = (
            OTELDEM_METRIC_TEMPLATES
            if getattr(self.cfg, "label_style", "fintech") == "oteldemo"
            else METRIC_TEMPLATES
        )
        try:
            tpl = templates[capability]
        except KeyError:
            raise KeyError(
                f"unknown metric capability '{capability}' for label_style "
                f"'{getattr(self.cfg, 'label_style', 'fintech')}'. known: {sorted(templates)}"
            )
        metric = params.get("metric") or DEFAULT_METRIC.get(capability, "")
        return tpl.format(**{
            "service": params.get("service", ""),
            "window": params.get("window", "5m"),
            "metric": metric,
        })
