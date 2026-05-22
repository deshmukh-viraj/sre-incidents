"""
reads all .md runbook files, splits them by H2 heading,
embeds each chunk with sentence-transformers, and saves the FAISS index.

Run once at setup:
    python -m rag.ingest_runbooks

Re-run whenever runbooks are updated.
"""

import os
import glob
from pathlib import Path

from langchain_text_splitters import MarkdownHeaderTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

RUNBOOKS_DIR = Path(__file__).parent.parent.parent / "runbooks"
FAISS_INDEX = Path(__file__).parent.parent / "data" / "faiss_index"
EMBED_MODEL= "all-MiniLM-L6-v2"


def load_runbooks()-> list[Document]:
    """
    read every .md file from runbooks/ and split by H2 headers.
    each H2 section becomes one document chunk.
    metadata carries the source filename and runbook ID.
    """
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#",  "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4")
        ]
    )

    docs = []
    md_files = sorted(glob.glob(str(RUNBOOKS_DIR / "*.md")))

    if not md_files:
        raise FileNotFoundError(
            f"No .md files found in {RUNBOOKS_DIR}. "
            "Place your runbook files there first."
        )

    for filepath in md_files:
        filename = Path(filepath).name
        runbook_id = filename.split("_")[0] 

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        chunks = splitter.split_text(content)

        for chunk in chunks:
            # Build a rich Document with metadata for filtering
            doc = Document(
                page_content=chunk.page_content if hasattr(chunk, "page_content") else str(chunk),
                metadata={
                    "source": filename,
                    "runbook_id": runbook_id,
                    "section": chunk.metadata.get("h2") if hasattr(chunk, "metadata") else "",
                }
            )
            docs.append(doc)

        print(f" {filename} -> {len(chunks)} chunks")

    return docs


def build_faiss_index(docs: list[Document]) -> FAISS:
    """embed all chunks and build the FAISS index."""
    print(f"\n[ingest] Loading embedding model: {EMBED_MODEL}")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    print(f"[ingest] Embedding {len(docs)} chunks...")
    index = FAISS.from_documents(docs, embeddings)
    return index


def save_index(index: FAISS) -> None:
    FAISS_INDEX.mkdir(parents=True, exist_ok=True)
    index.save_local(str(FAISS_INDEX))
    print(f"[ingest] Index saved to {FAISS_INDEX}")


def main():
    print("=" * 60)
    print("FAISS Runbook Ingestion")
    print("=" * 60)

    print(f"\n[ingest] Loading runbooks from {RUNBOOKS_DIR}")
    docs = load_runbooks()
    print(f"\n[ingest] Total chunks: {len(docs)}")

    index = build_faiss_index(docs)
    save_index(index)

    print(f"\n[ingest] Ingested {len(docs)} chunks from {RUNBOOKS_DIR}")
    print("[ingest] Ready for retrieval.\n")


if __name__ == "__main__":
    main()