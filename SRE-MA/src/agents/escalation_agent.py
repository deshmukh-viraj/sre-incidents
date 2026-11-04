"""
agents/escalation_agent.py
---------------------------
Escalation agent: called when diagnosis confidence is too low after all attempts.

Responsibility:
    - build a structured escalation report (loops, tokens, best hypothesis)
    - notify the on-call team via Slack
    - mark the incident as ESCALATED in state
    - B7: on budget/loop exhaustion, build the handoff package (reason + options)
      so the run can land in human_gate (flag HANDOFF_ENABLED, default 0)

Writes to state:
    resolution_status, escalation_message, resolved_at,
    handoff_to_human (package build — the transfer itself happens in human_gate)
"""

import datetime
import os

from src.graph.state import AgentState, ResolutionStatus
from src.graph.routing import MAX_DIAGNOSIS_LOOPS
from src.tools.sre_tool import notify_slack
from src.agents.schemas import HandoffMessage

HANDOFF_OPTIONS = [
    "approve: execute the suggested plan",
    "take_over: human remediation from here",
    "dismiss: close the incident as reviewed",
]


def _evidence_package(state: AgentState, cap: int = 4) -> str:
    """the investigative artifacts a human handoff must carry: actual tool
    results the agent collected, plus the diagnosed issues. empty -> ''."""
    tool_results = [
        str(e)[6:140]
        for e in state.get("investigation_scratchpad", [])
        if str(e).startswith("tool: ")
    ][-cap:]
    issues = state.get("diagnosis_issues") or []
    lines = []
    if tool_results:
        lines.append("Investigation evidence (tool results):\n  - " + "\n  - ".join(tool_results))
    if issues:
        lines.append("Diagnosis issues: " + "; ".join(str(i) for i in issues[:3]))
    return ("\n" + "\n".join(lines)) if lines else ""


def _handoff_enabled() -> bool:
    """B7 flag (default 0; MULTI_AGENT=1 enables all upgrades)."""
    return os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("HANDOFF_ENABLED", "0") == "1"


def _exhaustion_cause(state: AgentState) -> str | None:
    """budget/loop exhaustion detector — the only trigger for a handoff package."""
    if state.get("budget_exhausted"):
        return "budget_exhausted"
    if int(state.get("diagnosis_loops", 0) or 0) >= MAX_DIAGNOSIS_LOOPS:
        return "diagnosis_loops_exhausted"
    if int(state.get("critic_reject_count", 0) or 0) >= 3:
        return "critic_rejected_after_retry"
    return None


def route_after_escalate(state: AgentState):
    """B7: an UNPRESENTED handoff package lands in human_gate; everything else
    keeps the pre-change escalate -> communicator edge."""
    pkg = state.get("handoff_to_human") or {}
    if _handoff_enabled() and pkg and not pkg.get("presented"):
        print("[router] escalate -> human_gate (handoff package pending)")
        return "human_gate"
    return "communicator"


# Node 8: escalate

def escalate_node(state: AgentState) -> dict:
    """
    call this when we've tried everything and still don't know what's wrong.
    sends a slack message to wake up the on-call person.
    """
    print(f"\n[escalate] Escalating {state['incident_id']}, evaluating escalation reason....")

    incident_id = state['incident_id']
    severity = state.get('severity', 'unknown')
    alert_name = state.get('alert_name', 'unknown')

    #determine the reason for escalation
    if state.get('diagnosis_mode') == "llm" and state.get("diagnosis_loops", 0):
        reason = "DIAGNOSIS_EXHAUSTED"
        hypotheses = state.get("hypotheses", [])
        # best = max confidence, not first written — the handoff cites the strongest theory
        best = max(hypotheses, key=lambda h: h.get("confidence", 0)) if hypotheses else None
        best_guess = (best or {}).get("hypothesis", "Unknown") if hypotheses else "Unknown"
        max_conf = max((h.get("confidence", 0) for h in hypotheses), default=0)

        message = (
            f" *AI diagnosis failed*\n"
            f"Incident: '{incident_id}' | Severity: {severity}\n"
            f"Alert: '{alert_name}'\n"
            f"Best Guess: {best_guess} (Conf: {max_conf:.0%})\n"
            f"{_evidence_package(state)}\n"
            f"Action Required: Manual root cause analysis needed"
        )
    
    elif state.get("verified") == False and state.get("action_taken"):
        reason = "REMEDIATION_FAILED"
        action_taken = state.get("action_taken")
        current_p99 = state.get("final_p99_latency_s", "N/A")

        message = (
            f" *AI Remediation Failed*\n"
            f"Incident: {incident_id} | Severity: {severity}\n"
            f"Alert: {alert_name}\n"
            f"Action Attempted: {action_taken}\n"
            f"Current P99: {current_p99}s (still breaching)\n"
            f"Action Required: Manual fix or revert required"
        )
    
    else:
        #escalate with the actual finding, proposed action, and policy rejections  never a bare generic line
        reason = "UNKNOWN"
        hypotheses = state.get("hypotheses", [])
        best = max(hypotheses, key=lambda h: h.get("confidence", 0)) if hypotheses else None
        best_guess = (state.get("root_cause") or (best or {}).get("hypothesis")) or "no diagnosis reached"
        llm_action = state.get("llm_suggested_action")
        rejections = state.get("policy_rejections") or []

        message = (
            f" *AI Investigation Inconclusive*\n"
            f"Incident: '{incident_id}' | Severity: {severity}\n"
            f"Alert: '{alert_name}'\n"
            f"Finding: {best_guess}\n"
            + (f"Proposed Action: {llm_action}\n" if llm_action else "")
            + (f"Policy Rejections: {'; '.join(str(r.get('reason', r)) for r in rejections)}\n" if rejections else "")
            + f"{_evidence_package(state)}\n"
            + f"Action Required: Manual root cause analysis needed"
        )

    print(f"\n[escalate] {reason}: {incident_id}")
    print(message)

    # the SEV1 early-page hop already woke the on-call; re-paging is noise —
    # the human just needs the terminal status so they can cancel or take over
    if state.get("early_paged"):
        print("[escalate] early page already delivered — recording status without a second page")
        message = (f"{message}\n\n(early page was delivered while the agent kept working; "
                   f"latest status above — cancel/alert-hold if the fix verified)")
    else:
        notify_slack(
            message=message,
            channel="incidents",
            severity="critical",
            incident_id=incident_id
        )

    return {
        "resolution_status": ResolutionStatus.ESCALATED.value,
        "escalation_message": message,
        "resolved_at": datetime.datetime.utcnow().isoformat(),
        **_handoff_package(state, message, reason),
    }


def _handoff_package(state: AgentState, message: str, reason: str) -> dict:
    """B7: build the handoff package ONCE (the single package builder). The
    transfer itself (ownership + count) happens in human_gate_node."""
    if not _handoff_enabled():
        return {}
    cause = _exhaustion_cause(state)
    if not cause or state.get("handoff_to_human") or int(state.get("handoff_count", 0) or 0):
        return {}
    pkg = HandoffMessage(
        timestamp=datetime.datetime.utcnow().isoformat(),
        from_worker="escalate", to_worker=None, kind="handoff",
        content=message, options=list(HANDOFF_OPTIONS),
    ).model_dump()
    pkg["reason"] = reason
    pkg["cause"] = cause
    print(f"[escalate] handoff package built (cause={cause}, options={len(pkg['options'])})")
    return {"handoff_to_human": pkg}
