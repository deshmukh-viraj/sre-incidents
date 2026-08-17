"""
api/app.py
--------------
fastapi app. handles http, runs the graph, keeps track of incidents.
"""
import uuid
import datetime
import traceback
from datetime import datetime, timedelta, timezone

from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from prometheus_client import make_asgi_app, Counter, Histogram, Gauge


from src.graph.state import ResolutionStatus
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
from src.graph.routing import ALERT_CORRELATION_WINDOW, DEFAULT_CORRELATION_WINDOW, infer_service
from src.graph.timeutils import parse_ts
from src.utils.logger import inc_id
from src.tools.kg_tool import find_active_upstream_incident

# in memory incidents store
_incidents = {}

# prometheus metrics for agents itself
incident_counter = Counter(
    "agent_incident_total", "Total incidents processed", ["status", "severity"],
)
business_mttr_hist = Histogram(
    "agent_business_mttr_seconds",
    "Business MTTR distribution in seconds",
    buckets=[5,10,30,60,120,300,600]
)
agent_mttr_hist = Histogram(
    "agent_execution_latency_seconds",
    "Agent execution latency distribution in seconds",
    buckets=[5,10,30,60,120,300,600]
)
active_gauge = Gauge(
    "agent_active_incidents", "Currently active incidents"
)
token_counter = Counter(
    "agent_tokens_total", "Total LLM tokens consumed"
)
attribution_counter = Counter(
    "agent_attribution_total", "Total incidents by resolution cause",
    ["resolution_cause", "arm"]
)
# verification_pass_rate = Gauge(
#     "agent_verification_pass_rate", 
#     "Rate of successful verifications (GATE-4) out of all successfil executions (GATE2)"
# )
credit_revoked_counter = Counter(
    "agent_credit_invoked_total", "Total incidents where provisional agent credit was revoked (GATE5)"
)

gate_eval_counter = Counter(
    "agent_gate_evaluation_total",
    "Attribution gate evaluations by gate number and outcome",
    ["gate", "result"]
)

attribution_lead_hist = Histogram(
    "agent_attribution_lead_seconds",
    "Signed gap between measured recovery and actio end (-ve = pre exisring recovery)",
    buckets=[-300,-120,-60,-30,-10,0,10,30,60,120,300]
)


#HoldOut ARM

# the only honest way to measure our own false-positive rate. a fraction of incidents 
# run the FULL decision path --diagnose, claim, target-bind, log the plan -- then execute a NO-OP.
# verification and attribution run unchanged.

# any incident in the shadow arm labelled agent_remediated isBY DEFINATION a fals-positive,
# agent did nothing, so agent fixed nothing. that ratio is the number this whole framework exists to produce.
import os
HOLDOUT_RATE = float(os.getenv("ATTRIBUTION_HOLDOUT_RATE"))
_HOLDOUT_SEED = os.getenv("ATTRIBUTION_HOLDOUT_SEED")

