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
P99_LATENCY_CRITICAL= 3.0    # seconds, SEV1
P99_LATENCY_WARNING = 1.5  # seconds, SEV2/3
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


def _apply_config_pack():
    try:
        from src.config.loader import config_pack
        pack = config_pack() or {}
        g = globals()
        for key, value in (pack.get("thresholds") or {}).items():
            if key in g and isinstance(value, (int, float)):
                g[key] = type(g[key])(value)
    except Exception as e:
        print(f"[config-pack] threshold override failed: {e}")


_apply_config_pack()


#per action class temporal windows
# a single hardcoded grace_period=30 for every action type, as exists today, is not defensible: 
# a feature-flag flip and a full rollback do not propagate at the same speed

#time for poll + prometheus query 
VERIFICATION_OVERHEAD_SECONDS = 25
DELTA_EFFECT_SECONDS = {
   
    "set_feature_flag": 15,
    "rollback_deployment": 120,
    "rolling_restart": 90,
    "capture_diagnostics": 15,
    "notify": 5,
    "execute_kg_suggestion": 45,
}

# remote (AWS OTel demo) flag flips: the alert can only clear after (a) flagd
# file-sync picks up the write, (b) load-gen traffic resumes, (c) the OTel SDK
# exports the healthy series (60s export granularity), (d) Prometheus re-evals
# and pushes the resolve.75s = 15s propagation + 60s export ceiling;
# the remaining wait is absorbed by the verification polls themselves.
REMOTE_FLAG_EFFECT_SECONDS = 300
REMOTE_FLAG_PROPAGATION_SECONDS = 75


##stability windows determine how long we wait after an action before trusting metrics again
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

NOVEL_DB_PRESSURE = 0.80
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
    er = s.get("error_rate") or 0
    tr = s.get("throttle_error_rate") or 0
    if er >= ERROR_RATE_WARNING:
        out["error"] = 1
        #if error_rate is high but throttle_error_rate is low, card rails cannot explain the errors
        if tr < er * 0.5:
            out["non_throttle_errors"] = 1
    if tr >= THROTTLE_RATE_HIGH: out["throttle_rate"] = 1
    if (s.get("db_pool_utilization") or 0) >= NOVEL_DB_PRESSURE: out["db_pool"] = 1
    if (s.get("circuit_breaker_state") or 0) > CIRCUIT_BREAKER_OPEN: out["circuit_breaker"] = 1
    if (s.get("payment_decline_rate") or 0) >= DECLINE_RATE_HIGH: out["decline_rate"] = 1
    if lp.get("auth_failures", 0) >= 50: out["auth_failures"] = 1
    if lp.get("bulk_data_export"): out["bulk_export"] = 1
    return out


def _apply_residual_check(result: dict, state: AgentState) -> dict:
    """
    novelty/ood gate: cap confidence when matched rb cannot explain every live anomaly.

    uses NOVEL_DB_PRESSURE (0.50)  not DB_POOL_WARNING (0.85)  so sub-warning
    db pressure on a non-db runbook still counts as contradictory evidence.
    """
    if not result:
        return result
    explains = RUNBOOK_EXPLAINS.get(result.get("supporting_runbook"))
    if explains is None:
        return result

    anomalous = _anomalous_sig(state)
    unexplained = sorted(anomalous.keys() - explains)
    if not unexplained:
        return result

    result["confidence"] = min(result.get("confidence", 1.0), 0.65)
    result["residual_gate_failed"] = True
    notes = []
    if "db_pool" in unexplained:
        notes.append(
            f"Unexplained DB pressure detected "
            f"(db_pool_utilization={state.get('raw_signals', {}).get('db_pool_utilization')}, "
            f"novel threshold={NOVEL_DB_PRESSURE})"
        )
    other = [u for u in unexplained if u != "db_pool"]
    if other:
        notes.append(f"UNEXPLAINED by {result['supporting_runbook']}: {', '.join(other)}")
    result["evidence"] = (result.get("evidence") or []) + notes
    return result

