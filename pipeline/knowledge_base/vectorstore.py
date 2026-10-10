"""ChromaDB access for the pipeline: the store client, its collections, writes,
and the RAG query the generators use.

One embedder builds and queries the store (plan step 6.7, finding E8):
Chroma's own ONNX all-MiniLM-L6-v2, the function a collection gets when the
app's find_topic opens it without naming one. The index used to be built with
sentence-transformers (the same model through torch), so stored vectors and
query vectors came from two runtimes, and the pipeline needed torch.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from pipeline.config import get_kb_config

# Written into the build manifest (indexer.write_manifest).
EMBEDDER = "chromadb DefaultEmbeddingFunction (ONNX all-MiniLM-L6-v2)"


@lru_cache(maxsize=1)
def embedding_function():
    """Chroma's default embedder, created on first use: the model is fetched
    once into ~/.cache/chroma, so importing this module stays offline."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


def get_client(db_dir: Path | None = None) -> chromadb.PersistentClient:
    if db_dir is None:
        db_dir = get_kb_config()["db_dir"]
    db_dir = Path(db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(db_dir),
        settings=Settings(anonymized_telemetry=False),
    )


def get_or_create_collection(client: chromadb.PersistentClient, name: str) -> Any:
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
        embedding_function=embedding_function(),
    )


def upsert_documents(collection, documents: list[str], metadatas: list[dict], ids: list[str]) -> None:
    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embedding_function()(documents),
        metadatas=metadatas,
    )


def query(
    collection_name: str,
    query_text: str,
    n_results: int = 5,
    db_dir: Path | None = None,
) -> list[dict]:
    client = get_client(db_dir)
    try:
        collection = client.get_collection(collection_name, embedding_function=embedding_function())
    except Exception:
        return []
    count = collection.count()
    if count == 0:
        return []
    results = collection.query(
        query_embeddings=embedding_function()([query_text]),
        n_results=min(n_results, count),
        include=["documents", "metadatas", "distances"],
    )
    output: list[dict] = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({"text": doc, "metadata": meta, "distance": dist})
    return output
