"""
tools/kg_tool.py
----------------
Queries the Neo4j Knowledge Graph during incident diagnosis.
Called by llm_diagnoser_node to get:
  1. Service topology context (what does this service depend on?)
  2. Public documentation summaries (what do the docs say about this?)
  3. Known remediations (what fixed this before?)
  4. Past incidents (has this happened before?)

Returns a single formatted string injected into the LLM prompt.
"""

import os
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "sreagentpassword")

# Module-level driver — created once, reused on every call
_driver = None


def _get_driver():
    """
    Returns the shared Neo4j driver instance.
    Creates it on first call, reuses it after that.
    This prevents opening a new TCP connection on every alert.
    """
    global _driver
    if _driver is None:
        try:
            from neo4j import GraphDatabase
            _driver = GraphDatabase.driver(
                NEO4J_URI,
                auth=(NEO4J_USER, NEO4J_PASSWORD),
            )
            print("[kg_tool] Neo4j driver initialised")
        except Exception as e:
            print(f"[kg_tool] WARNING: Could not connect to Neo4j: {e}")
            return None
    return _driver


def query_knowledge_graph(
    affected_services: List[str],
    alert_name: Optional[str] = None,
) -> str:
    """
    Main entry point called by llm_diagnoser_node.

    Runs 4 queries against Neo4j and returns a formatted
    context string ready for LLM injection.

    Args:
        affected_services: List of service names from detector
                           Example: ["payment_gateway"]
        alert_name:        The firing alert name
                           Example: "PaymentGatewayP99LatencyHigh"

    Returns:
        Formatted string with all 4 context sections.
        Returns fallback string if Neo4j is unavailable.
    """
    driver = _get_driver()

    if driver is None:
        return "Knowledge Graph unavailable — Neo4j not running."

    context_sections = []

    try:
        with driver.session() as session:
            for service in affected_services:

                # ── Query 1: Downstream dependencies ──────────────────────
                # "What does this service call? If they are slow, this breaks."
                deps = _query_dependencies(session, service)
                if deps:
                    dep_names = [d["dependency"] for d in deps]
                    context_sections.append(
                        f"[Topology] {service} DEPENDS_ON: {', '.join(dep_names)}. "
                        f"If any of these are degraded, {service} will be impacted."
                    )

                    # Attach doc summaries for each dependency
                    for dep in deps:
                        if dep.get("doc_summary"):
                            context_sections.append(
                                f"[Docs: {dep['dependency']}] {dep['doc_summary']}"
                            )
                else:
                    context_sections.append(
                        f"[Topology] {service} has no downstream dependencies "
                        f"— it is likely a root node (database or external API)."
                    )

                # ── Query 2: Upstream services (who calls this service?) ───
                # "If this service is slow, who else gets hurt?"
                upstream = _query_upstream(session, service)
                if upstream:
                    context_sections.append(
                        f"[Impact] {service} is called by: "
                        f"{', '.join(upstream)}. "
                        f"Degradation here cascades to all of them."
                    )

                # ── Query 3: Known remediations for this service ───────────
                # "What has fixed problems on this service before?"
                remediations = _query_remediations(session, service, alert_name)
                if remediations:
                    rem_lines = []
                    for r in remediations[:3]:  # top 3 by success rate
                        rem_lines.append(
                            f"  - {r['action']} "
                            f"(success_rate={r['success_rate']:.0%}, "
                            f"avg_mttr={r['avg_mttr_s']}s)"
                        )
                    context_sections.append(
                        f"[Known Fixes for {service}]:\n" + "\n".join(rem_lines)
                    )

                # ── Query 4: Past incidents on this service ────────────────
                # "Has this exact pattern occurred before? What fixed it?"
                past = _query_past_incidents(session, service, alert_name)
                if past:
                    context_sections.append(
                        f"[Past Incident] Similar incident on {service}: "
                        f"root_cause='{past['root_cause']}', "
                        f"fixed_by='{past['action_taken']}', "
                        f"mttr={past['mttr_seconds']}s, "
                        f"success={past['success']}."
                    )

    except Exception as e:
        return f"Knowledge Graph query failed: {str(e)}"

    if not context_sections:
        return "No relevant context found in Knowledge Graph for these services."

    return "\n\n".join(context_sections)


# ── Individual query functions ─────────────────────────────────────────────

