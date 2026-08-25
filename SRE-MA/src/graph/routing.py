"""
all numeric thresholds and deterministic routing live here.
python does the math

hybrid architecture:
  - known incident patterns -> deterministic path
  - unknown patterns / ESCALATE fallback -> LLM reasoning path 
"""

from typing import Literal
from src.graph.state import AgentState, Severity


#thresholds
P99_LATENCY_CRITICAL= 3.0    # seconds — SEV1
P99_LATENCY_WARNING = 1.5  # seconds — SEV2/3
P99_LATENCY_RECOVERY = 0.5   
ERROR_RATE_CRITICAL= 0.10    # 10%
ERROR_RATE_WARNING = 0.05    # 5%
THROTTLE_RATE_HIGH = 0.05    # 429/504 rate
DB_POOL_WARNING = 0.85    # 85% full
DB_POOL_CRITICAL = 0.98    # 98% full
CIRCUIT_BREAKER_OPEN = 1.5     # state > 1 = open
SLO_BUDGET_LOW= 0.20    # 20% remaining
DECLINE_RATE_HIGH = 0.05    # 5% payment decline rate
DIAGNOSIS_CONFIDENCE = 0.70    # below this -> LLM fallback
MAX_DIAGNOSIS_LOOPS = 3       # prevent infinite re-diagnosis


#per action class temporal windows (gate 3)
# a single hardcoded grace_period=30 for every action type, as exists today, is not defensible: 
# a feature-flag flip and a full rollback do not propagate at the same speed

#time for poll + prometheus query 
VERIFICATION_OVERHEAD_SECONDS = 25
DELTA_EFFECT_SECONDS = {
    "set_feature_flag": 60,
    "rollback_deployment": 120,
    "rolling_restart": 90,
    "capture_diagnostics": 15,
    "notify": 5,
    "execute_kg_suggestion": 45,
}

#stability windows (gate 5)
#stability windows determine how long we wait after an action before trusting metrics again
T_STABLE_SECONDS = {
    Severity.SEV1.value: 300,
    Severity.SEV2.value: 600,
    Severity.SEV3.value: 1800,

}

ALERT_CORRELATION_WINDOW = {
    "SLOErrorBudgetBurnRateFast": 3600, #matches its own 1h rate() window
    "PaymentDeclineRateAbnormal": 900, 
    
}
DEFAULT_CORRELATION_WINDOW = 300 #fall back to existing debounce_sec value

NOVEL_DB_PRESSURE = 0.50
RUNBOOK_EXPLAINS = {
    "RB-001": {"p99", "error", "throttle_rate"},
    "RB-002": {"circuit_breaker", "error_rate", "p99", "db_pool"},
    "RB-003": {"auth_failures", "bulk_export", "error_rate"},
    "RB-004": {"db_pool", "slow_queries", "hikaripoolerror", "p99", "error_rate"},
    "RB-005": set(),
    "RB-006": {"decline_rate", "error_rate"},
}

def _anomalous_sig(state: AgentState):
    s = state.get("raw_signals", {}); lp = s.get("log_patterns", {})
    out = {}
    if (s.get("p99_latency_s") or 0) >= P99_LATENCY_WARNING: out["p99"] = 1
    if (s.get("error_rate") or 0) >= ERROR_RATE_WARNING: out["error"] = 1
    if (s.get("throttle_error_rate") or 0) >= THROTTLE_RATE_HIGH: out["throttle"] = 1
    if (s.get("db_pool_utilization") or 0) >= DB_POOL_WARNING: out["db_pool"] = 1
    if (s.get("circuit_breaker_state") or 0) > CIRCUIT_BREAKER_OPEN: out["circuit_breaker"] = 1
    if (s.get("payment_decline_rate") or 0) >= DECLINE_RATE_HIGH: out["decline_rate"] = 1
    if lp.get("auth_failures", 0) >= 50: out["auth_failures"] = 1
    if lp.get("bulk_data_export"): out["bulk_export"] = 1
    return out


def _apply_residual_check(result: dict, state: AgentState) -> dict:
    """novelty/ood gate: cap confidence when matched rb cannot explain every live anomaly"""
    if result and (explains := RUNBOOK_EXPLAINS.get(result.get("supporting_runbook"))) is not None:
        if unexplained := (_anomalous_sig(state).keys() - explains):
            result["confidence"] = min(result.get("confidence", 1.0), 0.65)
        result["evidence"] = (result.get("evidence") or []) + [
                f"UNEXPLAINED by {result['supporting_runbook']}: {', '.join(unexplained)}"
        ]
    return result

