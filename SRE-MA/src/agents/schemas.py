from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

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
