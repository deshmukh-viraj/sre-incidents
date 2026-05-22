"""
loads the FAISS index from disk and exposes a single retrieve() function.
called by the diagnoser_agent to get relevant runbook context before LLM call.

usage:
    from rag.retriever import retrieve
    chunks = retrieve("card rails throttling payment gateway latency", k=3)
"""

from pathlib import Path
from typing import List, Optional
from functools import lru_cache

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document

FAISS_INDEX = Path(__file__).parent.parent / "data" / "faiss_index"
EMBED_MODEL = "all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def _load_index() -> FAISS:
    """
    load the FAISS index from disk cached after first load.
    """
    if not FAISS_INDEX.exists():
        raise FileNotFoundError(
            f"FAISS index not found at {FAISS_INDEX}. "
            "Run: python -m rag.ingest_runbooks"
        )

    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    index = FAISS.load_local(
        str(FAISS_INDEX),
        embeddings,
        allow_dangerous_deserialization=True,
    )
    print(f"[retriever] FAISS index loaded from {FAISS_INDEX}")
    return index


def retrieve(query: str, k: int = 3, runbook_id: Optional[str] = None) -> List[Document]:
    """
    retrieve the top-k most relevant runbook chunks for a query.

    Args:
        query: Natural language description of the incident or question
        k: Number of chunks to return
        runbook_id: Optional filter only return chunks from this runbook
                    e.g. "RB-001" to get payment latency spike context

    Returns:
        List of Document objects with .page_content and .metadata
    """
    index = _load_index()

    if runbook_id:
        # Filter by runbook_id metadata
        candidates = index.similarity_search(query, k=k * 4)
        results = [
            doc for doc in candidates
            if doc.metadata.get("runbook_id") == runbook_id
        ][:k]
    else:
        results = index.similarity_search(query, k=k)

    return results


def retrieve_for_incident(
    alert_name: str,
    runbook_id: Optional[str],
    incident_summary: Optional[str],
    k: int = 4,
) -> List[Document]:
    """
    convenience wrapper for agent use.
    builds an optimised query from the incident context.
    if runbook_id is known (from alert labels), prioritise that runbook.
    """
    # Build a rich query from available context
    parts = []
    if incident_summary:
        parts.append(incident_summary)
    if alert_name:
        parts.append(alert_name.replace("_", " ").lower())

    query = " ".join(parts) if parts else "sre incident diagnosis remediation"

    return retrieve(query, k=k, runbook_id=runbook_id)


def format_chunks_for_llm(docs: List[Document]) -> str:
    """
    format retrieved chunks into a single context string for LLM injection.
    keeps it concise includes source and section for attribution.
    """
    if not docs:
        return "No relevant runbook context found."

    sections = []
    for doc in docs:
        source  = doc.metadata.get("source", "unknown")
        section = doc.metadata.get("section", "")
        header  = f"[{source}" + (f" — {section}" if section else "") + "]"
        sections.append(f"{header}\n{doc.page_content.strip()}")

    return "\n\n---\n\n".join(sections)