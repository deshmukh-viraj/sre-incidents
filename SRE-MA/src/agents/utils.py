import os
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

load_dotenv()

def _get_llm(temperature: float=0.1) -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL_OPENROUTER"),
        temperature=temperature,
        max_tokens=1000,
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE"),
    )

#print(_get_llm().invoke("hello").content)