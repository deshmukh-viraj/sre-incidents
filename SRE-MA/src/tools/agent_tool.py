"""
tools/agent_tools.py
--------------------------

sandboxed tool set for the DeepAgent/ReAct investigation harness.
"""

from langchain_core.tools import tool
from src.tools.prometheus_tool import _instant_query
from src.tools.loki_tool import query_loki, extract_log_summary
from src.tools.kg_tool import query_knowledge_graph, query_recent_resolved_incident
from src.tools.sre_tool import lookup_runbook

@tool
def query_prometheus_metric(expr: str) -> str:
    """run an instant PromQL queru and return the num value"""
    try:
        return f"{_instant_query(expr)}"
    except Exception as e:
        return f"query failed: {e}"

@tool
def query_service_logs(service: str) -> str:
    """return up to 6 summarized recent error log lines for a service"""
    try: 
        logs = extract_log_summary(query_loki(service))
        return "\n".join(logs[:6]) or "no error logs"
    except Exception as e:
        return f"log query failed: {e}"

@tool
def query_topology(service: str) -> str:
    """return neo4j dependencies + past incidents for a service"""
    try:
        return query_knowledge_graph([service], "")
    except Exception as e:
        return f"kg query failes: {e}"

@tool
def lookup_runbook_chunk(query: str) -> str:
    """return the 2 most relevant runbook chunks for a query"""
    try:
        return lookup_runbook(query=query, k=2).get("context", "no rubbook match")
    except Exception as e:
        return f"runbook lookup failed: {e}"

@tool
def propose_remediation(action: str, tool: str, target_service: str, blast_radius: str) -> str:
    """
    PROPOSE (not execute) a remediation, Ends the investigation.
    blast_radius: pod | service | cluster.
    execution is gated elsewhere
    """
    return "proposed recorded, investigation ended."


READ_TOOLS = [
    query_prometheus_metric,
    query_service_logs,
    query_topology,
    lookup_runbook_chunk
]

ALL_TOOLS = READ_TOOLS + [propose_remediation]