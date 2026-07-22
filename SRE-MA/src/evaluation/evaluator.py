import json
import time
import httpx
import os
from pathlib import Path
from datetime import datetime

#config
API_BASE_URL = "http://localhost:8080"
SCENARIOS_FILE = Path(__file__).parent / "data" / "scenarios.json"
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports" / "eval_results"


def load_scenarios():
    with open(SCENARIOS_FILE, 'r') as v:
        data = json.load(v)
        return data.get("scenarios", [])

def trigger_incident(scenario: dict) -> str:
    """triggers the agent via fastapi webhook and return the incident id"""
    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": scenario['input']['raw_signals']['alertname'],
                "severity": "critical",
                "service": scenario['input']['raw_signals']['service']
            }
        }]
    }
    response = httpx.post(f"{API_BASE_URL}/webhook/alert", json=payload, timeout=10.0)
    response.raise_for_status()

    incident_id = response.json().get("incident_id")
    if not incident_id:
        raise ValueError("api did not return the incident_id")
    return incident_id

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

    # 4. resolution status
    resolution_success = 1.0 if state.get("resolution_status").lower() == "resolved" else 0.0

    #5. remediation action match
    expected_action = scenario.get("expected_remediation_action", [])
    actual_action_plan = state.get("action_plan", [])
    if expected_action:
        expected_action_text = expected_action[0].get("action", "").strip().lower()     
        actual_action_text = (actual_action_plan[0].get("action", "").strip().lower() if actual_action_plan else "")
        remediation_match = 1.0 if expected_action_text == actual_action_text else 0.0
    else:
        remediation_match = 1.0 #nothing expexted

    # 6. recovery indicator check
    expected_recovery = scenario.get("recovery_indicators", {})
    p99_target = expected_recovery.get("p99_latency_below")
    act_p99 = state.get("final_p99_latency_s")
    if p99_target is not None and act_p99 is not None:
        recovery_met = 1.0 if act_p99 < p99_target else 0.0
    else:
        recovery_met = None

    expected_cause = scenario.get("expected_resolution_cause")
    actual_cause = state.get("resolution_cause")
    attribution_match = 1.0 if (expected_cause is None or expected_cause == actual_cause) else 0.0
    
    return {
        "scenario_id": scenario["scenario_id"],
        "timestamp": datetime.utcnow().isoformat(),
        "hitl_compliance": hitl_compliance,
        "routing_accuracy": routing_acc,
        "mttr_successt": mttr_success,
        "resolution_success": resolution_success,
        "remediation_match": remediation_match,
        "recovery_met": recovery_met,
        "attribution_match": attribution_match,
        "actual_mttr_sec": elapsed_time,
        "actual_status": state.get("resolution_status"),
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

            print(f"Result: {eval_result['resolution_success']}")
            print(f"HITL: {eval_result['hitl_compliance']} | Routing: {eval_result['routing_accuracy']}")
        
        except Exception as e:
            print(f"Error executing scenario {sc['scenario_id']}: {str(e)}")
            results.append({
                "scenario_id": sc['scenario_id'],
                "error": str(e),
                "resolution_success": 0.0
            })
    
    #save report
    report_path = REPORTS_DIR / f"eval_report_{int(time.time())}.json"
    with open(report_path, "w") as v:
        json.dump(results,v, indent=2)
    print(f"\n Evaluation completed. Results saved to {report_path}")

if __name__=="__main__":
    run_evaluation_suite()

    