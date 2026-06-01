"""
tools/prometheus_tool.py
------------------------
Queries the Prometheus HTTP API.
All metric values agents need are fetched through this single module.
No PromQL is hardcoded in agent files — all queries live here.
"""

import os
import httpx
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")


# ── Low-level query helpers ────────────────────────────────────────────────

def _instant_query(promql: str) -> Optional[float]:
    """
    Execute an instant PromQL query and return the first scalar result.
    Returns None if the query returns no data.
    """
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            print(f"[prometheus] no data for: {promql[:80]}")
            return None
        value= float(results[0]["value"][1])
        print(f"[prometheus {promql[:60]} -> {value:.4f}]")
        return value
    except Exception as e:
        print(f"[prometheus_tool] query failed: {promql!r} -> {e}")
        return None


def _range_query(promql: str, duration: str = "5m") -> list:
    """
    Execute a range query and return the raw result list.
    duration: lookback window, e.g. '5m', '1h'
    """
    import time
    end   = int(time.time())
    start = end - _parse_duration(duration)
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": "15"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])
    except Exception as e:
        print(f"[prometheus_tool] range query failed: {e}")
        return []


def _parse_duration(d: str) -> int:
    """Convert '5m', '1h', '30s' → seconds."""
    unit  = d[-1]
    value = int(d[:-1])
    return {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 60) * value


# ── Named metric queries (used by agents) ─────────────────────────────────

def get_p99_latency(service: str, window: str = "5m") -> Optional[float]:
    """Returns p99 latency in seconds for a given service."""
    return _instant_query(
        f'histogram_quantile(0.99, '
        f'sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) '
        f'by (le))'
    )


def get_p50_latency(service: str, window: str = "5m") -> Optional[float]:
    """Returns p50 (median) latency in seconds."""
    return _instant_query(
        f'histogram_quantile(0.50, '
        f'sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[{window}])) '
        f'by (le))'
    )


def get_error_rate(service: str, window: str = "5m") -> Optional[float]:
    """Returns 5xx error rate as a ratio (0.0 : 1.0)."""
    return _instant_query(
        f'sum(rate(http_requests_total{{service="{service}",status_code=~"5.."}}[{window}])) '
        f'/ sum(rate(http_requests_total{{service="{service}"}}[{window}]))'
    )


def get_throttle_error_rate(service: str, window: str = "5m") -> Optional[float]:
    """Returns 429+504 rate — confirms card_rails throttling (RB-001)."""
    return _instant_query(
        f'sum(rate(http_requests_total{{service="{service}",status_code=~"429|504"}}[{window}])) '
        f'/ sum(rate(http_requests_total{{service="{service}"}}[{window}]))'
    )


def get_db_pool_utilization(db: str = "postgres_primary") -> Optional[float]:
    """Returns DB connection pool utilization as ratio (0.0: 1.0)."""
    return _instant_query(
        f'db_connection_pool_active{{db="{db}"}} / db_connection_pool_max{{db="{db}"}}'
    )


def get_circuit_breaker_state(service: str) -> Optional[float]:
    """
    returns circuit breaker state: 0=closed, 1=half-open, 2=open.
    any value > 1 means the CB is OPEN (bad).
    """
    return _instant_query(
        f'circuit_breaker_state{{service="{service}"}}'
    )


def get_slo_budget_remaining(service: str) -> Optional[float]:
    """Returns remaining SLO error budget as ratio (0.0 : 1.0)."""
    return _instant_query(
        f'min(slo_error_budget_remaining_ratio{{service="{service}"}})'
    )


def get_payment_decline_rate(window: str = "10m") -> Optional[float]:
    """Returns payment decline rate — used for fraud model checks (RB-006)."""
    return _instant_query(
        f'sum(rate(payment_transactions_total{{status="declined"}}[{window}]))'
        f'/ sum(rate(payment_transactions_total[{window}]))'
    )


def get_fraud_model_latency_p99(window: str = "5m") -> Optional[float]:
    """Returns fraud model inference p99 latency in seconds (RB-006)."""
    return _instant_query(
        f'histogram_quantile(0.99, rate(fraud_model_prediction_latency_seconds_bucket[{window}]))'
    )


def get_service_memory_mb(service: str) -> Optional[float]:
    """Returns service memory RSS in MB."""
    val = _instant_query(f'process_memory_rss_bytes{{service="{service}"}}')
    return round(val / 1_000_000, 1) if val else None


def get_rps(service: str, window: str = "1m") -> Optional[float]:
    """Returns current requests per second for a service."""
    return _instant_query(
        f'sum(rate(http_requests_total{{service="{service}"}}[{window}]))'
    )


# ── Snapshot: collect all key signals for a service at once ───────────────

def collect_incident_signals(service: str) -> Dict[str, Any]:
    """
    Called by detector_agent to gather a full metric snapshot.
    Returns a dict with all relevant signals — no PromQL in agent code.
    """
    signals: Dict[str, Any] = {
        "service": service,
        "p99_latency_s": get_p99_latency(service),
        "p50_latency_s": get_p50_latency(service),
        "error_rate": get_error_rate(service),
        "throttle_error_rate": get_throttle_error_rate(service),
        "rps": get_rps(service),
        "circuit_breaker_state": get_circuit_breaker_state(service),
        "slo_budget_remaining": get_slo_budget_remaining(service),
        "db_pool_utilization": get_db_pool_utilization(),
        "service_memory_mb": get_service_memory_mb(service),
    }

    # Add payment-specific signals
    if service in ("payment_gateway", "api_gateway"):
        signals["payment_decline_rate"] = get_payment_decline_rate()
        signals["fraud_model_latency_p99"] = get_fraud_model_latency_p99()

    return signals