"""
graph/recovery_checker.py
"""

import datetime
from typing import Any, Dict, Optional

from src.agents.schemas import VerificationResult
from src.graph.routing import P99_LATENCY_WARNING, ERROR_RATE_WARNING
from src.graph.timeutils import parse_ts

RECOVERY_CHECKER_VERSION = "recovery-checker-v1"


def check_recovery(
    verification: VerificationResult,
    pre_metrics: Dict[str, Any],
    t_action_end: Optional[str] = None,
) -> Dict[str, Any]:
    """classify the poll outcome deterministically:
    
    - not_recovered:   poller says metrics did not recover
    - flap_risk    :   poller claims recovery while the observed values still
                       breach warning thresholds (alert clear + metrics bad,
                       or a run too thin to trust)
    - recovered    :   verified AND final observed values within thresholds
    """
    p99 = verification.p99_latency_s
    err = verification.error_rate

    result: Dict[str, Any] = {
        "checker_version": RECOVERY_CHECKER_VERSION,
        "verdict": "not_recovered",
        "verified": bool(verification.verified),
        "p99_latency_s": p99,
        "error_rate": err,
        "signal": verification.signal,
        "healthy_run_len": verification.healthy_run_len,
        "checked_at": datetime.datetime.utcnow().isoformat(),
        "pre_p99_latency_s": pre_metrics.get("p99_latency_s"),
        "pre_error_rate": pre_metrics.get("error_rate"),
        "lead_from_action_s": None,
    }

    if t_action_end and verification.t_clear:
        try:
            result["lead_from_action_s"] = round(
                (parse_ts(verification.t_clear) - parse_ts(t_action_end)).total_seconds(), 3)
        except (ValueError, TypeError):
            pass

    if not verification.verified:
        result["reason"] = "poller exhausted without sustained healthy run"
        return result

    still_breaching = (
        (p99 is not None and p99 >= P99_LATENCY_WARNING)
        or (err is not None and err >= ERROR_RATE_WARNING)
    )
    thin_run = verification.healthy_run_len < 2

    if still_breaching or thin_run:
        result["verdict"] = "flap_risk"
        result["reason"] = (
            f"recovery claimed but still_breaching={still_breaching}, "
            f"healthy_run_len={verification.healthy_run_len} (<2) — do not trust yet"
        )
        return result

    result["verdict"] = "recovered"
    result["reason"] = (
        f"verified via {verification.signal or 'metrics'} with "
        f"healthy_run_len={verification.healthy_run_len}"
    )
    return result
