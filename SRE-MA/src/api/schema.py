"""
api/schemas.py
--------------
pydantic models for all FastAPI request/response bodies.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union



#External Alerts

class AlertmanagerAlert(BaseModel):
    """single alert inside an alertmanager webhoook payload"""
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    status: str = Field(default="firing")
    generatorURL: Optional[str] = None
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
 

class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload format."""
    version: Optional[str]  = None
    groupKey: Optional[str] = None
    status: Optional[str] = None
    receiver: Optional[str] = None
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)
    groupLabels: Optional[Dict] = None
    commonLabels: Optional[Dict] = None



#incident Management (Requests)

class IncidentCreate(BaseModel):
    raw_signals: Dict[str, Any] = Field(
        description="Alert labels + any known metric values",
        examples=[{
            "alert_name": "PaymentGatewayP99LatencyHigh",
            "runbook": "RB-001",
            "team": "payments",
            "severity": "critical",
            "service": "payment_gateway",
        }],
    )
    source: str = Field(default="manual", description="alertmanager | manual | test")
    incident_id: Optional[str] = Field(default=None, description="Optional custom ID")


class ApprovalRequest(BaseModel):
    approved: bool = Field(description="True to approve and execute, false to reject")
    approver:str = Field(description="Name or ID of the engineer approving")
    notes:  Optional[str] = None


class ResolveRequest(BaseModel):
    notes: Optional[str] = Field(description="Notes for manual resolution")



#incident Management (Responses)

class IncidentResponse(BaseModel):
    incident_id: str
    status: str
    severity: Optional[str] = None
    alert_name: Optional[str] = None
    runbook_id: Optional[str] = None
    created_at: Optional[str] = None
    source: Optional[str] = None
    team: Optional[str] = None
    root_cause: Optional[str] = None
    diagnosis_summary: Optional[Union[str, List[str]]] = None
    evidence_summary: Optional[Union[str, List[str]]] = None
    blast_analysis: Optional[Union[str, List[str]]] = None
    diagnosis_mode: Optional[str] = None
    action_plan: Optional[List[Dict]] = None
    required_approval: Optional[bool] = None
    mttr_seconds: Optional[int] = None
    total_tokens_used: Optional[int] = None
    token_cost_usd: Optional[float] = None
    resolved_at: Optional[str] = None
    war_room_summary: Optional[Union[str, List[str]]] = None
    status_page_update: Optional[str] = None
    escalation_message: Optional[str] = None
    error: Optional[str] = None

    class Config:
        extra = "allow"  # allow extra fields from agent state


class IncidentCreatedResponse(BaseModel):
    incident_id: str
    status: str
    message: str


# internal / Agent State
class ActionItem(BaseModel):
    action: str
    tool: str
    params: Dict[str, Any]
    blast_radius: str
    reversible: bool
    required_approval: bool
    executed: bool
    result: Optional[str] =  None


# system Status & Health
class ComponentStatus(BaseModel):
    faiss_index: str
    neo4j_kg: str
    prometheus: str


class IncidentStats(BaseModel):
    total: int
    active: int
    resolved: int
    escalated: int
    success_rate: float


class AgentStatusResponse(BaseModel):
    components: ComponentStatus
    incidents: IncidentStats
   

class HealthCheckResponse(BaseModel):
    status: str
    active_incidents: int
    total_incidents: int