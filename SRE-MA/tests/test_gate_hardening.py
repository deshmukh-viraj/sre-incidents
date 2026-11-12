"""self-checks for the trace-6739 hardening: verifier scoping/grounding,
fail-closed gates, critic error-citation filter, preflight fail-closed."""

from src.graph.policy import policy_validator_node
from src.tools.backends import prometheus as prom_mod


def _plan_state(verifier_result, target="frontend"):
    return {
        "incident_id": "INC-H",
        "requires_approval": False,
        "executed_action_keys": [],
        "hypotheses": [{"confidence": 0.9, "evidence": [
            'http_server_request_duration_seconds_bucket{service_name="frontend"} -> 10.0',
            "backend=opensearch x9 [ERROR] cart checkout timeout"]}],
        "affected_services": [target],
        "diagnosis_mode": "deep_agents",
        "action_plan": [{"action": "restart frontend", "tool": "rolling_restart",
                         "params": {"service": target}, "blast_radius": "service",
                         "reversible": True, "requires_approval": False,
                         "executed": False, "result": None}],
        "verifier_result": verifier_result,
    }


def test_unverifiable_verdict_strips_the_plan():
    out = policy_validator_node(_plan_state(
        {"verdict": "unverifiable", "grounded": False, "claim": "x"}))
    assert out["plan_viable"] is False
    assert out["action_plan"] == []
    assert any("verifier could not confirm" in r["reason"] for r in out["policy_rejections"])


def test_contradicted_but_grounded_goes_to_human_not_strip():
    out = policy_validator_node(_plan_state(
        {"verdict": "contradicted", "grounded": True, "claim": "x"}))
    assert out["plan_viable"] is True
    assert out["requires_approval"] is True


def test_instant_rejects_ambiguous_multi_series(monkeypatch):
    def fake_bounded_get(url, cfg):
        return {"data": {"result": [
            {"value": [0, "0.02"]}, {"value": [0, "9.8"]}]}}
    monkeypatch.setattr(prom_mod, "bounded_get", fake_bounded_get)
    backend = prom_mod.PrometheusBackend(prom_mod.BackendConfig(
        kind="metrics", adapter="prometheus", url="http://x"))
    assert backend.instant('sum by (le, service_name)(rate(x[5m]))') is None


def test_instant_accepts_single_and_agreeing_series(monkeypatch):
    backend = prom_mod.PrometheusBackend(prom_mod.BackendConfig(
        kind="metrics", adapter="prometheus", url="http://x"))
    monkeypatch.setattr(prom_mod, "bounded_get",
                        lambda url, cfg: {"data": {"result": [{"value": [0, "0.02"]}]}})
    assert backend.instant("x") == 0.02
    monkeypatch.setattr(prom_mod, "bounded_get",
                        lambda url, cfg: {"data": {"result": [
                            {"value": [0, "0.02"]}, {"value": [1, "0.02"]}]}})
    assert backend.instant("x") == 0.02


def test_critic_rejects_error_text_citations():
    from src.agents.critic_agent import critic_node
    st = {"incident_id": "INC-C", "severity": "SEV1",
          "affected_services": ["frontend"],
          "hypotheses": [{"confidence": 0.65, "evidence": [
              "ERROR: Query 'x' returned no data",
              "Unknown metric: frontend_outbound_calls_total",
              "AMBIGUOUS: 12 series matched"]}],
          "action_plan": [], "proposed_target": "frontend",
          "proposed_tool": "rolling_restart",
          "raw_signals": {}}
    out = critic_node(st)
    joined = json.dumps(out["diagnosis_issues"]) if isinstance(
        out.get("diagnosis_issues"), list) else str(out)
    assert "query errors" in joined or "ungrounded" in joined


import json  # noqa: E402  (kept at bottom: used only by the critic test above)
