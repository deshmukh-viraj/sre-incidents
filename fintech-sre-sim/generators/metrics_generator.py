"""
metrics_generator.py
Generates Prometheus-exposition-format metrics for the fintech simulation topology.
Supports steady-state baseline with noise, seasonality, and scenario-driven anomaly injection.
"""

import time
import math
import random
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from prometheus_client import (
    Counter, Histogram, Gauge,
    CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
)
from http.server import HTTPServer, BaseHTTPRequestHandler


# Topology: services and their steady-state parameters
SERVICES = {
    "payment_gateway": {
        "base_rps": 420,           # requests per second at business hours
        "p50_latency_ms": 85,
        "p99_latency_ms": 320,
        "error_rate": 0.003,       # 0.3% baseline error rate
        "downstream": ["card_rails", "fraud_detector", "account_ledger"],
    },
    "kyc_service": {
        "base_rps": 38,
        "p50_latency_ms": 210,
        "p99_latency_ms": 890,
        "error_rate": 0.008,
        "downstream": ["identity_db"],
    },
    "fraud_detector": {
        "base_rps": 420,           # called for every payment
        "p50_latency_ms": 45,
        "p99_latency_ms": 180,
        "error_rate": 0.001,
        "downstream": ["redis_ml_model"],
    },
    "account_ledger": {
        "base_rps": 380,
        "p50_latency_ms": 12,
        "p99_latency_ms": 48,
        "error_rate": 0.0005,
        "downstream": ["postgres_primary"],
    },
    "notification_service": {
        "base_rps": 95,
        "p50_latency_ms": 55,
        "p99_latency_ms": 220,
        "error_rate": 0.012,       # slightly higher — third party email/SMS
        "downstream": [],
    },
    "api_gateway": {
        "base_rps": 1100,
        "p50_latency_ms": 8,
        "p99_latency_ms": 35,
        "error_rate": 0.002,
        "downstream": ["payment_gateway", "kyc_service", "notification_service"],
    },
}

DB_POOLS = {
    "postgres_primary": {"max_connections": 100, "base_active": 28},
    "postgres_replica": {"max_connections": 100, "base_active": 14},
    "identity_db":      {"max_connections": 50,  "base_active": 8},
    "redis_ml_model":   {"max_connections": 200, "base_active": 45},
}



# Noise and seasonality helpers

def seasonality_multiplier(hour: int) -> float:
    """Business-hours traffic curve: peaks 10–14 UTC, trough 02–05 UTC."""
    # Gaussian like envelope centred at noon
    morning_ramp  = 0.4 + 0.6 * (1 / (1 + math.exp(-0.9 * (hour - 7))))
    evening_decay = 1.0 - 0.5 * (1 / (1 + math.exp(-0.8 * (hour - 18))))
    return morning_ramp * evening_decay


def noise(base: float, sigma_pct: float = 0.08) -> float:
    """Gaussian multiplicative noise around a base value."""
    return max(0.0, base * random.gauss(1.0, sigma_pct))


def lognormal_latency(p50: float, p99: float) -> float:
    """
    Sample from a log-normal distribution calibrated to approximate
    the given p50 and p99. Returns a single latency sample in ms.
    """
    mu = math.log(p50)
    # Solve: p99 ≈ exp(mu + 2.326 * sigma)  →  sigma = (log(p99) - mu) / 2.326
    sigma = (math.log(p99) - mu) / 2.326
    return math.exp(random.gauss(mu, sigma))



# Scenario multipliers — applied on top of baseline

@dataclass
class ScenarioState:
    """Live mutable state injected by the scenario engine."""
    latency_multiplier: Dict[str, float] = field(default_factory=dict)
    error_rate_override: Dict[str, Optional[float]] = field(default_factory=dict)
    rps_multiplier: Dict[str, float] = field(default_factory=dict)
    db_pool_saturation: Dict[str, float] = field(default_factory=dict)  # 0.0–1.0
    circuit_breaker_open: Dict[str, bool] = field(default_factory=dict)
    active_scenario: Optional[str] = None

    def reset(self):
        self.latency_multiplier.clear()
        self.error_rate_override.clear()
        self.rps_multiplier.clear()
        self.db_pool_saturation.clear()
        self.circuit_breaker_open.clear()
        self.active_scenario = None


# Global shared scenario state (written by scenario engine, read by generators)
SCENARIO = ScenarioState()



# Prometheus metric definitions

REGISTRY = CollectorRegistry()

# Request throughput
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests by service, method, status_code",
    ["service", "method", "status_code"],
    registry=REGISTRY,
)

# Latency histogram (buckets calibrated for fintech: 10ms–10s)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)

