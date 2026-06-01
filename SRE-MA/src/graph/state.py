"""
single shared state object that flows through the entire langgraph
every agent reads from this and writes back to it
nnothing else is passed between agents this is the only contract
"""

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
    ESCALATED = "escalated"


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
    reversible:  bool
    requires_approval: bool
    executed:  bool
    result:  Optional[str]


class AgentState(TypedDict):
    #identity 
    incident_id:  str
    active_scenario: Optional[str]   # ground-truth label from simulator

    #raw input signals 
    raw_signals:  Dict[str, Any]  #alert labels + metric
    alert_name:  Optional[str]
    runbook_id: Optional[str] # e.g. "RB-001" from alert labels
    team:  Optional[str]

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
    resolution_status: str             # ResolutionStatus enum value
    resolution_notes:Optional[str]
    mttr_seconds:Optional[int]

    #LLM cost tracking
    llm_suggested_action: Optional[str]
    total_tokens_used: int
    token_cost_usd: float
    model_used: Optional[str]

    #Timestamps
    started_at: Optional[str]   
    resolved_at: Optional[str]   

    #Error handling
    errors: List[str]       # non fatal errors encountered


def initial_state(incident_id: str, raw_signals: Dict[str, Any]) -> AgentState:
    """
    factory: creates a clean initial agentstate from an incoming alert.
    call this in the fastAPI endpoint before invoking the graph.
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
        mttr_seconds=None,

        llm_suggested_action=None,
        total_tokens_used=0,
        token_cost_usd=0.0,
        model_used=None,

        started_at=datetime.datetime.utcnow().isoformat(),
        resolved_at=None,
        errors=[],

        diagnosis_summary= None,
        evidence_summary=None,
        blast_analysis=None,

    )