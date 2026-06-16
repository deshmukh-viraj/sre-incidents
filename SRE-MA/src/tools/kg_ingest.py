"""
populates the neo4j knowledge graph with:
  1. service topology
  2. alerts mapping
  3. public docs
  4. known remediations

run once at setup or when adding new stuff.
"""

import os
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")


#service topology
# Names must match EXACTLY what your simulator uses in metrics_generator.py
# and what _infer_service() returns in nodes.py

SERVICES = [
    "api_gateway",
    "payment_gateway",
    "kyc_service",
    "fraud_detector",
    "account_ledger",
    "notification_service",
    "card_rails",
    "postgres_primary",
    "postgres_replica",
    "redis",
    "mongodb",
]

#DEPENDS_ON means: left service calls right service
#if right service is slow/down, left service is impacted
DEPENDENCIES = [
    ("api_gateway", "payment_gateway"),
    ("api_gateway", "kyc_service"),
    ("api_gateway", "account_ledger"),
    ("payment_gateway", "card_rails"),
    ("payment_gateway", "fraud_detector"),
    ("payment_gateway", "account_ledger"),
    ("kyc_service", "mongodb"),
    ("fraud_detector", "redis"),
    ("account_ledger", "postgres_primary"),
    ("account_ledger", "postgres_replica"),
    ("notification_service", "account_ledger"),
]

#public documentation for each service
#content will be scraped and stored at ingest time
SERVICE_DOCS = {
    "payment_gateway": {
        "url": "https://microservices.io/patterns/reliability/circuit-breaker.html",
        "summary": "Circuit breaker pattern prevents cascading failures when downstream services degrade.",
    },
    "account_ledger": {
        "url": "https://www.postgresql.org/docs/current/runtime-config-connection.html",
        "summary": "PostgreSQL connection pool configuration. max_connections controls total allowed connections. Connection exhaustion causes new connections to be rejected.",
    },
    "postgres_primary": {
        "url": "https://www.postgresql.org/docs/current/runtime-config-connection.html",
        "summary": "PostgreSQL connection limits. When pool is exhausted, queries queue or fail. Replication lag increases under high load.",
    },
    "fraud_detector": {
        "url": "https://grafana.com/docs/grafana/latest/alerting/",
        "summary": "ML model serving. Feature pipeline staleness causes model drift. Flush cache and restart pipeline to recover.",
    },
    "card_rails": {
        "url": "https://stripe.com/docs/rate-limits",
        "summary": "Payment provider rate limiting. 429 responses indicate throttling. Implement exponential backoff and request queuing.",
    },
    "api_gateway": {
        "url": "https://sre.google/workbook/alerting-on-slos/",
        "summary": "SLO-based alerting. Error budget burn rate alerts indicate SLO breach risk. Fast burn rate requires immediate action.",
    },
    "redis": {
        "url": "https://redis.io/docs/management/persistence/",
        "summary": "Redis cache. Flush cache with FLUSHDB for targeted clearing. High memory usage triggers eviction policies.",
    },
}

#alert definitions which service each alert belongs to
#alert_name must match your payment_alerts.yml exactly
ALERTS = [
    {
        "name": "PaymentGatewayP99LatencyHigh",
        "service": "payment_gateway",
        "severity": "warning",
        "runbook_id": "RB-001",
        "description": "p99 latency above 1.5s threshold",
    },
    {
        "name": "PaymentGatewayP99LatencyCritical",
        "service": "payment_gateway",
        "severity": "critical",
        "runbook_id": "RB-001",
        "description": "p99 latency above 3.0s — SLO breach",
    },
    {
        "name": "SLOErrorBudgetBurnRateFast",
        "service": "payment_gateway",
        "severity": "critical",
        "runbook_id": "RB-001",
        "description": "Error budget burning at 14.4x — exhausts in 1 hour",
    },
    {
        "name": "CircuitBreakerOpen",
        "service": "payment_gateway",
        "severity": "critical",
        "runbook_id": "RB-002",
        "description": "Circuit breaker is OPEN between services",
    },
    {
        "name": "DBConnectionPoolNearExhaustion",
        "service": "account_ledger",
        "severity": "warning",
        "runbook_id": "RB-004",
        "description": "Connection pool above 85%",
    },
    {
        "name": "DBConnectionPoolExhausted",
        "service": "account_ledger",
        "severity": "critical",
        "runbook_id": "RB-004",
        "description": "Connection pool above 98% — new connections rejected",
    },
    {
        "name": "SlowQueryDetected",
        "service": "account_ledger",
        "severity": "warning",
        "runbook_id": "RB-004",
        "description": "p95 query duration above 1 second",
    },
    {
        "name": "AnomalousAPIRequestRate",
        "service": "api_gateway",
        "severity": "critical",
        "runbook_id": "RB-003",
        "description": "Request rate 10x above hourly average — possible attack",
    },
    {
        "name": "AuthenticationFailureRateSpiking",
        "service": "api_gateway",
        "severity": "warning",
        "runbook_id": "RB-003",
        "description": "Auth failures above 5 per second — credential stuffing",
    },
    {
        "name": "PaymentDeclineRateAbnormal",
        "service": "payment_gateway",
        "severity": "critical",
        "runbook_id": "RB-006",
        "description": "Decline rate above 5% - fraud model degradation",
    },
    {
        "name": "FraudModelLatencySpiking",
        "service": "fraud_detector",
        "severity": "warning",
        "runbook_id": "RB-006",
        "description": "Fraud model p99 inference latency above 200ms",
    },
    {
        "name": "ComplianceAuditTriggered",
        "service": "api_gateway",
        "severity": "info",
        "runbook_id": "RB-005",
        "description": "Compliance audit event volume spike",
    },
]