# severity classification (after detector collects signals)
def classify_severity(state: AgentState) -> str:
    """
    python classifies severity from raw metric values.
    returns severity enum value as string.
    """
    signals = state.get("raw_signals", {})

    p99 = signals.get("p99_latency_s")
    error_rate= signals.get("error_rate")
    db_pool = signals.get("db_pool_utilization")
    cb_state = signals.get("circuit_breaker_state")
    slo_budget= signals.get("slo_budget_remaining")
    decline_rate = signals.get("payment_decline_rate")

    #SEV1 — full outage signals
    if any([
        (p99 is not None and p99 >= P99_LATENCY_CRITICAL),
        (error_rate is not None and error_rate >= ERROR_RATE_CRITICAL),
        (db_pool is not None and db_pool >= DB_POOL_CRITICAL),
        (cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN),
    ]):
        return Severity.SEV1.value

    #SEV2 — major degradation
    if any([
        (p99 is not None and p99 >= P99_LATENCY_WARNING),
        (error_rate is not None and error_rate >= ERROR_RATE_WARNING),
        (db_pool is not None and db_pool >= DB_POOL_WARNING),
        (slo_budget is not None and slo_budget < SLO_BUDGET_LOW),
        (decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH),
    ]):
        return Severity.SEV2.value

    #SEV3 — partial degradation
    return Severity.SEV3.value


