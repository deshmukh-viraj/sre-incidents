"""
agents/execution_agent.py
--------------------------
execution agents: human approval gate and action executor.

    human_gate_node— node 6: pauses workflow; waits for /approve call in production.
                       in simulation mode (SIM_AUTO_APPROVE=true) auto-approves after timeout.
    execute_node— node 7: runs each action in the plan, appends incident to KG.

writes to state:
    human_approved, approval_timeout (human_gate)
    action_plan, resolution_status, resolved_at, mttr_seconds, resolution_notes (execute)
"""

import os
import datetime
import time

from dataclasses import asdict, dataclass, field
from langgraph.types import interrupt
from src.graph.state import AgentState, ResolutionStatus
from src.tools.sre_tool import execute_remediation, notify_slack, check_alert_status
from src.tools.prometheus_tool import collect_incident_signals
from src.tools.kg_tool import append_past_incident
from src.graph.policy import idem_key

from src.graph.routing import DELTA_EFFECT_SECONDS, P99_LATENCY_RECOVERY, ERROR_RATE_WARNING, VERIFICATION_OVERHEAD_SECONDS, REMOTE_FLAG_EFFECT_SECONDS
from src.graph.clear_time import build_clear_evidence
from src.graph.attribution_gates import evaluate_gate3, final_label
from src.agents.schemas import VerificationResult, ExecutionReceipt
from typing import Optional, Dict, Any, List

try:
    from langfuse import observe
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator


<<<<<<< Updated upstream
def _recovery_checker_enabled() -> bool:
    """B2 RecoveryChecker flag (default 0; MULTI_AGENT=1 enables all upgrades)."""
    return os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("RECOVERY_CHECKER_ENABLED", "0") == "1"
=======
def log_stage(incident_id: str, stage: str, detail: str = ""):
    """clean stage logger for real-time visibility."""
    suffix = f" | {detail}" if detail else ""
    print(f"\n[{incident_id}] ▶ {stage}{suffix}")


@dataclass
class VerificationResult:
    verified: bool
    p99_latency_s: Optional[float] = None
    error_rate: Optional[float] = None
    t_clear: Optional[str] = None
    signal: Optional[str] = None
    healthy_run_len: int = 0 
>>>>>>> Stashed changes


def _executor_receipts_enabled() -> bool:
    """B6 typed ExecutionReceipts flag (default 0; MULTI_AGENT=1 enables all)."""
    return os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("EXECUTOR_RECEIPTS_ENABLED", "0") == "1"


# node 6: human gate

def human_gate_node(state: AgentState) -> dict:
    """
    check if action requires human approval
    if yes, pauses the graph (interrupt). if no, passes through

    B7 (flag HANDOFF_ENABLED, default 0): an un-presented handoff_to_human
    package (budget/loop exhaustion) is transferred HERE — the single
    handoff-to-human write path: interrupt carries reason + options; the
    decision maps to the unchanged approval semantics (approve -> execute,
    reject -> escalate).
    """
    incident_id = state['incident_id']

    # B7 handoff transfer (before the normal approval check)
    handoff = os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("HANDOFF_ENABLED", "0") == "1"
    pkg = state.get("handoff_to_human") or {}
    if handoff and pkg and not pkg.get("presented"):
        print(f"[human_gate] Presenting handoff package for {incident_id} "
              f"(cause={pkg.get('cause')})")
        decision = interrupt({
            "question": "Agent budget/loop exhausted — choose how to proceed",
            "incident_id": incident_id,
            "reason": pkg.get("reason"),
            "options": pkg.get("options", []),
        })
        presented = {**pkg, "presented": True,
                     "decision": decision.get("option") or
                                 ("approved" if decision.get("approved") else "rejected")}
        print(f"[human gate] Handoff decision for {incident_id}: {presented['decision']}")
        return {
            "human_approved": decision.get("approved", False),
            "approved_by": decision.get("approver", "system"),
            "approval_timeout": False,
            "handoff_to_human": presented,
            "handoff_count": 1,
            "ownership": ["human"],
            "message_ledger": [{**pkg, "kind": "handoff"}],
        }

    #check if any action in the plan requires approval
    needs_approval = any(a.get("requires_approval", False) for a in state.get("action_plan", []))

    # deterministic human approval
    if not needs_approval or os.getenv("SIM_AUTO_APPROVE", "false").lower() == "true":
        log_stage(incident_id, "HUMAN_GATE", f"auto-approved (requires_approval={needs_approval})")
        return {"human_approved": True, "approval_timeout": False}