#known remediations per runbook
#these are seeded once — PastIncidents add real ones over time
REMEDIATIONS = [
    {
        "id": "REM-001",
        "runbook_id": "RB-001",
        "action": "Enable card_rails request queuing feature flag",
        "tool": "set_feature_flag",
        "success_rate": 0.85,
        "avg_mttr_s":  180,
    },
    {
        "id": "REM-002",
        "runbook_id": "RB-001",
        "action": "Rollback payment_gateway to previous deployment",
        "tool": "rollback_deployment",
        "success_rate": 0.90,
        "avg_mttr_s":  240,
    },
    {
        "id": "REM-003",
        "runbook_id": "RB-002",
        "action": "Rolling restart of account_ledger to release connections",
        "tool": "rolling_restart",
        "success_rate": 0.92,
        "avg_mttr_s": 120,
    },
    {
        "id": "REM-004",
        "runbook_id": "RB-004",
        "action": "Rolling restart of account_ledger pods",
        "tool": "rolling_restart",
        "success_rate": 0.88,
        "avg_mttr_s": 150,
    },
    {
        "id": "REM-005",
        "runbook_id": "RB-006",
        "action": "Restart feature pipeline to force fresh data pull",
        "tool": "restart_service",
        "success_rate": 0.80,
        "avg_mttr_s": 300,
    },
    {
        "id": "REM-006",
        "runbook_id": "RB-006",
        "action":  "Enable fraud model fallback rule-based scorer",
        "tool": "set_feature_flag",
        "success_rate": 0.95,
        "avg_mttr_s":  60,
    },
]


