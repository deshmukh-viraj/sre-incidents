"""
P1 golden-file test: with all roster flags off (MULTI_AGENT=0), the deterministic
spine (severity classification, deterministic diagnosis, routing decisions,
policy validation, llm_diagnoser prompts, deep_investigation prompts) is
byte-identical for the three control scenarios.

Regenerate with:  UPDATE_GOLDEN=1 ./mango/Scripts/python.exe -m pytest tests/test_golden_control_scenarios.py -q
"""
import json
import re
import os
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage, HumanMessage

from src.graph.state import initial_state
from src.graph.routing import (
    classify_severity,
    deterministic_diagnosis,
    route_after_diagnosis,
    route_after_investigation,
    route_after_human_gate,
    route_after_verification,
    P99_LATENCY_CRITICAL, P99_LATENCY_WARNING, P99_LATENCY_RECOVERY,
    ERROR_RATE_CRITICAL, ERROR_RATE_WARNING, THROTTLE_RATE_HIGH,
    DB_POOL_WARNING, DB_POOL_CRITICAL, CIRCUIT_BREAKER_OPEN,
    SLO_BUDGET_LOW, DECLINE_RATE_HIGH, DIAGNOSIS_CONFIDENCE, MAX_DIAGNOSIS_LOOPS,
)
from src.graph.policy import policy_validator_node, route_after_critic, route_after_policy
from src.agents.diagnoser_agent import llm_diagnoser
from src.agents.deep_investigation import deep_investigation_node
from src.agents.schemas import DiagnoserOutput, Hypothesis
from src.agents.remediator_agent import _build_action_plan

GOLDEN = Path(__file__).parent / "golden" / "control_scenarios.json"

# exact control scenario signal fixtures (sim-derived, deterministic)
SCENARIOS = {
    "payment_latency_spike": {
        "alert_name": "PaymentGatewayP99LatencyHigh",
        "runbook": "RB-001",
        "team": "payments",
        "severity": "critical",
        "service": "payment_gateway",
        "alert_started_at": "2026-01-01T00:00:00",
        "p99_latency_s": 2.9,
        "error_rate": 0.12,
        "throttle_error_rate": 0.08,
        "db_pool_utilization": 0.55,
        "circuit_breaker_state": 0.0,
        "rps": 120.0,
        "metrics_ok": True,
        "logs_ok": True,
        "log_patterns": {"card_rails_timeout": 3, "http_429_count": 140},
        "error_log_summaries": ["card_rails timeout after 5000ms (upstream=card_rails)"],
    },
    "cascading_failure": {
        "alert_name": "ServiceBehaviourAnomaly",
        "runbook": None,
        "severity": "critical",
        "service": "payment_gateway",
        "alert_started_at": "2026-01-01T00:00:00",
        "p99_latency_s": 2.2,
        "error_rate": 0.15,
        "throttle_error_rate": 0.01,
        "db_pool_utilization": 0.4,
        "circuit_breaker_state": 2.0,
        "metrics_ok": True,
        "logs_ok": True,
        "log_patterns": {},
        "error_log_summaries": ["circuit breaker open, upstream card_rails failing"],
    },
    "natural_calm_transient_spike": {
        "alert_name": "ServiceBehaviourAnomaly",
        "runbook": None,
        "severity": "warning",
        "service": "payment_gateway",
        "alert_started_at": "2026-01-01T00:00:00",
        "p99_latency_s": 1.2,
        "error_rate": 0.01,
        "metrics_ok": True,
        "logs_ok": False,
        "log_patterns": {},
        "error_log_summaries": [],
    },
}