<<<<<<< Updated upstream
    # natural-calm soak (INC-Paymen-9023): a transient spike can recover while the
    # pipeline investigates; paging a human for an alert that already calmed is a
    # false page. SEV2/SEV3 only (SEV1 pages immediately), fresh live signals,
    # bounded soak. ponytail: fixed 20s poll + NATURAL_CALM_SOAK_S cap (default
    # 120s) — upgrade path: alertmanager silence API instead of polling.
    soak = int(os.getenv("NATURAL_CALM_SOAK_S", "120") or 0)
    if soak > 0 and state.get("severity") in ("SEV2", "SEV3"):
        from src.agents.deep_investigation import _breaches
        deadline = time.time() + soak
        while True:
            breaches = []
            for svc in state.get("affected_services") or []:
                fresh = collect_incident_signals(svc)
                breaches += _breaches({**state, "raw_signals": fresh})
            if not breaches:
                print(f"[human_gate] NATURAL CALM for {incident_id}: live signals healthy "
                      f"— closing without human page")
                return {
                    "human_approved": False,
                    "natural_calm": True,
                    "resolution_status": ResolutionStatus.RESOLVED.value,
                    "resolution_cause": "natural_calm",
                    "resolution_notes": "alert calmed before approval — live signals healthy, no action taken",
                    "message_ledger": [{
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                        "from_worker": "human_gate", "kind": "note",
                        "content": "natural calm: live signals recovered before approval; plan not executed",
                    }],
                }
            if time.time() >= deadline:
                print(f"[human_gate] still breaching after {soak}s soak — paging human ({breaches[0]})")
                break
            time.sleep(min(20, max(1, deadline - time.time())))

    # llm /destructive (requires approval)
    print(f"[human_gate] Interrupting graph for {incident_id} awaiting for human approval...")
=======
    log_stage(incident_id, "HUMAN_GATE", f"waiting for approval (requires_approval={needs_approval})")
>>>>>>> Stashed changes

    actions_text = ""
    for a in state.get("action_plan", []):
        if a.get("requires_approval"):
            actions_text += f" {a['action']}\n"

    # P7: page AFTER the decision arrives — the node re-runs from the top on
    # Command(resume=...), so a pre-interrupt page would fire twice (the
    # interrupted run's partials are discarded). One page, with the decision.
    human_decsion = interrupt({
        "question": "Do you approve the action plan?",
        "incident_id": incident_id
    })
<<<<<<< Updated upstream
    print(f"[human gate] Resumed {incident_id} with human input: {human_decsion}")
    message = (
        f" *Approval decision for {incident_id}*\n"
        f"*Alert*: {state.get('alert_name', 'Unknown')}\n"
        f"*Diagnosis*: {state.get('diagnosis_summary') or 'Unknown'}\n"
        f"*Proposed Actions*:\n{actions_text}\n"
        f"*Decision*: {'APPROVED' if human_decsion.get('approved') else 'REJECTED'}"
        f" by {human_decsion.get('approver', 'system')}\n"
    )
    notify_slack(message=message, channel="incidents", severity="warning", incident_id=incident_id)
=======
    log_stage(incident_id, "HUMAN_GATE", f"resumed with input: {human_decsion.get('approved', False)}")
>>>>>>> Stashed changes
    return {
        "human_approved": human_decsion.get("approved", False),
        "approved_by": human_decsion.get("approver", "system"),
        "approval_timeout": False,
        "approval_notified": True,

    }

