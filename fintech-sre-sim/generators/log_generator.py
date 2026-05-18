"""
log_generator.py
Emits structured JSON logs that correlate with traces via shared trace_id/span_id.
Log patterns are calibrated to real fintech systems: PCI-relevant fields masked,
regulatory event markers, and anomaly patterns tied to scenario state.
"""

import json
import time
import uuid
import random
import hashlib
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from generators.metrics_generator import SERVICES, SCENARIO


# PAN masking and data sanitisation helpers

def mask_pan(pan: str) -> str:
    """PCI-DSS compliant PAN masking: first 6, last 4 only."""
    return f"{pan[:6]}{'*' * (len(pan) - 10)}{pan[-4:]}"

def mask_pii(value: str, field_type: str = "generic") -> str:
    """GDPR-compliant field masking with deterministic pseudonymisation."""
    if field_type == "email":
        parts = value.split("@")
        return f"{parts[0][:2]}***@{parts[1]}"
    if field_type == "account_id":
        return "ACC-" + hashlib.sha256(value.encode()).hexdigest()[:12].upper()
    return "***REDACTED***"

def fake_pan() -> str:
    """Generate a Luhn-valid test PAN (not a real card)."""
    # Use known test BINs (Visa test: 4111111111111111 range)
    prefix = random.choice(["411111", "555555", "378282"])
    middle = "".join([str(random.randint(0, 9)) for _ in range(9)])
    return mask_pan(prefix + middle + "1111")

def fake_account_id() -> str:
    return f"ACC-{uuid.uuid4().hex[:12].upper()}"

def fake_correlation_id() -> str:
    return f"corr-{uuid.uuid4()}"


# Log field templates per service and event type

# Endpoints per service drives log path variety
ENDPOINTS = {
    "payment_gateway": [
        "POST /v1/payments",
        "POST /v1/payments/authorize",
        "POST /v1/payments/capture",
        "POST /v1/refunds",
        "GET /v1/payments/{id}",
    ],
    "kyc_service": [
        "POST /v1/kyc/verify",
        "POST /v1/kyc/documents",
        "GET /v1/kyc/status/{id}",
    ],
    "fraud_detector": [
        "POST /v1/fraud/score",
        "POST /v1/fraud/report",
    ],
    "account_ledger": [
        "POST /v1/ledger/debit",
        "POST /v1/ledger/credit",
        "GET /v1/ledger/balance/{account_id}",
        "GET /v1/ledger/transactions",
    ],
    "notification_service": [
        "POST /v1/notify/email",
        "POST /v1/notify/sms",
        "POST /v1/notify/push",
    ],
    "api_gateway": [
        "POST /api/v2/pay",
        "GET /api/v2/account",
        "POST /api/v2/onboard",
    ],
}

ERROR_MESSAGES = {
    "500": [
        "Internal server error: unhandled exception in payment processor",
        "Database connection pool exhausted: max_connections reached",
        "Upstream dependency timeout: card_rails did not respond within 30s",
        "Serialization error: failed to deserialize payment response",
        "Unexpected nil pointer dereference in fraud scoring pipeline",
    ],
    "503": [
        "Service unavailable: circuit breaker OPEN for downstream: card_rails",
        "Rate limit exceeded: too many requests from merchant {merchant_id}",
        "Maintenance mode active",
        "Upstream health check failed: account_ledger returning 503",
    ],
    "504": [
        "Gateway timeout: upstream did not respond within 60s",
        "Database query timeout: query exceeded 30s limit",
        "External API timeout: card network response > 45s",
    ],
    "400": [
        "Validation error: amount must be positive integer",
        "Invalid currency code: {currency}",
        "Missing required field: payment_method",
        "Duplicate idempotency key: {key}",
    ],
    "401": [
        "Authentication failed: invalid API key",
        "JWT token expired",
        "Insufficient scopes for this operation",
    ],
}

