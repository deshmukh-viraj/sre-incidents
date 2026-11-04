from pydantic import BaseModel, Field
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Literal

class IncidentSummaryOutput(BaseModel):
    severity: str = Field(description="SEV1 through SEV5")
    summary: str = Field(description="2-sentence plain english incident summary")
    affected_services : List[str] = Field(description="list of affected service names")

class HypothesisOutput(BaseModel):
    hypothesis: str = Field(description="plain english root cause theory")
    evidence: List[str] = Field(description="supporting signal snippets")
    confidence: float = Field(description="0.0 to 1.0")
    alternative: Optional[str]= Field(default=None, description="competing explanation")
    supporting_runbook: Optional[str]= Field(default=None, description="runbook ID")

class DiagnosisOutput(BaseModel):
    hypotheses: List[HypothesisOutput]
    root_cause: Optional[str] = Field(default=None)

class ActionOutput(BaseModel):
    action: str
    tool: str
    params: Dict[str, Any]
    blast_radius: str
    reversible: bool
    requires_approval: bool

class ActionPlanOutput(BaseModel):
    actions: List[ActionOutput]
    root_cause: str
    requires_approval: bool

#retry path, eemediator extracts the propose_remediation call from the scratchpad
class RemediationProposal(BaseModel):
    action: str = Field(description="plain english remediation description")
    tool: str = Field(description="tool name from propose_remediation")
    target_service: str = Field(description="service to act on")
    blast_radius: str = Field(description="pod | service | cluster")


class Hypothesis(BaseModel):
    hypothesis: str = Field(description="Plain English root cause")
    evidence: List[str] = Field(default_factory=list, description="Supporting signals/logs")
    confidence: float = Field(ge=0.0, le=1.0)
    alternative: Optional[str] = None
    supporting_runbook: Optional[str] = None


class DiagnoserOutput(BaseModel):
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    root_cause: Optional[str] = None
    diagnosis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    blast_analysis: Optional[str] = None
    suggested_remediation_from_context: Optional[str] = None


class CommunicatorOutput(BaseModel):
    status_page_update: str
    war_room_summary: str
    escalation_message: Optional[str] = None


# --------------------------------------------------------------------------
# Typed roster contracts (R3). Extended here, never recreated elsewhere.
# --------------------------------------------------------------------------

class IncidentContext(BaseModel):
    """typed incident identity for handoffs / ledger entries."""
    incident_id: str
    alert_name: Optional[str] = None
    severity: Optional[str] = None
    affected_services: List[str] = Field(default_factory=list)
    summary: Optional[str] = None
    started_at: Optional[str] = None

class HypothesisReport(BaseModel):
    """typed single-worker hypothesis emission (B4): superset of HypothesisOutput
    plus the fields downstream routing consumes."""
    hypothesis: str
    evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    alternative: Optional[str] = None
    supporting_runbook: Optional[str] = None
    diagnosis_mode: Optional[str] = None
    source: Optional[str] = None

# R3 alias: ActionPlan IS ActionPlanOutput — one definition, canonical name.
ActionPlan = ActionPlanOutput

@dataclass
class VerificationResult:
    """post-execution verification snapshot (moved verbatim from
    execution_agent.py — the math consumer, _verify_recovery, is unchanged)."""
    verified: bool
    p99_latency_s: Optional[float] = None
    error_rate: Optional[float] = None
    t_clear: Optional[str] = None
    signal: Optional[str] = None
    healthy_run_len: int = 0

class VerificationChallenge(BaseModel):
    """B1 Verifier output: an adversarial check of one diagnosis claim."""
    claim: str = Field(description="the diagnosis claim under test")
    disproof_queries: List[str] = Field(default_factory=list,
                                        description="read-only PromQL queries using ONLY exact metric names from the allowed list given in the prompt (empty list beats an invented one)")
    verdict: Literal["upheld", "contradicted", "unverifiable"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: Optional[str] = None

class HandoffMessage(BaseModel):
    """one entry in the message ledger / the human handoff package (B7)."""
    timestamp: str
    from_worker: str
    to_worker: Optional[str] = None  # None => addressed to the human
    kind: str = Field(description="note | handoff | escalation")
    content: str
    options: List[str] = Field(default_factory=list,
                               description="choices offered to the human when to_worker is None")

class Budget(BaseModel):
    """R10: ONE global budget per run. Extends (not replaces) the existing
    recursion/token counters; reasoning workers decrement it."""
    max_iterations_total: int = 24
    max_tokens_total: int = 400_000
    spent_iterations: int = 0
    spent_tokens: int = 0

    @property
    def iterations_exhausted(self) -> bool:
        return self.spent_iterations >= self.max_iterations_total

    @property
    def tokens_exhausted(self) -> bool:
        return self.spent_tokens >= self.max_tokens_total

    @property
    def exhausted(self) -> bool:
        return self.iterations_exhausted or self.tokens_exhausted

class ExecutionReceipt(BaseModel):
    """B6: typed receipt for one executed (or DRY-RUN) action — the seam where
    the sandbox executor plugs in later."""
    action: str
    tool: str
    params: Dict[str, Any] = Field(default_factory=dict)
    success: bool
    dry_run: bool = False
    executed_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    idem_key: Optional[str] = None