# Database connection pool
db_connection_pool_active = Gauge(
    "db_connection_pool_active",
    "Active database connections",
    ["db", "pool"],
    registry=REGISTRY,
)
db_connection_pool_max = Gauge(
    "db_connection_pool_max",
    "Maximum pool size",
    ["db", "pool"],
    registry=REGISTRY,
)
db_query_duration_seconds = Histogram(
    "db_query_duration_seconds",
    "Database query duration",
    ["db", "operation"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
    registry=REGISTRY,
)

# Circuit breaker state (0=closed, 1=half-open, 2=open)
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    ["service", "upstream"],
    registry=REGISTRY,
)

# Payment-domain business metrics
payment_transactions_total = Counter(
    "payment_transactions_total",
    "Payment transactions by type and status",
    ["transaction_type", "status", "currency"],
    registry=REGISTRY,
)
payment_amount_processed_total = Counter(
    "payment_amount_processed_total",
    "Total payment value processed (USD cents)",
    ["transaction_type"],
    registry=REGISTRY,
)
fraud_score_histogram = Histogram(
    "fraud_score",
    "ML fraud score distribution (0–1)",
    ["decision"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=REGISTRY,
)
fraud_model_prediction_latency = Histogram(
    "fraud_model_prediction_latency_seconds",
    "Fraud model inference latency",
    ["model_version"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
    registry=REGISTRY,
)

# Infrastructure
process_memory_rss_bytes = Gauge(
    "process_memory_rss_bytes",
    "Process resident memory in bytes",
    ["service"],
    registry=REGISTRY,
)
process_cpu_seconds_total = Counter(
    "process_cpu_seconds_total",
    "Total CPU seconds consumed",
    ["service"],
    registry=REGISTRY,
)

# Compliance/audit events
compliance_events_total = Counter(
    "compliance_events_total",
    "Compliance events by regulation and event type",
    ["regulation", "event_type", "severity"],
    registry=REGISTRY,
)

# SLO tracking
slo_error_budget_remaining = Gauge(
    "slo_error_budget_remaining_ratio",
    "Remaining error budget ratio (1.0 = full)",
    ["service", "slo_name"],
    registry=REGISTRY,
)

# Initialise static gauges
for svc in SERVICES:
    slo_error_budget_remaining.labels(service=svc, slo_name="availability").set(0.98)
    slo_error_budget_remaining.labels(service=svc, slo_name="latency_p99").set(0.87)

for db, cfg in DB_POOLS.items():
    db_connection_pool_max.labels(db=db, pool="main").set(cfg["max_connections"])



# Metric emission loops


def _emit_service_metrics(service: str, cfg: dict, hour: int):
    """Emit one 'tick' of metrics for a single service."""
    mult = seasonality_multiplier(hour)
    rps = noise(cfg["base_rps"] * mult) * SCENARIO.rps_multiplier.get(service, 1.0)

    lat_mult = SCENARIO.latency_multiplier.get(service, 1.0)
    err_rate = SCENARIO.error_rate_override.get(service, cfg["error_rate"])

    cb_open = SCENARIO.circuit_breaker_open.get(service, False)

    # When circuit breaker is open, most requests fail immediately
    if cb_open:
        err_rate = 0.95
        lat_mult = 0.05  # fast-fails

    # Simulate rps / tick_interval request samples
    tick_rps = max(1, int(rps / 10))  # 100ms tick → rps/10 samples per tick

    success_count = 0
    error_count = 0
    for _ in range(tick_rps):
        is_error = random.random() < err_rate
        if is_error:
            error_count += 1
            status = random.choice(["500", "503", "504"])
            # Errors are often fast (immediate rejection) or very slow (timeout)
            latency = random.choice([
                noise(0.001),                                                # fast fail
                noise(cfg["p99_latency_ms"] * lat_mult * 3, 0.2) / 1000.0  # timeout
            ])
        else:
            success_count += 1
            status = "200"
            latency = noise(lognormal_latency(
                cfg["p50_latency_ms"] * lat_mult,
                cfg["p99_latency_ms"] * lat_mult,
            )) / 1000.0  # convert ms → seconds

        method = random.choice(["POST", "POST", "POST", "GET"])  # weighted POST
        http_requests_total.labels(service=service, method=method, status_code=status).inc()
        http_request_duration_seconds.labels(
            service=service,
            endpoint=f"/{service.split('_')[0]}/v1",
        ).observe(latency)

    # Update SLO error budget (simplified drain model)
    if tick_rps > 0:
        tick_err_rate = error_count / tick_rps
        budget_drain = tick_err_rate * 0.0001  # scale down to not drain too fast
        for slo_name in ["availability"]:
            current = slo_error_budget_remaining.labels(
                service=service, slo_name=slo_name
            )._value.get()
            slo_error_budget_remaining.labels(
                service=service, slo_name=slo_name
            ).set(max(0.0, current - budget_drain))


def _emit_db_metrics(db: str, cfg: dict):
    """Emit database pool and query metrics."""
    saturation = SCENARIO.db_pool_saturation.get(db, 0.0)
    max_conn = cfg["max_connections"]
    base_active = cfg["base_active"]

    # Saturate pool based on scenario injection
    active = min(max_conn, int(base_active + saturation * (max_conn - base_active)))
    active = max(0, int(noise(active, 0.05)))
    db_connection_pool_active.labels(db=db, pool="main").set(active)

    # Query latency degrades as pool approaches max
    pool_pressure = active / max_conn
    base_query_ms = 5.0 * (1 + 4 * pool_pressure ** 3)  # cubic degradation
    for op in ["SELECT", "INSERT", "UPDATE"]:
        db_query_duration_seconds.labels(db=db, operation=op).observe(
            noise(base_query_ms, 0.15) / 1000.0
        )


def _emit_payment_metrics():
    """Emit payment-domain business metrics."""
    # Simulate ~420 rps of payment volume
    for _ in range(42):  # 10% sample per tick
        txn_type = random.choices(
            ["card_present", "card_not_present", "ach", "wire"],
            weights=[0.45, 0.35, 0.15, 0.05]
        )[0]
        amount_cents = int(random.lognormvariate(math.log(5000), 1.2))  # realistic dist

        fraud_score = random.betavariate(1.5, 8)  # most payments are low-fraud
        decision = "block" if fraud_score > 0.7 else "allow"

        # Inject fraud model degradation if scenario active
        if SCENARIO.active_scenario == "fraud_model_degradation":
            fraud_score = random.betavariate(3, 3)  # confused model → flat dist
            decision = random.choice(["allow", "block"])

        status = "declined" if decision == "block" else random.choices(
            ["approved", "approved", "approved", "failed"], weights=[0.85, 0.85, 0.85, 0.15]
        )[0]

        payment_transactions_total.labels(
            transaction_type=txn_type, status=status, currency="USD"
        ).inc()
        if status == "approved":
            payment_amount_processed_total.labels(transaction_type=txn_type).inc(amount_cents)

        fraud_score_histogram.labels(decision=decision).observe(fraud_score)
        fraud_model_prediction_latency.labels(model_version="v2.3.1").observe(
            noise(0.035, 0.2)
        )


def _emit_circuit_breaker_metrics():
    """Emit circuit breaker states based on scenario."""
    for service in SERVICES:
        for upstream in SERVICES[service]["downstream"]:
            is_open = SCENARIO.circuit_breaker_open.get(service, False)
            state = 2 if is_open else 0
            circuit_breaker_state.labels(service=service, upstream=upstream).set(state)


def _emit_infrastructure_metrics():
    """Emit process-level resource metrics."""
    base_memory = {
        "payment_gateway": 512 * 1024 * 1024,
        "kyc_service": 256 * 1024 * 1024,
        "fraud_detector": 1024 * 1024 * 1024,  # ML model in memory
        "account_ledger": 384 * 1024 * 1024,
        "notification_service": 192 * 1024 * 1024,
        "api_gateway": 384 * 1024 * 1024,
    }
    for svc, base_mem in base_memory.items():
        # Memory slowly grows (simulate mild leak) then GC drops
        t = time.time()
        sawtooth = (t % 300) / 300  # 5-min GC cycle
        multiplier = 1.0 + 0.3 * sawtooth
        if SCENARIO.active_scenario == "db_connection_exhaustion" and svc == "account_ledger":
            multiplier *= 2.2  # memory pressure during connection storm
        process_memory_rss_bytes.labels(service=svc).set(
            noise(base_mem * multiplier, 0.03)
        )
        process_cpu_seconds_total.labels(service=svc).inc(noise(0.1, 0.1))


# Main generation loop

class MetricsGenerator:
    def __init__(self, tick_interval: float = 0.1):
        self.tick_interval = tick_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while self._running:
            hour = time.gmtime().tm_hour
            for service, cfg in SERVICES.items():
                _emit_service_metrics(service, cfg, hour)
            for db, cfg in DB_POOLS.items():
                _emit_db_metrics(db, cfg)
            _emit_payment_metrics()
            _emit_circuit_breaker_metrics()
            _emit_infrastructure_metrics()
            time.sleep(self.tick_interval)


# Prometheus HTTP exposition endpoint

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            output = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(output)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


def start_metrics_server(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[metrics] Prometheus endpoint: http://0.0.0.0:{port}/metrics")
    return server