#deterministic diagnosis routing 
def deterministic_diagnosis(state: AgentState) -> dict:
    """
    try to identify root cause from numeric signals alone.
    returns a hypothesis dict if pattern is clear, or None if ambiguous.

    """
    signals = state.get("raw_signals", {})
    log_patterns = signals.get("log_patterns", {})
    runbook_id = state.get("runbook_id")

    p99 = signals.get("p99_latency_s")
    error_rate = signals.get("error_rate")
    throttle_rate = signals.get("throttle_error_rate")
    db_pool = signals.get("db_pool_utilization")
    cb_state = signals.get("circuit_breaker_state")
    decline_rate= signals.get("payment_decline_rate")
    auth_failures = log_patterns.get("auth_failures", 0)
    bulk_export = log_patterns.get("bulk_data_export", False)
    slow_queries = log_patterns.get("slow_queries", 0)
    hikaripoolerr = log_patterns.get("hikaripoolerror", False)

    #RB-001: payment latency spike
    if runbook_id == "RB-001" and p99 is not None and p99 >= P99_LATENCY_WARNING:
        return _apply_residual_check({
            "hypothesis": "Card rails throttling causing payment latency spike",
            "evidence":[
                f"p99={p99:.3f}s (threshold={P99_LATENCY_WARNING}s)",
                f"error_rate={error_rate:.3f}" if error_rate else "",
                f"throttle_rate={throttle_rate:.3f}" if throttle_rate else "",
            ],
            "confidence":0.88,
            "alternative": "DB pool pressure or bad deployment causing latency",
            "supporting_runbook": "RB-001",
            "diagnosis_mode":"deterministic",
        }, state)

    #RB-002: circuit breaker trip
    if runbook_id == "RB-002" and cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN:
        return _apply_residual_check({
            "hypothesis":"Circuit breaker is OPEN, downstream service failing",
            "evidence":[
                f"circuit_breaker_state={cb_state:.1f} (open threshold={CIRCUIT_BREAKER_OPEN})",
                f"db_pool={db_pool:.2f}" if db_pool else "",
            ],
            "confidence": 0.92,
            "alternative": "Deployment caused downstream 500s",
            "supporting_runbook": "RB-002",
            "diagnosis_mode": "deterministic",
        }, state)

    #RB-003: data exfiltration
    if runbook_id=="RB-003" and (bulk_export or auth_failures >= 50):
        return _apply_residual_check({
            "hypothesis":"Possible data exfiltration — bulk export or credential stuffing",
            "evidence":[
                f"bulk_data_export={bulk_export}",
                f"auth_failures={auth_failures}",
            ],
            "confidence": 0.85,
            "alternative": "Legitimate audit tool activity",
            "supporting_runbook": "RB-003",
            "diagnosis_mode": "deterministic",
        }, state)

    #RB-004: db connection exhaustion
    if runbook_id == "RB-004" and (
        (db_pool is not None and db_pool >= DB_POOL_WARNING)
        or hikaripoolerr
        or slow_queries >= 3
    ):
        return _apply_residual_check({
            "hypothesis":"Database connection pool exhaustion",
            "evidence": [
                f"db_pool={db_pool:.2f}" if db_pool else "",
                f"hikaripoolerr={hikaripoolerr}",
                f"slow_queries={slow_queries}",
            ],
            "confidence":0.90,
            "alternative": "Traffic burst exceeding pool capacity",
            "supporting_runbook": "RB-004",
            "diagnosis_mode":"deterministic",
        }, state)

    #RB-005: compliance audit 
    if runbook_id == "RB-005":
        return _apply_residual_check({
            "hypothesis": "Compliance audit triggered",
            "evidence": [
                f"runbook_id={runbook_id} label present on alert",
            ],
            "confidence": 0.95,
            "alternative": None,
            "supporting_runbook": "RB-005",
            "diagnosis_mode": "deterministic",
        }, state)

    #RB-006: fraud model degradation
    if runbook_id == "RB-006" and decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH:
        return _apply_residual_check({
            "hypothesis": "Fraud model degradation causing false positive payment declines",
            "evidence": [
                f"payment_decline_rate={decline_rate:.3f} (baseline ~0.008)",
            ],
            "confidence": 0.82,
            "alternative": "Genuine fraud spike",
            "supporting_runbook": "RB-006",
            "diagnosis_mode":"deterministic",
        }, state)

    # #no runbook label in alert or runbook label present but confirming signals not strong enough
    # #pattern match freely against all signals

    # #circuit breaker
    # if cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN:
    #     return _apply_residual_check({
    #         "hypothesis": "Circuit breaker OPEN- downstream service failing",
    #         "evidence": [f"circuit_breaker_state={cb_state:.1f}"],
    #         "confidence": 0.92,
    #         "alternative": "Deployment caused downstream 500s",
    #         "supporting_runbook": runbook_id or "RB-002",
    #     }, state)
    
    # #db pool
    # if (db_pool is not None and db_pool >= DB_POOL_WARNING) or hikaripoolerr:
    #     return _apply_residual_check({
    #         "hypothesis": "Database connection pool exhastion",
    #         "evidence": [
    #             f"db_pool={db_pool:.2f}" if db_pool else "",
    #             f"hikaripoolerr={hikaripoolerr}",
                
    #         ],
    #         "confidence": 0.90,
    #         "alternative": "Traffic burst exceeding pool capacity",
    #         "supporting_runbook": runbook_id or "RB-004"
    #     }, state)
    
    # #throttle rate - card rails
    # if throttle_rate is not None and throttle_rate >= THROTTLE_RATE_HIGH:
    #     return _apply_residual_check({
    #         "hypothesis": "Card rails throttling causing payment latency spike",
    #         "evidence": [f"throttle_rate={throttle_rate:.3f}"],
    #         "confidence": 0.88,
    #         "alternative": "Recent deployment regression",
    #         "supporting_runbook": runbook_id or "RB-001",
    #     }, state)
    
    # #p99 alone -high letency without clear cause
    # if p99 is not None and p99 > P99_LATENCY_CRITICAL:
    #     return _apply_residual_check({
    #         "hypothesis": "Severe latency spike - cause unclear from metrics alone",
    #         "evidence": [f"p99={p99:.3f}s  (critical threshold={P99_LATENCY_CRITICAL})"],
    #         "confidence": 0.65,
    #         "alternative": "Multiple possible causes",
    #         "supporting_runbook": runbook_id or "RB-001",
    #     }, state)
    
    # #security
    # if auth_failures >= 50 and bulk_export:
    #     return _apply_residual_check({
    #         "hypothesis": "Data exfiltration - credential stuffing with bulk export",
    #         "evidence": [
    #             f"auth_failures={auth_failures}",
    #             f"bulk_export={bulk_export}",
    #         ],
    #         "confidence": 0.85,
    #         "alternative": "Legitimate audit tool activity",
    #         "supporting_runbook": runbook_id or "RB-003",
    #     }, state)

    # #fraud model
    # if decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH:
    #     return _apply_residual_check({
    #         "hypothesis": "Fraud model degradation - false positive decpline",
    #         "evidence": [f"decline_rate={decline_rate:.3f}"],
    #         "confidence": 0.82,
    #         "alternative": "Genuine fraud spike",
    #         "supporting_runbook": runbook_id or "RB-006",
    #     }, state)

    #nothing matched
    return None



