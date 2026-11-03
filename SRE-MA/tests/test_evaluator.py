"""the evaluator must track scenarios.json's real schema — this is the guard
against the KeyError-on-every-scenario drift that previously zeroed the suite."""

import json
from pathlib import Path

from src.evaluation.evaluator import evaluate_state, load_scenarios

SCENARIOS = {s["scenario_id"]: s for s in load_scenarios()}


def test_scenarios_file_has_expected_schema():
    for sc in SCENARIOS.values():
        assert "ground_truth" in sc, f"{sc['scenario_id']}: no ground_truth block"
        assert "evaluation_criteria" in sc, f"{sc['scenario_id']}: no evaluation_criteria block"
        gt = sc["ground_truth"]
        for key in ("expected_runbook", "expected_requires_approval",
                    "expected_remediation_actions", "recovery_indicators"):
            assert key in gt, f"{sc['scenario_id']}: ground_truth.{key} missing"


def test_perfect_run_scores_all_ones():
    sc = SCENARIOS["EVAL-001"]
    gt = sc["ground_truth"]
    state = {
        "resolution_status": "resolved",
        "agent_state": {
            "requires_approval": gt["expected_requires_approval"],
            "runbook_id": gt["expected_runbook"],
            "action_plan": gt["expected_remediation_actions"],
            "final_p99_latency_s": gt["recovery_indicators"]["p99_latency_below"] - 0.1,
        },
    }
    result = evaluate_state(state, sc, elapsed_time=120)
    assert result["hitl_compliance"] == 1.0
    assert result["routing_accuracy"] == 1.0
    assert result["resolution_success"] == 1.0
    assert result["remediation_match"] == 1.0
    assert result["recovery_met"] == 1.0
    assert result["attribution_match"] is None  # expectation absent -> not scored


def test_wrong_routing_and_hitl_score_zero():
    sc = SCENARIOS["EVAL-001"]
    gt = sc["ground_truth"]
    state = {
        "resolution_status": "escalated",
        "agent_state": {
            "requires_approval": not gt["expected_requires_approval"],
            "runbook_id": "RB-999" if gt["expected_runbook"] != "RB-999" else "RB-000",
            "action_plan": [],
            "final_p99_latency_s": None,
        },
    }
    result = evaluate_state(state, sc, elapsed_time=999)
    assert result["hitl_compliance"] == 0.0
    assert result["routing_accuracy"] == 0.0
    assert result["resolution_success"] == 0.0
    assert result["recovery_met"] is None
