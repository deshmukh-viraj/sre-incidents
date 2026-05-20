from pathlib import Path
from typing import List
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

index_dir = Path('data/faiss_index')

_db = None

def _load_db():
    global _db
    if _db is None:
        if not index_dir.exists():
            raise FileNotFoundError(f"FAISS index not found at {index_dir}.")
        
        embeddings = HuggingFaceEmbeddings(model_name='all-MiniLM-L6-v2')
        _db = FAISS.load_local(str(index_dir), embeddings, allow_dangerous_deserialization=True)
    return _db


def retrieve(query: str, k: int=3) -> List[Document]:
    db = _load_db()
    return db.similarity_search(query, k=k)
