"""
scenario_engine.py
Defines and executes the 6 core failure scenarios.
Each scenario is a timed sequence of state mutations applied to the shared SCENARIO object,
producing correlated anomalies across metrics, logs, and traces simultaneously.
"""

import time
import threading
import random
from dataclasses import dataclass, field
from typing import List, Callable, Optional
from generators.metrics_generator import SCENARIO


# Scenario phase: a snapshot of state to apply for a duration

@dataclass
class ScenarioPhase:
    name: str
    duration_seconds: float
    latency_multiplier: dict = field(default_factory=dict)
    error_rate_override: dict = field(default_factory=dict)
    rps_multiplier: dict = field(default_factory=dict)
    db_pool_saturation: dict = field(default_factory=dict)
    circuit_breaker_open: dict = field(default_factory=dict)
    on_enter: Optional[Callable] = None  # hook for logging/alerting side effects


@dataclass
class Scenario:
    id: str
    name: str
    description: str
    phases: List[ScenarioPhase]
    root_cause: str
    expected_alerts: List[str]
    slo_impact: str

# Scenario 1: Payment Latency Spike
# A downstream card rail throttles -> latency bleeds up through payment_gateway
# Pattern: gradual ramp -> peak -> slow recovery


SCENARIO_PAYMENT_LATENCY_SPIKE = Scenario(
    id="RB-001",
    name="payment_latency_spike",
    description="Card rails rate-limiting causes p99 latency to breach 2s SLO",
    root_cause="card_rails throttling due to downstream provider maintenance window",
    expected_alerts=[
        "PaymentGatewayP99LatencyHigh",
        "SLOErrorBudgetBurnRateFast",
        "DownstreamDependencyDegraded",
    ],
    slo_impact="Latency SLO breach: p99 > 2s for payment_gateway",
    phases=[
        ScenarioPhase(
            name="ramp_up",
            duration_seconds=60,
            latency_multiplier={"payment_gateway": 3.5, "card_rails": 5.0},
            error_rate_override={"payment_gateway": 0.04},
        ),
        ScenarioPhase(
            name="peak",
            duration_seconds=-1,
            latency_multiplier={"payment_gateway": 7.0, "card_rails": 12.0},
            error_rate_override={"payment_gateway": 0.12, "card_rails": 0.18},
        ),
        ScenarioPhase(
            name="slow_recovery",
            duration_seconds=90,
            latency_multiplier={"payment_gateway": 2.0},
            error_rate_override={"payment_gateway": 0.025},
        ),
        ScenarioPhase(
            name="resolved",
            duration_seconds=30,
        ),
    ],
)


# Scenario 2: Circuit Breaker Trip under Downstream Outage
# account_ledger's postgres replica fails -> ledger retries -> CB trips

SCENARIO_CIRCUIT_BREAKER_TRIP = Scenario(
    id="RB-002",
    name="circuit_breaker_trip",
    description="Postgres replica failover triggers circuit breaker cascade",
    root_cause="Postgres replica OOM → failover to primary → connection storm → CB trip on account_ledger",
    expected_alerts=[
        "CircuitBreakerOpen",
        "PaymentTransactionFailureRateHigh",
        "DBConnectionPoolNearExhaustion",
        "LedgerServiceUnavailable",
    ],
    slo_impact="Availability SLO breach: account_ledger error rate > 95% for 8 minutes",
    phases=[
        ScenarioPhase(
            name="db_failover",
            duration_seconds=30,
            db_pool_saturation={"postgres_primary": 0.85},
            latency_multiplier={"account_ledger": 4.0},
            error_rate_override={"account_ledger": 0.35},
        ),
        ScenarioPhase(
            name="circuit_breaker_opens",
            duration_seconds=-1,
            circuit_breaker_open={"payment_gateway": True, "account_ledger": True},
            error_rate_override={
                "payment_gateway": 0.95,
                "account_ledger": 0.97,
            },
            db_pool_saturation={"postgres_primary": 0.98},
        ),
        ScenarioPhase(
            name="half_open_probe",
            duration_seconds=60,
            error_rate_override={"account_ledger": 0.30},
            db_pool_saturation={"postgres_primary": 0.6},
        ),
        ScenarioPhase(
            name="resolved",
            duration_seconds=30,
            db_pool_saturation={"postgres_primary": 0.2},
        ),
    ],
)


# Scenario 3: Data Exfiltration Alert
# Anomalous bulk export + high-rate authenticated requests from single IP