#KG ingestor
class KGIngestor:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASSWORD),
        )

    def close(self):
        self.driver.close()

    def run(self):
        print("=" * 60)
        print("Neo4j Knowledge Graph Ingestion")
        print("=" * 60)

        with self.driver.session() as session:
            self._clear_static_nodes(session)
            self._create_services(session)
            self._create_dependencies(session)
            self._create_alerts(session)
            self._create_remediations(session)
            self._link_alerts_to_remediations(session)
            self._seed_past_incidents(session)

        print("\n KG ingestion complete.\n")

    def _clear_static_nodes(self, session):
        """
        clear out the old stuff before recreating, but don't delete past incidents.
        """
        session.run("MATCH (n:Service) DETACH DELETE n")
        session.run("MATCH (n:Alert) DETACH DELETE n")
        session.run("MATCH (n:Remediation) DETACH DELETE n")
        print("\n[clear] Cleared Service, Alert, Remediation nodes")
        print("[clear] PastIncident nodes preserved")

    def _create_services(self, session):
        print("\n[services] Creating service nodes...")
        for svc in SERVICES:
            doc_info = SERVICE_DOCS.get(svc, {})
            doc_url = doc_info.get("url", "")
            doc_summary = doc_info.get("summary", "")

            session.run(
                """
                CREATE (s:Service {
                    name: $name,
                    doc_url: $doc_url,
                    doc_summary: $doc_summary
                })
                """,
                name=svc,
                doc_url=doc_url,
                doc_summary=doc_summary,
            )
            print(f"  {svc}" + (f"  doc attached" if doc_url else ""))

    
    def _create_dependencies(self, session):
        print("\n[deps] Creating DEPENDS_ON relationships...")
        for src, dst in DEPENDENCIES:
            session.run(
                """
                MATCH (a:Service {name: $src}), (b:Service {name: $dst})
                CREATE (a)-[:DEPENDS_ON]->(b)
                """,
                src=src, dst=dst,
            )
            print(f"   {src} -> {dst}")

    
    def _create_alerts(self, session):
        print("\n[alerts] Creating Alert nodes...")
        for alert in ALERTS:
            session.run(
                """
                CREATE (a:Alert {
                    name: $name,
                    severity: $severity,
                    runbook_id: $runbook_id,
                    description: $description
                })
                """,
                **alert,
            )
            # Link alert to its service
            session.run(
                """
                MATCH (a:Alert {name: $alert_name}),
                      (s:Service {name: $service_name})
                CREATE (a)-[:AFFECTS]->(s)
                """,
                alert_name=alert["name"],
                service_name=alert["service"],
            )
            print(f"  {alert['name']} -> {alert['service']}")

    
    def _create_remediations(self, session):
        print("\n[remediations] Creating Remediation nodes...")
        for rem in REMEDIATIONS:
            session.run(
                """
                CREATE (r:Remediation {
                    id: $id,
                    runbook_id: $runbook_id,
                    action: $action,
                    tool: $tool,
                    success_rate: $success_rate,
                    avg_mttr_s: $avg_mttr_s
                })
                """,
                **rem,
            )
            print(f"  {rem['id']}: {rem['action'][:50]}")

    
    def _link_alerts_to_remediations(self, session):
        print("\n[links] Linking Alerts to Remediations via runbook_id...")
        session.run(
            """
            MATCH (a:Alert), (r:Remediation)
            WHERE a.runbook_id = r.runbook_id
            CREATE (a)-[:RESOLVED_BY]->(r)
            """
        )
        print("  Alert -> Remediation links created")

    
    def _seed_past_incidents(self, session):
        print("\n[past_incidents] seeding historial incident data...")
        incidents = [
            {
                "incident_id": "INC-HIST-001",
                "alert_name": "ServiceBehaviourAnomaly",
                "root_cause": "External payment processor API latency",
                "action_taken": "enable rate_limiting",
                "success": True,
                "mttr_seconds": 120
            },
            {
                "incident_id": "INC-HIST-002",
                "alert_name": "PaymentGatewayP99LatencyHigh",
                "root_cause": "Downstrean card_rails API throttling requests.",
                "action_taken": "set_feature_flag",
                "success": True,
                "mttr_seconds": 90
            }
        ]
        for inc in incidents:
            session.run("""
            MERGE (a:Alert {name: $alert_name})
            MERGE (p:PastIncident {incident_id: $incident_id})
            SET p.root_cause = $root_cause,
                p.action_taken = $action_taken,
                p.success = $success,
                p.mttr_seconds = $mttr_seconds,
                p.timestamp = datetime()
            MERGE (p)-[:TRIGGERED_BY] -> (a)
        """, **inc)
        print(f" seedded {inc['incident_id']} -> {inc['alert_name']}")


def append_past_incident(
    incident_id: str,
    alert_name: str,
    root_cause: str,
    action_taken: str,
    mttr_seconds: int,
    success: bool,
):
    """
    called by execute_node after fixing an incident.
    adds it to the graph so we know for next time.
    """
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
    )
    try:
      
        with driver.session() as session:
            #create PastIncident node
            session.run(
                """
                CREATE (p:PastIncident {
                    incident_id: $incident_id,
                    root_cause: $root_cause,
                    action_taken: $action_taken,
                    mttr_seconds: $mttr_seconds,
                    success: $success,
                    timestamp: datetime()
                })
                """,
                incident_id=incident_id,
                root_cause=root_cause,
                action_taken=action_taken,
                mttr_seconds=mttr_seconds,
                success=success,
            )
            #link to the Alert node if it exists
            session.run(
                """
                MERGE (p:PastIncident {incident_id: $incident_id}),
                      (a:Alert {name: $alert_name})
                MERGE (p)-[:TRIGGERED_BY]->(a)
                """,
                incident_id=incident_id,
                alert_name=alert_name,
            )
            print(f"[kg_ingest] Appended PastIncident {incident_id} to KG")
    except Exception as e:
        print(f"critical ingestion failure: {e}")
        raise
    #     print(f"[kg_ingest] Failed to append incident: {e}")
    # finally:
    #     driver.close()


if __name__ == "__main__":
    ingestor = KGIngestor()
    ingestor.run()
    ingestor.close()