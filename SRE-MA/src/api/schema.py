"""
api/schemas.py
--------------
Pydantic models for all FastAPI request/response bodies.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class IncidentCreate(BaseModel):
    raw_signals: Dict[str, Any] = Field(description="Alert labels + any known metric values")
    source: str = Field(default="manual", description="alertmanager | manual | test")
    incident_id: Optional[str] = Field(default=None, description="Optional custom ID")


class IncidentResponse(BaseModel):
    incident_id: str
    status: str
    severity: Optional[str]   = None
    alert_name: Optional[str]   = None
    created_at: Optional[str]   = None
    source: Optional[str]   = None
    root_cause: Optional[str]   = None
    action_plan: Optional[List]  = None
    mttr_seconds: Optional[int]   = None
    token_cost_usd: Optional[float] = None
    resolved_at: Optional[str]   = None

    class Config:
        extra = "allow"  # allow extra fields from agent state


class ApprovalRequest(BaseModel):
    approved: bool
    approver:str = Field(description="Name or ID of the engineer approving")
    notes:  Optional[str] = None


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload format."""
    version: Optional[str]  = None
    groupKey: Optional[str] = None
    status: Optional[str] = None
    receiver: Optional[str] = None
    alerts: List[Dict[str, Any]] = Field(default_factory=list)
    groupLabels: Optional[Dict] = None
    commonLabels: Optional[Dict] = None


class AgentStatusResponse(BaseModel):
    detector_ok: bool
    diagnoser_ok: bool
    remediator_ok: bool
    communicator_ok: bool
    faiss_index_ok: bool
    open_incidents: int
    resolved_incidents: int
    resolution_success_rate: float