import json
from src.graph.orchestrator import run_incident

def test_known_incidents():
    """test path A"""
    print("\n Test 1: knwon incidents (RB-001)")
    result = run_incident(
        incident_id="test-001",
        raw_signals={
            "alert_name":    "PaymentGatewayP99LatencyHigh",
            "runbook":       "RB-001",
            "team":          "payments",
            "severity":      "critical",
            "service":       "payment_gateway"
        }
    )
    print(f"severity:           {result.get('severity')}")
    print(f"diagnosis_mode:     {result.get('diagnosis_mode')}")
    print(f"root_cause:         {result.get('root_cause')}")
    print(f"action_plan count:  {len(result.get('action_plan', []))}")
    print(f"resolution_status:  {result.get('resolution_status')}")
    print(f"mttr_seconds:       {result.get('mttr_seconds')}")
    print(f"token_cost_used:    {result.get('token_cost_usd', 0):.4f}")

    assert result.get('severity') is not None, "FAILED - severity is not set"
    assert result.get('resolution_status') is not None, "FAILED - never resolved"
    assert result.get("mttr_seconds") is not None, "FAILED - no mttr"
    print("PASSED")



def test_novel_incidents():
    """test path B"""
    print("\n  Test 2: Novel incident (no runbook)")

    result = run_incident(
        incident_id="test-002",
        raw_signals={
            "alert_name": "UnknownServiceDegradation",
            "team": "platform",
            "severity": "warning",
            "service": "notification_service"
        }
    )

    print(f"severity:          {result.get('severity')}")
    print(f"diagnosis_mode:    {result.get('diagnosis_mode')}")
    print(f"root_cause:        {result.get('root_cause')}")
    print(f"resolution_status: {result.get('resolution_status')}")
    print(f"tokens_used:       {result.get('total_tokens_used')}")
    print(f"token_cost_used:   {result.get('token_cost_usd', 0):.4f}")

    assert result.get("resolution_status") is not None, "FAILED - never resolved"
    print("PASSED")


def test_db_incidents():
    print("\n  Test-3: DB connection exhaustion (RB-004)")

    result = run_incident(
        incident_id="test-003",
        raw_signals={
            "alert_name":    "DBConnectionPoolExhaustion",
            "runbook":       "RB-004",
            "team":          "platform",
            "severity":      "warning",
            "service":       "account_ledger"
        }
    )
    print(f"severity:          {result.get('severity')}")
    print(f"root_cause:        {result.get('root_cause')}")
    print(f"resolution_status: {result.get('resolution_status')}")
    print(f"action_plan:")
    for a in result.get("action_plan", []):
        print(f"  → {a['action'][:60]} executed={a['executed']}")

    assert result.get("resolution_status") is not None, "FAILED — never resolved"
    print("PASSED")


def test_llm_path():
    """
    Forces LLM diagnoser by providing signals that match no
    deterministic pattern. No runbook label, no strong metrics.
    """
    print("\n--- Test 4: LLM diagnoser path ---")

    result = run_incident(
        incident_id="TEST-004",
        raw_signals={
            "alert_name":            "ServiceBehaviourAnomaly",
            # No runbook label — novel alert
            "team":                  "platform",
            "service":               "fraud_detector",

            # Signals below all deterministic thresholds
            # p99 below P99_LATENCY_WARNING (1.5)
            # error_rate below ERROR_RATE_WARNING (0.05)
            # db_pool below DB_POOL_WARNING (0.85)
            "p99_latency_s":         1.2,
            "error_rate":            0.03,
            "throttle_error_rate":   0.01,
            "db_pool_utilization":   0.45,
            "circuit_breaker_state": 0.0,
            "slo_budget_remaining":  0.60,
            "payment_decline_rate":  0.01,

            # Log patterns all false
            "log_patterns": {
                "card_rails_timeout":   False,
                "hikaripoolerror":      False,
                "circuit_breaker_open": False,
                "bulk_data_export":     False,
                "auth_failures":        0,
                "slow_queries":         0,
                "http_500_count":       5,
                "http_429_count":       0,
            },
            "error_log_summaries": [
                "2026-05-18T02:14:23 [WARN] service=fraud_detector | Prediction latency increasing",
                "2026-05-18T02:14:25 [WARN] service=fraud_detector | Feature pipeline staleness detected",
            ],
            "security_log_summaries": [],
        }
    )

    print(f"severity:          {result.get('severity')}")
    print(f"diagnosis_mode:    {result.get('diagnosis_mode')}")
    print(f"root_cause:        {result.get('root_cause')}")
    print(f"diagnosis_summary: {result.get('diagnosis_summary')}")
    print(f"evidence_summary:  {result.get('evidence_summary')}")
    print(f"blast_analysis:    {result.get('blast_analysis')}")
    print(f"resolution_status: {result.get('resolution_status')}")
    print(f"tokens_used:       {result.get('total_tokens_used')}")
    print(f"token_cost_usd:    ${result.get('token_cost_usd', 0):.4f}")
    print(f"llm_action:        {result.get('llm_suggested_action')}")
    

    assert result.get("diagnosis_mode") == "llm", "FAILED — should have used LLM"
    print("PASSED")

if __name__=="__main__":
    print("="*50)
    print("graph tests")
    print("="*50)

    test_known_incidents()
    test_novel_incidents()
    test_db_incidents()
    test_llm_path()

    print("="*50)
    print("ALL graph tests done")