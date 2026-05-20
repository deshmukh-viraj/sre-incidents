import os
from pathlib import Path
from langchain_text_splitters import MarkdownHeaderTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

runbooks_dir = Path("runbooks")
index_dir = Path("data/faiss_index")


def ingest():
    embeddings = HuggingFaceEmbeddings(model_name='all-MiniLM-L6-v2')
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
        ("####", "Header 4"),
    ]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)

    docs = []
    md_files = sorted(runbooks_dir.glob('*.md'))
    for file in md_files:
        content = file.read_text(encoding='utf-8')
        chunks = splitter.split_text(content)
        for chunk in chunks:
            chunk.metadata['source'] = file.name
        docs.extend(chunks)
    
    if not docs:
        raise ValueError("No runbooks chunks found. check runbooks/ directoru")
    
    db = FAISS.from_documents(docs, embeddings)
    index_dir.mkdir(parents=True, exist_ok=True)
    db.save_local(str(index_dir))
    print(f"Ingested {len(md_files)} runbooks -> {len(docs)} chunks -> saved to {index_dir}/")

if __name__ == "__main__":
    ingest()