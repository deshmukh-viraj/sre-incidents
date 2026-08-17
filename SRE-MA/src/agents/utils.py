import os
import json
import re
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
try:
    from openai import AuthenticationError as OpenAIAuthError
except ImportError:
    OpenAIAuthError = Exception

load_dotenv()

def _get_llm(temperature: float = 0.1):
    # fallback llms to prevent 502/ResourceExhausted from a single free endpoint
    primary_model = os.getenv("LLM_MODEL_OPENROUTER", "nvidia/nemotron-3-ultra-550b-a55b:free")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    openrouter_base = os.getenv("OPENAI_API_BASE")
    groq_key = os.getenv("GROQ_API_KEY")

    models = []
    if openrouter_key:
        models.append(ChatOpenAI(
            model=primary_model,
            temperature=temperature,
            max_retries=5,
            api_key=openrouter_key,
            base_url=openrouter_base,
        ))
        if "nvidia" in primary_model:
            models.append(ChatOpenAI(
                model="meta-llama/llama-3.3-70b-instruct:free",
                temperature=temperature,
                api_key=openrouter_key,
                base_url=openrouter_base,
            ))

    if groq_key:
        models.append(ChatGroq(
            model=os.getenv("LLM_MODEL_GROQ", "llama3-70b-8192"),
            temperature=temperature,
            api_key=groq_key,
        ))

    if not models:
        return ChatOpenAI(
            model=primary_model,
            temperature=temperature,
            api_key=openrouter_key,
            base_url=openrouter_base,
        )

    primary = models[0]
    if len(models) <= 1:
        return primary

    return primary.with_fallbacks(
        models[1:],
        exceptions_to_handle=(Exception, OpenAIAuthError),
    )

#print(_get_llm().invoke('what is your name??').content)

def parse_json_from_llm(raw_text: str) -> dict:
    """
    robust extraction and repair for LLM JSON responses (handles markdown, 
    unescaped quotes, trailing commas, missing commas, and truncated JSON).
    """
    if not raw_text or not raw_text.strip():
        return {}

    text = raw_text.strip()
    
    # Trim leading text before first { or [
    first_brace = text.find('{')
    first_bracket = text.find('[')
    indices = [i for i in (first_brace, first_bracket) if i != -1]
    if indices:
        text = text[min(indices):]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r',\s*([\}\]])', r'\1', cleaned)
    cleaned = re.sub(r'("\s*|\b(?:true|false|null|\d+(?:\.\d+)?)\s*)\n?(\s*")', r'\1,\2', cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    stack = []
    in_string = False
    escape = False
    repaired_chars = []

    for char in cleaned:
        if escape:
            escape = False
            repaired_chars.append(char)
            continue
        if char == '\\' and in_string:
            escape = True
            repaired_chars.append(char)
            continue
        if char == '"':
            in_string = not in_string
            repaired_chars.append(char)
            continue
        if in_string:
            repaired_chars.append(char)
            continue
        if char in '{[':
            stack.append('}' if char == '{' else ']')
            repaired_chars.append(char)
        elif char in '}]':
            if stack and stack[-1] == char:
                stack.pop()
            repaired_chars.append(char)
        else:
            repaired_chars.append(char)

    if in_string:
        repaired_chars.append('"')

    repaired_str = "".join(repaired_chars).strip()
    repaired_str = re.sub(r',\s*$', '', repaired_str)

    while stack:
        repaired_str += stack.pop()

    try:
        return json.loads(repaired_str)
    except json.JSONDecodeError:
        return {}