THRESHOLDS = {
    "P99_LATENCY_CRITICAL": P99_LATENCY_CRITICAL,
    "P99_LATENCY_WARNING": P99_LATENCY_WARNING,
    "P99_LATENCY_RECOVERY": P99_LATENCY_RECOVERY,
    "ERROR_RATE_CRITICAL": ERROR_RATE_CRITICAL,
    "ERROR_RATE_WARNING": ERROR_RATE_WARNING,
    "THROTTLE_RATE_HIGH": THROTTLE_RATE_HIGH,
    "DB_POOL_WARNING": DB_POOL_WARNING,
    "DB_POOL_CRITICAL": DB_POOL_CRITICAL,
    "CIRCUIT_BREAKER_OPEN": CIRCUIT_BREAKER_OPEN,
    "SLO_BUDGET_LOW": SLO_BUDGET_LOW,
    "DECLINE_RATE_HIGH": DECLINE_RATE_HIGH,
    "DIAGNOSIS_CONFIDENCE": DIAGNOSIS_CONFIDENCE,
    "MAX_DIAGNOSIS_LOOPS": MAX_DIAGNOSIS_LOOPS,
}


def _fake_llm_diagnoser(monkeypatch, capture):
    """structured-output fake per R3 pattern shape: include_raw dict contract."""
    class FakeStructured:
        def invoke(self, messages):
            capture.extend(m.content for m in messages)
            parsed = DiagnoserOutput(
                hypotheses=[Hypothesis(hypothesis="canned hypothesis",
                                       evidence=["p99=2.2"], confidence=0.75)],
                root_cause="canned hypothesis",
                diagnosis_summary="canned summary",
                evidence_summary="canned evidence",
                blast_analysis="canned blast",
            )
            raw = SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 20},
                                  response_metadata={})
            return {"parsed": parsed, "raw": raw}

    class FakeLLM:
        def with_structured_output(self, schema, include_raw=False):
            return FakeStructured()

    monkeypatch.setattr("src.agents.diagnoser_agent._get_llm", lambda temperature=0.1: FakeLLM())


def _fake_harness(monkeypatch, capture, escalate):
    """fake create_react_agent harness; escalate=False yields no terminal call."""
    def fake_harness():
        class FakeHarness:
            def invoke(self, inp, config=None):
                msgs = list(inp["messages"])
                capture.append(msgs[0].content)   # the formatted INVESTIGATOR_PROMPT
                capture.append(msgs[1].content)   # the ctx block
                out = list(msgs) + [AIMessage(content="observed signals; narrowing down")]
                if escalate:
                    out.append(AIMessage(content="", tool_calls=[{
                        "name": "escalate_with_reason",
                        "args": {"reason": "insufficient evidence: pool metrics flat"},
                        "id": "call_golden_1", "type": "tool_call",
                    }]))
                return {"messages": out}
        return FakeHarness()

    monkeypatch.setattr("src.agents.deep_investigation.get_harness", fake_harness)

    class FakeFinLLM:
        def bind_tools(self, tools, tool_choice=None):
            return self

        def invoke(self, messages):
            capture.append(f"forced-finalize:{messages[-1].content[:60]}")
            return AIMessage(content="")  # never yields a terminal tool call

    monkeypatch.setattr("src.agents.deep_investigation._get_llm", lambda temperature=0.0: FakeFinLLM())


