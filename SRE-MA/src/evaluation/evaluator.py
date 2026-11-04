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
from datetime import datetime

#config
API_BASE_URL = os.getenv("API_BASE_URL", "http://agent:8080")
SCENARIOS_FILE = Path(__file__).parent / "data" / "scenarios.json"
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports" / "eval_results"


def load_scenarios():
    with open(SCENARIOS_FILE, 'r') as v:
        data = json.load(v)
        return data.get("scenarios", [])


def trigger_incident(scenario: dict) -> str:
    """triggers the agent via direct POST /incidents endpoint and return the incident id"""
    raw_signals = scenario['input']['raw_signals']
    payload = {
        "incident_id": f"eval-{scenario['scenario_id']}-{uuid.uuid4().hex[:8]}",
        "raw_signals": raw_signals
    }
    response = httpx.post(f"{API_BASE_URL}/incidents", json=payload, timeout=10.0)
    response.raise_for_status()
    return response.json()["incident_id"]


def wait_for_completion(incident_id: str, timeout: int = 300) -> dict:
    """polls the api unitl the incident is resolved o fails, return final state"""

    start_time = time.time()
    while time.time() - start_time < timeout:
        response = httpx.get(f"{API_BASE_URL}/incidents/{incident_id}", timeout=10.0)
        if response.status_code == 200:
            state = response.json()
            status = state.get("resolution_status", "").lower()
            if status in ["resolved", "failed", "escalated"]:
                return state
        time.sleep(5)
    raise TimeoutError(f"Incident {incident_id} did not complete within {timeout}s")


def evaluate_state(state: dict, scenario: dict, elapsed_time: int) -> dict:
    """runs deterministic assertion against the final agent state"""

    # 1. hitl compliance (critical)
    expected_hitl = scenario['expected_hitl']
    actual_hitl = state.get("requires_approval", False)
    hitl_compliance = 1.0 if expected_hitl == actual_hitl else 0.0

    # 2. routing accuracy
    expected_rb = scenario["expected_runbook"]
    actual_rb = state.get("runbook_id")
    if expected_rb is None:
        routing_acc = 1.0 if actual_rb is None else 0.0
    else:
        routing_acc = 1.0 if expected_rb == actual_rb else 0.0

    # 3. mttr target
    target_mttr = scenario["target_mttr_seconds"]
    mttr_success = 1.0 if elapsed_time <= target_mttr else 0.0

    # 4. resolution status — distinguish resolved, escalated_with_evidence, escalated_without_evidence, failed
    resolution_status = state.get("resolution_status", "").lower()
    
    # Check evidence quality to determine escalated_with_evidence vs escalated_without_evidence
    has_evidence = (
        state.get("root_cause") is not None or
        state.get("investigation_steps", 0) > 0 or
        state.get("evidence_summary") is not None or
        len(state.get("hypotheses", [])) > 0
    )
    
    if resolution_status == "resolved":
        resolution_score = 1.0
        escalation_class = "resolved"
    elif resolution_status == "escalated":
        if has_evidence:
            #escalated with evidence - agent investigated but couldn't conclude
            resolution_score = 0.5  # Partial credit per ESCALATION_WITH_EVIDENCE_GAP_IS
            escalation_class = "escalated_with_evidence"
        else:
            # Escalated without evidence
            resolution_score = 0.0
            escalation_class = "escalated_without_evidence"
    else:
        resolution_score = 0.0
        escalation_class = "failed"

    # 5. remediation action match
    expected_action = scenario.get("expected_remediation_action", [])
    actual_action_plan = state.get("action_plan", [])
    if expected_action:
        expected_action_text = expected_action[0].get("action", "").strip().lower()
        actual_action_text = (actual_action_plan[0].get("action", "").strip().lower() if actual_action_plan else "")
        remediation_match = 1.0 if expected_action_text == actual_action_text else 0.0
    else:
        remediation_match = 1.0  # nothing expected

    # 6. recovery indicator check
    expected_recovery = scenario.get("recovery_indicators", {})
    p99_target = expected_recovery.get("p99_latency_below")
    act_p99 = state.get("final_p99_latency_s")
    if p99_target is not None and act_p99 is not None:
        recovery_met = 1.0 if act_p99 < p99_target else 0.0
    else:
        recovery_met = None

    # 7. attribution match (agent vs natural calm)
    expected_cause = scenario.get("expected_resolution_cause")
    actual_cause = state.get("resolution_cause")
    attribution_match = 1.0 if (expected_cause is None or expected_cause == actual_cause) else 0.0
    
    return {
        "scenario_id": scenario["scenario_id"],
        "timestamp": datetime.utcnow().isoformat(),
        "hitl_compliance": hitl_compliance,
        "routing_accuracy": routing_acc,
        "mttr_success": mttr_success,
        "resolution_status": resolution_status,
        "escalation_class": escalation_class,
        "resolution_score": resolution_score,
        "remediation_match": remediation_match,
        "recovery_met": recovery_met,
        "attribution_match": attribution_match,
        "actual_mttr_sec": elapsed_time,
        "actual_status": resolution_status,
        "actual_runbook": actual_rb,
        "actual_action": actual_action_plan[0].get("action") if actual_action_plan else None,
        "actual_p99": act_p99,
        "actual_resolution_cause": actual_cause
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
    
    #save report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"eval_report_{int(time.time())}.json"
    with open(report_path, "w") as v:
        json.dump(results, v, indent=2)
    print(f"\n Evaluation completed. Results saved to {report_path}")


if __name__=="__main__":
    run_evaluation_suite()