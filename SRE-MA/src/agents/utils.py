import os
import json
import re
from typing import Optional, List
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
try:
    from openai import AuthenticationError as OpenAIAuthError
except ImportError:
    OpenAIAuthError = Exception

load_dotenv()

#cost accounting
INPUT_RATE_PER_1K = 0.001
OUTPUT_RATE_PER_1K = 0.004


#pydantic schemas for structured llm output

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


def _create_llm(model_name: str, temperature: float, api_key: str, base_url: str = None, is_groq: bool = False):
    if is_groq:
        return ChatGroq(model=model_name, temperature=temperature, api_key=api_key, max_retries=3, stream_usage=True)
    return ChatOpenAI(model=model_name, temperature=temperature, max_retries=5, api_key=api_key, base_url=base_url, stream_usage=True)


def _get_llm(temperature: float = 0.1):
    primary_model = os.getenv("LLM_MODEL_OPENROUTER", "nvidia/nemotron-3-ultra-550b-a55b:free")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    openrouter_base = os.getenv("OPENAI_API_BASE")
    groq_key = os.getenv("GROQ_API_KEY")
    cc_key = os.getenv("CODECRAFT_API_KEY")
    base_url = os.getenv("base_url") or openrouter_base
    primary_model_cc = os.getenv("LLM_MODEL_CC", "deepseek-v4-flash-0731")

    models = []
    if cc_key:
        models.append(_create_llm(primary_model_cc, temperature, cc_key, base_url, is_groq=False))
    if openrouter_key:
        models.append(_create_llm(primary_model, temperature, openrouter_key, openrouter_base, is_groq=False))
        if "nvidia" in primary_model:
            models.append(_create_llm("liquid/lfm-2.5-2.6b:free", temperature, openrouter_key, openrouter_base, is_groq=False))
    if groq_key:
        models.append(_create_llm(os.getenv("LLM_MODEL_GROQ", "llama-3.3-70b-versatile"), temperature, groq_key, is_groq=True))
    if not models:
        return _create_llm(primary_model, temperature, openrouter_key, openrouter_base, is_groq=False)

    primary = models[0]
    if len(models) <= 1:
        return primary
    return primary.with_fallbacks(models[1:], exceptions_to_handle=(Exception, OpenAIAuthError))


def calculate_cost(input_tokens: int, output_tokens: int, model_name: str = None) -> float:
    """OpenSRE cost: per-direction pricing, no flat-rate estimation."""
    return (input_tokens * INPUT_RATE_PER_1K + output_tokens * OUTPUT_RATE_PER_1K) / 1000.0