# severity classification (after detector collects signals)
def classify_severity(state: AgentState) -> str:
    """
    severity from the alert annotation label first (it is the paging contract),
    falling back to raw metric values when no label is present.
    metrics-only classification produced a SEV1 on a warning-labelled burn alert
    because the detector enriches a live p99 sample that the alert never fired on
    """
    signals = state.get("raw_signals", {})

    alert_label = (signals.get("severity") or "").strip().lower()
    label_severity = {
        "critical": Severity.SEV1.value,
        "warning": Severity.SEV2.value,
        "info": Severity.SEV3.value,
    }.get(alert_label)
    if label_severity:
        return label_severity

    p99 = signals.get("p99_latency_s")
    error_rate= signals.get("error_rate")
    db_pool = signals.get("db_pool_utilization")
    cb_state = signals.get("circuit_breaker_state")
    slo_budget= signals.get("slo_budget_remaining")
    decline_rate = signals.get("payment_decline_rate")

    #SEV1 full outage signals
    if any([
        (p99 is not None and p99 >= P99_LATENCY_CRITICAL),
        (error_rate is not None and error_rate >= ERROR_RATE_CRITICAL),
        (db_pool is not None and db_pool >= DB_POOL_CRITICAL),
        (cb_state is not None and cb_state > CIRCUIT_BREAKER_OPEN),
    ]):
        return Severity.SEV1.value

    #SEV2 major degradation
    if any([
        (p99 is not None and p99 >= P99_LATENCY_WARNING),
        (error_rate is not None and error_rate >= ERROR_RATE_WARNING),
        (db_pool is not None and db_pool >= DB_POOL_WARNING),
        (slo_budget is not None and slo_budget < SLO_BUDGET_LOW),
        (decline_rate is not None and decline_rate >= DECLINE_RATE_HIGH),
    ]):
        return Severity.SEV2.value

    #SEV3 partial degradation
    return Severity.SEV3.value


