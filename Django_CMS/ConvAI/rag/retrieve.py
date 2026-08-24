"""Retrieval over an agent's knowledge base.

No vector database. The chunks for one agent are read out of Postgres, stacked
into a numpy matrix and scored with a single dot product — vectors are stored
L2-normalised, so that dot product *is* cosine similarity.

This is the right shape for the workload: a knowledge base here is a handful of
documents, i.e. hundreds to a few thousand chunks. Scoring 5,000 × 1536 floats
is a couple of milliseconds and rounds to nothing beside the embedding call
that had to happen first. It also keeps *one* source of truth — switching a
document off is a WHERE clause, not an index rebuild.

If a deployment ever outgrows this, the change is local: keep the schema and
put an ANN index in front of ``_matrix_for``.
"""
from __future__ import annotations

import logging
import struct
import threading

import numpy as np

from ..models import RagChunk, RagDocument

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
# Below this cosine similarity a chunk is noise rather than a weak match.
# Deliberately low: 3-small scores unrelated text around 0.1-0.15, and cutting
# too aggressively is worse than letting the model see a mediocre extract and
# decide for itself.
MIN_SCORE = 0.20

# Guard against one runaway knowledge base making every reply slow. Well above
# any realistic upload here.
MAX_CHUNKS = 50_000


class _Cache:
    """Per-process cache of one agent's stacked vectors.

    Retrieval otherwise re-reads and re-parses every chunk on every single
    turn of every conversation. The version key is cheap to compute and covers
    everything that can change what retrieval should return: chunks added or
    removed, and documents toggled on or off.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[tuple, np.ndarray, list]] = {}

    def get(self, agent_id: int, version: tuple):
        with self._lock:
            cached = self._entries.get(agent_id)
        if cached and cached[0] == version:
            return cached[1], cached[2]
        return None, None

    def put(self, agent_id: int, version: tuple, matrix, rows) -> None:
        with self._lock:
            self._entries[agent_id] = (version, matrix, rows)

    def clear(self, agent_id: int | None = None) -> None:
        with self._lock:
            if agent_id is None:
                self._entries.clear()
            else:
                self._entries.pop(agent_id, None)


_cache = _Cache()


def invalidate(agent_id: int | None = None) -> None:
    """Drop cached vectors — call after upload, delete, or an enable toggle."""
    _cache.clear(agent_id)


def _version(agent_id: int) -> tuple:
    """A cheap fingerprint of what this agent's retrievable set currently is."""
    from django.db.models import Count, Max
    stats = RagDocument.objects.filter(
        agent_id=agent_id, enabled=True, status=RagDocument.Status.READY,
    ).aggregate(n=Count("id"), latest=Max("updated_at"))
    return (stats["n"] or 0, stats["latest"])


def _matrix_for(agent_id: int):
    """Return (matrix, rows) for the agent's enabled, ready chunks.

    ``rows`` is parallel to the matrix: one ``(document_name, text)`` per row.
    """
    version = _version(agent_id)
    matrix, rows = _cache.get(agent_id, version)
    if matrix is not None:
        return matrix, rows

    records = list(
        RagChunk.objects
        .filter(document__agent_id=agent_id, document__enabled=True,
                document__status=RagDocument.Status.READY)
        .order_by("document_id", "ordinal")
        .values_list("embedding", "text", "document__original_name")[:MAX_CHUNKS]
    )
    if not records:
        empty = np.zeros((0, 0), dtype=np.float32)
        _cache.put(agent_id, version, empty, [])
        return empty, []

    vectors, rows = [], []
    width = len(records[0][0]) // struct.calcsize("f")
    for blob, text, name in records:
        # Documents embedded with a different model have a different width;
        # they are not comparable and are skipped rather than silently mixed.
        if len(blob) // struct.calcsize("f") != width:
            continue
        vectors.append(np.frombuffer(bytes(blob), dtype="<f4"))
        rows.append((name, text))

    matrix = np.vstack(vectors) if vectors else np.zeros((0, 0), dtype=np.float32)
    _cache.put(agent_id, version, matrix, rows)
    return matrix, rows


def search(agent_id: int, query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """Return the ``top_k`` best-matching chunks for ``query``.

    Each hit is ``{"document", "text", "score"}``. An empty list means the
    knowledge base has nothing relevant (or nothing at all) — the caller says
    so in words rather than inventing an answer.
    """
    query = (query or "").strip()
    if not query:
        return []

    matrix, rows = _matrix_for(agent_id)
    if not len(rows):
        return []

    from ..llm_factory import make_embeddings
    vector = np.asarray(make_embeddings().embed_query(query), dtype=np.float32)
    if vector.shape[0] != matrix.shape[1]:
        # The platform embedding model was changed after these documents were
        # ingested. Say so plainly instead of returning nonsense.
        raise RuntimeError(
            "The knowledge base was built with a different embedding model. "
            "Re-upload the documents, or restore the previous model under "
            "Settings → Agents."
        )
    norm = float(np.linalg.norm(vector))
    if norm:
        vector = vector / norm

    scores = matrix @ vector  # stored vectors are normalised → cosine similarity
    top_k = max(1, int(top_k or DEFAULT_TOP_K))
    best = np.argsort(-scores)[:top_k]
    return [
        {"document": rows[i][0], "text": rows[i][1], "score": float(scores[i])}
        for i in best if scores[i] >= MIN_SCORE
    ]


def format_hits(hits: list[dict]) -> str:
    """Render hits as the text the agent's search tool hands back to the model.

    Sources are named on every extract so the model can attribute what it says,
    and so a wrong answer is traceable to the document that caused it.
    """
    if not hits:
        return ("No relevant passage was found in the knowledge base. Tell the "
                "user you don't have that information rather than guessing.")
    parts = [
        f"[{i}] From \"{hit['document']}\" (relevance {hit['score']:.2f}):\n{hit['text']}"
        for i, hit in enumerate(hits, start=1)
    ]
    return "\n\n---\n\n".join(parts)


def knowledge_base_summary(agent_id: int) -> dict:
    """Counts for the agent's knowledge base, used by the UI and the prompt."""
    documents = RagDocument.objects.filter(agent_id=agent_id)
    ready = documents.filter(status=RagDocument.Status.READY)
    return {
        "total": documents.count(),
        "ready": ready.count(),
        "active": ready.filter(enabled=True).count(),
        "names": list(ready.filter(enabled=True)
                      .values_list("original_name", flat=True)[:50]),
    }
