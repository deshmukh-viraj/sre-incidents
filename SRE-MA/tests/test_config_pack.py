"""P10 TASK D: fintech config pack (CONFIG_PACK_ENABLED, default 0)."""
from src.config import loader
from src.graph import routing


def test_flag_off_pack_empty():
    assert loader.config_pack() == {}  # cached; env was off during import


def test_flag_on_pack_overrides_thresholds(monkeypatch):
    monkeypatch.setenv("CONFIG_PACK_ENABLED", "1")
    loader.config_pack.cache_clear()
    routing.MAX_DIAGNOSIS_LOOPS = 3  # reset in case another test changed it
    pack = loader.config_pack()
    assert pack["thresholds"]["P99_LATENCY_CRITICAL"] == 3.0
    assert pack["gateway_services"] == ["payment_gateway", "api_gateway"]
    # apply reads the pack: change a value and re-apply
    monkeypatch.setattr(loader, "config_pack", lambda: {
        "thresholds": {"P99_LATENCY_CRITICAL": 9.9, "MAX_DIAGNOSIS_LOOPS": 7}})
    routing._apply_config_pack()
    try:
        assert routing.P99_LATENCY_CRITICAL == 9.9
        assert routing.MAX_DIAGNOSIS_LOOPS == 7
    finally:
        routing.P99_LATENCY_CRITICAL = 3.0
        routing.MAX_DIAGNOSIS_LOOPS = 3


def test_service_alias_override(monkeypatch):
    monkeypatch.setattr(loader, "config_pack", lambda: {
        "service_aliases": {"PaymentGateway": "pg_override"}})
    assert routing.infer_service("PaymentGatewayAlert") == "pg_override"
    # an unmatched alert keeps the pre-change default (payment_gateway)
    assert routing.infer_service("TotallyUnknownAlert") == "payment_gateway"


def test_service_alias_default_when_pack_empty(monkeypatch):
    monkeypatch.setattr(loader, "config_pack", lambda: {})
    assert routing.infer_service("PaymentGatewayAlert") == "payment_gateway"
    assert routing.infer_service("FraudModelAlert") == "fraud_detector"


def test_gateway_metrics_use_pack_names(monkeypatch):
    import src.tools.prometheus_tool as pt
    captured = []

    def fake_query(expr, *a, **k):
        captured.append(expr)
        return 0

    monkeypatch.setattr(pt, "_instant_query", fake_query)
    monkeypatch.setattr(loader, "config_pack", lambda: {
        "gateway_services": ["svc_custom"],
        "payment_metrics": {"decline_counter": "my_declines_total",
                            "fraud_latency_histogram": "my_fraud_bucket"}})
    pt.collect_incident_signals("svc_custom", window="5m")
    joined = " | ".join(captured)
    assert "my_declines_total" in joined
    assert "my_fraud_bucket" in joined
    assert "payment_transactions_total" not in joined
