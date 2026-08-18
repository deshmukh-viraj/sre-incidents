"""
attribution_gates.py
----------------------

decides if the agent actually fix the issue, or if the system just healed itself.

This file evaluates gate-3 (the temporal window) using the real recovery time
(provided by cleae_time.py, which looks back at prometheus history)

"""

from dataclasses import dataclass
from typing import Optional, Any

from src.graph.clear_time import ClearEvidence

AGENT_REMEDIATED = "AGENT_REMEDIATED"
NATURAL_CALM = "NATURAL_CALM"
ACTION_FAILED = "ACTION_FAILED"
AMBIGUOUS = "AMBIGUOUS"
HUMAN_REMEDIATED = "HUMAN_REMEDIATED"

@dataclass
class Gate3Result:
    passed: bool
    label_hint: str
    lead_seconds: Optional[float] #how long after we acted did it clear? -ve means it cleared before we acted
    reason: str

    def t_clear_ok(self) -> bool:
        """quick check to see if we actu found a valid recovery tiime"""
        return self.lead_seconds is not None


def evaluate_gate3(
    ev: ClearEvidence,
    t_action_end: float,
    *,
    tolerance_sec: float = 60.0,
    grace_sec: float = 5.0 
) -> Gate3Result:
    """
    looks at the real recovery evidence and decides if our action cause it.
    """
    #if it never recovered out action didnt work
    if ev.t_clear_true is None:
        return Gate3Result(
            passed=False,
            label_hint=ACTION_FAILED,
            lead_seconds=None,
            reason="metric never actually recovered in the window we checked"
        )

    #how long after we finished actiing did it take to clear?
    #a negative numbr means it cleared "before" we finished acting
    lead = ev.t_clear_true - t_action_end

    # was the system already fixing itself when we stepped in?
    if ev.already_healing:
        return Gate3Result(
            passed=False,
            label_hint=NATURAL_CALM,
            lead_seconds=lead,
            reason=(
                f"metric was already dropping before agent acted "
                f"(trend: {ev.per_action_trend_pct:.2%}). agent can not claim this one"
            )
        )

    # was it already green before we even started??
    if ev.per_action_healthy:
        return Gate3Result(
            passed=False,
            label_hint=NATURAL_CALM,
            lead_seconds=lead,
            reason="System was already healthy when agent started acting"
        )
    
    # dit it clear way before we fininshed?? 
    if lead < -grace_sec:
        return Gate3Result(
            passed=False,
            label_hint=NATURAL_CALM,
            lead_seconds=lead,
            reason=f"It recovered {abs(lead):.1f}s before agent finished acting"
        )
    
    # did it take way too long to clear?? if so, it was probably a coincidence
    if lead > tolerance_sec:
        return Gate3Result(
            passed=False,
            label_hint=AMBIGUOUS,
            lead_seconds=lead,
            reason=(
                f"It recovered {lead: .1f}s after agent acted. "
                f"Thats outside agent {tolerance_sec:.0f}s tolerance, so its probably not an agent"
            )
        )

    # if agent acted, and it recovered in realistic timeframe
    return Gate3Result(
        passed=True,
        label_hint=AGENT_REMEDIATED,
        lead_seconds=lead,
        reason=f"Recovered {lead:.1f}s after agent acted. Looks like agent fixed it"
    )


def final_label(
    gate1_claim: bool,
    gate2_target_bound: bool,
    gate3: Gate3Result,
    gate4_verified: bool,
    gate5_stable: bool,
    *,
    shadow_arm: bool = False
) -> str:
    """
    Mashes all 5 gates together inot final incident label.
    if shadow_arm is true, it mean we faked the execution, so agent can never claim
    credit for fixing it, even if all other gates pass.
    """
    if not gate1_claim:
        return NATURAL_CALM
    
    if not gate2_target_bound:
        return AMBIGUOUS

    #if we are running the shadow test, we didnt fix it
    if shadow_arm:
        return NATURAL_CALM if gate3.t_clear_ok() else ACTION_FAILED

    #if gate3 failed, use its reason
    if not gate3.passed:
        return gate3.label_hint

    if not gate4_verified:
        return ACTION_FAILED

    #if it flapped, we revoke credit
    if not gate5_stable:
        return AMBIGUOUS

    #all gates passed
    return AGENT_REMEDIATED
