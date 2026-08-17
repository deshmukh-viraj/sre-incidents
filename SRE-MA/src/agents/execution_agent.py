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

from dataclasses import asdict, dataclass
from langgraph.types import interrupt
from src.graph.state import AgentState, ResolutionStatus
from src.tools.sre_tool import execute_remediation, notify_slack, check_alert_status
from src.tools.prometheus_tool import collect_incident_signals
from src.tools.kg_tool import append_past_incident


from src.graph.routing import DELTA_EFFECT_SECONDS, P99_LATENCY_RECOVERY, ERROR_RATE_WARNING, VERIFICATION_OVERHEAD_SECONDS
from src.graph.clear_time import build_clear_evidence
from src.graph.attribution_gates import evaluate_gate3, final_label
from typing import Optional, Dict, Any, List



@dataclass
class VerificationResult:
    verified: bool
    p99_latency_s: Optional[float] = None
    error_rate: Optional[float] = None
    t_clear: Optional[str] = None
    signal: Optional[str] = None 

# node 6: human gate 

def human_gate_node(state: AgentState) -> dict:
    """
    check if action requires human approval
    if yes, pauses the graph (interrupt). if no, passes through
    """
    incident_id = state['incident_id']
    
    #check if any action in the plan requires approval
    needs_approval = any(a.get("requires_approval", False) for a in state.get("action_plan", []))

    # deterministic human approval 
    if not needs_approval or os.getenv("SIM_AUTO_APPROVE", "false").lower() == "true":
        print(f"\n[human_gate] Auto approved for {incident_id} (requires_approval={needs_approval})")
        return {"human_approved": True, "approval_timeout": False}

    # llm /destructive (requires approval)
    print(f"[human_gate] Interrupting graph for {incident_id} awaiting for human approval...")

    actions_text = ""
    for a in state.get("action_plan", []):
        if a.get("requires_approval"):
            actions_text += f" {a['action']}\n"

    #send a detailed slack message to ask for approval
    message = (
        f" *Approval Required for {incident_id}*\n"
        f"*Alert*: {state.get('alert_name', 'Unknown')}\n"
        f"*Diagnosis*: {state.get('diagnosis_summary') or 'Unknown'}\n"
        f"*Proposed Actions*:\n{actions_text}\n"
        f"Please approve via the API or dashboard."
    )
    notify_slack(message=message, channel="incidents", severity="warning", incident_id=incident_id)

    human_decsion = interrupt({
        "question": "Do you approve the action plan?",
        "incident_id": incident_id
    })
    print(f"[human gate] Resumed {incident_id} with human input: {human_decsion}")
    return {
        "human_approved": human_decsion.get("approved", False), 
        "approved_by": human_decsion.get("approver", "system"),
        "approval_timeout": False

    }