@observe(name="verify_recovery")
def _verify_recovery(
    alert_name: str,
    service: str, 
    pre_metrics: dict,
    grace_period: int = 30,
    polls: int = 5,
    interval: int = 15,
    min_consecutive: int = 4,
    )-> VerificationResult:
    """
    verifies a fix actually worked using a fast, two step check to prevent false esclation.
    1. check the real data(primary): compare prometheus metrics (p99 latency, error rate) before/after.
    2. check the alert state(secondary): accept it if alertmanager already resolved

    why: the alertmanager approach only checked alerts, which are slow to update and caused false escalations
    even when the underlying fix worked.
    """
    from src.tools.prometheus_tool import collect_incident_signals
    from src.graph.routing import P99_LATENCY_WARNING, ERROR_RATE_WARNING, P99_LATENCY_RECOVERY

    pre_p99 = pre_metrics.get("p99_latency_s")
    pre_error = pre_metrics.get("error_rate")

    log_stage(alert_name, "VERIFICATION_GRACE", f"waiting {grace_period}s for action to propagate...")
    time.sleep(grace_period)

    consecutive_healthy = 0
    first_healthy_ts = None
    curr_p99 = None
    curr_error = None

    for i in range(polls):
        log_stage(alert_name, f"VERIFY_POLL {i+1}/{polls}", f"grace={grace_period}s, interval={interval}s")

        #secondary: quick alertmanager check
        am_state = check_alert_status(alert_name)
        if am_state not in ("firing", "unknown"):
            log_stage(alert_name, "AM_RESOLVED", f"Alertmanager state={am_state}")
            signals = collect_incident_signals(service, window="1m")
            curr_p99 = signals.get("p99_latency_s")
            curr_error = signals.get("error_rate")
            
            # return structured dataclass with t_clear and signal type
            return VerificationResult(
                verified=True,
                p99_latency_s=curr_p99,
                error_rate=curr_error,
                t_clear=datetime.datetime.utcnow().isoformat(),
                signal="alertmanager",
                healthy_run_len=max(consecutive_healthy, min_consecutive),
            )

                #primary: prometheus metric comparison 
        log_stage(alert_name, "VERIFY_METRICS", f"querying {service} metrics...")
        signals = collect_incident_signals(service, window="1m")
        curr_p99 = signals.get("p99_latency_s")
        curr_error = signals.get("error_rate")
       
        # check if p99 latency improved (dropped below warning threshold OR improved by >50%)
        p99_ok = False
        if curr_p99 is not None:
            if curr_p99 < P99_LATENCY_RECOVERY:
                p99_ok = True
                log_stage(alert_name, "P99_RECOVERED", f"p99={curr_p99:.3f}s < {P99_LATENCY_RECOVERY}s")
            elif pre_p99 is not None and curr_p99 < pre_p99 * 0.5:
                p99_ok = True
                log_stage(alert_name, "P99_IMPROVED", f"p99 {pre_p99:.3f}s -> {curr_p99:.3f}s")
            else:
                log_stage(alert_name, "P99_STILL_HIGH", f"p99={curr_p99:.3f}s (pre={pre_p99}s, thresh={P99_LATENCY_RECOVERY}s)")

        # check if error rate improved
        error_ok = False
        if curr_error is not None:
            if curr_error < ERROR_RATE_WARNING:
                error_ok = True
                log_stage(alert_name, "ERROR_RATE_RECOVERED", f"error_rate={curr_error:.4f} < {ERROR_RATE_WARNING}")
            elif pre_error is not None and curr_error < pre_error * 0.5:
                error_ok = True
                log_stage(alert_name, "ERROR_RATE_IMPROVED", f"error_rate {pre_error:.4f} -> {curr_error:.4f}")
            else:
                log_stage(alert_name, "ERROR_RATE_STILL_HIGH", f"error_rate={curr_error:.4f} (pre={pre_error}, thresh={ERROR_RATE_WARNING})")

        #require at least min_consecutive healthy checks before confirming recovery to avoid premature close on single dip
        if p99_ok or error_ok:
            consecutive_healthy += 1
            if first_healthy_ts is None:
                first_healthy_ts = datetime.datetime.utcnow().isoformat()
            log_stage(alert_name, "HEALTHY", f"consecutive={consecutive_healthy}/{min_consecutive}")
            if consecutive_healthy >= min_consecutive:
                log_stage(alert_name, "METRICS_RECOVERED", f"p99_ok={p99_ok}, error_ok={error_ok}, run_len={consecutive_healthy}")
                return VerificationResult(
                    verified=True,
                    p99_latency_s=curr_p99,
                    error_rate=curr_error,
                    t_clear=first_healthy_ts,
                    signal="p99" if p99_ok else "error_rate",
                    healthy_run_len=consecutive_healthy,
                )
        else:
            consecutive_healthy = 0
            first_healthy_ts = None

        log_stage(alert_name, "POLL_WAIT", f"metrics not stable, waiting {interval}s...")
        time.sleep(interval)

    log_stage(alert_name, "VERIFICATION_EXHAUSTED", f"exhausted {polls} polls — no recovery")
    return VerificationResult(
        verified=False,
        p99_latency_s=curr_p99,
        error_rate=curr_error,
        t_clear=None,
        signal=None,
        healthy_run_len=consecutive_healthy,
    )