SCENARIO_DATA_EXFILTRATION = Scenario(
    id="RB-003",
    name="data_exfiltration_alert",
    description="Credential stuffing attack leads to bulk PII export attempt",
    root_cause="Compromised API key used for automated bulk account data extraction",
    expected_alerts=[
        "AnomalousAPIRequestRate",
        "BulkDataExportDetected",
        "AuthenticationAnomalyFromSingleIP",
        "SIEMCorrelationAlertTriggered",
    ],
    slo_impact="Security incident — no direct SLO breach but compliance notification required",
    phases=[
        ScenarioPhase(
            name="credential_stuffing",
            duration_seconds=120,
            rps_multiplier={"api_gateway": 4.5},
            error_rate_override={"api_gateway": 0.08},  # 8% auth failures
        ),
        ScenarioPhase(
            name="bulk_export_active",
            duration_seconds=-1,
            rps_multiplier={"api_gateway": 12.0, "account_ledger": 8.0},
            error_rate_override={"api_gateway": 0.02},  # attacker is authenticated
        ),
        ScenarioPhase(
            name="rate_limit_kicks_in",
            duration_seconds=60,
            rps_multiplier={"api_gateway": 1.2},
            error_rate_override={"api_gateway": 0.45},  # 429s flooding
        ),
        ScenarioPhase(name="resolved", duration_seconds=30),
    ],
)



# Scenario 4: DB Connection Pool Exhaustion
# Memory leak in account_ledger v2.3.1 never closes connections

SCENARIO_DB_CONNECTION_EXHAUSTION = Scenario(
    id="RB-004",
    name="db_connection_exhaustion",
    description="Connection leak in account_ledger exhausts postgres connection pool",
    root_cause="Connection not closed in error path of ledger debit handler (PR #4721)",
    expected_alerts=[
        "DBConnectionPoolNearExhaustion",
        "DBConnectionPoolExhausted",
        "SlowQueryDetected",
        "LedgerTransactionLatencyHigh",
    ],
    slo_impact="Latency SLO degradation: account_ledger p99 > 5s; approaching availability breach",
    phases=[
        ScenarioPhase(
            name="leak_growing",
            duration_seconds=90,
            db_pool_saturation={"postgres_primary": 0.70},
            latency_multiplier={"account_ledger": 2.5},
        ),
        ScenarioPhase(
            name="pool_near_exhaustion",
            duration_seconds=60,
            db_pool_saturation={"postgres_primary": 0.92},
            latency_multiplier={"account_ledger": 8.0},
            error_rate_override={"account_ledger": 0.15},
        ),
        ScenarioPhase(
            name="pool_exhausted",
            duration_seconds=-1,
            db_pool_saturation={"postgres_primary": 1.0},
            error_rate_override={"account_ledger": 0.85},
            latency_multiplier={"account_ledger": 20.0},
        ),
        ScenarioPhase(
            name="rollback_applied",
            duration_seconds=60,
            db_pool_saturation={"postgres_primary": 0.3},
            latency_multiplier={"account_ledger": 1.2},
        ),
    ],
)


# Scenario 5: Compliance Audit Event (SOX, PCI-DSS)
# Simulates a triggered regulatory audit requiring evidence trail

SCENARIO_COMPLIANCE_AUDIT = Scenario(
    id="RB-005",
    name="compliance_audit_event",
    description="Automated PCI-DSS QSA audit trigger with evidence collection",
    root_cause="Quarterly PCI-DSS audit cycle — not an incident but requires SRE involvement",
    expected_alerts=[
        "ComplianceAuditTriggered",
        "EvidenceCollectionRequired",
        "PrivilegedAccessReviewRequired",
    ],
    slo_impact="No direct SLO impact — audit read-only access to prod logs and metrics",
    phases=[
        ScenarioPhase(
            name="audit_initiated",
            duration_seconds=30,
            rps_multiplier={"api_gateway": 1.05},  # audit tool traffic
        ),
        ScenarioPhase(
            name="evidence_collection",
            duration_seconds=180,
            rps_multiplier={"account_ledger": 1.3},  # log queries
        ),
        ScenarioPhase(name="audit_complete", duration_seconds=30),
    ],
)


# Scenario 6: Fraud Model Degradation
# ML model serving stale weights -> precision drops -> false positive surge

SCENARIO_FRAUD_MODEL_DEGRADATION = Scenario(
    id="RB-006",
    name="fraud_model_degradation",
    description="Fraud ML model drift causes false positive surge, blocking legitimate payments",
    root_cause="Feature pipeline failure silently fed stale features for 4h before model drift detected",
    expected_alerts=[
        "FraudModelPrecisionDrop",
        "FraudFalsePositiveRateSpiking",
        "PaymentDeclineRateAbnormal",
        "FraudModelLatencySpiking",
    ],
    slo_impact="Business SLO breach: payment decline rate > 5% (baseline: 0.8%)",
    phases=[
        ScenarioPhase(
            name="silent_drift",
            duration_seconds=120,
            # No direct latency/error impact — precision degrades silently
        ),
        ScenarioPhase(
            name="false_positive_surge",
            duration_seconds=180,
            error_rate_override={"payment_gateway": 0.08},  # legitimate payments declined
            latency_multiplier={"fraud_detector": 1.8},
        ),
        ScenarioPhase(
            name="feature_pipeline_fix",
            duration_seconds=60,
            error_rate_override={"payment_gateway": 0.015},
            latency_multiplier={"fraud_detector": 1.1},
        ),
        ScenarioPhase(name="resolved", duration_seconds=30),
    ],
)