def _pick_arm(incident_id: str) -> bool:
    """
    True == this incident is in the shadow (no-op) arm.
    Deterministic per incident_id when a seed is set, so a validaton run is
    reproducible, otherwise randomised per incident.
    """
    if HOLDOUT_RATE <=0.0:
        return False
    
    if _HOLDOUT_SEED:
        import hashlib
        digest = hashlib.sha256(f"{_HOLDOUT_SEED}:{incident_id}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    else:
        import random
        draw = random.random()
    return draw < HOLDOUT_RATE


def _utcnow(): 
    """tz-aware UTC now."""
    return datetime.now(timezone.utc)

def _do_revoke(revoke_credit, revoke_incident_id: str, caused_by_id: str) -> None:
    """
    execute a gate-5 revocation exactly once, counting it onlt on success."""
    try:
        revoke_credit(revoke_incident_id, caused_by_id)
        credit_revoked_counter.inc()
        print(f"[api] Gate-5: revoked credit for {revoke_incident_id} (re-fire ->) {caused_by_id}")
    except Exception as e:
        print(f"[api] gate-5: revocation FAILED for {revoke_incident_id}: {e}")


#prod architecture should use the alertmanager api to create a silence for this alertname/service pair 
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


def _run_agent(incident_id: str, raw_signals: dict, config: Optional[dict] = None):

    inc_id.set(incident_id)
    try:
        active_gauge.inc()
        shadow = _pick_arm(incident_id)
        if shadow:
            print(f"[agent] {incident_id} -> SHADOW arm (execution suppressed)")
            raw_signals = {**raw_signals, "shadow_execution": True}
            if incident_id in _incidents:
                _incidents[incident_id]["shadow_execution"] = True
                if isinstance(_incidents[incident_id].get("raw_signals"), dict):
                    _incidents[incident_id]["raw_signals"]["shadow_execution"] = True
        
        print(f"[agent] Starting graph for incident {incident_id}")
        result = run_incident(incident_id, raw_signals, config=config)

        if shadow:
            result = {**result, "shadow_execution": True}
        if incident_id in _incidents:
            _incidents[incident_id].update({
                "status": result.get('resolution_status'),
                "severity": result.get('severity'),
                "root_cause": result.get('root_cause'),
                "action_plan": result.get('action_plan'),
                "diagnosis_summary": result.get('diagnosis_summary'),
                "blast_analysis": result.get('blast_analysis'),
                "evidence_summary": result.get('evidence_summary'),
                "business_mttr_seconds": result.get('business_mttr_seconds'),
                "agent_mttr_seconds": result.get('agent_mttr_seconds'),
                "total_tokens_used": result.get('total_tokens_used'),
                "token_cost_usd": result.get('token_cost_usd'),
                "alert_started_at": result.get('alert_started_at'),
                "agent_invoked_at": result.get('agent_invoked_at'),
                "action_executed_at": result.get('action_executed_at'),
                "verified_at": result.get('verified_at'),
                "escalation_message": result.get('escalation_message'),
                "status_page_update": result.get('status_page_update'),
                "war_room_summary": result.get('war_room_summary'),

                **_attribution_fields(result)
            })

        status = result.get('resolution_status')
        _record_attribution(result, raw_signals.get('severity', 'unknown'))

        business_mttr = result.get("business_mttr_seconds")
        if business_mttr:
            business_mttr_hist.observe(business_mttr)
        agent_mttr = result.get("agent_mttr_seconds")
        if agent_mttr:
            agent_mttr_hist.observe(agent_mttr)
        tokens = result.get('total_tokens_used', 0)
        if tokens:
            token_counter.inc(tokens)

        print(f"[agent] Graph completed for {incident_id} —> status={status}")

    except Exception as e:
        print(f"[agent]  AGENT CRASHED for {incident_id} ")
        print(f"[agent] Error: {e}")
        traceback.print_exc()
        _incidents[incident_id].update({
            "status": ResolutionStatus.FAILED.value,
            "error": str(e)
        })
    finally:
        active_gauge.dec()


def _record_attribution(result: dict, severity:str) -> None:
    """ emit all attribution metrics for a completed graph run"""

    status = result.get("resolution_status")
    resolution_cause = result.get("resolution_cause") or "unknown"
    arm = "shadow" if result.get("shadow_execution") else "action"

    incident_counter.labels(status=status, severity=severity or "unknown").inc()
    attribution_counter.labels(resolution_cause=resolution_cause, arm=arm).inc()

    for gate_num, gate_key in (("1", "gate1_claim"), ("2", "gate2_target_bound"),
                                ("3", "gate3_temporal"), ("4", "gate4_verified"),
                                ("5", "gate5_stable")):
                        gate_val = result.get(gate_key)
                        if gate_val is not None:
                            gate_eval_counter.labels(
                                gate=gate_num, result="pass" if gate_val else "fail"
                            ).inc()

    lead = result.get("attribution_lead_seconds")
    if lead is not None:
        attribution_lead_hist.observe(lead)


def _attribution_fields(result: dict) -> dict:
    """the attribution slice of an incident recorn, for api responses"""
    return {
        "resolution_cause": result.get("resolution_cause"),
        "shadow_execution": bool(result.get("shadow_execution")),
        "t_clear_true": result.get("t_clear_true"),
        "t_clear_polled": result.get("t_clear_polled"),
        "clear_source": result.get("clear_source"),
        "attribution_lead_seconds": result.get("attribution_lead_seconds"),
        "gate3_reason": result.get("gate3_reason"),
        "gates": {
            "gate1_claim": result.get("gate1_claim"),
            "gate2_target_bound": result.get("gate2_target_bound"),
            "gate3_temporal": result.get("gate3_temporal"),
            "gate4_verified": result.get("gate4_verified"),
            "gate5_stable": result.get("gate5_stable")
        }
    }


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
                "business_mttr_seconds": result.get('business_mttr_seconds'),
                "agent_mttr_seconds": result.get('agent_mttr_seconds'),
                "alert_started_at": result.get('alert_started_at'),
                "agent_invoked_at": result.get('agent_invoked_at'),
                "action_executed_at": result.get('action_executed_at'),
                "verified_at": result.get('verified_at'),
                **_attribution_fields(result)
            })

        #approve incidents must count toward attribution too
        _record_attribution(
            result,
            _incidents.get(incident_id, {}).get('severity') or 'unknown',
        )

        print(f"[agent] Graph completed for {incident_id} — status={result.get('resolution_status')}")

    except Exception as e:
        print(f"[agent]  AGENT CRASHED during resume for {incident_id}")
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
        "alert_started_at": raw_signals.get("alert_started_at") or datetime.utcnow().isoformat(),
        "agent_invoked_at": datetime.utcnow().isoformat(),
        "raw_signals": raw_signals,
        "root_cause": None,
        "diagnosis_summary": None,
        "evidence_summary": None,
        "blast_analysis": None,
        "action_plan": [],
        "business_mttr_seconds": None,
        "agent_mttr_seconds": None,
        "action_executed_at": None,
        "verified_at": None,
        "error": None
    }