#route after diagnosis
def route_after_diagnosis(
    state: AgentState,
) -> Literal["remediator", "llm_diagnoser", "escalate"]:
    """
    after diagnosis attempt:
    - high confidence deterministic -> remediator (skip LLM)
    - low confidence OR no pattern -> llm_diagnoser
    - llm already tried and still low confidence -> escalate
    """
    hypotheses = state.get("hypotheses", [])
    diagnosis_mode = state.get("diagnosis_mode")
    loops = state.get("diagnosis_loops", 0)

    if loops >= MAX_DIAGNOSIS_LOOPS:
        return "escalate"

    if not hypotheses:
        # no hypothesis at all -> try LLM, unless we already tried or are about to
        if diagnosis_mode == "llm":
            return "escalate"
        return "llm_diagnoser"

    max_confidence = max(h.get("confidence", 0) for h in hypotheses)

    if max_confidence >= DIAGNOSIS_CONFIDENCE:
        return "remediator"

    # confidence too low
    if diagnosis_mode == "llm":
        return "escalate"
    return "llm_diagnoser"


#check if approval is needed
def route_after_remediator(
    state: AgentState,
) -> Literal["human_gate", "execute"]:
    """
    human ke pass jana, otherwise execute immediately.
    """
    if state.get("requires_approval"):
        return "human_gate"
    return "execute"


def route_after_verification(state: AgentState) -> Literal["end_resolved", "escalate_execution"]:
    """after the executor runs a runbook and we poll metrics:
    - metrics recivered -> end (success)
    - metrics still breaching -> escalate (remediation failed)
    """
    if state.get("verified"):
        return "end_resolved"
    else:
        return "escalate_execution"


def route_after_investigation(state) -> Literal["critic", "llm_diagnoser"]:
    """harness failure (diagnosisi_mode == 'pending_llm') degrades to legacy on shot"""
    if state.get("diagnosis_mode") == "pending_llm":
        return "llm_diagnoser"
    return "critic"


def route_after_human_gate(state):
    """fix: a denied approval must NEVER reach the executor"""
    return "execute" if state.get("human_approved") else "escalate"

        
#blast radius classifier
def classify_blast_radius(action: dict) -> str:
    """
    figure out how bad this action is.
    pod -> auto-approve usually (just restarting a pod)
    service -> warn (might impact some users)
    cluster -> always ask human (could break everything)
    """
    tool = action.get("tool", "")
    params = action.get("params", {})

    if "rollback" in tool or "rollback" in str(params):
        return "service"
    if "restart" in tool:
        service = params.get("service", "")
        if service in ("postgres_primary", "postgres_replica"):
            return "cluster"
        return "service"
    if "feature_flag" in tool:
        return "pod"
    if "flush_cache" in tool:
        return "pod"
    if "kill_query" in tool:
        return "service"

    return "service"


def requires_human_approval(action: dict) -> bool:
    """
    hardcoded rule to decide if we need a human.
    blast_radius=cluster -> yes
    reversible=False -> yes
    blast_radius=service -> yes (unless we say otherwise)
    """
    blast = classify_blast_radius(action)
    reversible = action.get("reversible", True)

    if blast == "cluster":
        return True
    if not reversible:
        return True
    if blast == "service":
        return True
    return False


#infer service name
def infer_service(alert_name: str) -> str:
    mapping = {
        "PaymentGateway": "payment_gateway",
        "SLOError": "payment_gateway",
        "SLOBudget": "payment_gateway",
        "PaymentDecline": "payment_gateway",
        "PaymentTransaction": "payment_gateway",
        "CircuitBreaker": "payment_gateway",
        "DBConnection": "account_ledger",
        "SlowQuery": "account_ledger",
        "Ledger": "account_ledger",
        "ServiceMemory": "account_ledger",
        "Anomalous": "api_gateway",
        "Authentication": "api_gateway",
        "Compliance": "api_gateway",
        "FraudModel": "fraud_detector",
    }
    for keyword, svc in mapping.items():
        if keyword.lower() in alert_name.lower():
            return svc
    return "payment_gateway"