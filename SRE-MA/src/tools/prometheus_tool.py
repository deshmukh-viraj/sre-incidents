"""
tools/prometheus_tool.py
------------------------
ask prometheus for metrics.
we put all the promql queries here so the agents don't have to worry about them.
"""

import os
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")


def _instant_query(promql: str) -> Optional[float]:
    """run a quick promql query and just get the number back."""
    try:
        url = f"{PROMETHEUS_URL}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("data", {}).get("result", [])
            if not results:
                print(f"[prometheus] no data for: {promql[:80]}")
                return None
            value = float(results[0]["value"][1])
            print(f"[prometheus {promql[:60]} -> {value:.4f}]")
            return value
    except Exception as e:
        print(f"[prometheus_tool] query failed: {promql!r} -> {e}")
        return None


def collect_incident_signals(service: str, window: str = "5m") -> Dict[str, Any]:
    """
    grabs all the important metrics for a service in one go.
    returns a dict so the detector agent has an easy time.
    """
    #one-line queries in a dict replacing 10 wrapper functions.
    signals: Dict[str, Any] = {
        "service": service,
        "p99_latency_s": _instant_query(f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) by (le))'),
        "p50_latency_s": _instant_query(f'histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) by (le))'),
        "error_rate": _instant_query(f'sum(rate(http_requests_total{{service="{service}",status_code=~"5.."}}[{window}])) / sum(rate(http_requests_total{{service="{service}"}}[{window}]))'),
        "throttle_error_rate": _instant_query(f'sum(rate(http_requests_total{{service="{service}",status_code=~"429|504"}}[{window}])) / sum(rate(http_requests_total{{service="{service}"}}[{window}]))'),
        "rps": _instant_query(f'sum(rate(http_requests_total{{service="{service}"}}[1m]))'),
        "circuit_breaker_state": _instant_query(f'circuit_breaker_state{{service="{service}"}}'),
        "slo_budget_remaining": _instant_query(f'min(slo_error_budget_remaining_ratio{{service="{service}"}})'),
        "db_pool_utilization": _instant_query(f'db_connection_pool_active{{db="postgres_primary"}} / db_connection_pool_max{{db="postgres_primary"}}'),
        "service_memory_mb": round(val / 1_000_000, 1) if (val := _instant_query(f'process_memory_rss_bytes{{service="{service}"}}')) else None,
    }

    if service in ("payment_gateway", "api_gateway"):
        pay_window = window if window != "5m" else "10m"
        signals["payment_decline_rate"] = _instant_query(f'sum(rate(payment_transactions_total{{status="declined"}}[{pay_window}])) / sum(rate(payment_transactions_total[{pay_window}]))')
        signals["fraud_model_latency_p99"] = _instant_query(f'histogram_quantile(0.99, rate(fraud_model_prediction_latency_seconds_bucket[{window}]))')

    return signals