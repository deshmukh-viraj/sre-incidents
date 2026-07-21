"""
one big state object that gets passed around the langgraph nodes.
every agent reads and writes to this.
this is the only way they share data.
"""

import os
from typing import TypedDict, List, Optional, Dict, Any
from enum import Enum


class Severity(str, Enum):
    SEV1 ="SEV1"   
    SEV2 = "SEV2"   
    SEV3 = "SEV3"   
    SEV4 = "SEV4"   
    SEV5 = "SEV5"  


class ResolutionStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    EXECUTED_UNVERIFIED = "executed_unverified"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    ESCALATED = "escalated"
    FAILED = "failed"

class ResolutionCause(str, Enum):
    AGENT_REMEDIATED = "agent_remediated"
    NATURAL_CALM = "natural_calm"
    HUMAN_REMEDIATED = "human_remediated"
    EXTERNAL_REMEDIATED = "external_remediated"
    AMBIGUOUS = "ambiguous"
    ACTION_FAILED = "action_failed"

class AttributionStatus(str, Enum):
    PROVISIONAL = "provisional"
    FINAL = "final"

class Hypothesis(TypedDict):
    hypothesis:str           # plain-English root cause theory
    evidence:List[str]     # list of supporting signal snippets
    confidence:float         # 0.0 – 1.0
    alternative:Optional[str] 
    supporting_runbook:Optional[str] 


class ActionItem(TypedDict):
    action:str            
    tool: str            # which sre_tool to call
    params: Dict[str, Any] 
    blast_radius:str            
    reversible: bool
    requires_approval: bool
    executed: bool
    result: Optional[str]


class AgentState(TypedDict, total=False):
    #identity 
    incident_id:  str
    active_scenario: Optional[str]   # ground-truth label from simulator

    #raw input signals 
    raw_signals: Dict[str, Any]  #alert labels + metric
    alert_name: Optional[str]
    runbook_id: Optional[str] 
    team: Optional[str]

    #detector outputs
    incident_summary:Optional[str]
    severity: Optional[str]  # Severity enum value
    affected_services: List[str]

    #Diagnoser outputs
    hypotheses: List[Hypothesis]
    diagnosis_summary: Optional[str] # plain english summary of root cause hypothesis (2-3 sentences)
    evidence_summary: Optional[str]
    blast_analysis: Optional[str]
    root_cause: Optional[str]
    diagnosis_mode: Optional[str]   # deterministic | llm | hybrid
    diagnosis_loops: int             # loop counter capped at max_diagnosis_loops

    #remediator outputs
    action_plan: List[ActionItem]
    requires_approval: bool
    human_approved:bool
    approval_timeout:  bool

    #communicator output
    status_page_update: Optional[str]    
    war_room_summary: Optional[str]   
    escalation_message: Optional[str]   

    #resolution tracking
    action_taken: str
    resolution_status: str             # resolutionStatus enum value
    resolution_notes:Optional[str]
    business_mttr_seconds:Optional[int]
    agent_mttr_seconds:Optional[int]
    verified: bool                     # true if post execution metric check passed

    #LLM cost tracking
    llm_suggested_action: Optional[str]
    total_tokens_used: int
    token_cost_usd: float
    model_used: Optional[str]

    #Timestamps
    alert_started_at: str 
    agent_invoked_at: str 
    action_executed_at: Optional[str]   
    verified_at: Optional[str]   

    #Error handling
    errors: List[str]      
    final_p99_latency_s: Optional[float]
    error_rate: Optional[float]

    #attribution (5 gate test)
    agent_identity: str
    claim_id: str
    t_claim: Optional[str]
    execution_evidence: List[Dict[str, Any]]
    t_clear: Optional[str]
    verification_evidence: Optional[Dict[str, Any]]
    resolution_cause: Optional[str]
    attribution_status: Optional[str]
    recurrence_of: Optional[str]


def initial_state(incident_id: str, raw_signals: Dict[str, Any]) -> AgentState:
    """
    factory method: creates a fresh agent state from a new alert.
    call this in the fastapi route before starting the graph.
    """
    import datetime

    return AgentState(
        incident_id=incident_id,
        active_scenario=raw_signals.get('__sim_scenario'),

        raw_signals=raw_signals,
        alert_name=raw_signals.get('alert_name') or raw_signals.get("alertname"),
        runbook_id=raw_signals.get('runbook'),
        team=raw_signals.get('team'),

        incident_summary=None,
        severity=None,
        affected_services=[],

        hypotheses=[],
        root_cause=None,
        diagnosis_mode=None,
        diagnosis_loops=0,

        action_plan=[],
        requires_approval=False,
        human_approved=False,
        approval_timeout=False,

        status_page_update=None,
        war_room_summary=None,
        escalation_message=None,

        resolution_status=ResolutionStatus.OPEN.value,
        resolution_notes=None,
        business_mttr_seconds=None,
        agent_mttr_seconds=None,
        verified=False,

        llm_suggested_action=None,
        total_tokens_used=0,
        token_cost_usd=0.0,
        model_used=None,

        alert_started_at=raw_signals.get('alert_started_at') or datetime.datetime.utcnow().isoformat(),
        agent_invoked_at=datetime.datetime.utcnow().isoformat(),
        action_executed_at=None,
        verified_at=None,
        errors=[],

        diagnosis_summary= None,
        evidence_summary=None,
        blast_analysis=None,
        final_p99_latency_s = None,
        error_rate = None,

        agent_identity=os.getenv("AGENT_IDENTITY", "sre-agent-a1"),
        claim_id=None,
        t_claim=None,
        execution_evidence=[],
        t_clear=None,
        verification_evidence=None,
        resolution_cause=None,
        attribution_status=None,
        recurrence_of=None,
    )