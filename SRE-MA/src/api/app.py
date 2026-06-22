"""
api/app.py
--------------
fastapi app. handles http, runs the graph, keeps track of incidents.
"""

import uuid
import datetime
import traceback
import fastapi
from datetime import datetime, timedelta

from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from prometheus_client import make_asgi_app, Counter, Histogram, Gauge

from src.graph.state import ResolutionStatus, initial_state
from src.graph.orchestrator import run_incident, app as sre_graph
from src.api.schema import (
    IncidentCreate, 
    IncidentCreatedResponse,
    IncidentResponse,
    ApprovalRequest,
    ResolveRequest,
    AlertmanagerWebhook,
    AlertmanagerAlert,
    AgentStatusResponse,
    HealthCheckResponse,
    ComponentStatus,
    IncidentStats
)


# in memory incidents store
_incidents = {}

# prometheus metrics for agents itself
incident_counter = Counter(
    "agent_incident_total", "Total incidents processed", ["status", "severity"],
)
mttr_hist = Histogram(
    "agent_mttr_seconds",
    "MTTR distribution in seconds",
    buckets=[5,10,30,60,120,300,600]
)
active_gauge = Gauge(
    "agent_active_incidents", "Currently active incidents"
)
token_counter = Counter(
    "agent_tokens_total", "Total LLM tokens consumed"
)

#TODI: prod architecture should use the alertmanager api to create a silence for this alertname/service pair 
#instead of in-memmory debouncing
#silencing provides bi-directional feedback and survives api restarts
#track last processed time for alert signatures
_alert_debounce = {}
DEBOUNCE_SEC=300

# app
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[api] SRE Agent starting...")
    try:
        from src.rag.retriever import _load_index
        _load_index()
        print("[api] FAISS index loaded")
    except Exception as e:
        print(f"[api] Failed to load FAISS index: {e}")
    
    yield
    print("[api] SRE Agent shutting down...")


