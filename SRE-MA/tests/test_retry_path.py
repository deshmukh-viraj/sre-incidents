"""
test_retry_path.py — regression tests for the critic->remediator retry path (ASD-STE100 work order)

no docker, no LLM, no neo4j. pure state + routing assertions.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch
from src.graph.policy import route_after_critic
from src.agents.remediator_agent import remediator_node, _extract_from_scratchpad


def _state(**kw):
    base = {
        "incident_id": "test-retry-1",
        "requires_retry": False,
        "critic_reject_count": 0,
        "action_plan": [],
        "hypotheses": [{"hypothesis": "x", "confidence": 0.9, "evidence": [], "alternative": None, "supporting_runbook": None}],
        "raw_signals": {},
        "correlated_alerts": [],
        "affected_services": ["payment_gateway"],
        "severity": "SEV2",
        "runbook_id": "RB-001",
        "llm_suggested_action": None,
    }
    base.update(kw)
    return base


#P0-2 router: ungrounded/empty -> deep_investigation; plan-quality -> remediator; limit -> escalate
def test_router_retry_goes_to_remediator():
    # plan-quality critique (neither 'empty proposal' nor 'ungrounded') -> remediator
    s = _state(requires_retry=True, critic_reject_count=0, critique="plan too risky for SEV1")
    assert route_after_critic(s) == "remediator"

def test_router_ungrounded_or_empty_goes_to_repair_then_ladder():
    """ladder: first rejection -> one cheap repair hop; after the repair hop the
    confident hypothesis goes to the deterministic planner, the unconfident one
    to a full (budget-capped) re-investigation."""
    for critique in ("empty proposal on SEV1", "ungrounded proposal: tool=x, target=y"):
        s = _state(requires_retry=True, critic_reject_count=0, critique=critique)
        assert route_after_critic(s) == "repair_proposal", critique
        # deterministic rung: high-confidence hypothesis already in hand
        assert route_after_critic(dict(s, repair_attempts=1)) == "remediator", critique
        # no confidence -> full re-investigation
        s2 = dict(s, repair_attempts=1, hypotheses=[{"hypothesis": "x", "confidence": 0.4}])
        assert route_after_critic(s2) == "deep_investigation", critique

def test_router_repairs_before_hard_escalation():
    """the old double-reject->escalate sent a good diagnosis to humans on the first
    wrong tool name; the ladder no longer burns two runs before a cheap fix."""
    assert route_after_critic(_state(requires_retry=True, critic_reject_count=2, critique="ungrounded")) == "repair_proposal"

def test_router_triple_reject_escalates():
    s = _state(requires_retry=False, critic_reject_count=3, critique="ungrounded")
    s["escalate_reason"] = "critic_rejected_after_retry"
    assert route_after_critic(s) == "escalate"

def test_router_accepted_stays_normal():
    assert route_after_critic(_state(requires_retry=False)) == "remediator"


#extraction test: scratchpad with propose_remediation -> non-empty action_plan
def test_extract_from_scratchpad_parses_proposal():
    s = _state(requires_retry=True, critic_reject_count=1)
    s["investigation_scratchpad"] = [
        'ai: calling propose_remediation(action="Restart account_ledger to flush leaked connections", tool="restart_service", target_service="account_ledger", blast_radius="service")'
    ]
    plan = _extract_from_scratchpad(s)
    assert plan, "extraction should find the proposal"
    assert plan[0]["tool"] == "restart_service"
    assert plan[0]["params"]["service"] == "account_ledger"
    assert "action" in plan[0]

def test_remediator_uses_scratchpad_on_retry():
    s = _state(requires_retry=True, critic_reject_count=1)
    s["investigation_scratchpad"] = [
        'ai: calling propose_remediation(action="Enable card_rails queue", tool="set_feature_flag", target_service="payment_gateway", blast_radius="pod")'
    ]
    out = remediator_node(s)
    assert out["action_plan"], "remediator must produce a non-empty plan on retry"
    assert out["requires_retry"] is False
    assert out["action_plan"][0]["tool"] == "set_feature_flag"


#every LLM call starts with role=system (prompt-channel defect guard)
def test_llm_extract_proposal_starts_with_system_message():
    import inspect
    from src.agents import remediator_agent
    src = inspect.getsource(remediator_agent._llm_extract_proposal)
    sys_pos = src.find("SystemMessage(content=")
    hum_pos = src.find("HumanMessage(content=")
    assert sys_pos != -1, "must construct a SystemMessage"
    assert hum_pos != -1, "must construct a HumanMessage"
    assert sys_pos < hum_pos, "system message must be constructed before the human message"
