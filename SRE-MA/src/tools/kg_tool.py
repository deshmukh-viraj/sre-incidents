"""
tools/kg_tool.py
----------------
ask neo4j for graph info.
we use this to figure out dependencies, docs, and what fixed stuff before.
"""

import os
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

try:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
except Exception as e:
    print(f"[kg_tool] WARNING: Could not connect to Neo4j: {e}")
    driver = None


def query_knowledge_graph(affected_services: List[str], alert_name: Optional[str] = None) -> str:
    if driver is None:
        return "Knowledge Graph unavailable — Neo4j not running."

    context_sections = []
    try:
        with driver.session() as session:
            for service in affected_services:
                
                # 1. downstream dependencies
                deps_result = session.run("MATCH (s:Service {name: $service})-[:DEPENDS_ON]->(dep:Service) RETURN dep.name AS dependency, dep.doc_summary AS doc_summary", service=service)
                deps = [dict(r) for r in deps_result]
                if deps:
                    context_sections.append(f"[Topology] {service} DEPENDS_ON: {', '.join(d['dependency'] for d in deps)}. If any of these are degraded, {service} will be impacted.")
                    for dep in deps:
                        if dep.get("doc_summary"):
                            context_sections.append(f"[Docs: {dep['dependency']}] {dep['doc_summary']}")
                else:
                    context_sections.append(f"[Topology] {service} has no downstream dependencies — it is likely a root node (database or external API).")

                # 2. upstream services
                upstream_result = session.run("MATCH (caller:Service)-[:DEPENDS_ON]->(s:Service {name: $service}) RETURN caller.name AS caller", service=service)
                upstream = [r["caller"] for r in upstream_result]
                if upstream:
                    context_sections.append(f"[Impact] {service} is called by: {', '.join(upstream)}. Degradation here cascades to all of them.")

                # 3. known remediations
                if alert_name:
                    rem_q = "MATCH (a:Alert {name: $alert_name})-[:RESOLVED_BY]->(r:Remediation) RETURN r.action AS action, r.success_rate AS success_rate, r.avg_mttr_s AS avg_mttr_s ORDER BY r.success_rate DESC LIMIT 3"
                    rem_result = session.run(rem_q, alert_name=alert_name)
                else:
                    rem_q = "MATCH (a:Alert)-[:AFFECTS]->(s:Service {name: $service}) MATCH (a)-[:RESOLVED_BY]->(r:Remediation) RETURN r.action AS action, r.success_rate AS success_rate, r.avg_mttr_s AS avg_mttr_s ORDER BY r.success_rate DESC LIMIT 3"
                    rem_result = session.run(rem_q, service=service)
                
                remediations = [dict(r) for r in rem_result]
                if remediations:
                    rem_lines = [f"  - {r['action']} (success_rate={r['success_rate']:.0%}, avg_mttr={r['avg_mttr_s']}s)" for r in remediations]
                    context_sections.append(f"[Known Fixes for {service}]:\n" + "\n".join(rem_lines))

                # 4. past incidents
                if alert_name:
                    past_q =( 
                    "MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert {name: $alert_name}) "
                    "WHERE p.success = true AND p.service = $service "
                    "RETURN p.root_cause AS root_cause, p.action_taken AS action_taken, "
                    "p.business_mttr_seconds AS business_mttr_seconds, "
                    "p.success AS success ORDER BY p.timestamp DESC LIMIT 1"
                    )
                    past_result = session.run(past_q, alert_name=alert_name, service=service)
                else:
                    past_q = "MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert)-[:AFFECTS]->(s:Service {name: $service}) WHERE p.success = true RETURN p.root_cause AS root_cause, p.action_taken AS action_taken, p.business_mttr_seconds AS business_mttr_seconds, p.success AS success ORDER BY p.timestamp DESC LIMIT 1"
                    past_result = session.run(past_q, service=service)
                
                records = [dict(r) for r in past_result]    
                if records:
                    past = records[0]
                    context_sections.append(f"[Past Incident] Similar incident on {service}: root_cause='{past['root_cause']}', fixed_by='{past['action_taken']}', mttr={past['business_mttr_seconds']}s, success={past['success']}.")

    except Exception as e:
        return f"knowledge graph query failed: {str(e)}"

    if not context_sections:
        return "no relevant context found in knowledge graph for these services."

    return "\n\n".join(context_sections)


