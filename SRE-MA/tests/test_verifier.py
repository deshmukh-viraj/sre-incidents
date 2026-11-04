"""B1 Verifier unit tests (P2). Flag off = no-op; flag on = typed challenge routing."""
from types import SimpleNamespace

from src.tools import dispatcher
from src.tools.registry import ToolSpec
from src.agents import verifier_agent
from src.agents.verifier_agent import verifier_node, route_after_verifier, verifier_enabled
from src.agents.schemas import VerificationChallenge
from src.graph.routing import route_after_investigation


def _state(**kw):
    base = {
        "incident_id": "INC-V", "alert_name": "PaymentGatewayP99LatencyHigh",
        "severity": "SEV2", "affected_services": ["payment_gateway"],
        "root_cause": "Card rails throttling causing payment latency spike",
        "hypotheses": [{"hypothesis": "Card rails throttling causing payment latency spike",
                        "evidence": ["p99=2.9"], "confidence": 0.88}],
        "verifier_reroutes": 0, "total_tokens_used": 0, "token_cost_usd": 0.0,
    }
    base.update(kw)
    return base


def _fake_registry(monkeypatch):
    specs = {
        "query_prometheus_metric": ToolSpec(
            name="query_prometheus_metric", description="prom", read_only=True,
            callable=lambda expr: 0.01, timeout_s=5, row_cap=40),
        "propose_remediation": ToolSpec(
            name="propose_remediation", description="write", read_only=False,
            callable=lambda **kw: "wrote", timeout_s=5, row_cap=40),
    }
    monkeypatch.setattr(dispatcher, "REGISTRY", specs)


def _fake_llm(monkeypatch, first, second=None, raise_on=None):
    calls = {"n": 0}

    class FakeStructured:
        def __init__(self):
            self.include_raw = None

        def invoke(self, messages):
            calls["n"] += 1
            if raise_on and calls["n"] >= raise_on:
                raise RuntimeError("llm exploded")
            payload = first if calls["n"] == 1 else (second or first)
            raw = SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5},
                                  response_metadata={})
            return {"parsed": payload, "raw": raw}

    class FakeLLM:
        def with_structured_output(self, schema, include_raw=False):
            return FakeStructured()

    monkeypatch.setattr("src.agents.verifier_agent._get_llm", lambda temperature=0.0: FakeLLM())


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("VERIFIER_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    assert verifier_enabled() is False
    assert verifier_node(_state()) == {}
    assert route_after_investigation({"diagnosis_mode": "deep_agents"}) == "critic"


def test_flag_on_routes_to_verifier(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    assert route_after_investigation({"diagnosis_mode": "deep_agents"}) == "verifier"
    assert route_after_investigation({"diagnosis_mode": "pending_llm"}) == "llm_diagnoser"


def test_upheld_goes_to_critic(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    _fake_registry(monkeypatch)
    challenge = VerificationChallenge(claim="c", disproof_queries=["up"],
                                      verdict="upheld", confidence=0.9)
    _fake_llm(monkeypatch, challenge)
    out = verifier_node(_state())
    assert out["verifier_result"]["verdict"] == "upheld"
    assert out["tool_invocations"][0]["tool"] == "query_prometheus_metric"
    assert route_after_verifier({**_state(), "verifier_result": out["verifier_result"],
                                 "verifier_reroutes": out["verifier_reroutes"]}) == "critic"


def test_contradicted_grounded_reroutes_once(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    _fake_registry(monkeypatch)
    challenge = VerificationChallenge(claim="c",
                                      disproof_queries=["up1", "up2", "up3", "up4"],
                                      verdict="contradicted", confidence=0.95)
    _fake_llm(monkeypatch, challenge, challenge)
    state = _state()
    out = verifier_node(state)
    assert out["verifier_result"]["verdict"] == "contradicted"
    assert out["verifier_result"]["grounded"] is True
    assert len(out["tool_invocations"]) == 3          # max 3 disproof queries proposed
    assert out["verifier_reroutes"] == 1              # cap-1 reroute counter set
    s2 = {**state, "verifier_result": out["verifier_result"], "verifier_reroutes": 1}
    assert route_after_verifier(s2) == "deep_investigation"
    # second contradiction (cap reached) -> critic
    out2 = verifier_node({**state, "verifier_reroutes": 1})
    assert out2["verifier_reroutes"] == 1
    assert route_after_verifier({**state, "verifier_result": out2["verifier_result"],
                                 "verifier_reroutes": 1}) == "critic"


def test_contradicted_ungrounded_falls_to_critic(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    specs = {
        "query_prometheus_metric": ToolSpec(
            name="query_prometheus_metric", description="prom", read_only=True,
            callable=lambda expr: "ERROR: no data", timeout_s=5, row_cap=40),
    }
    monkeypatch.setattr(dispatcher, "REGISTRY", specs)
    challenge = VerificationChallenge(claim="c", disproof_queries=["q"],
                                      verdict="contradicted", confidence=0.9)
    _fake_llm(monkeypatch, challenge, challenge)
    out = verifier_node(_state())
    assert out["verifier_result"]["grounded"] is False
    assert out["verifier_reroutes"] == 0
    assert route_after_verifier({**_state(), "verifier_result": out["verifier_result"],
                                 "verifier_reroutes": 0}) == "critic"


def test_fail_open_on_llm_error(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    _fake_registry(monkeypatch)
    _fake_llm(monkeypatch, None, raise_on=1)
    out = verifier_node(_state())
    assert out["verifier_result"]["verdict"] == "unverifiable"
    assert route_after_verifier({**_state(), "verifier_result": out["verifier_result"]}) == "critic"


def test_verifier_uses_read_only_dispatcher(monkeypatch):
    """grep-proof in behavior: a disproof 'query' naming a write tool still goes
    through query_prometheus_metric (read-only) — write tools unreachable."""
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    _fake_registry(monkeypatch)
    seen = {}

    orig = dispatcher.run_tool

    def spy(name, args=None, **kw):
        seen["name"] = name
        return orig(name, args, **kw)

    monkeypatch.setattr(verifier_agent.dispatcher, "run_tool", spy)
    challenge = VerificationChallenge(claim="c", disproof_queries=["x"],
                                      verdict="upheld", confidence=0.8)
    _fake_llm(monkeypatch, challenge, challenge)
    verifier_node(_state())
    assert seen["name"] == "query_prometheus_metric"


def test_no_scenario_identity_in_prompts(monkeypatch):
    """R7: active_scenario / __sim_scenario must never reach verifier prompts."""
    monkeypatch.setenv("VERIFIER_ENABLED", "1")
    _fake_registry(monkeypatch)
    challenge = VerificationChallenge(claim="c", disproof_queries=["q"],
                                      verdict="upheld", confidence=0.8)
    _fake_llm(monkeypatch, challenge, challenge)
    state = _state(active_scenario="payment_latency_spike")  # leaked label present
    verifier_node(state)
    # verdict path never reads active_scenario; prove the claim/evidence strings
    # contain no scenario identity
    from src.agents.verifier_agent import _claim_of, _evidence_of
    assert "payment_latency_spike" not in (_claim_of(state) + _evidence_of(state))