def _query_dependencies(session, service: str) -> list:
    """
    Find everything this service DEPENDS_ON.
    Returns list of {dependency, doc_summary, doc_url} dicts.
    """
    result = session.run(
        """
        MATCH (s:Service {name: $service})-[:DEPENDS_ON]->(dep:Service)
        RETURN dep.name        AS dependency,
               dep.doc_summary AS doc_summary,
               dep.doc_url     AS doc_url
        """,
        service=service,
    )
    return [dict(record) for record in result]


def _query_upstream(session, service: str) -> list:
    """
    Find everything that calls this service (reverse direction).
    Returns list of service names.
    """
    result = session.run(
        """
        MATCH (caller:Service)-[:DEPENDS_ON]->(s:Service {name: $service})
        RETURN caller.name AS caller
        """,
        service=service,
    )
    return [record["caller"] for record in result]


def _query_remediations(
    session,
    service: str,
    alert_name: Optional[str] = None,
) -> list:
    """
    Find known remediations for this service.
    If alert_name provided, filter to remediations for that specific alert.
    Orders by success_rate descending.
    """
    if alert_name:
        result = session.run(
            """
            MATCH (a:Alert {name: $alert_name})-[:RESOLVED_BY]->(r:Remediation)
            RETURN r.action       AS action,
                   r.tool         AS tool,
                   r.success_rate AS success_rate,
                   r.avg_mttr_s   AS avg_mttr_s
            ORDER BY r.success_rate DESC
            LIMIT 3
            """,
            alert_name=alert_name,
        )
    else:
        result = session.run(
            """
            MATCH (a:Alert)-[:AFFECTS]->(s:Service {name: $service})
            MATCH (a)-[:RESOLVED_BY]->(r:Remediation)
            RETURN r.action       AS action,
                   r.tool         AS tool,
                   r.success_rate AS success_rate,
                   r.avg_mttr_s   AS avg_mttr_s
            ORDER BY r.success_rate DESC
            LIMIT 3
            """,
            service=service,
        )
    return [dict(record) for record in result]


def _query_past_incidents(
    session,
    service: str,
    alert_name: Optional[str] = None,
) -> Optional[dict]:
    """
    Find the most recent successful past incident for this alert/service.
    Returns the single most relevant past incident dict, or None.
    """
    if alert_name:
        result = session.run(
            """
            MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert {name: $alert_name})
            WHERE p.success = true
            RETURN p.root_cause   AS root_cause,
                   p.action_taken AS action_taken,
                   p.mttr_seconds AS mttr_seconds,
                   p.success      AS success,
                   p.timestamp    AS timestamp
            ORDER BY p.timestamp DESC
            LIMIT 1
            """,
            alert_name=alert_name,
        )
    else:
        result = session.run(
            """
            MATCH (p:PastIncident)-[:TRIGGERED_BY]->(a:Alert)
                  -[:AFFECTS]->(s:Service {name: $service})
            WHERE p.success = true
            RETURN p.root_cause   AS root_cause,
                   p.action_taken AS action_taken,
                   p.mttr_seconds AS mttr_seconds,
                   p.success      AS success
            ORDER BY p.timestamp DESC
            LIMIT 1
            """,
            service=service,
        )

    records = [dict(r) for r in result]
    return records[0] if records else None


def append_past_incident(
    incident_id:  str,
    alert_name:   str,
    root_cause:   str,
    action_taken: str,
    mttr_seconds: int,
    success:      bool,
) -> None:
    """
    Called by execute_node after every resolution.
    Writes a PastIncident node so the system learns from this incident.

    This is the learning loop — the KG gets smarter over time.
    """
    driver = _get_driver()
    if driver is None:
        print("[kg_tool] Cannot append incident — Neo4j unavailable")
        return

    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (p:PastIncident {
                    incident_id:  $incident_id,
                    root_cause:   $root_cause,
                    action_taken: $action_taken,
                    mttr_seconds: $mttr_seconds,
                    success:      $success,
                    timestamp:    datetime()
                })
                """,
                incident_id=incident_id,
                root_cause=root_cause,
                action_taken=action_taken,
                mttr_seconds=mttr_seconds,
                success=success,
            )
            # Link to Alert node if it exists in KG
            session.run(
                """
                MATCH (p:PastIncident {incident_id: $incident_id}),
                      (a:Alert {name: $alert_name})
                CREATE (p)-[:TRIGGERED_BY]->(a)
                """,
                incident_id=incident_id,
                alert_name=alert_name,
            )
            print(f"[kg_tool] PastIncident {incident_id} appended to KG")
    except Exception as e:
        print(f"[kg_tool] Failed to append PastIncident: {e}")