SECURITY_EVENTS = [
    {
        "event_type": "authentication_failure",
        "severity": "WARN",
        "message": "Failed authentication attempt",
        "regulation": "PCI-DSS",
        "control": "8.3.6",
    },
    {
        "event_type": "privilege_escalation_attempt",
        "severity": "CRITICAL",
        "message": "User attempted to access admin endpoint without elevated role",
        "regulation": "SOX",
        "control": "ITGC-07",
    },
    {
        "event_type": "pii_access",
        "severity": "INFO",
        "message": "PII data accessed",
        "regulation": "GDPR",
        "control": "Art.5",
    },
    {
        "event_type": "bulk_data_export",
        "severity": "WARN",
        "message": "Bulk export of customer records initiated",
        "regulation": "GDPR",
        "control": "Art.32",
    },
    {
        "event_type": "anomalous_transaction_volume",
        "severity": "CRITICAL",
        "message": "Transaction volume 10x above baseline from single merchant",
        "regulation": "PCI-DSS",
        "control": "10.7",
    },
]


# Log record construction

def _build_base_record(
    service: str,
    level: str,
    message: str,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the base log record with OpenTelemetry-compatible fields."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": service,
        "message": message,
        "trace_id": trace_id or uuid.uuid4().hex,
        "span_id": span_id or uuid.uuid4().hex[:16],
        "correlation_id": fake_correlation_id(),
        "environment": "production-sim",
        "region": "us-east-1",
        "pod": f"{service}-{uuid.uuid4().hex[:8]}",
        "version": "2.3.1",
    }


def build_request_log(
    service: str,
    status_code: int,
    latency_ms: float,
    trace_id: str,
    span_id: str,
) -> Dict[str, Any]:
    """HTTP request access log record."""
    endpoints = ENDPOINTS.get(service, ["GET /unknown"])
    endpoint = random.choice(endpoints)
    level = "ERROR" if status_code >= 500 else ("WARN" if status_code >= 400 else "INFO")

    record = _build_base_record(service, level, f"HTTP {status_code} {endpoint}", trace_id, span_id)
    record.update({
        "http_method": endpoint.split()[0],
        "http_path": endpoint.split()[1],
        "http_status": status_code,
        "duration_ms": round(latency_ms, 2),
        "request_id": uuid.uuid4().hex,
    })

    if service == "payment_gateway" and status_code < 400:
        record.update({
            "payment": {
                "pan": fake_pan(),
                "amount_cents": int(random.lognormvariate(8.5, 1.2)),
                "currency": random.choice(["USD", "EUR", "GBP"]),
                "merchant_id": f"MID-{random.randint(100000, 999999)}",
                "transaction_id": f"TXN-{uuid.uuid4().hex[:16].upper()}",
                "account_id": mask_pii(fake_account_id(), "account_id"),
            }
        })
    if status_code >= 500:
        errors = ERROR_MESSAGES.get(str(status_code), ["Unknown error"])
        record["error"] = {
            "code": f"ERR_{status_code}_{service.upper()[:4]}",
            "message": random.choice(errors),
            "stack_trace": f"  at {service}.handler (/{service}/main.py:142)\n"
                           f"  at asyncio.run_forever (/python/asyncio/base_events.py:570)",
        }
    return record


def build_db_log(
    db: str,
    operation: str,
    duration_ms: float,
    rows_affected: int,
    trace_id: str,
) -> Dict[str, Any]:
    """Database query log record."""
    slow = duration_ms > 1000
    level = "WARN" if slow else "DEBUG"
    msg = f"Slow query detected: {operation} took {duration_ms:.0f}ms" if slow else f"DB {operation}"

    record = _build_base_record("db_proxy", level, msg, trace_id)
    record.update({
        "db": {
            "system": "postgresql",
            "name": db,
            "operation": operation,
            "duration_ms": round(duration_ms, 2),
            "rows_affected": rows_affected,
            "statement_hash": hashlib.md5(f"{db}{operation}".encode()).hexdigest()[:8],
        }
    })
    if slow:
        record["alert"] = "SLOW_QUERY"
    return record


def build_security_log(
    service: str,
    source_ip: str,
    user_id: str,
    trace_id: str,
) -> Dict[str, Any]:
    """Security/compliance event log record."""
    event = random.choice(SECURITY_EVENTS)
    record = _build_base_record(service, event["severity"], event["message"], trace_id)
    record.update({
        "security_event": {
            "event_type": event["event_type"],
            "regulation": event["regulation"],
            "control_reference": event["control"],
            "source_ip": source_ip,
            "user_id": mask_pii(user_id, "account_id"),
            "user_agent": "Mozilla/5.0 (compatible; sim-client/1.0)",
            "geo": random.choice(["US", "GB", "DE", "SG", "BR"]),
        },
        "audit_required": True,
        "siem_forwarded": True,
    })
    return record