def append_past_incident(
    incident_id: str,
    alert_name: str,
    service: str,
    root_cause: str,
    action_taken: str,
    business_mttr_seconds: Optional[int],
    success: bool,
    resolution_cause: Optional[str] = None,
    verified_at: Optional[str] = None,
    severity: Optional[str] = None,
) -> None:
    if driver is None:
        print("[kg_tool] cannot append incident — Neo4j unavailable")
        return
    try:
        with driver.session() as session:
            session.run(
                "CREATE (p:PastIncident { "
                "incident_id: $incident_id, alert_name: $alert_name, service: $service, "
                "root_cause: $root_cause, action_taken: $action_taken, "
                "business_mttr_seconds: $business_mttr_seconds, success: $success, "
                "resolution_cause: $resolution_cause, verified_at: $verified_at, "
                "severity: $severity, timestamp: datetime() })",
                incident_id=incident_id, alert_name=alert_name, service=service,
                root_cause=root_cause, action_taken=action_taken,
                business_mttr_seconds=business_mttr_seconds, success=success,
                resolution_cause=resolution_cause, verified_at=verified_at,
                severity=severity,
            )
            session.run(
                "MERGE (a:Alert {name: $alert_name}) "
                "WITH a " 
                "MATCH (p:PastIncident {incident_id: $incident_id}) " 
                "MERGE (p)-[:TRIGGERED_BY]->(a) ",
                incident_id=incident_id, alert_name=alert_name,
            )
            print(f"[kg_tool] PastIncident {alert_name} appended to KG")
    except Exception as e:
        print(f"[kg_tool] Failed to append PastIncident: {e}")


def query_past_incident_only(service: str, alert_name: str) -> Optional[dict]:
    """
    return only the root_cause and action_taken of the most recent successful
    """
    if driver is None: return None
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert {name: $alert_name}) "
                "WHERE p.service = $service AND p.success = true "
                "RETURN p.root_cause AS root_cause, p.action_taken AS action_taken "
                "ORDER BY p.timestamp DESC LIMIT 1",
                service=service, alert_name=alert_name,
            )
            records = [dict(r) for r in result]
            return records[0] if records else None
    except Exception as e:
        print(f"[kg_tool] query_past_incident_only failed for alert {alert_name} on {service}: {e}")
        return None
    

def query_recent_resolved_incident(alert_name: str, service: str) -> Optional[dict]:
    """
    gate-5: finds if this alert was recently solved as agent_remediated
    """
    if driver is None: return None
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert {name: $alert_name}) "
                "WHERE p.service = $service AND p.resolution_cause = 'agent_remediated' "
                "RETURN p.incident_id AS incident_id, p.severity AS severity, "
                "p.verified_at AS verified_at ORDER BY p.timestamp DESC LIMIT 1",
                alert_name=alert_name, service=service,
            )
            records = [dict(r) for r in result]
            return records[0] if records else None
    except Exception as e:
        print(f"[kg_tool] query_recent_resolved_incident failed for alert {alert_name} on {service}: {e}")
        return None


def revoke_credit(incident_id: str, new_incident_id: str) -> None:
    """
    gate-5: atomically revokes the credit for old inicident and links new one asrecurrence
    """
    if driver is None: return 
    tx = None
    try:
        with driver.session() as session:
            tx = session.begin_transaction()
            tx.run(
                "MATCH (p:PastIncident {incident_id: $incident_id}) "
                "SET p.resolution_cause = 'natural_calm', p.attribution_status = 'final', p.credit_revoked = true",
                incident_id=incident_id,
            )
            tx.run(
                "MATCH (new:PastIncident {incident_id: $new_incident_id}) "
                "SET new.recurrence_of = $incident_id",
                new_incident_id=new_incident_id, incident_id=incident_id,
            )
            tx.commit()
            print(f"[kg_tool] credit revoked for {incident_id}, linked to {new_incident_id}")
    except Exception as e:
        if tx: tx.rollback()
        print(f"[kg_tool] revoke_credit failed: {e}")
        raise