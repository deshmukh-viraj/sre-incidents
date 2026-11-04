"""
tools/prometheus_tool.py
------------------------
metrics capability facade. query translation lives in the backend adapter
(src/tools/backends/prometheus.py); this module maps capability requests
to values and keeps the historical function signatures used across the repo.
"""

import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("SRE_ENVIRONMENT", os.getenv("SRE_ENVIRONMENT", "fintech_sim"))

from src.tools.backends.registry import get_backend  # noqa: E402

_PROMETHEUS_URL = None  # resolved lazily from the active pack


def prometheus_url() -> str:
    global _PROMETHEUS_URL
    if _PROMETHEUS_URL is None:
        backend = get_backend("metrics")
        _PROMETHEUS_URL = backend.url if backend else os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    return _PROMETHEUS_URL


def _instant_query(promql: str) -> Optional[float]:
    """run a quick metric query and just get the number back."""
    backend = get_backend("metrics")
    if backend is None:
        return None
    return backend.instant(promql)


def collect_incident_signals(service: str, window: str = "5m") -> Dict[str, Any]:
    """
    grabs all the important metrics for a service in one go.
    returns a dict so the detector agent has an easy time.
    capability exprs come from the adapter's template registry; execution goes
    through _instant_query so callers/tests can intercept at one seam.
    """
    backend = get_backend("metrics")
    if backend is None:
        return {"service": service}

    def q(capability: str, **params) -> Optional[float]:
        return _instant_query(backend.template(capability, service=service, window=window, **params))

    signals: Dict[str, Any] = {
        "service": service,
        "p99_latency_s": q("p99_latency"),
        "p50_latency_s": q("p50_latency"),
        "error_rate": q("error_rate"),
        "throttle_error_rate": q("throttle_error_rate"),
        "rps": q("rps"),
        "circuit_breaker_state": q("circuit_breaker_state"),
        "slo_budget_remaining": q("slo_budget_remaining"),
        "db_pool_utilization": q("db_pool_utilization"),
        "service_memory_mb": round(val / 1_000_000, 1) if (val := q("service_memory_bytes")) else None,
    }

    # P10: gateway special case + payment metric names from the config pack
    from src.config.loader import config_pack
    pack = config_pack()
    gateways = pack.get("gateway_services") or ("payment_gateway", "api_gateway")
    pay_metrics = pack.get("payment_metrics") or {}

    if service in gateways:
        pay_window = window if window != "5m" else "10m"
        signals["payment_decline_rate"] = _instant_query(backend.template(
            "payment_decline_rate", service=service, window=pay_window,
            metric=pay_metrics.get("decline_counter")))
        signals["fraud_model_latency_p99"] = _instant_query(backend.template(
            "fraud_model_latency_p99", service=service, window=window,
            metric=pay_metrics.get("fraud_latency_histogram")))

    return signals
