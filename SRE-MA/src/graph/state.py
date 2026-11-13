"""
One state object shared by every node in the graph. Each agent reads
what it needs and returns the keys it wants to update; langgraph merges
those writes (lists append, scalars replace). If two agents need to
share anything, it travels through this state.
"""

import os
from typing import TypedDict, List, Optional, Dict, Any, Annotated
from enum import Enum


def _list_add(cur: Optional[List], new: Optional[List]) -> List:
    """additive reducer: append instead of replace (message ledger merge)."""
    return list(cur or []) + list(new or [])


def _add_int(cur: Optional[int], new: Optional[int]) -> int:
    return int(cur or 0) + int(new or 0)


def _merge_budget(cur: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """budget merge: spent_* accumulate (monotone), max_* take the latest config."""
    cur = dict(cur or {})
    new = dict(new or {})
    merged = {
        "max_iterations_total": new.get("max_iterations_total", cur.get("max_iterations_total", 24)),
        "max_tokens_total": new.get("max_tokens_total", cur.get("max_tokens_total", 400_000)),
        "spent_iterations": int(cur.get("spent_iterations", 0) or 0) + int(new.get("spent_iterations", 0) or 0),
        "spent_tokens": int(cur.get("spent_tokens", 0) or 0) + int(new.get("spent_tokens", 0) or 0),
    }
    return merged


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
    confidence:float         # 0.0 to 1.0
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
    correlated_alerts: List[Dict[str, Any]]

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

    #attribution (the five checks)
    agent_identity: str
    claim_id: str
    t_claim: Optional[str]
    execution_evidence: List[Dict[str, Any]]
    t_clear: Optional[str]
    verification_evidence: Optional[Dict[str, Any]]
    resolution_cause: Optional[str]
    recurrence_of: Optional[str]

    t_clear_true: Optional[float]
    t_clear_polled: Optional[str]
    clear_source: Optional[str]
    healthy_run_len: Optional[int]
    samples_seen: Optional[int]
    attribution_lead_seconds: Optional[float]
    gate3_reason: Optional[str]
    gate1_claim: Optional[bool]
    gate2_target_bound: Optional[bool]
    gate3_temporal: Optional[bool]
    gate4_verified: Optional[bool]
    gate5_stable: Optional[bool]
    shadow_execution: bool

    #deep agent harness fields
    investigation_scratchpad: List[str]
    investigation_steps: int
    investigation_complete: bool
    proposed_tool: Optional[str]
    proposed_target: Optional[str]
    proposed_flag: Optional[str]      # set_feature_flag proposals: flag key named by the agent
    proposed_variant: Optional[str]   # and the variant to restore (usually 'off')
    proposed_blast_radius: Optional[str]
    critique: Optional[str]
    diagnosis_issues: List[str]
    policy_rejections: List[str]
    executed_action_keys: List[str]
    plan_viable: bool
    requires_retry: bool        # critic sets True when it rejects the proposal
    critic_reject_count: int    # counts critic rejections to bound retries
    escalate_reason: Optional[str]   # agent_escalated | critic_rejected_after_retry | None
    repair_attempts: Optional[int]   # one-call repair hops used before a full re-investigation
    early_paged: bool                # SEV1 paged humans mid-ladder; escalate dedupes

    # multi-agent coordination fields
    # additive message ledger (merges, never overwrites; the structured layer over
    # the raw investigation_scratchpad transcript, which keeps its replace semantics)
    message_ledger: Annotated[List[Dict[str, Any]], _list_add]
    ownership: Annotated[List[str], _list_add]        # ownership transfer trail
    handoff_count: Annotated[int, _add_int]           # handoffs-to-human so far
    budget: Annotated[Dict[str, Any], _merge_budget]  # one global budget
    budget_exhausted: bool
    receipts: Annotated[List[Dict[str, Any]], _list_add]        # execution receipts
    tool_invocations: Annotated[List[Dict[str, Any]], _list_add]  # audit of tool calls
    verifier_result: Optional[Dict[str, Any]]         # verification challenge dict
    verifier_reroutes: Annotated[int, _add_int]       # cap-1 return-to-investigation counter
    reattacks: Annotated[int, _add_int]               # failed-execution counter, budget 1
    reattack_hint: Optional[str]                      # post-action evidence hint, consumed by deep_investigation
    checker_result: Optional[Dict[str, Any]]          # extra recovery signal
    handoff_to_human: Optional[Dict[str, Any]]        # handoff package {reason, options, ...}
    gate_version: Optional[str]                       # attribution gate version id
    prompt_versions: Dict[str, str]                   # worker -> prompt version
    approval_notified: bool                           # dedupes the human_gate slack page on resume



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
        correlated_alerts=[],

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
        recurrence_of=None,

        t_clear_true=None,
        t_clear_polled=None,
        clear_source=None,
        attribution_lead_seconds=None,
        gate3_reason=None,
        gate1_claim=None,
        gate2_target_bound=None,
        gate3_temporal=None,
        gate4_verified=None,
        gate5_stable=None,
        shadow_execution=bool(raw_signals.get("shadow_execution", False)),
        healthy_run_len=None,
        samples_seen=None,

        #deep agent harness fields
        investigation_scratchpad=[],
        investigation_steps=0,
        investigation_complete=False,
        proposed_tool=None,
        proposed_target=None,
        proposed_flag=None,
        proposed_variant=None,
        proposed_blast_radius=None,
        critique=None,
        diagnosis_issues=[],
        policy_rejections=[],
        executed_action_keys=[],
        requires_retry=False,
        critic_reject_count=0,
        plan_viable=True,
        escalate_reason=None,
        repair_attempts=0,
        early_paged=False,

        #multi-agent coordination fields
        message_ledger=[],
        ownership=[incident_id],
        handoff_count=0,
        budget={
            "max_iterations_total": int(os.getenv("MAX_ITERATIONS_TOTAL", "24")),
            "max_tokens_total": int(os.getenv("MAX_TOKENS_TOTAL", "400000")),
            "spent_iterations": 0,
            "spent_tokens": 0,
        },
        budget_exhausted=False,
        receipts=[],
        tool_invocations=[],
        verifier_result=None,
        verifier_reroutes=0,
        reattacks=0,
        reattack_hint=None,
        checker_result=None,
        handoff_to_human=None,
        gate_version=os.getenv("GATE_VERSION", "gates-v1"),
        prompt_versions={},
        approval_notified=False,
    )