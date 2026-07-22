from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from src.graph.state import AttributionStatus, AgentState, ResolutionCause
from src.graph.routing import DELTA_EFFECT_SECONDS

def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """parses iso strings strictly to timezone aware UTC datetimes"""

    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00.00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"Invalid timestamp format for {ts}: {e}")


def classify_attribution(state: AgentState) -> Dict[str, Any]:
    """
    evaluates gates 1-4 of the the five gate attributin test at incident close
    """

    # if gate-1: claim
    if not state.get("claim_id") or not state.get("t_claim"):
        return {
            "resolution_cause": ResolutionCause.NATURAL_CALM.value,
            "attribution_status": AttributionStatus.FINAL.value,
        }

    # if gate-2: execution (must be bounf to the correct target)\
    evidence = state.get("execution_evidence", [])
    gate2 = any(e.get("success") and e.get("target_bound") for e in evidence)
    if not gate2:
        return {
            "resolution_cause": ResolutionCause.ACTION_FAILED.value,
            "attribution_status": AttributionStatus.FINAL.value,
        }

    # if gate-3: temporal window
    t_claim = parse_ts(state.get("t_claim"))
    t_clear = parse_ts(state.get("t_clear"))

    if not t_claim or not t_clear or t_clear < t_claim:
        return {
            "resolution_cause": ResolutionCause.AMBIGUOUS.value,
            "attribution_status": AttributionStatus.FINAL.value,
        }

    # find max allowed clear time across all executed actuons
    max_allowed_clear_time = None
    for e in evidence:
        t_action_end = parse_ts(e.get("t_action_end"))
        if t_action_end:
            delta = DELTA_EFFECT_SECONDS.get(e.get("tool", ""), 30)
            allowed_time = t_action_end + timedelta(seconds=delta)
            if max_allowed_clear_time is None or allowed_time > max_allowed_clear_time:
                max_allowed_clear_time = allowed_time
    
    gate3 = max_allowed_clear_time is not None and t_clear <= max_allowed_clear_time
    if not gate3:
        return {
            "resolution_cause": ResolutionCause.AMBIGUOUS.value,
            "attribution_status": AttributionStatus.FINAL.value,
        }
    
    # gate-4: independent verification
    gate4 = bool(state.get("verified")) and state.get("verification_evidence") is not None

    if gate2 and gate2 and gate4:
        return {
            "resolution_cause": ResolutionCause.AGENT_REMEDIATED.value,
            "attribution_status": AttributionStatus.PROVISIONAL.value
        }

    return {
        "resolution_cause": ResolutionCause.AMBIGUOUS.value,
        "attribution_status": AttributionStatus.FINAL.value
    }