def find_correlated_incident(service: str, alert_name: str) -> Optional[str]:
    """
    return the inident_id to correlate this alert against, if one exist
    covers both still-active incidents AND recently resolved ones whose underlying
    metric winodw can stiil be drining
    """
    win = ALERT_CORRELATION_WINDOW.get(alert_name, DEFAULT_CORRELATION_WINDOW)
    
    for incident_id, i in _incidents.items():
        raw_signals = i.get("raw_signals", {})
        inc_service = raw_signals.get("service")
        inc_alert = i.get("alert_name") or raw_signals.get("alert_name") or raw_signals.get("alertname")

        #match by service if known, or fallback to alert_name matching when service is missing/unknown
        same_service = (service and service != "unknown-service" and inc_service == service)
        same_alert = (alert_name and alert_name != "unknown-alert" and inc_alert == alert_name)

        if not (same_service or same_alert):
            continue
        
        status = i.get("status")
        if status not in (ResolutionStatus.FAILED.value, ResolutionStatus.ESCALATED.value):
            if status == ResolutionStatus.RESOLVED.value:
                verified_at = i.get("verified_at")
                if not verified_at:
                    continue
                elapsed = (_utcnow() - parse_ts(verified_at)).total_seconds()
                if elapsed < win:
                    return incident_id
            else:
                return incident_id
    return None



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
        service = labels.get('service') or infer_service(alert_name)

        # 5-gate logic stability
        # we only detect the re-fire here. the actual revoke_credit() call is deferred until we
        # know the id of the incident that caused it
        
        from src.graph.routing import T_STABLE_SECONDS
        from src.tools.kg_tool import query_recent_resolved_incident, revoke_credit,get_dependencies
        import datetime as dt 

        deps = get_dependencies(service)
        parent_incident = None 
        if deps:
            for iid, inc in _incidents.items():
                inc_service = inc.get("raw_signals", {}).get("service")
                if inc_service in deps and inc.get("status") not in (
                    ResolutionStatus.RESOLVED.value, ResolutionStatus.FAILED.value, ResolutionStatus.ESCALATED.value
                ):
                    parent_incident = {"incident_id": iid, "upstream_service": inc_service, "root_cause": inc.get("root_cause"), "live": True}
                    break
        if not parent_incident:
            parent_incident = find_active_upstream_incident(service)

        if parent_incident:
            print(f"[api] CASCADE DETECTED: {alert_name} on {service} is a symptom of "
                   f"{parent_incident['incident_id']} on {parent_incident['upstream_service']}")
                   
            # link this alert to the parent incident and skip spawning a new agent
            parent_id = parent_incident['incident_id']
            if parent_id in _incidents:
                _incidents[parent_id].setdefault("correlated_alerts", [])
                _incidents[parent_id]["correlated_alerts"].append({
                    "alert_name": alert_name,
                    "received_at": _utcnow().isoformat(),
                    "role": "SYMPTOM"
                })
            ignored.append(alert_name)
            continue #skip the rest of loop, do not spawn a new lg agent
                
            
        pending_revoke_id = None
        recent = query_recent_resolved_incident(alert_name, service)
        if recent:
            verified_at = recent.get("verified_at")
            if verified_at:
                #parse timestamp
                elapsed = (_utcnow() - parse_ts(verified_at)).total_seconds()
                stable_window = T_STABLE_SECONDS.get(recent.get("severity", "SEV2"), 900)

                if elapsed < stable_window:
                    pending_revoke_id = recent["incident_id"]
                    print(
                        f"[api] Gate-5: re-fire of {alert_name} {elapsed:.0f}s into a "
                        f"{stable_window}s stability window -> credit revocation pending"
                    )
        

        #debounce per (service, alertname)
        # service for 5 mins
        alert_key = (service, alert_name)
        
        # correlation gate: check both active and recently resolved incident
        # using a winodw sized
        corr_incident_id = find_correlated_incident(service, alert_name)
        if corr_incident_id:
            _incidents[corr_incident_id].setdefault("correlated_alerts", [])
            _incidents[corr_incident_id]["correlated_alerts"].append({
                "alert_name": alert_name, 
                "received_at": _utcnow().isoformat(),
            })
            print(f"[api] Correlated {alert_name} -> existing incident {corr_incident_id} (not spawning, new run)")

            # a re-fire is still a re-fire even if we fold it into an open incident
            if pending_revoke_id:
                _do_revoke(revoke_credit, pending_revoke_id, corr_incident_id)
            ignored.append(alert_name)
            continue

        #time based debouncing
        if alert_key in _alert_debounce:
            last_seen = _alert_debounce[alert_key]
            if _utcnow() - last_seen < timedelta(seconds=DEBOUNCE_SEC):
                if pending_revoke_id:
                    _do_revoke(revoke_credit, pending_revoke_id, "DEBOUNCED")
                ignored.append(alert_name)
                continue
        
        _alert_debounce[alert_key] = _utcnow()
        
        #safe to run
        incident_id = f"INC-{alert_name[:6]}-{uuid.uuid4().hex[:4].upper()}"

        # now that the causing incident id exists, settle the gate5 revocation
        if pending_revoke_id:
            _do_revoke(revoke_credit, pending_revoke_id, incident_id)

        raw_signals = {
            **labels,
            "alert_name": labels.get('alertname'),
            "runbook": labels.get('runbook'),
            "team": labels.get("team"),
            "severity": labels.get("severity"),
            "service": labels.get("service"),
            "alert_started_at": alert.startsAt,
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
        wanted = set(status)
        incidents = [i for i in incidents if i.get('status') in wanted] 
        
    incidents.sort(key=lambda x: x.get('alert_started_at', ''), reverse=True)
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
        _incidents[incident_id]["approved_at"] = _utcnow().isoformat()

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
    _incidents[incident_id]["verified_at"] = _utcnow().isoformat()
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
        prometheus_ok = resp.status_code==200
    except Exception:
        pass

    total = len(_incidents)
    resolved = sum(1 for i in _incidents.values() if i.get('status') == ResolutionStatus.RESOLVED.value)
    escalated = sum(1 for i in _incidents.values() if i.get('status') == ResolutionStatus.ESCALATED.value)
    active = sum(1 for i in _incidents.values() if i.get('status')==ResolutionStatus.INVESTIGATING.value)

    return AgentStatusResponse(
        components=ComponentStatus(
            faiss_index = "ok" if faiss_ok else "unavailable",
            neo4j_kg = "ok" if neo4j_ok else "unavailable",
            prometheus = "ok" if prometheus_ok else "unavailable"
        ),
        incidents=IncidentStats(
            total=total,
            active=active,
            resolved=resolved,
            escalated=escalated,
            success_rate=round(resolved / total, 3) if total > 0 else 0.0,
        ),
    )