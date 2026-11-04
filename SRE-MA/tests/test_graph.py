"""
live webhook smoke test.

the module used to POST at import time (breaking pytest collection); the
request now runs only when the agent API is actually reachable, and asserts
the webhook contract.
"""

import httpx
import pytest

API = "http://localhost:8080"

payload = {
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {
            # THIS ALERT NAME DOES NOT EXIST IN YOUR ROUTING.PY
            "alertname": "ServiceBehaviourAnomaly",
            "severity": "critical",
            "service": "payment_gateway",
            # NO RUNBOOK LABEL
        },
        "annotations": {
            "summary": "Payment gateway experiencing intermittent timeouts with no upstream degradation"
        }
    }]
}


def test_webhook_smoke():
    """POST one novel-alert webhook; requires the live API (uvicorn src.api.app)."""
    try:
        response = httpx.post(f"{API}/webhook/alert", json=payload, timeout=10.0)
    except httpx.HTTPError:
        pytest.skip(f"agent API not running on {API}")
    assert response.status_code in (200, 202), response.text
    body = response.json()
    # v2 webhook shape: {"created": [incident_id, ...], "count": N, ...}
    assert body.get("created") or body.get("incident_id") or body.get("status"), body