def _verify_recovery(
    alert_name: str,
    service: str, 
    pre_metrics: dict,
    grace_period: int = 30,
    polls: int = 5,
    interval: int = 15,
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

    print(f"[execute] waiting {grace_period}s grace period for action to propagate...")
    time.sleep(grace_period)

    for i in range(polls):
        print(f"[execute] verification poll {i+1}/{polls}:")

        #secondary: quick alertmanager check
        am_state = check_alert_status(alert_name)
        if am_state not in ("firing", "unknown"):
            print(f"[execute] Alert explicitly resolved in Alertmanager")
            signals = collect_incident_signals(service, window="1m")
            curr_p99 = signals.get("p99_latency_s")
            curr_error = signals.get("error_rate")
            
            # return structured dataclass with t_clear and signal type
            return VerificationResult(
                verified=True,
                p99_latency_s=curr_p99,
                error_rate=curr_error,
                t_clear=datetime.datetime.utcnow().isoformat(),
                signal="alertmanager"
            )

        #primary: prometheus metric comparison 
        print(f"[execute] Checking prometheus metrics for {service}...")
        signals = collect_incident_signals(service, window="1m")
        curr_p99 = signals.get("p99_latency_s")
        curr_error = signals.get("error_rate")
       

        # check if p99 latency improved (dropped below warning threshold OR improved by >50%)
        p99_ok = False
        if curr_p99 is not None:
            if curr_p99 < P99_LATENCY_RECOVERY:
                p99_ok = True
                print(f"[execute] p99={curr_p99:.3f}s < {P99_LATENCY_RECOVERY}s threshold")
            elif pre_p99 is not None and curr_p99 < pre_p99 * 0.5:
                p99_ok = True
                print(f"[execute] p99 improved {pre_p99:.3f}s -> {curr_p99:.3f}s")
            else:
                print(f"[execute] p99={curr_p99:.3f}s still elevated (pre={pre_p99}s, threshold={P99_LATENCY_RECOVERY}s)")

        # check if error rate improved
        error_ok = False
        if curr_error is not None:
            if curr_error < ERROR_RATE_WARNING:
                error_ok = True
                print(f"[execute] error_rate={curr_error:.4f} < {ERROR_RATE_WARNING} threshold")
            elif pre_error is not None and curr_error < pre_error * 0.5:
                error_ok = True
                print(f"[execute] error_rate improved {pre_error:.4f} ->{curr_error:.4f} ")
            else:
                print(f"[execute] error_rate={curr_error:.4f} still elevated (pre={pre_error}, threshold={ERROR_RATE_WARNING})")

        # if either key metric recovered, count it as verified
        if p99_ok or error_ok:
            print(f"[execute] Prometheus metrics confirm recovery (p99_ok={p99_ok}, error_ok={error_ok})")
            return VerificationResult(
                verified=True,
                p99_latency_s=curr_p99,
                error_rate=curr_error,
                t_clear=datetime.datetime.utcnow().isoformat(),
                signal="p99" if p99_ok else "error_rate"
            )

        print(f"[execute] Metrics not yet recovered. Waiting {interval}s before next poll...")
        time.sleep(interval)

    print(f"[execute] Exhausted {polls} polls — metrics did not recover")
    return VerificationResult(
        verified=False,
        p99_latency_s=curr_p99,
        error_rate=curr_error,
        t_clear=None,
        signal=None
    )

def _signal_expr(service: str, signal: Optional[str]) -> tuple[str, float, str]:
    """
    maps the verification signal (which metric actually recovered) to the prometheus expression
    threshold, comparison that gates3's range query needs to ask the same questiion against real history
    """
    if signal == "error_rate":
        expr = (
            f'sum(rate(http_requests_total{{service="{service}", status_code=~"5.."}}[1m])) '
            f'/ sum(rate(https_requests_total{{service="{service}"}}[1m]))'
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
    print(f"\n[execute] Executing action plan for {state['incident_id']}")

    # gate-1: claim (capture the timestamp the moment the agent takes the wheel)
    t_claim = datetime.datetime.utcnow().isoformat()
    claim_id = state["incident_id"]

    action_plan = state.get("action_plan", [])
    start_ts = datetime.datetime.fromisoformat(state["alert_started_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    invoked_ts = datetime.datetime.fromisoformat(state["agent_invoked_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    approved_by = state.get("approved_by")

    all_success = True
    updated_plan = []
    
    #gate-2: executionm evidence list
    execution_evidence: List[Dict[str, Any]] = []

    # extract service name from state for post-remediation metric checks
    affected_services = (state.get("affected_services") or ["payment_gateway"])
    service = affected_services[0]

    # snapshot pre-remediation metrics so we can compare after  
    pre_metrics = state.get("raw_signals", {})
    print(f"[execute] Pre-remediation snapshot: p99={pre_metrics.get('p99_latency_s')}, error_rate={pre_metrics.get('error_rate')}")
    
    is_shadow = bool(state.get("shadow_execution"))
    if is_shadow:
        print(f"[execute] SHADOW mode active for {state['incident_id']} — suppressing execution")

    action_start_ts = datetime.datetime.utcnow()
    for action in action_plan:
        if is_shadow:
            print(f"[execute] SHADOW: suppressing {action['action']}")
            result = {"success": True, "result": "shadow, no-op", "dry_run": True}
        else:
            result = execute_remediation(
                action=action["action"],
                tool=action["tool"],
                params=action["params"],
            )
        
        # gate-2: executin and target binding
        target_service = action["params"].get("service", service)
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
        result = _verify_recovery(
            alert_name=alert_name,
            service=service,
            pre_metrics=pre_metrics,
            grace_period=grace_period
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

    ev = build_clear_evidence(
        expr=expr, 
        threshold=threshold,
        t_action_start=action_start_ts,
        t_action_end=action_executed_at,
        t_now=datetime.datetime.utcnow(),
        comparison=comparison,
        t_clear_polled=t_clear_polled,
    )
    g3 = evaluate_gate3(ev, action_executed_at.timestamp(), tolerance_sec=tolerance)
    gate1_claim = bool(claim_id and t_claim)
    gate2_target_bound = any(e["success"] and e["target_bound"] for e in execution_evidence)
    
    #gate4 stay tied to the polling based verified flag
    # when recovery happened, gate 4 is the smpler did it recover at all check
    gate4_verified = bool(verified)
    gate5_stable = True 
    
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
        
    }

