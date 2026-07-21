"""
agents/diagnoser_agent.py
--------------------------
diagnoser agent: two nodes in the LangGraph pipeline.

    diagnoser_node  — deterministic pattern matching, no LLM
    llm_diagnoser   — LLM + FAISS runbooks + Neo4j KG fallback

responsibility:
    - try deterministic diagnosis first (fast, free, no API calls)
    - fall back to LLM only when deterministic confidence is too low
    - extract both root cause and suggested remediation from context

writes to state:
    hypotheses, root_cause, diagnosis_summary, evidence_summary, blast_analysis, 
    llm_suggested_action, diagnosis_mode, model_used, total_token_used, total_cost_usd

"""

import os
import json
import re

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage

from src.graph.state import AgentState
from src.graph.routing import deterministic_diagnosis, DIAGNOSIS_CONFIDENCE
from src.tools.kg_tool import query_knowledge_graph
from src.tools.sre_tool import lookup_runbook
from src.agents.utils import _get_llm

load_dotenv()


# node 1: deterministic diagnoser

def diagnoser_node(state: AgentState) -> dict:
    """
    try hardcoded checks first using just numbers.
    if we find something obvious, return high confidence and skip the llm.
    otherwise, return empty so it goes to llm.
    """
    print(f"\n[diagnoser] Deterministic diagnosis attempt for {state['incident_id']}")

    result = deterministic_diagnosis(state)
    if result:
        confidence = result.get("confidence", 0)
        print(f"[diagnoser] Pattern matched: {result['hypothesis'][:60]} (confidence={confidence:.2f})")

        evidence_list = [e for e in result.get("evidence", []) if e]
        evidence_str = "\n".join([f" - {e}" for e in evidence_list]) if evidence_list else "Rule matched on metrics threshold"

        hypothesis = {
            "hypothesis": result['hypothesis'],
            "evidence": evidence_list,
            "evidence_str": evidence_str,
            "confidence": confidence,
            "alternative": result.get("alternative"),
            "supporting_runbook": result.get("supporting_runbook")
        }
        
        affected_services = state.get("affected_services", [])
        affected_service = affected_services[0] if affected_services else "unknown_service"

        return {
            "hypotheses": [hypothesis],
            "root_cause": result["hypothesis"] if confidence >= DIAGNOSIS_CONFIDENCE else None,
            "diagnosis_mode": "deterministic",
            "diagnosis_loops": state.get("diagnosis_loops", 0) + 1,
            "diagnosis_summary": f"Deterministic pattern matched: {result['hypothesis']}",
            "evidence_summary": evidence_str,
            "blast_analysis": f"Action targets {affected_service} with potential service disruption."
        }

    
    print(f"[diagnoser] No deterministic pattern matched, checking KG Memory...")
    
    from src.tools.kg_tool import query_knowledge_graph
    alert_name = state.get("alert_name")
    affected_services = state.get("affected_services", ["unknown"])
    past_inci = query_knowledge_graph(affected_services=affected_services, alert_name=alert_name)

    if past_inci:
        print(f"[diagnoser] Reusing past remediation: {past_inci}")
        return {
            "hypotheses": [{
                "hypothesis": past_inci,
                "evidence": ["Retrived from Knowledge GRAPH (past incident)"],
                "confidence": 0.95,
                "alternative": None,
                "supporting_runbook": "KG-Memory"
            }],
            "root_cause": past_inci,
            "diagnosis_mode": "KG-Memory-Recall",
            "diagnosis_loops": state.get("diagnosis_loops", 0) + 1,
            "diagnosis_summary": f"Retrived from KG-Memory: {past_inci}",
            "evidence_summary": "Matched previous successful incident in Neo4j",
            "llm_suggested_action": past_inci
        }

    print("[diagnoser] no past incident found will route to LLM diagnoser")
    return {
        "hypotheses": [],
        "diagnosis_mode": "deterministic",
        "diagnosis_loops": state.get("diagnosis_loops", 0) + 1,
    }


