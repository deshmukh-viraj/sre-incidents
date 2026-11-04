import os
import json
import re
import warnings
from typing import Optional, List
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from pydantic import BaseModel, Field
from openai import AuthenticationError as OpenAIAuthError

load_dotenv()


INPUT_RATE_PER_1K = 0.0005
OUTPUT_RATE_PER_1K = 0.0015


def calculate_cost(input_tokens: int, output_tokens: int, model_name: str = None) -> float:
    """Flat-rate cost estimate. model_name accepted for back-compat, ignored."""
    return (input_tokens * INPUT_RATE_PER_1K + output_tokens * OUTPUT_RATE_PER_1K) / 1000.0


def _extract_usage(msg) -> tuple[int, int]:
    """xtract (input_tokens, output_tokens) from a message's usage_metadata.
    handles double-counting: if input_token_details exists, use its input_tokens
    as fallback ONLY when flat input_tokens is absent/zero. Never add both.
    """
    usage = getattr(msg, "usage_metadata", None) or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # this avoids double-counting
    if input_tokens == 0:
        details = usage.get("input_token_details", {}) or {}
        input_tokens = details.get("input_tokens", 0)
    if output_tokens == 0:
        details = usage.get("output_token_details", {}) or {}
        output_tokens = details.get("output_tokens", 0)

    return input_tokens, output_tokens


def _accumulate_usage(messages: list) -> tuple[int, int]:
    """sum input/output tokens across all messages, avoiding doublecounting."""
    total_input = 0
    total_output = 0
    for m in messages:
        inp, out = _extract_usage(m)
        total_input += inp
        total_output += out
    return total_input, total_output


def _create_llm(model_name: str, temperature: float, api_key: str, base_url: str = None, is_groq: bool = False, is_nvidia: bool = False, simple: bool = False):
    timeout = 30 if simple else 120
    if is_groq:
        return ChatGroq(model=model_name, temperature=temperature, api_key=api_key, max_retries=0, streaming=True, timeout=timeout)
    elif is_nvidia:
        return ChatNVIDIA(model=model_name, temperature=temperature, api_key=api_key, base_url=base_url)

    return ChatOpenAI(model=model_name, temperature=temperature, max_retries=0, api_key=api_key, base_url=base_url, stream_usage=True, streaming=True, timeout=timeout, model_kwargs={"thinking": "disabled" if simple else "enabled"})


def _get_llm(temperature: float = 0.1, simple: bool = False):
    primary_model = os.getenv("LLM_MODEL_OPENROUTER", "nvidia/nemotron-3-ultra-550b-a55b:free")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    openrouter_base = os.getenv("OPENAI_API_BASE")
    groq_key = os.getenv("GROQ_API_KEY")
    cc_key = os.getenv("CODECRAFT_API_KEY")
    base_url = os.getenv("base_url") or openrouter_base
    primary_model_cc = os.getenv("LLM_MODEL_CC", "deepseek-v4-flash-0731")

    models = []
    if cc_key:
        models.append(_create_llm(primary_model_cc, temperature, cc_key, base_url, simple=simple))
    if openrouter_key:
        models.append(_create_llm(primary_model, temperature, openrouter_key, openrouter_base, is_groq=False, simple=simple))
    if groq_key:
        models.append(_create_llm(os.getenv("MODEL_GROQ"), temperature, groq_key, is_groq=True, simple=simple))
    if not models:
        return _create_llm(primary_model, temperature, openrouter_key, openrouter_base, is_groq=False, simple=simple)

    primary = models[0]
    return primary

# ponytail: removed the P9 invoke_structured_chain fallback wrapper chain (2026-10-29) —
# every caller now uses langchain's llm.with_structured_output() directly.
# Ceiling: no cross-provider retry when the primary providers fail to produce
# structured output; each agent's own except block (fail-open paths in verifier/
# diagnoser/communicator) is the last resort. Upgrade path: a simple retry loop
# around with_structured_output at the call sites that need it.
