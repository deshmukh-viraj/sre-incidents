"""
evaluation/evaluator.py
--------------------------
evaluator: deterministic assertion suite against final agent state.
- has_hitl (critical gate)
- routing_accuracy (runbook match)
- mttr_success (target-relative)
- resolution_status (resolved/escalated_with_evidence/escalated_without_evidence/failed)
- remediation_match (exact action match)
- recovery_met (p99 threshold)
- attribution_match (expected_cause vs actual cause)
"""

import json
import time
import httpx
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

#config
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
SCENARIOS_FILE = Path(__file__).parent / "data" / "scenarios.json"
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports" / "eval_results"

TERMINAL_STATUSES = {"resolved", "failed", "escalated", "executed_unverified"}


def load_scenarios():
    with open(SCENARIOS_FILE, 'r') as v:
        data = json.load(v)
        return data.get("scenarios", [])


def trigger_incident(scenario: dict) -> str:
    """triggers the agent via direct POST /incidents endpoint and return the incident id.
    the ANSWER is stripped first: the `runbook` label and `__sim_scenario`
    ground-truth key never reach the agent, so its own proposal is what gets scored."""
    raw_signals = dict(scenario['input']['raw_signals'])
    raw_signals.pop("runbook", None)
    raw_signals.pop("__sim_scenario", None)
    payload = {
        "incident_id": f"eval-{scenario['scenario_id']}-{uuid.uuid4().hex[:8]}",
        "raw_signals": raw_signals
    }
    response = httpx.post(f"{API_BASE_URL}/incidents", json=payload, timeout=10.0)
    response.raise_for_status()
    return response.json()["incident_id"]


def wait_for_completion(incident_id: str, timeout: int = 400) -> dict:
    """polls the api until the incident reaches a terminal status, return final state"""

    start_time = time.time()
    while time.time() - start_time < timeout:
        response = httpx.get(f"{API_BASE_URL}/incidents/{incident_id}", timeout=10.0)
        if response.status_code == 200:
            state = response.json()
            status = (state.get("resolution_status") or state.get("status") or "").lower()
            if status in TERMINAL_STATUSES:
                return state
        time.sleep(5)
    raise TimeoutError(f"Incident {incident_id} did not complete within {timeout}s")


