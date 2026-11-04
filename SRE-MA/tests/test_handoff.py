"""B7 handoff package + Command(resume=...) HITL resume (P7)."""
import inspect
from types import SimpleNamespace

from src.agents import execution_agent
from src.agents import escalation_agent
from src.agents.escalation_agent import escalate_node, route_after_escalate
from src.agents.execution_agent import human_gate_node
from src.graph.state import initial_state, ResolutionStatus


def _exhausted_state():
    s = dict(initial_state("INC-HO", {
        "alert_name": "NovelDegradation", "service": "payment_gateway",
        "severity": "critical", "alert_started_at": "2026-01-01T00:00:00",
    }))
    s.update(severity="SEV2", diagnosis_mode="llm", diagnosis_loops=3,
             budget_exhausted=True, hypotheses=[])
    return s


def test_flag_off_no_handoff(monkeypatch):
    monkeypatch.delenv("HANDOFF_ENABLED", raising=False)
    monkeypatch.delenv("MULTI_AGENT", raising=False)
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)
    out = escalate_node(_exhausted_state())
    assert "handoff_to_human" not in out
    assert route_after_escalate({**_exhausted_state(), **out}) == "communicator"


def test_budget_exhausted_builds_package_and_lands_in_human_gate(monkeypatch):
    monkeypatch.setenv("HANDOFF_ENABLED", "1")
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)
    out = escalate_node(_exhausted_state())
    pkg = out["handoff_to_human"]
    assert pkg["cause"] == "budget_exhausted"
    assert pkg["reason"] and len(pkg["options"]) >= 2
    # budget-exhausted run lands in human_gate carrying options
    assert route_after_escalate({**_exhausted_state(), **out}) == "human_gate"


def test_loop_exhaustion_triggers_package(monkeypatch):
    monkeypatch.setenv("HANDOFF_ENABLED", "1")
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)
    s = _exhausted_state()
    s["budget_exhausted"] = False
    out = escalate_node(s)
    assert out["handoff_to_human"]["cause"] == "diagnosis_loops_exhausted"


def test_non_exhaustion_escalation_has_no_package(monkeypatch):
    monkeypatch.setenv("HANDOFF_ENABLED", "1")
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)
    s = _exhausted_state()
    s.update(budget_exhausted=False, diagnosis_loops=1, critic_reject_count=0,
             verified=False, action_taken="rollback_deployment")
    out = escalate_node(s)
    assert "handoff_to_human" not in out


def test_human_gate_transfers_ownership_once(monkeypatch):
    monkeypatch.setenv("HANDOFF_ENABLED", "1")
    decisions = []

    def fake_interrupt(payload):
        decisions.append(payload)
        return {"approved": True, "approver": "on-call-1", "option": "approve: execute the suggested plan"}

    monkeypatch.setattr(execution_agent, "interrupt", fake_interrupt)
    monkeypatch.setattr("src.tools.sre_tool.DRY_RUN", True)
    pkg = {"timestamp": "t", "from_worker": "escalate", "to_worker": None,
           "kind": "handoff", "content": "msg", "options": ["a", "b"],
           "reason": "budget_exhausted", "cause": "budget_exhausted"}
    s = _exhausted_state()
    s["handoff_to_human"] = pkg
    out = human_gate_node(s)
    assert out["human_approved"] is True
    assert out["handoff_count"] == 1 and out["ownership"] == ["human"]
    assert out["handoff_to_human"]["presented"] is True
    # interrupt payload carried the options to the human
    assert decisions[0]["options"] == ["a", "b"]
    # presented package never re-prompts (no loop): escalate rebuild skipped
    out2 = escalate_node({**s, **out})
    assert "handoff_to_human" not in out2 or out2["handoff_to_human"] is None
    assert route_after_escalate({**s, "handoff_to_human": out["handoff_to_human"],
                                 "handoff_count": 1}) == "communicator"


def test_approve_endpoint_uses_command_resume():
    """done-when: approve endpoint resumes via Command(resume=...)."""
    from src.api import app as app_mod
    src = inspect.getsource(app_mod._resume_agent)
    assert "Command(resume=resume_payload)" in src
    src2 = inspect.getsource(app_mod.approve_incident)
    assert "resume_payload = {\"approved\": req.approved" in src2
    assert "agent_graph.update_state" not in src2
