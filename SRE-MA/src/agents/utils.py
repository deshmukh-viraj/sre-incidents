import os
from langchain_groq import ChatGroq

def _get_llm(temperature: float=0.1) -> ChatGroq:
    return ChatGroq(
        model=os.getenv("LLM_MODEL"),
        temperature=temperature,
        max_tokens=1000,
        api_key=os.getenv("GROQ_API_KEY"),
    )