# Scenario 7: Cascading Failure (DB Leak causes payment latency)
SCENARIO_CASCADING_FAILURE = Scenario(
    id="RB-CASCADE",
    name="cascading_failure",
    description="DB connection leak in account_ledger cause payment_gateway latenc to spike",
    root_cause="Connection leak in ledger cascading to payment gateway timeout",
    expected_alerts=[
        "DBConnectionPoolNearExhaustion",
        "PaymentGatewayP99LatencyHigh",
        "SLOErrorBudgetBurnRateFast",
    ],
    
    slo_impact="Latency SLO breach: p99 > 3s for payemnt_gateway; DB pool > 90%",
    phases=[
        ScenarioPhase(
            name="db_leak_starts",
            duration_seconds=60,
            db_pool_saturation={"postgres_primary": 0.75},
            # Both services are affected
            latency_multiplier={"Payment_gateway":2.0, "account_ledger": 3.0}, 
            error_rate_override={"account_ledger": 0.05},
        ),
        ScenarioPhase(
            name="cascading_peak",
            duration_seconds=-1,
            db_pool_saturation={"postgres_primary": 0.98}, #DB is dyiing
            latency_multiplier={"payment_gateway": 7.0, "account_ledger": 12.0}, #payment timeout waiting for db
            error_rate_override={"payment_gateway": 0.15, "account_ledger": 0.40},
        ),
        ScenarioPhase(
            name="recovery",
            duration_seconds=120,
            db_pool_saturation={"postgres_primary": 0.30},
            latency_multiplier={"payment_gateway": 1.2, "account_ledger": 1.5},
            error_rate_override={"payment_gateway": 0.01, "account_ledger": 0.01},

        ),
        ScenarioPhase(name="resolved", duration_seconds=30),
    ],
)

ALL_SCENARIOS = {
    s.name: s for s in [
        SCENARIO_PAYMENT_LATENCY_SPIKE,
        SCENARIO_CIRCUIT_BREAKER_TRIP,
        SCENARIO_DATA_EXFILTRATION,
        SCENARIO_DB_CONNECTION_EXHAUSTION,
        SCENARIO_COMPLIANCE_AUDIT,
        SCENARIO_FRAUD_MODEL_DEGRADATION,
        SCENARIO_CASCADING_FAILURE,
    ]
}


# Scenario executor
class ScenarioEngine:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._resolved_event = threading.Event()

    def run_scenario(self, scenario_name: str, on_phase_change: Optional[Callable] = None):
        """Execute a scenario synchronously (blocks until complete)."""
        scenario = ALL_SCENARIOS.get(scenario_name)
        if not scenario:
            raise ValueError(f"Unknown scenario: {scenario_name}. Available: {list(ALL_SCENARIOS.keys())}")

        print(f"\n[scenario] ->  Starting: {scenario.name}")
        print(f"[scenario]    Root cause: {scenario.root_cause}")
        print(f"[scenario]    Expected alerts: {', '.join(scenario.expected_alerts)}")

        SCENARIO.active_scenario = scenario.name
        self._resolved_event.clear()
        
        try:
            for phase in scenario.phases:
                if self._stop_event.is_set():
                    break


                SCENARIO.latency_multiplier = phase.latency_multiplier
                SCENARIO.error_rate_override = phase.error_rate_override
                SCENARIO.rps_multiplier = phase.rps_multiplier
                SCENARIO.db_pool_saturation = phase.db_pool_saturation
                SCENARIO.circuit_breaker_open = phase.circuit_breaker_open

                if phase.on_enter:
                    phase.on_enter()
                if on_phase_change:
                    on_phase_change(scenario.name, phase.name)
                
                if phase.duration_seconds == -1:
                    print(f"[scenario]   Phase: {phase.name} (infinite- waiting for manual resolution)")
                    while not self._stop_event.is_set() and not self._resolved_event.is_set():
                        time.sleep(1)
                
                else: 
                    print(f"[scenario]   Phase: {phase.name} ({phase.duration_seconds}s)")
                    self._stop_event.wait(timeout=phase.duration_seconds)
        
        
        finally:
            SCENARIO.reset()
            print(f"[scenario]   Scenario complete: {scenario.name}\n")

    def run_scenario_async(self, scenario_name: str, on_phase_change: Optional[Callable] = None):
        """Run a scenario in background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run_scenario,
            args=(scenario_name, on_phase_change),
            daemon=True,
        )
        self._thread.start()
    
    def resolve(self):
        self._resolved_event.set()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        SCENARIO.reset()

    def run_training_sequence(self, interval_between: float = 30.0):
        """Run all scenarios in sequence — for SRE training sessions."""
        for name in ALL_SCENARIOS:
            print(f"\n{'='*60}")
            print(f"TRAINING: {name}")
            print(f"{'='*60}")
            self.run_scenario(name)
            print(f"[training] Cooldown {interval_between}s before next scenario...")
            self._stop_event.wait(timeout=interval_between)
            if self._stop_event.is_set():
                break