def build_scenario_log(scenario_name: str, service: str, detail: str) -> Dict[str, Any]:
    """Annotated log for scenario injection points — ground-truth labels for ML training."""
    record = _build_base_record(service, "ERROR", detail)
    record.update({
        "__sim_scenario": scenario_name,       # ground truth label
        "__sim_injected": True,
        "anomaly_type": scenario_name,
    })
    return record


# Log emitter

class LogGenerator:
    def __init__(self, output_file: Optional[str] = None, tick_interval: float = 0.5):
        self.output_file = output_file
        self.tick_interval = tick_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._file = None
        self._trace_registry: Dict[str, str] = {}  # trace_id -> span_id for correlation

    def register_trace(self, trace_id: str, span_id: str):
        """Called by trace generator to register IDs for log correlation."""
        self._trace_registry[trace_id] = span_id
        if len(self._trace_registry) > 10000:
            # Evict oldest 20%
            keys = list(self._trace_registry.keys())
            for k in keys[:2000]:
                del self._trace_registry[k]

    def _emit(self, record: Dict[str, Any]):
        line = json.dumps(record, default=str)
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()
        else:
            print(line)

    def start(self):
        if self.output_file:
            self._file = open(self.output_file, "a")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._file:
            self._file.close()

    def _loop(self):
        while self._running:
            self._emit_tick()
            time.sleep(self.tick_interval)

    def _emit_tick(self):
        scenario = SCENARIO.active_scenario

        for service, cfg in SERVICES.items():
            # Generate a few log records per service per tick
            rps_sample = max(1, int(cfg["base_rps"] * 0.005))  # ~0.5% of traffic logged per tick
            err_rate = SCENARIO.error_rate_override.get(service, cfg["error_rate"])
            lat_mult = SCENARIO.latency_multiplier.get(service, 1.0)

            for _ in range(rps_sample):
                trace_id = uuid.uuid4().hex
                span_id = uuid.uuid4().hex[:16]
                is_error = random.random() < err_rate
                status_code = (
                    random.choice([500, 503, 504]) if is_error
                    else random.choices([200, 201, 400, 401], weights=[0.87, 0.06, 0.04, 0.03])[0]
                )
                base_latency = cfg["p50_latency_ms"] * lat_mult
                latency = max(1, random.gauss(base_latency, base_latency * 0.3))
                if SCENARIO.circuit_breaker_open.get(service):
                    latency = random.choice([1.5, 2.0, 30000.0])  # fast fail or timeout

                log = build_request_log(service, status_code, latency, trace_id, span_id)

                # Annotate scenario-injected anomalies for ground truth
                if scenario and (is_error or lat_mult > 1.5):
                    log["__sim_scenario"] = scenario
                    log["__sim_injected"] = True

                self._emit(log)

        # DB logs — emit slow query events during connection exhaustion
        if scenario == "db_connection_exhaustion":
            for _ in range(3):
                trace_id = uuid.uuid4().hex
                slow_ms = random.gauss(8000, 2000)
                self._emit(build_db_log("postgres_primary", "SELECT", slow_ms, 0, trace_id))
            self._emit(build_scenario_log(
                "db_connection_exhaustion",
                "account_ledger",
                "FATAL: remaining connection slots reserved for non-replication superuser connections",
            ))

        # Security events: low-frequency background + burst during data_exfiltration scenario
        security_rate = 0.02 if scenario != "data_exfiltration_alert" else 0.8
        if random.random() < security_rate:
            src_ip = f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
            if scenario == "data_exfiltration_alert":
                src_ip = f"185.{random.randint(100,200)}.{random.randint(0,255)}.{random.randint(1,254)}"
            self._emit(build_security_log(
                "api_gateway",
                src_ip,
                f"user-{random.randint(1000, 9999)}",
                uuid.uuid4().hex,
            ))

        # Compliance event logs
        if random.random() < 0.005:
            self._emit({
                **_build_base_record("compliance_engine", "INFO", "Regulatory event recorded"),
                "compliance": {
                    "regulation": random.choice(["PCI-DSS", "GDPR", "SOX"]),
                    "event_type": "data_access_audit",
                    "data_subject_pseudonym": fake_account_id(),
                    "purpose": "fraud_prevention",
                    "legal_basis": "legitimate_interest",
                    "retention_days": 2555,
                    "audit_trail_id": uuid.uuid4().hex,
                }
            })