#deterministic diagnosis routing 
def deterministic_diagnosis(state: AgentState) -> dict:
    """
    try to identify root cause from numeric signals alone.
    returns a hypothesis dict if pattern is clear, or None if ambiguous.

    """
    signals = state.get("raw_signals", {})
    raw = signals.get("log_patterns") or state.get("log_patterns", {})

    if isinstance(raw, list):
        log_patterns = {str(item): True for item in raw}
    elif isinstance(raw, dict):
        log_patterns = raw
    else:
        log_patterns = {}

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
        #verify positive evidence of card rails throttling (timeout, 429s, or dominant throttle rate).
        #simple ratio check. dynamic anomaly detector over carrier response codes.
        has_throttling_signals = bool(
            log_patterns.get("card_rails_timeout")
            or log_patterns.get("http_429_count", 0) > 0
            or (throttle_rate is not None and throttle_rate >= THROTTLE_RATE_HIGH and (not error_rate or throttle_rate / error_rate >= 0.5))
        )
        if has_throttling_signals:
            return _apply_residual_check({
                "hypothesis": "Card rails throttling causing payment latency spike",
                "evidence": [
                    f"p99={p99:.3f}s above internal warning threshold={P99_LATENCY_WARNING}s (alert annotation may cite its own)",
                    f"error_rate={error_rate:.3f}" if error_rate else "",
                    f"throttle_rate={throttle_rate:.3f}" if throttle_rate else "",
                ],
                "confidence": 0.88,
                "alternative": "DB pool pressure or bad deployment causing latency",
                "supporting_runbook": "RB-001",
                "diagnosis_mode": "deterministic",
            }, state)
        else:
            return _apply_residual_check({
                "hypothesis": "Payment latency spike - cause ambiguous, card rails throttling unconfirmed",
                "evidence": [
                    f"p99={p99:.3f}s elevated",
                    f"error_rate={error_rate:.3f}" if error_rate else "",
                    f"throttle_rate={throttle_rate:.3f} explains only minority of errors" if (throttle_rate and error_rate) else "",
                    "no 429 errors or card_rails timeouts in logs" if not log_patterns.get("card_rails_timeout") else "",
                ],
                "confidence": 0.50,
                "alternative": "Bad deployment or downstream dependency issue",
                "supporting_runbook": "RB-001",
                "diagnosis_mode": "deterministic",
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
            "hypothesis":"Possible data exfiltration - bulk export or credential stuffing",
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

    # budget ran out: single escalation path (hand off to a human)
    if state.get("budget_exhausted"):
        print("[router] budget exhausted -> escalate")
        return "escalate"

    # a passing hypothesis routes to remediation regardless of loop count:
    # failed attempts (e.g. a timed-out deep investigation) must not waste the
    # budget needed by the successful attempt that followed
    if hypotheses:
        max_confidence = max(h.get("confidence", 0) for h in hypotheses)
        if max_confidence >= DIAGNOSIS_CONFIDENCE:
            return "remediator"

    if loops >= MAX_DIAGNOSIS_LOOPS:
        return "escalate"

    if not hypotheses:
        # no hypothesis at all -> try LLM, unless we already tried or are about to
        if diagnosis_mode == "llm":
            return "escalate"
        # NOTE: from the deterministic diagnoser this branch is deliberately wired
        # to deep_investigation (orchestrator.py maps "llm_diagnoser" -> deep agent
        # for novel-signal incidents); the one-shot llm_diagnoser node itself is
        # only reached from route_after_investigation when the harness degrades.
        print("[router] no hypothesis -> deep_investigation (novel signal, via llm_diagnoser route name)")
        return "llm_diagnoser"

    # confidence too low
    if diagnosis_mode == "llm":
        return "escalate"
    print(f"[router] max confidence < {DIAGNOSIS_CONFIDENCE:.2f} -> deep_investigation")
    return "llm_diagnoser"


#check if approval is needed
def route_after_remediator(
    state: AgentState,
) -> Literal["human_gate", "execute", "escalate"]:
    """
    budget ran out mid-remediation: escalate, the incident hands off to a
    human. Otherwise the plan executes right away.
    """
    if state.get("budget_exhausted"):
        return "escalate"
    if state.get("requires_approval"):
        return "human_gate"
    return "execute"


def route_after_verification(
    state: AgentState,
) -> Literal["end_resolved", "re_investigate", "escalate_execution"]:
    """after the executor runs a plan and we poll metrics:
    - metrics recovered -> end (success)
    - action executed but metrics still breaching and the re-attack budget is
      unspent -> one more investigation pass carrying the post-action evidence
    - re-attack already spent, or the plan never executed -> escalate (human)
    """
    if state.get("verified"):
        return "end_resolved"
    if state.get("action_executed_at") and int(state.get("reattacks", 0) or 0) < 1:
        return "re_investigate"
    return "escalate_execution"


def route_after_investigation(state) -> Literal["critic", "llm_diagnoser", "verifier", "escalate"]:
    """harness failure (diagnosis_mode == 'pending_llm') degrades to the legacy one-shot diagnoser"""
    #budget-exhausted before more LLM work -> escalate
    if state.get("budget_exhausted"):
        print("[router] budget exhausted -> escalate")
        return "escalate"
    if state.get("diagnosis_mode") == "pending_llm":
        print(f"[router] deep_investigation -> llm_diagnoser (pending_llm)")
        return "llm_diagnoser"
    # adversarial check between investigation and critic;
    # fail-open default keeps the pre-change target.
    from src.agents.verifier_agent import verifier_enabled
    if verifier_enabled(state):
        print("[router] deep_investigation -> verifier (VERIFIER_ENABLED)")
        return "verifier"
    print(f"[router] deep_investigation -> critic (mode={state.get('diagnosis_mode')})")
    return "critic"


def route_after_human_gate(state: AgentState) -> Literal["execute", "escalate", "end_resolved"]:
    """fix: a denied approval must NEVER reach the executor.
    natural_calm: live signals recovered before the page, nothing to execute,
    close via the final communicator instead."""
    if state.get("natural_calm"):
        return "end_resolved"
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
        # a flag flip is environment-wide config, not a single pod 
        # a global flag change was labeled blast=pod)
        return "service"
    if "flush_cache" in tool:
        return "pod"
    if "kill_query" in tool:
        return "service"

    return "service"


# action types that may run autonomously once the policy tier gate passes them
AUTONOMOUS_ACTION_TOOLS = {
    "set_feature_flag",
    "rolling_restart",
    "kubectl_restart_deployment",
    "capture_diagnostics",
    "notify",
}

def requires_human_approval(action: dict) -> bool:
    """
    coarse pre-gate: cluster blast or unknown reversibility always needs a human,
    and only autonomy-eligible action types may pass. service-blast actions using
    an eligible tool are NOT blanket-gated here  the policy validator's tier gate
    (service catalog) makes the final call; gating every service-level action
    would contradict the autonomy rule (stateless app restarts are auto-eligible).
    """
    blast = classify_blast_radius(action)
    reversible = action.get("reversible", True)
    if blast == "cluster":
        return True
    if not reversible:
        return True
    return action.get("tool") not in AUTONOMOUS_ACTION_TOOLS


#infer service name
_DEFAULT_SERVICE_ALIASES = {
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


def infer_service(alert_name: str) -> str:
    #the fintech pack's service_aliases override the coded map
    try:
        from src.config.loader import config_pack
        mapping = (config_pack().get("service_aliases") or _DEFAULT_SERVICE_ALIASES)
    except Exception:
        mapping = _DEFAULT_SERVICE_ALIASES
    for keyword, svc in mapping.items():
        if keyword.lower() in alert_name.lower():
            return svc
    return "payment_gateway"