def evaluate_state(state: dict, scenario: dict, elapsed_time: int) -> dict:
    """runs deterministic assertions against the final agent state.

    scenario schema (src/evaluation/data/scenarios.json):
      ground_truth.expected_requires_approval / expected_runbook /
      expected_remediation_actions / recovery_indicators
      evaluation_criteria.within_mttr_target_seconds
    agent fields live nested under the API record's agent_state.
    """
    agent = state.get("agent_state") or {}
    ground_truth = scenario.get("ground_truth", {})
    criteria = scenario.get("evaluation_criteria", {})

    def agent_field(key, default=None):
        return agent.get(key, state.get(key, default))

    # 1. hitl compliance (critical)
    expected_hitl = ground_truth.get("expected_requires_approval")
    actual_hitl = bool(agent_field("requires_approval", False))
    hitl_compliance = 1.0 if expected_hitl == actual_hitl else 0.0

    # 2. routing accuracy
    expected_rb = ground_truth.get("expected_runbook")
    actual_rb = agent_field("runbook_id")
    if expected_rb is None:
        routing_acc = 1.0 if actual_rb is None else 0.0
    else:
        routing_acc = 1.0 if expected_rb == actual_rb else 0.0

    # 3. mttr target
    target_mttr = criteria.get("within_mttr_target_seconds")
    mttr_success = 1.0 if (target_mttr is not None and elapsed_time <= target_mttr) else 0.0

    # 4. resolution status — resolved / escalated_with_evidence / escalated_without_evidence / failed
    resolution_status = (state.get("resolution_status") or state.get("status") or "").lower()

    # check evidence quality to determine escalated_with_evidence vs escalated_without_evidence
    has_evidence = (
        agent_field("root_cause") is not None or
        (agent_field("investigation_steps", 0) or 0) > 0 or
        agent_field("evidence_summary") is not None or
        len(agent_field("hypotheses", []) or []) > 0
    )

    if resolution_status == "resolved":
        resolution_score = 1.0
        escalation_class = "resolved"
    elif resolution_status == "escalated":
        if has_evidence:
            # escalated with evidence - agent investigated but couldn't conclude
            resolution_score = 0.5  # Partial credit per ESCALATION_WITH_EVIDENCE_GAP_IS
            escalation_class = "escalated_with_evidence"
        else:
            # escalated without evidence
            resolution_score = 0.0
            escalation_class = "escalated_without_evidence"
    elif resolution_status == "waiting_for_approval":
        # HITL pending: correct routing behavior, not a resolution
        resolution_score = 1.0 if expected_hitl else 0.0
        escalation_class = "hitl_pending"
    elif resolution_status == "executed_unverified":
        resolution_score = 0.0
        escalation_class = "executed_unverified"
    else:
        resolution_score = 0.0
        escalation_class = "failed"
    resolution_success = resolution_score

    # 5. remediation action match (tool-level; action text is LLM-worded)
    expected_actions = ground_truth.get("expected_remediation_actions", [])
    actual_action_plan = agent_field("action_plan", []) or []
    if expected_actions:
        expected_tools = {a.get("tool") for a in expected_actions}
        actual_tools = {a.get("tool") for a in actual_action_plan}
        remediation_match = 1.0 if expected_tools & actual_tools else 0.0
    else:
        remediation_match = 1.0  # nothing expected

    # 6. recovery indicator check
    expected_recovery = ground_truth.get("recovery_indicators", {})
    p99_target = expected_recovery.get("p99_latency_below")
    act_p99 = agent_field("final_p99_latency_s")
    if p99_target is not None and act_p99 is not None:
        recovery_met = 1.0 if act_p99 < p99_target else 0.0
    else:
        recovery_met = None

    # 7. attribution match — scored ONLY when the scenario declares an expectation;
    # an absent expectation must not silently read as a perfect score
    expected_cause = ground_truth.get("expected_resolution_cause")
    actual_cause = agent_field("resolution_cause")
    attribution_match = (
        1.0 if str(expected_cause).lower() == str(actual_cause).lower()
        else 0.0
    ) if expected_cause else None

    return {
        "scenario_id": scenario["scenario_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hitl_compliance": hitl_compliance,
        "routing_accuracy": routing_acc,
        "mttr_success": mttr_success,
        "resolution_status": resolution_status,
        "escalation_class": escalation_class,
        "resolution_score": resolution_score,
        "resolution_success": resolution_success,
        "remediation_match": remediation_match,
        "recovery_met": recovery_met,
        "attribution_match": attribution_match,
        "actual_mttr_sec": elapsed_time,
        "actual_status": resolution_status,
        "actual_runbook": actual_rb,
        "actual_action": actual_action_plan[0].get("action") if actual_action_plan else None,
        "actual_action_tools": sorted({a.get("tool") for a in actual_action_plan}),
        "actual_p99": act_p99,
        "actual_resolution_cause": actual_cause,
    }


def run_evaluation_suite():
    scenarios = load_scenarios()
    results = []

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting evaluation suite for {len(scenarios)} scenarios...")

    for sc in scenarios:
        print(f"\n Running {sc['scenario_id']} ({sc['input']['raw_signals']['alertname']})")
        try:
            start_time = time.time()
            incident_id = trigger_incident(sc)
            print(f"Triggered Incident ID: {incident_id}")

            final_state = wait_for_completion(incident_id, timeout=400)
            elapsed_time = int(time.time() - start_time)

            eval_result = evaluate_state(final_state, sc, elapsed_time)
            results.append(eval_result)

            print(f"Result: {eval_result['resolution_status']} | Escalation class: {eval_result['escalation_class']}")
            print(f"HITL: {eval_result['hitl_compliance']} | Routing: {eval_result['routing_accuracy']}")

        except Exception as e:
            print(f"Error executing scenario {sc['scenario_id']}: {str(e)}")
            results.append({
                "scenario_id": sc['scenario_id'],
                "error": str(e),
                "resolution_status": "error"
            })

    # save report + console summary
    report_path = REPORTS_DIR / f"eval_report_{int(time.time())}.json"
    with open(report_path, "w") as v:
        json.dump(results, v, indent=2)
    scored = [r for r in results if "error" not in r]
    if scored:
        print("\n Summary:")
        print(f"  scenarios run:      {len(scored)}/{len(results)}")
        print(f"  resolution score:   {sum(r['resolution_score'] for r in scored) / len(scored):.2f}")
        print(f"  hitl compliance:    {sum(r['hitl_compliance'] for r in scored) / len(scored):.2f}")
        print(f"  routing accuracy:   {sum(r['routing_accuracy'] for r in scored) / len(scored):.2f}")
        with_ev = sum(1 for r in scored if r.get('escalation_class') == 'escalated_with_evidence')
        without_ev = sum(1 for r in scored if r.get('escalation_class') == 'escalated_without_evidence')
        print(f"  escalations:        {with_ev} with evidence (0.5) / {without_ev} without (0.0)")
    print(f"\n Evaluation completed. Results saved to {report_path}")


if __name__=="__main__":
    run_evaluation_suite()
