"""B8 communicator consumes receipts/handoff (P8) — agent-authored final report."""
from src.agents import communicator_agent
from src.graph.state import initial_state


def _state():
    s = dict(initial_state("INC-C8", {"alert_name": "A", "service": "s",
                                      "severity": "SEV2", "alert_started_at": "t"}))
    s.update(severity="SEV2", resolution_status="resolved")
    return s


def test_flag_off_no_facts_block():
    assert communicator_agent._facts_block(_state()) == ""


def test_facts_block_from_receipts_and_handoff(monkeypatch):
    monkeypatch.setenv("COMMS_ENABLED", "1")
    s = _state()
    s["receipts"] = [{"action": "scale_deployment", "tool": "kubectl", "success": True,
                      "dry_run": True}]
    s["handoff_to_human"] = {"presented": True, "cause": "budget_exhausted",
                             "decision": "approved"}
    block = communicator_agent._facts_block(s)
    assert "scale_deployment via kubectl: OK (dry-run)" in block
    assert "cause=budget_exhausted, decision=approved" in block


def test_prompt_carries_facts_and_fallback_includes_them(monkeypatch):
    monkeypatch.setenv("COMMS_ENABLED", "1")
    s = _state()
    s["receipts"] = [{"action": "set_feature_flag", "tool": "sre_tool",
                      "success": True, "dry_run": True}]

    captured = {}

    class _Raw:
        usage_metadata = {"input_tokens": 1, "output_tokens": 1}
        response_metadata = {}

    class _FakeStructured:
        def invoke(self, msgs):
            captured["prompt"] = msgs[0].content
            raise RuntimeError("force fallback")

    class _FakeLLM:
        def with_structured_output(self, *a, **k):
            return _FakeStructured()

    monkeypatch.setattr(communicator_agent, "_get_llm", lambda **k: _FakeLLM())
    out = communicator_agent.communicator_node(s)
    # agent-authored template: prompt carried the authoritative facts...
    assert "Execution facts" in captured["prompt"]
    assert "set_feature_flag via sre_tool: OK (dry-run)" in captured["prompt"]
    # ...and the fallback report still contains them (no fabricated success)
    assert "set_feature_flag" in out["war_room_summary"]
