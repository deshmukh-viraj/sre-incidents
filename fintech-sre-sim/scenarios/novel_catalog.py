"""
scenarios/novel_catalog.py

catalog of novel/unknown incident scenarios for axis1 and axis2 evaluation. 
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class NovelScenario:
    """a novel incident scenario with evaluation oracle."""
    scenario_id: str
    name: str
    description: str
    root_cause: str  #oracle ground truth for llm-judge comparison
    axis: str  # "axis1_unlabeled" or "axis2_anomalous"
    trigger_alert: str  # alert name that should fire
    expected_severity: str  #SEV1/SEV2/SEV3
    acceptable_actions: List[str]  #allowed tool names for this scenario
    expected_evidence: List[str] = field(default_factory=list)  #expected signal patterns
    #for axis2:the runbook label that gets attached but is wrong/incomplete
    anomalous_runbook: Optional[str] = None
    #extra signals to inject for axis2 to make the runbook anomalous
    extra_signals: Dict[str, Any] = field(default_factory=dict)
    #duration to wait after triggering scenario before checking (seconds)
    stabilization_wait: int = 90


#unlabeled novel incidents (no runbook label on alert)
#these fire alerts that deliberately have no runbook label

AXIS1_SCENARIOS = [
    NovelScenario(
        scenario_id="NO-0001",
        name="novel_multi_signal_degradation",
        description="Ambiguous multi-signal degradation: correlated latency spikes and DB pool saturation without standard runbook patterns.",
        root_cause="Concurrent DB connection pool saturation in account_ledger combined with payment gateway downstream thread contention.",
        axis="axis1_unlabeled",
        trigger_alert="SLOBurnUnknown", #our new catch-all rule
        expected_severity="SEV2",
        acceptable_actions=["notify", "restart_service", "capture_diagnostics", "set_feature_flag"],
        expected_evidence=["p99_latency_s > 1.0", "db_pool_utilization > 0.50"],
        stabilization_wait=90,
    ),
]


#labeled-but-anomalous incidents
#alert fires with a runbook label, but live signals contradict the runbook blast pattern
# _apply_residual_check in routing.py caps confidence -> routes to deep_investigation

AXIS2_SCENARIOS = [
    NovelScenario(
        scenario_id="NO-0002",
        name="mislabeled_rb001_with_db_pressure",
        description="PaymentGatewayP99LatencyHigh fires with runbook=RB-001, but DB pool pressure (unexplained by RB-001) is concurrently high.",
        root_cause="DB connection pool exhaustion masquerading as card rails latency — runbook RB-001 cannot explain the DB pool signal.",
        axis="axis2_anomalous",
        trigger_alert="PaymentGatewayP99LatencyHigh",
        expected_severity="SEV1",
        acceptable_actions=["restart_service", "kill_query", "notify", "capture_diagnostics"],
        expected_evidence=["p99_latency_s > 1.5", "db_pool_utilization >= 0.80"],
        anomalous_runbook="RB-001",
        extra_signals={"db_pool_utilization": 0.85}, #injected via webhook to trigger residual gate
        stabilization_wait=90,
    ),
    NovelScenario(
        scenario_id="NO-0003",
        name="mislabeled_rb004_with_throttle",
        description="DBConnectionPoolExhausted fires with runbook=RB-004, but card rails throttle rate is concurrently high.",
        root_cause="Card rails throttling causing payment latency, not DB exhaustion — runbook RB-004 cannot explain the throttle signal.",
        axis="axis2_anomalous",
        trigger_alert="DBConnectionPoolExhausted",
        expected_severity="SEV1",
        acceptable_actions=["set_feature_flag", "notify", "capture_diagnostics"],
        expected_evidence=["db_pool_utilization > 0.98", "throttle_error_rate >= 0.05"],
        anomalous_runbook="RB-004",
        extra_signals={"throttle_error_rate": 0.06},
        stabilization_wait=90,
    ),
]


#all novel scenarios combined
NOVEL_CATALOG = AXIS1_SCENARIOS + AXIS2_SCENARIOS


#export for easy iteration
__all__ = [
    "NovelScenario",
    "AXIS1_SCENARIOS",
    "AXIS2_SCENARIOS",
    "NOVEL_CATALOG",
]