"""
test_orchestrator_fixes.py — P0-1 / P1-4 / P1-5 regression tests (work order: fix SRE incident orchestrator)

no docker, no LLM, no neo4j. pure state + config assertions.
"""
import sys
import inspect
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.graph.policy import policy_validator_node, ALLOWED_WRITE_TOOLS, APPROVED_KUBECTL_ACTIONS


def _state(**kw):
    base = {
        "incident_id": "test-fix-1",
        "executed_action_keys": [],
        "hypotheses": [{"hypothesis": "h", "confidence": 0.9, "evidence": [], "alternative": None, "supporting_runbook": None}],
        "requires_approval": False,
        "action_plan": [],
    }
    base.update(kw)
    return base


# P0-1: escalation and approved kubectl tools pass the whitelist
def test_escalation_tools_pass_policy():
    for tool in ("escalate", "escalate_with_reason"):
        s = _state(action_plan=[{"action": "a", "tool": tool, "params": {}}])
        out = policy_validator_node(s)
        assert out["plan_viable"] is True, tool
        assert out["action_plan"], tool

def test_approved_kubectl_actions_pass_policy():
    for tool in sorted(APPROVED_KUBECTL_ACTIONS):
        s = _state(action_plan=[{"action": "a", "tool": tool, "params": {"service": "payment_gateway"}}])
        out = policy_validator_node(s)
        assert out["plan_viable"] is True, tool

def test_unknown_tool_still_rejected():
    s = _state(action_plan=[{"action": "a", "tool": "rm_rf_slash", "params": {}}])
    out = policy_validator_node(s)
    assert out["plan_viable"] is False
    assert out["policy_rejections"], "rejection reason must be recorded, never discarded"


# P1-4: escalation message carries the finding, proposal, and rejections
def test_escalation_message_is_specific():
    from src.agents.escalation_agent import escalate_node
    s = {
        "incident_id": "INC-X",
        "severity": "SEV1",
        "alert_name": "SLOBurnUnknown",
        "root_cause": "DB pool pressure on postgres_primary",
        "llm_suggested_action": "restart account_ledger",
        "policy_rejections": [{"action": "a", "reason": "tool 'x' not in allowed list"}],
        "hypotheses": [],
    }
    with patch("src.agents.escalation_agent.notify_slack"):
        out = escalate_node(s)
    msg = out["escalation_message"]
    assert "required manual investigation" not in msg
    assert "DB pool pressure" in msg
    assert "restart account_ledger" in msg
    assert "not in allowed list" in msg

def test_escalation_message_without_evidence_still_actionable():
    from src.agents.escalation_agent import escalate_node
    s = {"incident_id": "INC-Y", "severity": "SEV2", "alert_name": "A", "hypotheses": []}
    with patch("src.agents.escalation_agent.notify_slack"):
        out = escalate_node(s)
    assert "no diagnosis reached" in out["escalation_message"]


# P1-5: client config — streaming, timeout, retries, no import-time LLM calls
def test_chatopenai_config():
    from src.agents import utils
    src = inspect.getsource(utils._create_llm)
    assert "streaming=True" in src
    assert "timeout = 30 if simple else 120" in src, "simple calls must target <30s; reasoning nodes get headroom"
    assert "max_retries=0" in src
    assert '"thinking"' in src, "simple nodes must disable thinking"

def test_no_import_time_llm_call():
    import src.agents.utils as utils
    src = inspect.getsource(utils)
    lines = [l for l in src.splitlines() if l.strip().startswith("print(_get_llm()")]
    assert not lines, "module must not invoke the LLM at import time"

def test_fallbacks_disabled():
    from src.agents import utils
    src = inspect.getsource(utils._get_llm)
    assert "with_fallbacks" not in src, "fallbacks are temporarily disabled for isolated testing"


# INC-SLOBur-6DFF regression: a passing diagnosis must not be discarded
# because failed attempts consumed the diagnosis_loops budget
def test_passing_hypothesis_routes_to_remediator_despite_loop_cap():
    from src.graph.routing import route_after_diagnosis
    s = {
        "hypotheses": [{"hypothesis": "db pool contention", "confidence": 0.74,
                        "evidence": [], "alternative": None, "supporting_runbook": None}],
        "diagnosis_mode": "llm",
        "diagnosis_loops": 3,  # diagnoser + timed-out deep investigation + llm_diagnoser
    }
    assert route_after_diagnosis(s) == "remediator"

def test_low_confidence_loops_still_escalate():
    from src.graph.routing import route_after_diagnosis
    s = {
        "hypotheses": [{"hypothesis": "guess", "confidence": 0.55,
                        "evidence": [], "alternative": None, "supporting_runbook": None}],
        "diagnosis_mode": "llm",
        "diagnosis_loops": 3,
    }
    assert route_after_diagnosis(s) == "escalate"
