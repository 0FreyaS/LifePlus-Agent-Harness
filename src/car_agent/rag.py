from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any


def _embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    normalized = re.sub(r"\s+", "", text.lower())
    tokens = [normalized[index:index + 2] for index in range(max(1, len(normalized) - 1))]
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class KnowledgeRetriever:
    """Small local RAG index backed by Chroma and deterministic local embeddings."""

    def __init__(self, directory: str | Path, knowledge_dir: str | Path):
        self.directory = str(directory)
        self.knowledge_dir = Path(knowledge_dir)
        self.collection: Any = None
        self.backend = "local-vector"
        self._documents: list[dict[str, Any]] = []

    def open(self) -> None:
        if not self.knowledge_dir.is_dir():
            raise FileNotFoundError("Knowledge directory unavailable; refusing to clear the existing index")
        documents: list[str] = []
        ids: list[str] = []
        metadatas: list[dict[str, str]] = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            sections = [part.strip() for part in re.split(r"\n(?=#)", path.read_text(encoding="utf-8")) if part.strip()]
            for index, section in enumerate(sections):
                ids.append(f"{path.stem}:{index}")
                documents.append(section[:4000])
                metadatas.append({"source": path.name, "section": str(index)})
        self._documents = [
            {"id": item_id, "text": document, "source": metadata["source"],
             "embedding": _embedding(document)}
            for item_id, document, metadata in zip(ids, documents, metadatas)
        ]
        try:
            import chromadb
        except ImportError:
            return

        client = chromadb.PersistentClient(path=self.directory)
        self.collection = client.get_or_create_collection(
            "lifeplus_knowledge", metadata={"hnsw:space": "cosine"}
        )
        if documents:
            self.collection.upsert(ids=ids, documents=documents, metadatas=metadatas,
                                   embeddings=[_embedding(document) for document in documents])
        # Replace this index's configured document set only after new content is stored.
        previous = set(self.collection.get(include=["metadatas"])["ids"])
        removed = sorted(previous - set(ids))
        if removed:
            self.collection.delete(ids=removed)
        self.backend = "chroma"

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.collection:
            query_vector = _embedding(query)
            ranked = sorted(
                self._documents,
                key=lambda item: sum(left * right for left, right in zip(query_vector, item["embedding"])),
                reverse=True,
            )[:limit]
            return [{"text": item["text"], "source": item["source"],
                     "score": round(sum(left * right for left, right in zip(query_vector, item["embedding"])), 4)}
                    for item in ranked]
        result = self.collection.query(query_embeddings=[_embedding(query)], n_results=limit)
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            {"text": text, "source": metadata.get("source", ""), "score": round(1 - float(distance), 4)}
            for text, metadata, distance in zip(documents, metadatas, distances)
        ]