# nnode 2: llm diagnoser
def llm_diagnoser(state: AgentState) -> dict:
    """
    ask the llm if we don't know what's going on.
    we give it:
    - some runbook text
    - neo4j graph stuff
    - prometheus metrics + loki logs

    llm should figure out root cause and what to do next.
    only call this if our hardcoded checks aren't sure.
    """
    print(f"\n[llm_diagnoser] LLM diagnosis for {state['incident_id']}")

    llm = _get_llm(temperature=0.1)
    raw = state.get("raw_signals", {})
    alert = state.get("alert_name", "unknown")
    summary = state.get("incident_summary", "")
    runbook = state.get("runbook_id")
    services = state.get("affected_services", [])

    #context: FAISS runbook chunks
    rb_result = lookup_runbook(query=f"{alert} {' '.join(services)}", runbook_id=runbook, k=3)
    book_context = rb_result.get("context", "No runbook context found.")

    # context: Neo4j KG
    kg_context = query_knowledge_graph(affected_services=services, alert_name=alert)

    # signal summary (exclude log summaries / patterns)
    signals_str = (
        "\n".join(
            f"  {k}: {v}"
            for k, v in raw.items()
            if v is not None and not k.endswith("summaries") and k != "log_patterns"
        )
        or "No signals available"
    )

    # log summaries
    error_logs = "\n".join(raw.get("error_log_summaries", [])[:6]) or "None"

    system_prompt = f"""You are an expert SRE diagnosing a production incident.
You will be given:
1. Metrics signals from Prometheus
2. Log excerpts from Loki
3. Relevant runbook context
4. Domain knowledge graph context

Your job is to identify the most likely root cause and return structured JSON.

Rules:
- Your confidence score must be honest — 0.70+ only if evidence is strong
- Be specific — name the exact service and failure mode
- diagnosis_summary must connect the signals to the root cause clearly
- blast_analysis must name specific downstream services
- Never invent signals or actions not present in the data
- Always suggest the most likely alternative hypothesis

Return ONLY valid JSON matching this schema. No explanation, no markdown fences.

Schema:
{{
    "hypotheses": [
    {{
        "hypothesis": "string -- plain english root cause",
        "evidence": ["supporting signal or log"],
        "confidence": 0.0,
        "alternative": "competing explanation or null",
        "supporting_runbook": "RB-00X or null"
    }}
    ],
    "root_cause": "most likely hypothesis — one clear sentence",
    "diagnosis_summary": "2-4 sentences explaining what is happening, why, and how signals connect. Write for an on-call engineer who just woke up.",
    "evidence_summary": "bullet list of the key signals that led to this diagnosis",
    "blast_analysis": "what breaks next if this is not fixed in the next 10 minutes",
    "suggested_remediation_from_context": "exact fix from KG/docs if found, else null"
}}
"""

    human_prompt = f"""INCIDENT: {alert}
SEVERITY: {state.get('severity', 'unknown')}

METRIC_SIGNALS:
{signals_str}

RECENT ERROR LOGS:
{error_logs or 'No error logs available'}

RUNBOOK CONTEXT:
{book_context}

DOMAIN KNOWLEDGE (TOPOLOGY + DOCS + PAST INCIDENTS):
{kg_context}

Diagnose this incident. Return JSON ONLY.
"""

    try:
        resp = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)])
        raw_json = resp.content.strip()

        # extract the outermost JSON object regardless of markdown fences or surrounding text
        m = re.search(r'\{.*\}', raw_json, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON object found in LLM response ({len(raw_json)} chars)")
        
        extracted_json = m.group()
        try:
            parsed = json.loads(extracted_json)
        except json.JSONDecodeError as json_err:
            print(f"[llm_diagnoser] Standard json parsed failed: {json_err}")
            repaired_str = repair_json(extracted_json)
            parsed = json.loads(repaired_str)
        hypotheses = parsed.get("hypotheses", [])
        root_cause = parsed.get("root_cause")
        llm_action = parsed.get("suggested_remediation_from_context")
        diagnosis_summary = parsed.get("diagnosis_summary")
        evidence_summary = parsed.get("evidence_summary")
        blast_analysis = parsed.get("blast_analysis")

        # token tracking
        usage = resp.response_metadata.get("token_usage", {})
        new_tokens = usage.get("total_tokens", 0)
        cost = new_tokens * 0.000015
        max_conf = max((h.get("confidence", 0) for h in hypotheses), default=0)

        print(f"[llm_diagnoser] Root cause: {root_cause[:60] if root_cause else 'None'}")
        print(f"[llm_diagnoser] Max confidence: {max_conf:.2f} | tokens: {new_tokens}")
        if llm_action:
            print(f"[llm_diagnoser] KG action: {llm_action[:80]}")

        return {
            "hypotheses": hypotheses,
            "root_cause": root_cause if max_conf >= DIAGNOSIS_CONFIDENCE else None,
            "diagnosis_summary": diagnosis_summary,
            "evidence_summary": evidence_summary,
            "blast_analysis": blast_analysis,
            "llm_suggested_action": llm_action,
            "diagnosis_mode": "llm",
            "diagnosis_loops": state.get("diagnosis_loops", 0) + 1,
            "model_used": os.getenv("LLM_MODEL", "llama3-70b-8192"),
            "total_tokens_used": state.get("total_tokens_used", 0) + new_tokens,
            "token_cost_usd": state.get("token_cost_usd", 0.0) + cost,
        }

    except Exception as e:
        print(f"[llm_diagnoser] ERROR: {e}")
        return {
            "hypotheses": [],
            "diagnosis_mode": "llm",
            "diagnosis_loops": state.get("diagnosis_loops", 0) + 1,
            "errors": state.get("errors", []) + [f"llm_diagnoser failed: {str(e)}"],
        }
