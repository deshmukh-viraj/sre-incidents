"""self-checks for the control-plane hardening (security gap 4)."""

from src.graph.policy import policy_validator_node


def _action():
    return {"action": "do thing", "tool": "capture_diagnostics",
            "params": {"service": "payment_gateway"}, "blast_radius": "pod",
            "reversible": True, "requires_approval": False, "executed": False,
            "result": None}


def test_kill_switch_forces_human_gate(monkeypatch):
    st = {
        "incident_id": "INC-KS",
        "action_plan": [_action()],
        "hypotheses": [{"confidence": 0.99, "evidence": []}],
        "requires_approval": False,
        "executed_action_keys": [],
    }
    out = policy_validator_node(st)
    assert out["action_plan"][0]["requires_approval"] is False  # baseline: autonomous

    monkeypatch.setenv("SRE_AUTONOMY_DISABLED", "1")
    out2 = policy_validator_node(st)
    assert out2["action_plan"][0]["requires_approval"] is True  # kill switch: human gate


def test_annotation_answer_pattern_screening():
    from src.api.app import _ANSWER_PATTERN

    leak = "Payment latency high. Set flag=checkout_high_risk_variant to variant=control."
    redacted = _ANSWER_PATTERN.sub("[redacted]", leak)
    assert "checkout_high_risk_variant" not in redacted
    assert "control" not in redacted.replace("[redacted]", "")

    clean = "Payment p99 latency above SLO for 10 minutes. Runbook: https://runbooks/rb-1"
    assert _ANSWER_PATTERN.search(clean) is None  # legitimate context is untouched
