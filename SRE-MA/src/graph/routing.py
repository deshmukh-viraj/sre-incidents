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


#Severity classification (after detector collects signals)

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


#route after detector

def route_after_detector(state: AgentState,) -> Literal["diagnoser", "resolved"]:
    """
    After detector collects signals, ALWAYS proceed to diagnosis.
    
    We do NOT short-circuit to "resolved" here based on naive metric checks.
    Why? Because a blanket "latency is fine" check will silently drop 
    critical non-latency alerts like Fraud Model Degradation (RB-006), 
    Data Exfiltration (RB-003), or Compliance Audits (RB-005).
    
    If an alert was a "flap" and self-resolved, the Diagnoser agent 
    will query the metrics/logs, see they are healthy, and conclude 
    "Transient spike, no action needed" naturally.
    """
    return "diagnoser"


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

    #RB-001: Payment Latency Spike
    if runbook_id == "RB-001" and p99 is not None and p99 >= P99_LATENCY_WARNING:
        return {
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
        }

    #RB-002: Circuit Breaker Trip
    if runbook_id == "RB-002" and cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN:
        return {
            "hypothesis":"Circuit breaker is OPEN, downstream service failing",
            "evidence":[
                f"circuit_breaker_state={cb_state:.1f} (open threshold={CIRCUIT_BREAKER_OPEN})",
                f"db_pool={db_pool:.2f}" if db_pool else "",
            ],
            "confidence": 0.92,
            "alternative": "Deployment caused downstream 500s",
            "supporting_runbook": "RB-002",
            "diagnosis_mode": "deterministic",
        }

    #RB-003: Data Exfiltration
    if runbook_id=="RB-003" and (bulk_export or auth_failures >= 50):
        return {
            "hypothesis":"Possible data exfiltration — bulk export or credential stuffing",
            "evidence":[
                f"bulk_data_export={bulk_export}",
                f"auth_failures={auth_failures}",
            ],
            "confidence": 0.85,
            "alternative": "Legitimate audit tool activity",
            "supporting_runbook": "RB-003",
            "diagnosis_mode": "deterministic",
        }

    #RB-004: DB Connection Exhaustion
    if runbook_id == "RB-004" and (
        (db_pool is not None and db_pool >= DB_POOL_WARNING)
        or hikaripoolerr
        or slow_queries >= 3
    ):
        return {
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
        }

    #RB-005: Compliance audit 
    if runbook_id == "RB-005":
        return {
            "hypothesis": "Compliance audit triggered",
            "evidence": [
                f"runbook_id={runbook_id} label present on alert",
            ],
            "confidence": 0.95,
            "alternative": None,
            "supporting_runbook": "RB-005",
            "diagnosis_mode": "deterministic",
        }

    #RB-006: Fraud Model Degradation
    if runbook_id == "RB-006" and decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH:
        return {
            "hypothesis": "Fraud model degradation causing false positive payment declines",
            "evidence": [
                f"payment_decline_rate={decline_rate:.3f} (baseline ~0.008)",
            ],
            "confidence": 0.82,
            "alternative": "Genuine fraud spike",
            "supporting_runbook": "RB-006",
            "diagnosis_mode":"deterministic",
        }

    #NO runbook label in alert or runbook label present but confirming signals not strong enough
    #pattern match freely against all signals

    #circuit breaker
    if cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN:
        return {
            "hypothesis": "Circuit breaker OPEN- downstream service failing",
            "evidence": [f"circuit_breaker_state={cb_state:.1f}"],
            "confidence": 0.92,
            "alternative": "Deployment caused downstream 500s",
            "supporting_runbook": "RB-002",
        }
    
    #db pool
    if (db_pool is not None and db_pool >= DB_POOL_WARNING) or hikaripoolerr:
        return {
            "hypothesis": "Database connection pool exhastion",
            "evidence": [
                f"db_pool={db_pool:.2f}" if db_pool else "",
                f"hikaripoolerr={hikaripoolerr}",
                
            ],
            "confidence": 0.90,
            "alternative": "Traffic burst exceeding pool capacity",
            "supporting_runbook": "RB-004"
        }
    
    #throttle rate - card rails
    if throttle_rate is not None and throttle_rate >= THROTTLE_RATE_HIGH:
        return {
            "hypothesis": "Card rails throttling causing payment latency spike",
            "evidence": [f"throttle_rate={throttle_rate:.3f}"],
            "confidence": 0.88,
            "alternative": "Recent deployment regression",
            "supporting_runbook": "RB-001",
        }
    
    #p99 alone -high letency without clear cause
    if p99 is not None and p99 > P99_LATENCY_CRITICAL:
        return {
            "hypothesis": "Severe latency spike - cause unclear from metrics alone",
            "evidence": [f"p99={p99:.3f}s  (critical threshold={P99_LATENCY_CRITICAL})"],
            "confidence": 0.65,
            "alternative": "Multiple possible causes",
            "supporting_runbook": "RB-001",
        }
    
    #Security
    if auth_failures >= 50 and bulk_export:
        return {
            "hypothesis": "Data exfiltration - credential stuffing with bulk export",
            "evidence": [
                f"auth_failures={auth_failures}",
                f"bulk_export={bulk_export}",
            ],
            "confidence": 0.85,
            "alternative": "Legitimate audit tool activity",
            "supporting_runbook": "RB-003",
        }

    #fraud model
    if decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH:
        return {
            "hypothesis": "Fraud model degradation - false positive decpline",
            "evidence": [f"decline_rate={decline_rate:.3f}"],
            "confidence": 0.82,
            "alternative": "Genuine fraud spike",
            "supporting_runbook": "RB-006",
        }

    #Nothing matched
    return None



#Route after diagnosis
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
        # No hypothesis at all -> try LLM
        if diagnosis_mode == "llm":
            return "escalate"
        return "llm_diagnoser"

    max_confidence = max(h.get("confidence", 0) for h in hypotheses)

    if max_confidence >= DIAGNOSIS_CONFIDENCE:
        return "remediator"

    # Confidence too low
    if diagnosis_mode == "llm":
        # LLM already tried and still not confident -> give up
        return "escalate"

    # haven't tried the llm yet -> give it a shot
    return "llm_diagnoser"


#Check if approval is needed
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

        
#Blast radius classifier
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