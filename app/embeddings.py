"""Embeddings and vector search behind one narrow interface.

This module is the pgvector seam. Callers pass texts in and get chunk
ids + scores out; nothing outside this file knows vectors are packed
float32 BLOBs scored brute-force with numpy. The AWS migration
reimplements search() as a pgvector ORDER BY and converts the column
in one Alembic migration — ingestion, Context Builder, and chat flow
are untouched (see docs/ARCHITECTURE.md).
"""
import numpy as np
from openai import OpenAI

from app.config import (OPENAI_API_KEY, EMBEDDING_MODEL,
                        EMBEDDING_DIMENSIONS)
from app.models import Chunk, CapsuleChunk

_client = OpenAI(api_key=OPENAI_API_KEY)

_BATCH = 100  # texts per embeddings API call


def pack(vector) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack(blob) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def embed_texts(texts):
    """→ list of unit-norm float32 vectors, one per text. One API call
    per _BATCH texts. Raises on API failure — callers decide policy."""
    vectors = []
    for i in range(0, len(texts), _BATCH):
        resp = _client.embeddings.create(model=EMBEDDING_MODEL,
                                         dimensions=EMBEDDING_DIMENSIONS,
                                         input=texts[i:i + _BATCH])
        for item in resp.data:
            v = np.asarray(item.embedding, dtype=np.float32)
            vectors.append(v / (np.linalg.norm(v) or 1.0))
    return vectors


def embed_query(text):
    return embed_texts([text])[0]


def capsule_chunk_ids(capsule_ids):
    """The union of chunk ids belonging to the given capsules — the
    allowlist scoped retrieval searches within."""
    if not capsule_ids:
        return []
    rows = (CapsuleChunk.query.with_entities(CapsuleChunk.chunk_id)
            .filter(CapsuleChunk.capsule_id.in_(list(capsule_ids))).all())
    return [r.chunk_id for r in rows]


def search(workspace_id, query_vec, k=8, chunk_ids=None, token_budget=None):
    """Scoped cosine search. Always workspace-scoped (tenant isolation);
    chunk_ids optionally restricts to a candidate set — the capsule
    hook. token_budget stops filling once the summed chunk tokens would
    exceed it: retrieval fills a budget, not a fixed k.
    → [(chunk_id, score)] best-first."""
    q = (Chunk.query.with_entities(Chunk.id, Chunk.embedding,
                                   Chunk.token_count)
         .filter(Chunk.workspace_id == workspace_id,
                 Chunk.embedding.isnot(None)))
    if chunk_ids is not None:
        q = q.filter(Chunk.id.in_(list(chunk_ids)))
    rows = q.all()
    if not rows:
        return []

    matrix = np.vstack([unpack(r.embedding) for r in rows])
    scores = matrix @ np.asarray(query_vec, dtype=np.float32)

    results, spent = [], 0
    for idx in np.argsort(scores)[::-1]:
        if len(results) >= k:
            break
        tokens = rows[idx].token_count or 0
        if token_budget is not None and results and spent + tokens > token_budget:
            continue  # skip too-big chunk, maybe a smaller one still fits
        results.append((rows[idx].id, float(scores[idx])))
        spent += tokens
    return results