app = FastAPI(
    title="SRE Multi-Agent Orchestrator",
    description="Multi-agent incident reponse system",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


def _run_agent(incident_id: str, raw_signals: dict, config:dict):
    try:
        active_gauge.inc()
        print(f"[agent] Starting graph for incident {incident_id}")
        result = run_incident(incident_id, raw_signals, config=config)

        if incident_id in _incidents:
            _incidents[incident_id].update({
                "status": result.get('resolution_status'),
                "severity": result.get('severity'),
                "root_cause": result.get('root_cause'),
                "action_plan": result.get('action_plan'),
                "diagnosis_summary": result.get('diagnosis_summary'),
                "blast_analysis": result.get('blast_analysis'),
                "evidence_summary": result.get('evidence_summary'),
                "mttr_seconds": result.get('mttr_seconds'),
                "total_tokens_used": result.get('total_tokens_used'),
                "token_cost_usd": result.get('token_cost_usd'),
                "resolved_at": result.get('resolved_at'),
                "escalation_message": result.get('escalation_message'),
                "status_page_update": result.get('status_page_update'),
                "war_room_summary": result.get('war_room_summary')
            })

        status = result.get('resolution_status')
        severity = raw_signals.get('severity', 'unknown')

        incident_counter.labels(status=status, severity=severity).inc()
        mttr = result.get("mttr_seconds")
        if mttr:
            mttr_hist.observe(mttr)
        tokens = result.get('total_tokens_used', 0)
        if tokens:
            token_counter.inc(tokens)

        print(f"[agent] Graph completed for {incident_id} — status={status}")

    except Exception as e:
        print(f"[agent] *** AGENT CRASHED for {incident_id} ***")
        print(f"[agent] Error: {e}")
        traceback.print_exc()
        _incidents[incident_id].update({
            "status": ResolutionStatus.FAILED.value,
            "error": str(e)
        })
    finally:
        active_gauge.dec()


def _resume_agent(incident_id: str, config: dict):
    try:
        active_gauge.inc()
        print(f"[agent] Resuming graph for incident {incident_id}")
        from src.graph.orchestrator import app as sre_graph
        result = sre_graph.invoke(None, config=config)

        if incident_id in _incidents:
            _incidents[incident_id].update({
                "status": result.get('resolution_status'),
                "action_plan": result.get('action_plan'),
                "mttr_seconds": result.get('mttr_seconds'),
                "resolved_at": result.get('resolved_at'),
            })

        print(f"[agent] Graph completed for {incident_id} — status={result.get('resolution_status')}")

    except Exception as e:
        print(f"[agent] *** AGENT CRASHED during resume for {incident_id} ***")
        print(f"[agent] Error: {e}")
        traceback.print_exc()
        _incidents[incident_id].update({
            "status": ResolutionStatus.FAILED.value,
            "error": str(e)
        })
    finally:
        active_gauge.dec()

def _create_record(incident_id: str, raw_signals: dict, source: str) -> dict:
    return {
        "incident_id": incident_id,
        "status": ResolutionStatus.OPEN.value,
        "severity": raw_signals.get("severity"),
        "alert_name": raw_signals.get("alert_name") or raw_signals.get("alertname"),
        "runbook_id": raw_signals.get("runbook"),
        "team": raw_signals.get("team"),
        "source": source,
        "created_at": datetime.utcnow().isoformat(),
        "raw_signals": raw_signals,
        "root_cause": None,
        "diagnosis_summary": None,
        "evidence_summary": None,
        "blast_analysis": None,
        "action_plan": [],
        "mttr_seconds": None,
        "resolved_at": None,
        "error": None
    }


# endpoints

@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")

@app.get("/health", response_model=HealthCheckResponse, tags=["system"])
async def health():
    """just a simple up/down check"""
    active = sum(1 for i in _incidents.values() if i.get("status")==ResolutionStatus.INVESTIGATING.value) 
    return HealthCheckResponse(
        status="ok",
        active_incidents=active,
        total_incidents=len(_incidents)
    )

@app.post("/incidents", response_model=IncidentCreatedResponse, status_code=202)
async def create_incident(payload: IncidentCreate, background_tasks: BackgroundTasks):
    """
    make an incident manually. returns fast and processes in background.
    """
    incident_id = payload.incident_id or str(uuid.uuid4())
    raw_signals = payload.raw_signals

    _incidents[incident_id] = _create_record(incident_id, raw_signals, payload.source)
    background_tasks.add_task(_run_agent, incident_id, raw_signals)

    print(f"[api] Incident created: {incident_id} source= {payload.source}")
    return IncidentCreatedResponse(
        incident_id=incident_id, 
        status="accepted",
        message=f"Agent processing incident {incident_id}"
    )


@app.post("/webhook/alert", status_code=200)
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    background_tasks: BackgroundTasks
):
    """
    catch webhooks from alertmanager.
    dedupes using langgraph thread_id so we don't spam.
    """
    created, ignored = [], []
    for alert in payload.alerts:
        if alert.status != "firing":
            continue

        labels = alert.labels or {}
        annots = alert.annotations or {}

        alert_name = labels.get('alertname', 'unknown-alert')
        service = labels.get('service', 'unknown-service')

        #create alert key
        alert_key = service
        
        # debounce: skip if there's already an active incident for this service
        has_active = any(
            i.get("raw_signals", {}).get("service") == service
            and i.get("status") not in (
                ResolutionStatus.RESOLVED.value,
                ResolutionStatus.FAILED.value,
                ResolutionStatus.ESCALATED.value,
            )
            for i in _incidents.values()
        )
        if has_active:
            ignored.append(alert_name)
            continue

        #time based debouncing
        if alert_key in _alert_debounce:
            last_seen = _alert_debounce[alert_key]
            if datetime.utcnow() - last_seen < timedelta(seconds=DEBOUNCE_SEC):
                ignored.append(alert_name)
                continue
        
        _alert_debounce[alert_key] = datetime.utcnow()
        
        #safe ti run
        incident_id = f"INC-{alert_name[:6]}-{uuid.uuid4().hex[:4].upper()}"

        raw_signals = {
            **labels,
            "alert_name": labels.get('alertname'),
            "runbook": labels.get('runbook'),
            "team": labels.get("team"),
            "severity": labels.get("severity"),
            "service": labels.get("service"),
            "fired_at": alert.startsAt,
            "summary": annots.get("summary"),
            "description": annots.get("description")
        }
        
        config = {"configurable": {"thread_id": incident_id}}
        _incidents[incident_id] = _create_record(incident_id, raw_signals, "alertmanager")
        background_tasks.add_task(_run_agent, incident_id, raw_signals, config)
        created.append(incident_id)

        print(f"[api] Alert received: {alert_name} -> {incident_id}")
    return {"created": created, "count": len(created), "ignored_duplicates": ignored}



