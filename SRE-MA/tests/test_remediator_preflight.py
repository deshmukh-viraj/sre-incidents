"""B5 remediator bounded pre-flight mini-loop (P5): novel path only."""
from src.tools import dispatcher
from src.tools.registry import ToolSpec
from src.agents.remediator_agent import (
    remediator_node, _preflight_validate, _preflight_enabled,
    REMEDIATOR_PREFLIGHT_QUERIES,
)
from src.graph.state import initial_state


def _fake_registry(monkeypatch, value="0.20"):
    specs = {"query_prometheus_metric": ToolSpec(
        name="query_prometheus_metric", description="prom", read_only=True,
        callable=lambda expr: value, timeout_s=5, row_cap=40)}
    monkeypatch.setattr(dispatcher, "REGISTRY", specs)


def _novel_state():
    s = dict(initial_state("INC-PF", {"alert_name": "NovelDegradation", "service": "payment_gateway",
                                      "p99_latency_s": 2.4, "error_rate": 0.12}))
    s.update(affected_services=["payment_gateway"], severity="SEV2",
             diagnosis_mode="deep_agents", requires_retry=True,
             hypotheses=[{"hypothesis": "novel degradation", "evidence": ["p99=2.4"],
                          "confidence": 0.65, "supporting_runbook": None}],
             llm_suggested_action="Enable rate-limiting via feature flag",
             proposed_tool="set_feature_flag", proposed_target="payment_gateway")
    return s


def _rb001_state():
    s = dict(initial_state("INC-PF-RB1", {
        "alert_name": "PaymentGatewayP99LatencyHigh", "runbook": "RB-001",
        "service": "payment_gateway", "p99_latency_s": 2.9, "error_rate": 0.12,
        "throttle_error_rate": 0.08, "log_patterns": {"card_rails_timeout": 3,
                                                      "http_429_count": 140},
    }))
    s.update(affected_services=["payment_gateway"], severity="SEV1",
             diagnosis_mode="deterministic",
             hypotheses=[{"hypothesis": "Card rails throttling causing payment latency spike",
                          "evidence": ["p99=2.9"], "confidence": 0.88,
                          "supporting_runbook": "RB-001"}])
    return s


def test_flag_off_no_validation_calls(monkeypatch):
    monkeypatch.delenv("REMEDIATOR_PREFLIGHT_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    _fake_registry(monkeypatch)
    out = remediator_node(_novel_state())
    assert all("preflight" not in (i.get("worker") or "") for i in out.get("tool_invocations", []))


def test_novel_path_runs_at_most_3_validation_queries(monkeypatch):
    monkeypatch.setenv("REMEDIATOR_PREFLIGHT_ENABLED", "1")
    _fake_registry(monkeypatch, value="0.01")  # healthy readings -> premise gone
    out = remediator_node(_novel_state())
    calls = [i for i in out.get("tool_invocations", []) if i.get("worker") == "remediator-preflight"]
    assert 0 < len(calls) <= REMEDIATOR_PREFLIGHT_QUERIES
    assert out["requires_approval"] is True          # stale premise -> human confirms
    assert out["action_plan"][0]["tool"] == "set_feature_flag"  # plan content unchanged


def test_premise_still_valid_keeps_auto_path(monkeypatch):
    monkeypatch.setenv("REMEDIATOR_PREFLIGHT_ENABLED", "1")
    _fake_registry(monkeypatch, value="2.6")  # still breaching -> premise valid
    out = remediator_node(_novel_state())
    calls = [i for i in out.get("tool_invocations", []) if i.get("worker") == "remediator-preflight"]
    assert calls and all(i["within_threshold"] is False for i in calls)
    assert out["requires_approval"] is False


def test_rb001_plan_byte_identical_with_flag(monkeypatch):
    """done-when: RB-001 (deterministic path) plan byte-identical pre/post flag."""
    _fake_registry(monkeypatch)
    monkeypatch.delenv("REMEDIATOR_PREFLIGHT_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    before = remediator_node(_rb001_state())
    monkeypatch.setenv("REMEDIATOR_PREFLIGHT_ENABLED", "1")
    after = remediator_node(_rb001_state())
    import json
    assert json.dumps(before["action_plan"], sort_keys=True) == \
        json.dumps(after["action_plan"], sort_keys=True)
    assert "tool_invocations" not in after and "message_ledger" not in after


def test_read_only_enforcement_in_loop(monkeypatch):
    monkeypatch.setenv("REMEDIATOR_PREFLIGHT_ENABLED", "1")
    _fake_registry(monkeypatch)
    out = _preflight_validate(_novel_state(), [{"action": "a", "tool": "t", "params": {}}])
    assert all(c["tool"] == "query_prometheus_metric" for c in out["tool_invocations"])