def _signal_expr(service: str, signal: Optional[str]) -> tuple[str, float, str]:
    """
    maps the verification signal (which metric actually recovered) to the prometheus expression
    threshold, comparison that gates3's range query needs to ask the same questiion against real history
    """
    if signal == "error_rate":
        expr = (
            f'sum(rate(http_requests_total{{service="{service}", status_code=~"5.."}}[1m])) '
            f'/ sum(rate(http_requests_total{{service="{service}"}}[1m]))'
        )
        return expr, ERROR_RATE_WARNING, "below"
    
    expr = (
        f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket'
        f'{{service="{service}"}}[1m])) by (le))'
    )
    return expr, P99_LATENCY_RECOVERY, "below"



# node 7: execute
def execute_node(state: AgentState) -> dict:
    """
    run the action plan here.
    records outcome in neo4j graph.
    also checks if the metric got better before marking as resolved, so we dont lie.
    """
    incident_id = state['incident_id']
    log_stage(incident_id, "EXECUTION_START", f"plan has {len(state.get('action_plan', []))} actions")

    # gate-1: claim (capture the timestamp the moment the agent takes the wheel)
    t_claim = datetime.datetime.utcnow().isoformat()
    claim_id = state["incident_id"]

    action_plan = state.get("action_plan", [])
    start_ts = datetime.datetime.fromisoformat(state["alert_started_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    invoked_ts = datetime.datetime.fromisoformat(state["agent_invoked_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    approved_by = state.get("approved_by")

    all_success = True
    updated_plan = []
    receipts: List[Dict[str, Any]] = []  # B6: typed ExecutionReceipt dicts

    executed_keys = list(state.get("executed_action_keys", []))
    
    #gate-2: executionm evidence list
    execution_evidence: List[Dict[str, Any]] = []

    # extract service name from state for post-remediation metric checks
    affected_services = (state.get("affected_services") or ["payment_gateway"])
    service = affected_services[0]

    # snapshot pre-remediation metrics so we can compare after  
    pre_metrics = state.get("raw_signals", {})
    log_stage(incident_id, "PRE_REMEDIATION", f"p99={pre_metrics.get('p99_latency_s')}, error_rate={pre_metrics.get('error_rate')}")
    
    is_shadow = bool(state.get("shadow_execution"))
    if is_shadow:
        log_stage(incident_id, "EXECUTION", "SHADOW mode — suppressing execution")

    # remote (AWS OTel demo) remediation: the alert labels authorize the exact
    # flag+variant, so override whatever the proposal carried and mark the
    # action remote so sre_tool routes it to the aws-tunnel admin bridge.
    raw_alarm = state.get("raw_signals") or {}
    remote_flag_authorized = bool(raw_alarm.get("remote_flag"))

    action_start_ts = datetime.datetime.utcnow()
    for action in action_plan:
        key = idem_key(state["incident_id"], action)
        if key in executed_keys:
            log_stage(incident_id, "EXECUTION", f"idempotent skip: {key}")
            updated_plan.append({**action, "executed": True, "result": "idempotent skip"})
            continue
        params = action["params"]
        if is_shadow:
            log_stage(incident_id, "EXECUTION", f"SHADOW: suppressing {action['action']}")
            result = {"success": True, "result": "shadow, no-op", "dry_run": True}
        else:
            if action.get("tool") == "set_feature_flag" and remote_flag_authorized:
                params = {
                    **params,
                    "flag": raw_alarm["remote_flag"],
                    "variant": raw_alarm.get("remote_variant", params.get("variant", "off")),
                    "remote": True,
                }
                action = {**action, "params": params}
                log_stage(incident_id, "REMOTE_EXECUTION", f"flag='{params['flag']}' -> variant='{params['variant']}'")
            result = execute_remediation(
                action=action["action"],
                tool=action["tool"],
                params=params,
            )

        # gate-2: executin and target binding
        target_service = params.get("service", service)
        target_bound = target_service in affected_services

        execution_evidence.append({
            "action": action['action'],
            "tool": action['tool'],
            "target_service": target_service,
            "target_bound":target_bound,
            "success":result.get("success") or False,
            "t_action_end": datetime.datetime.utcnow().isoformat(),
        })
    
        if not result.get("success"):
            all_success = False

        updated_plan.append({
            **action,
            "executed": True,
            "result": result.get("result") or result.get("error"),
        })
        if result.get("success"):
            executed_keys.append(key)

        # B6: typed receipt from the result we ALREADY have — no new network
        # calls; the seam where the sandbox executor plugs in later.
        if _executor_receipts_enabled():
            try:
                receipts.append(ExecutionReceipt(
                    action=action["action"], tool=action["tool"],
                    params=action.get("params", {}),
                    success=bool(result.get("success")),
                    dry_run=bool(result.get("dry_run")),
                    executed_at=result.get("executed_at"),
                    result=result.get("result"),
                    error=result.get("error"),
                    idem_key=key,
                ).model_dump())
            except Exception as e:
                print(f"[execute] receipt validation fallback: {e}")
                receipts.append({"action": action["action"], "tool": action["tool"],
                                 "success": bool(result.get("success")),
                                 "error": str(e)})

    action_executed_at = datetime.datetime.utcnow()
    action_executed_at_iso = action_executed_at.isoformat()
    # agent_mttr = time from invocation to action completion.
    # in simulation this is always 0s (deterministic path + no-op actions are instant).
    # in production this will reflect real execution latency. not a bug.
    agent_mttr = int((action_executed_at - invoked_ts).total_seconds())

    # gate-3: temporal window (calculate dynamic grace period based on tools)
    grace_period = max(
        (DELTA_EFFECT_SECONDS.get(a.get("tool", ""), 30) for a in action_plan), default=30,
    )
    # remote OTLP demo: alert clearing waits on the 60s OTLP export cadence +
    # the rule's for-window, so remote flag flips get a longer generous budget
    remote_flip = action_plan and remote_flag_authorized and any(
        a.get("tool") == "set_feature_flag" for a in action_plan
    )
    if remote_flip:
        grace_period = max(REMOTE_FLAG_EFFECT_SECONDS, grace_period)

    # gate-4: verification initiate safe defaults
    alert_name = state.get("alert_name", "unknown")
    verified = False
    verified_at_iso = None
    business_mttr = None
    final_p99 = None
    final_error = None
    verification_evidence = None
    t_clear_polled = None


    if all_success:
        print(f"[execute] Verifying recovery for {alert_name} (service={service})...")
        verify_kwargs = {}
        if remote_flip:
            # alert clears ~5.5m after evals stop (5m window + AM group wait)
            # trust AM clearance as primary signal; shorter budget for demo speed
            verify_kwargs = {"polls": 14, "interval": 25}
        result = _verify_recovery(
            alert_name=alert_name,
            service=service,
            pre_metrics=pre_metrics,
            grace_period=grace_period,
            **verify_kwargs,
        )
        verified = result.verified
        final_p99 = result.p99_latency_s
        final_error = result.error_rate

        if verified:
            verified_at = datetime.datetime.utcnow()
            verified_at_iso = verified_at.isoformat()
            t_clear_polled = result.t_clear
            business_mttr = int((verified_at - start_ts).total_seconds())
            verification_evidence = asdict(result)

    # B2 RecoveryChecker: deterministic ADDITIONAL signal, emitted after the
    # poll; verdicts unchanged this phase (checker_result is informational).
    checker_result = None
    if _recovery_checker_enabled():
        from src.graph.recovery_checker import check_recovery
        try:
            checker_result = check_recovery(
                result if all_success else VerificationResult(verified=False),
                pre_metrics,
                t_action_end=action_executed_at_iso,
            )
            print(f"[execute] RecoveryChecker verdict: {checker_result.get('verdict')}")
        except Exception as e:
            print(f"[execute] RecoveryChecker failed (non-fatal): {e}")
            checker_result = {"verdict": "error", "reason": str(e)}


    if all_success and verified:
        status = ResolutionStatus.RESOLVED.value
        print(f"[execute] Done: Business MTTR={business_mttr}s Agent MTTR={agent_mttr}s success=True  verified=True")
    elif all_success:
        status = ResolutionStatus.EXECUTED_UNVERIFIED.value
        print(f"[execute] Done: Agent MTTR={agent_mttr}s  success=True  verified=False — metric did not improve")
    else:
        status = ResolutionStatus.FAILED.value
        print(f"[execute] Done: Agent MTTR={agent_mttr}s  success=False")

    # gate 5 : attribution uses TRUE recovery time (range-backfill)
    # not the polled timestamp above
    tool_name = action_plan[0]["tool"] if action_plan else ""
    tolerance = DELTA_EFFECT_SECONDS.get(tool_name, 30) + VERIFICATION_OVERHEAD_SECONDS
    signal = verification_evidence.get('signal') if verification_evidence else None
    expr, threshold, comparison = _signal_expr(service, signal)

    polled_healthy = verification_evidence.get("healthy_run_len", 0) if verification_evidence else 0
    ev = build_clear_evidence(
        expr=expr, 
        threshold=threshold,
        t_action_start=action_start_ts,
        t_action_end=action_executed_at,
        t_now=datetime.datetime.utcnow(),
        comparison=comparison,
        t_clear_polled=t_clear_polled,
        polled_healthy_run_len=polled_healthy,
    )
    g3 = evaluate_gate3(ev, action_executed_at.timestamp(), tolerance_sec=tolerance)
    gate1_claim = bool(claim_id and t_claim)
    gate2_target_bound = any(e["success"] and e["target_bound"] for e in execution_evidence)
    
    #gate4 stay tied to the polling based verified flag
    # when recovery happened, gate 4 is the smpler did it recover at all check
    gate4_verified = bool(verified)
    # gate 5 requires at least 2 consecutive healthy observations.
    # Ceiling: bounded by verification polling duration.asynchronous post-incident monitoring.
    gate5_stable = bool(ev.healthy_run_len and ev.healthy_run_len >= 2)
    
    resolution_cause = final_label(
        gate1_claim=gate1_claim,
        gate2_target_bound=gate2_target_bound,
        gate3=g3,
        gate4_verified=gate4_verified,
        gate5_stable=gate5_stable,

        shadow_arm=(bool(state.get("shadow_execution"))),
    )
    print(f"[attribution] resolution_cause={resolution_cause} lead={g3.lead_seconds} reason={g3.reason}")

    # record outcome in knowledge graph for future incident correlation
    append_past_incident(
        incident_id = state["incident_id"],
        alert_name = state.get("alert_name", "unknown"),
        service = service,
        root_cause = state.get("root_cause") or "unknown",
        action_taken = "; ".join(a["action"] for a in action_plan),
        business_mttr_seconds = business_mttr,
        success = all_success and verified,
        resolution_cause =resolution_cause,
        verified_at = verified_at_iso,
        severity = state.get("severity"),
    )

    return {

        "action_plan": updated_plan,
        "action_taken": "; ".join(a["action"] for a in action_plan),
        "resolution_status": status,
        "verified": verified,
        "action_executed_at": action_executed_at_iso,
        "verified_at": verified_at_iso,
        "business_mttr_seconds": business_mttr,
        "agent_mttr_seconds": agent_mttr,
        "final_p99_latency_s": final_p99,
        "error_rate": final_error,
        "resolution_notes": f"{len(action_plan)} action(s) executed. Business MTTR: {business_mttr}s, Agent MTTR: {agent_mttr}s. Verified: {verified}",

        # 5 gate attribution fields
        "claim_id": claim_id,
        "t_claim": t_claim,
        "execution_evidence": execution_evidence,
        "verification_evidence": verification_evidence,
        "resolution_cause": resolution_cause,
        "shadow_execution": (bool(state.get("shadow_execution"))),

        # true recovery fields 
        "t_clear_true": ev.t_clear_true,
        "t_clear_polled": t_clear_polled,
        "clear_source": ev.clear_source,
        "healthy_run_len": ev.healthy_run_len,
        "samples_seen": ev.samples_seen,
        "attribution_lead_seconds": g3.lead_seconds,
        "gate3_reason": g3.reason,
        "gate1_claim":gate1_claim,
        "gate2_target_bound":gate2_target_bound,
        "gate3_temporal":g3.passed,
        "gate4_verified":gate4_verified,
        "gate5_stable":gate5_stable,
        "executed_action_keys": executed_keys,
        "checker_result": checker_result,
        **({"receipts": receipts} if _executor_receipts_enabled() else {}),

    }

