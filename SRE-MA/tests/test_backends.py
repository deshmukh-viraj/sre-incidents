"""unit tests for the backend adapter layer: templates, bounds, packs."""

import urllib.parse

import pytest

from src.tools.backends.base import BackendConfig
from src.tools.backends.prometheus import PrometheusBackend, METRIC_TEMPLATES
from src.tools.backends.loki import LokiBackend, LOG_CAPABILITIES
from src.tools.backends.tempo import TempoBackend
from src.tools.backends import registry


def _cfg(kind, adapter, url="http://x", max_rows=10, enabled=True):
    return BackendConfig(kind=kind, adapter=adapter, url=url, timeout_s=1.0, max_rows=max_rows, enabled=enabled)


# --- metrics adapter: all dialect lives here ---

def test_prom_templates_are_parameterized():
    b = PrometheusBackend(_cfg("metrics", "prometheus"))
    p99 = b.template("p99_latency", service="payment_gateway", window="5m")
    assert 'service="payment_gateway"' in p99 and "[5m]" in p99
    er = b.template("error_rate", service="account_ledger", window="1m")
    assert 'status_code=~"5.."' in er and "[1m]" in er
    assert b.template("service_up") == "up"


def test_prom_unknown_capability_is_a_named_error():
    b = PrometheusBackend(_cfg("metrics", "prometheus"))
    with pytest.raises(KeyError, match="unknown metric capability"):
        b.template("nope")


def test_prom_capability_translates_and_evaluates(monkeypatch):
    b = PrometheusBackend(_cfg("metrics", "prometheus"))
    seen = {}
    monkeypatch.setattr(b, "instant", lambda expr: seen.setdefault("expr", expr) and 0.42)
    assert b.capability("p99_latency", service="s", window="5m") == 0.42
    assert "histogram_quantile" in seen["expr"]  # caller never sees/authors the dialect


# --- logs adapter ---

def test_loki_selector_and_capabilities():
    b = LokiBackend(_cfg("logs", "loki"))
    assert b.build_selector("payment_gateway") == '{service="payment_gateway"}'
    assert b.build_selector("api_gateway") == '{service="api_gateway"}'
    assert set(LOG_CAPABILITIES) == {"service_errors", "audit_events", "slow_queries"}
    with pytest.raises(KeyError, match="unknown log capability"):
        b.log_capability("nope")


def test_loki_query_clips_to_pack_max_rows(monkeypatch):
    b = LokiBackend(_cfg("logs", "loki", max_rows=3))
    payload = {"data": {"result": [{"values": [[1, '{"level":"ERROR"}']] * 50}]}}
    monkeypatch.setattr("src.tools.backends.loki.bounded_get", lambda url, cfg: payload)
    rows = b.query('{service="x"}', limit=50)
    assert len(rows) == 3  # pack bound beats caller's limit


# --- traces adapter ---

def test_tempo_search_builds_traceql(monkeypatch):
    b = TempoBackend(_cfg("traces", "tempo", max_rows=5))
    captured = {}
    def fake_get(url, cfg):
        captured["url"] = url
        return {"traces": [{"traceID": "abc", "rootServiceName": "payment_gateway",
                            "rootTraceName": "GET /pay", "durationMs": 120,
                            "startTimeUnixNano": 1}]}
    monkeypatch.setattr("src.tools.backends.tempo.bounded_get", fake_get)
    spans = b.search(service="payment_gateway", min_duration_ms=100)
    assert len(spans) == 1 and spans[0]["trace_id"] == "abc"
    q = urllib.parse.unquote(captured["url"])
    assert 'service="payment_gateway"' in q
    assert "duration>=100ms" in q


# --- registry: environment packs ---

def test_default_pack_is_fintech_sim_with_no_traces():
    registry.reset_cache()
    assert registry.active_environment() == "fintech_sim"
    assert registry.get_backend("metrics").url == "http://localhost:9090"
    assert registry.get_backend("logs").url == "http://localhost:3100"
    assert registry.get_backend("traces") is None  # disabled in this pack
    registry.reset_cache()


def test_sandbox_pack_disables_traces_until_a_search_backend_exists(monkeypatch):
    monkeypatch.setenv("SRE_ENVIRONMENT", "sandbox")
    registry.reset_cache()
    assert registry.get_backend("metrics").url == "http://localhost:9091"
    # verified live 2026-11-10: the demo instance runs Jaeger v2, which serves
    # no trace-search route, so the pack keeps traces disabled and honest
    assert registry.get_backend("traces") is None
    registry.reset_cache()


def test_unknown_pack_is_a_named_error(monkeypatch):
    monkeypatch.setenv("SRE_ENVIRONMENT", "production-corp")
    registry.reset_cache()
    with pytest.raises(ValueError, match="unknown environment pack"):
        registry.get_backend("metrics")
    registry.reset_cache()
