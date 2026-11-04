"""P9 wiring tests: global budget middleware (the invoke_structured_chain
fallback wrapper was removed 2026-10-29 — structured output now goes through
langchain with_structured_output directly at each call site)."""
from src.graph.budget import budget_guard, _exhausted
from src.graph.routing import route_after_investigation, route_after_diagnosis
from src.graph.policy import route_after_policy, route_after_critic


def test_budget_guard_blocks_and_charges(monkeypatch):
    calls = []

    def worker(state):
        calls.append(1)
        return {"foo": "bar"}

    monkeypatch.setenv("BUDGET_ENABLED", "1")
    s = {"budget": {"max_iterations_total": 2, "max_tokens_total": 100,
                    "spent_iterations": 2}, "total_tokens_used": 0}
    assert not _exhausted({**s, "budget": {"max_iterations_total": 0, "max_tokens_total": 0,
                                           "spent_iterations": 99}})  # 0 limits = unlimited
    out = budget_guard(worker)(s)
    assert out == {"budget_exhausted": True} and not calls  # blocked, never ran

    s2 = {"budget": {"max_iterations_total": 5, "max_tokens_total": 100,
                     "spent_iterations": 1}, "total_tokens_used": 10}
    out2 = budget_guard(worker)(s2)
    assert out2["foo"] == "bar"
    assert out2["budget"]["spent_iterations"] == 2
    assert calls == [1]


def test_budget_exhausted_routes(monkeypatch):
    monkeypatch.setenv("BUDGET_ENABLED", "1")
    s = {"budget_exhausted": True}
    assert route_after_investigation(s) == "escalate"
    assert route_after_diagnosis(s) == "escalate"
    assert route_after_policy(s) == "escalate"
    assert route_after_critic(s) == "escalate"