def _capture_scenario(name, monkeypatch):
    raw = dict(SCENARIOS[name])
    state = initial_state("INC-GOLDEN", raw)
    severity = classify_severity({"raw_signals": raw})

    det = deterministic_diagnosis(state)

    # post-diagnoser routing (as diagnoser_node would set it)
    if det:
        post = {"hypotheses": [det], "diagnosis_mode": det.get("diagnosis_mode"),
                "diagnosis_loops": 1}
    else:
        post = {"hypotheses": [], "diagnosis_mode": "pending_llm", "diagnosis_loops": 1}
    post_state = {**state, **post}
    route_diag = route_after_diagnosis(post_state)

    prompt_capture = []
    llm_delta = None
    deep_delta = None
    deep_prompt_capture = []

    # llm_diagnoser capture (deterministic context: FAISS absent, KG driver None)
    _fake_llm_diagnoser(monkeypatch, prompt_capture)
    llm_state = {**state, "diagnosis_mode": "pending_llm", "hypotheses": [],
                 "diagnosis_loops": 1, "affected_services": [raw["service"]]}
    llm_delta = llm_diagnoser(llm_state)
    route_after_llm = route_after_diagnosis({**llm_state, **llm_delta})

    # deep_investigation capture (novel path)
    deep_capture = []
    _fake_harness(monkeypatch, deep_capture, escalate=(name == "cascading_failure"))
    deep_state = {**state, "diagnosis_mode": "pending_llm", "hypotheses": [],
                  "diagnosis_loops": 1, "affected_services": [raw["service"]]}
    deep_delta = deep_investigation_node(deep_state)

    # policy validation over the runbook plan (RB-001) or empty plan
    plan = _build_action_plan(raw.get("runbook") or "RB-000", state) if det else []
    pol_in = {**state, "action_plan": plan,
              "hypotheses": [det] if det else [],
              "requires_approval": any(a.get("requires_approval") for a in plan)}
    pol_out = policy_validator_node(pol_in)

    # static routes exercised with representative states
    routes = {
        "route_after_diagnosis": route_diag,
        "route_after_llm_diagnoser": route_after_llm,
        "route_after_investigation_pending_llm": route_after_investigation(
            {"diagnosis_mode": "pending_llm"}),
        "route_after_investigation_deep": route_after_investigation(
            {"diagnosis_mode": "deep_agents"}),
        "route_after_critic_accept": route_after_critic(
            {"requires_retry": False, "escalate_reason": None}),
        "route_after_policy": route_after_policy(pol_out),
        "route_after_human_gate_approved": route_after_human_gate({"human_approved": True}),
        "route_after_human_gate_denied": route_after_human_gate({"human_approved": False}),
        "route_after_verification_ok": route_after_verification({"verified": True}),
        "route_after_verification_fail": route_after_verification({"verified": False}),
    }

    return {
        "severity": severity,
        "deterministic_diagnosis": det,
        "routes": routes,
        "policy_output": pol_out,
        "llm_diagnoser_prompts": [_sanitize(c) for c in prompt_capture],
        "llm_diagnoser_delta": {k: v for k, v in llm_delta.items() if k != "hypotheses"},
        "llm_diagnoser_hypotheses": llm_delta.get("hypotheses"),
        "deep_investigation_prompts": [_sanitize(c) for c in deep_capture],
        "deep_investigation_delta": {
            k: v for k, v in deep_delta.items()
            if k not in ("hypotheses",) and k != "investigation_scratchpad"
        } | {"scratchpad_len": len(deep_delta.get("investigation_scratchpad", []))},
    }


def _sanitize(text: str) -> str:
    """strip machine-specific transport noise (neo4j/socket error tracebacks)
    from captured prompts so the golden file is portable across environments."""
    text = re.sub(
        r"Failed to establish connection to ResolvedIPv[46]Address\([^)]*\)[^\\\"]*",
        "[kg-transport-error]", text)
    text = re.sub(r"Failed to resolve '[^']*'", "[dns-resolve-error]", text)
    return text


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True)


def test_golden_control_scenarios(monkeypatch):
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    monkeypatch.delenv("VERIFIER_ENABLED", raising=False)
    monkeypatch.delenv("RECOVERY_CHECKER_ENABLED", raising=False)
    captured = {"thresholds": THRESHOLDS,
                "scenarios": {n: _capture_scenario(n, monkeypatch) for n in SCENARIOS}}
    rendered = _dump(captured)

    if os.getenv("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered + "\n", encoding="utf-8")

    assert GOLDEN.exists(), "golden file missing — run with UPDATE_GOLDEN=1 once"
    expected = GOLDEN.read_text(encoding="utf-8").rstrip("\n")
    assert rendered == expected, (
        "golden mismatch: deterministic spine changed with flags off. "
        "Diff the rendered output against tests/golden/control_scenarios.json "
        "(render with UPDATE_GOLDEN=1 into a scratch path to compare).")