@app.get("/incidents", response_model=List[IncidentResponse])
async def list_incidents(
    status: List[str] = None,
    limit: int=50
): 
    """
    list all incidents, newest at the top
    """
    incidents = list(_incidents.values())
    if status:
        incidents = [i for i in incidents if i.get('status')==status] 
        
    incidents.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return incidents[:limit]

    

@app.get("/incidents/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: str):
    """
    get all the details for one incident
    """
    if incident_id not in _incidents:
        raise HTTPException(status_code=404, detail=f'Incident {incident_id} not found')
    
    incident_data = _incidents[incident_id].copy()
    try:
        from src.graph.orchestrator import app as agent_graph
        config = {"configurable": {"thread_id": incident_id}}
        state = agent_graph.get_state(config)
        if state and state.values:
            incident_data["agent_state"] = state.values
    except Exception as e:
        print(f"[api] Failed to get graph state for {incident_id}: {e}")
        
    return incident_data


@app.get("/incidents/{incident_id}/approve")
async def approve_incident(incident_id: str,background_tasks: BackgroundTasks,approver: str = "admin",
notes: str = ""):
    if incident_id not in _incidents:
        raise HTTPException(status_code=404,detail=f"Incident {incident_id} not found")
        
    try:
        from src.graph.orchestrator import app as agent_graph
        config = {
            "configurable": {"thread_id": incident_id}
        }
        agent_graph.update_state(config, values={"human_approved": True}, as_node="human_gate")

        _incidents[incident_id]["approved_by"] = approver
        _incidents[incident_id]["approved_at"] = datetime.utcnow().isoformat()

        if notes:
            _incidents[incident_id]["approval_notes"] = notes

        background_tasks.add_task( _resume_agent,incident_id,config)

        return {
            "status": "approved",
            "incident_id": incident_id,
            "approver": approver
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resume graph: {e}")

        

@app.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, payload: ResolveRequest):
    """
    force an incident to be resolved manually
    """
    if incident_id not in _incidents:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    _incidents[incident_id]["status"] = ResolutionStatus.RESOLVED.value
    _incidents[incident_id]["resolved_at"] = datetime.datetime.utcnow().isoformat()
    _incidents[incident_id]["resolution_notes"] = payload.notes

    return {"status": "resolved", "incident_id": incident_id}



@app.get("/agents/status", response_model=AgentStatusResponse)
async def agent_status():
    """health check for all the agents components"""
    faiss_ok = False
    neo4j_ok = False
    prometheus_ok = False
    
    try:
        from src.rag.retriever import _load_index
        _load_index()
        faiss_ok = True
    except Exception as e:
        pass

    try:
        from src.tools.kg_tool import _get_driver
        driver = _get_driver()
        if driver:
            with driver.session() as s:
                s.run("RETURN 1")
            neo4j_ok = True
    except Exception as e:
        pass
    
    try:
        import httpx, os
        resp = httpx.get(f"{os.getenv('PROMETHEUS_URL', 'http://localhost:9090')}/api/v1/query",
        params={'query': 'up'}, timeout=3.0)
        prom_ok = resp.status_code=200
    except Exception:
        pass

    total = len(_incidents)
    resolved = sum(1 for i in _incidents.values() if i.get('status') == ResolutionStatus.RESOLVED.value)
    escalated = sum(1 for i in _incidents.values() if i.get('status') == ResolutionStauts.ESCALATED.value)
    active = sum(1 for i in _incidents.values() if i.get('status')==ResolutionStatus.INVESTIGATING.value)

    return AgentStatusResponse(
        components=ComponentStatus(
            faiss_index = "ok" if faiss_ok else "unavailable",
            neo4j_kg = "ok" if neo4j_ok else "unavailable",
            prometheus = "ok" if prom_ok else "unavailable"
        ),
        incidents=IncidentStats(
            total=total,
            active=active,
            resolved=resolved,
            escalated=escalated,
            success_rate=round(resolved / total, 3) if total > 0 else 0.0,
        ),
    )