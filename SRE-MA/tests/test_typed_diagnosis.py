"""B4 typed HypothesisReport emission (P4): flag on = typed + prompt_versions;
flag off = byte-identical legacy emission."""
from types import SimpleNamespace

from src.agents import diagnoser_agent
from src.agents.schemas import DiagnoserOutput, Hypothesis, HypothesisReport
from src.graph.state import initial_state


def _fake_llm(monkeypatch):
    class FakeStructured:
        def invoke(self, messages):
            parsed = DiagnoserOutput(
                hypotheses=[Hypothesis(hypothesis="DB pool exhaustion",
                                       evidence=["db_pool=0.86"], confidence=0.8)],
                root_cause="DB pool exhaustion",
                diagnosis_summary="s", evidence_summary="e", blast_analysis="b",
            )
            raw = SimpleNamespace(usage_metadata={"input_tokens": 50, "output_tokens": 10},
                                  response_metadata={})
            return {"parsed": parsed, "raw": raw}

    class FakeLLM:
        def with_structured_output(self, schema, include_raw=False):
            return FakeStructured()

    monkeypatch.setattr("src.agents.diagnoser_agent._get_llm", lambda temperature=0.1: FakeLLM())


def _state():
    s = dict(initial_state("INC-TD", {
        "alert_name": "DBConnectionPoolExhaustion", "runbook": "RB-004",
        "service": "account_ledger", "p99_latency_s": 2.0, "error_rate": 0.2,
    }))
    s.update(affected_services=["account_ledger"], severity="SEV2",
             diagnosis_mode="pending_llm", hypotheses=[], diagnosis_loops=1)
    return s


def test_hypothesis_report_schema_accepts_diagnoser_output():
    r = HypothesisReport(hypothesis="x", evidence=["e"], confidence=0.5,
                         diagnosis_mode="llm", source="llm_diagnoser")
    assert r.model_dump()["source"] == "llm_diagnoser"


def test_hypothesis_report_rejects_out_of_range_confidence():
    import pydantic
    try:
        HypothesisReport(hypothesis="x", evidence=[], confidence=1.5)
        assert False, "should have raised"
    except pydantic.ValidationError:
        pass


def test_flag_off_emission_unchanged(monkeypatch):
    monkeypatch.delenv("TYPED_DIAGNOSIS_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    _fake_llm(monkeypatch)
    out = diagnoser_agent.llm_diagnoser(_state())
    assert "source" not in out["hypotheses"][0]
    assert "prompt_versions" not in out


def test_flag_on_emits_typed_reports_and_prompt_version(monkeypatch):
    monkeypatch.setenv("TYPED_DIAGNOSIS_ENABLED", "1")
    _fake_llm(monkeypatch)
    out = diagnoser_agent.llm_diagnoser(_state())
    h = out["hypotheses"][0]
    assert h["source"] == "llm_diagnoser" and h["diagnosis_mode"] == "llm"
    assert out["prompt_versions"] == {"llm_diagnoser": "llm-diagnoser-v1"}
    # routing math untouched: same confidence, same root-cause gating
    assert h["confidence"] == 0.8 and out["root_cause"] == "DB pool exhaustion"
