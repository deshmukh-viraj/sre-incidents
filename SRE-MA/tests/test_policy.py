"""unit tests for the policy-as-code gate (allowlist, bounds, routing)."""

from src.graph.policy import (
    policy_validator_node,
    route_after_policy,
    route_after_critic,
    ALLOWED_WRITE_TOOLS,
    REVERSIBLE_WRITE_TOOLS,
    MAX_ACTIONS_PER_INCIDENT,
)


def _state(plan=None, conf=0.9, executed=None, **extra):
    base = {
        "incident_id": "INC-T",
        "action_plan": plan or [],
        "hypotheses": [{"confidence": conf}] if conf is not None else [],
        "requires_approval": False,
        "executed_action_keys": executed or [],
    }
    base.update(extra)
    return base


def _action(tool="set_feature_flag", blast="pod", reversible=True):
    return {"action": f"do {tool}", "tool": tool, "params": {"service": "payment_gateway"},
            "blast_radius": blast, "reversible": reversible, "requires_approval": False,
            "executed": False, "result": None}


# --- allowlist + idempotency ---

def test_non_allowlisted_tool_rejected():
    out = policy_validator_node(_state(plan=[_action(tool="drop_database")]))
    assert out["plan_viable"] is False
    assert out["policy_rejections"][0]["reason"].startswith("tool 'drop_database' not in allowed list")


def test_kubectl_verbs_are_allowlisted():
    assert "kubectl_restart_deployment" in ALLOWED_WRITE_TOOLS
    out = policy_validator_node(_state(plan=[_action(tool="kubectl_restart_deployment")]))
    assert out["plan_viable"] is True
    assert out["action_plan"][0]["tool"] == "kubectl_restart_deployment"


def test_already_executed_action_rejected_idempotently():
    a = _action()
    out = policy_validator_node(_state(plan=[a], executed=None))
    # first pass passes; mark executed and re-validate
    assert out["plan_viable"] is True
    import json
    from src.graph.policy import idem_key
    key = idem_key("INC-T", a)
    out2 = policy_validator_node(_state(plan=[a], executed=[key]))
    assert out2["plan_viable"] is False
    assert "already executed" in out2["policy_rejections"][0]["reason"]


# --- the spec bounds (added, additive) ---

def test_max_two_actions_per_incident():
    plan = [_action(), _action(tool="rolling_restart", blast="service"), _action(tool="notify")]
    out = policy_validator_node(_state(plan=plan))
    assert len(out["action_plan"]) == MAX_ACTIONS_PER_INCIDENT
    assert any("max 2 actions" in r["reason"] for r in out["policy_rejections"])
    assert out["plan_viable"] is True


def test_cluster_blast_never_auto_executes():
    out = policy_validator_node(_state(plan=[_action(blast="cluster", reversible=True)]))
    assert out["requires_approval"] is True
    assert all(a["requires_approval"] for a in out["action_plan"])


def test_unknown_reversibility_routes_to_human():
    out = policy_validator_node(_state(plan=[_action(tool="rollback_deployment")]))
    assert out["requires_approval"] is True


def test_known_reversible_pod_action_auto_executes():
    out = policy_validator_node(_state(plan=[_action()]))
    assert out["requires_approval"] is False


def test_low_confidence_forces_approval():
    out = policy_validator_node(_state(plan=[_action()], conf=0.30))
    assert out["requires_approval"] is True


# --- routing ---

def test_route_after_policy():
    assert route_after_policy({"plan_viable": False}) == "escalate"
    assert route_after_policy({"plan_viable": True, "requires_approval": True}) == "human_gate"
    assert route_after_policy({"plan_viable": True, "requires_approval": False}) == "execute"


def test_route_after_critic_ladder():
    assert route_after_critic({"escalate_reason": "agent_escalated"}) == "escalate"
    assert route_after_critic({"escalate_reason": None, "requires_retry": False}) == "remediator"
    # first rejection -> cheap repair
    assert route_after_critic({
        "escalate_reason": None, "requires_retry": True, "repair_attempts": 0,
        "critique": "empty proposal", "hypotheses": [{"confidence": 0.3}],
    }) == "repair_proposal"
    # second rejection + high-confidence diagnosis -> deterministic plan
    assert route_after_critic({
        "escalate_reason": None, "requires_retry": True, "repair_attempts": 1,
        "critique": "ungrounded", "hypotheses": [{"confidence": 0.9}],
    }) == "remediator"
    # second rejection + low confidence -> full re-investigation
    assert route_after_critic({
        "escalate_reason": None, "requires_retry": True, "repair_attempts": 1,
        "critique": "ungrounded", "hypotheses": [{"confidence": 0.3}],
    }) == "deep_investigation"
    # hard ceiling -> human
    assert route_after_critic({
        "escalate_reason": "critic_rejected_after_retry", "requires_retry": True,
    }) == "escalate"


def test_reversible_map_matches_allowlist_core():
    # every reversible write tool must also be allowlisted (a reversible action
    # the policy rejects outright would be dead config)
    assert REVERSIBLE_WRITE_TOOLS <= ALLOWED_WRITE_TOOLS
