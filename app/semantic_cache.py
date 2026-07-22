"""Semantic response cache — behind a swappable storage port.

The chat pipeline talks to ONE stable façade (module-level `lookup`,
`store`, `invalidate`, `stats`). Behind it sits a storage *port*
(`SemanticCacheBackend`) with pluggable *adapters*. The first adapter,
`OrmBruteForceBackend`, keeps entries in the existing SQL store
(`SemanticCacheEntry`) and scores them with numpy cosine — perfectly
fine while the cache is small. A later Redis-vector / ANN adapter
implements the same port, is registered in `_BACKENDS`, and selected
via `SEMANTIC_CACHE_BACKEND`; **the chat pipeline never changes.**

What keeps the port genuinely swappable:
  * Callers pass the query embedding IN — it was already computed for
    routing, and the cache never owns an embedding provider.
  * lookup/store exchange plain dataclasses (`CacheHit` / `CacheKey`),
    never ORM rows, so a Redis adapter returns the same shapes.
  * Freshness (TTL + `knowledge_version`) is enforced *inside* the
    adapter, so every adapter honours "never return a stale answer".
  * The façade degrades safely: any backend error is swallowed to a
    miss / no-op, so a cache fault can never 500 a chat.

Config: app.config.SEMANTIC_CACHE_* (enabled, backend, threshold, TTL).
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.config import (SEMANTIC_CACHE_ENABLED, SEMANTIC_CACHE_BACKEND,
                        SEMANTIC_CACHE_THRESHOLD, SEMANTIC_CACHE_TTL_SECONDS)
from app.embeddings import pack, unpack
from app.models import db, SemanticCacheEntry, utcnow

log = logging.getLogger(__name__)


# ── Wire types (what crosses the port) ───────────────────
@dataclass
class CacheKey:
    """Everything needed to persist one entry. `embedding` is a unit-norm
    float32 vector; adapters store it in whatever form they prefer."""
    workspace_id: str
    question: str
    embedding: np.ndarray
    response: str
    knowledge_version: int
    repo_context: dict = field(default_factory=dict)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ttl_seconds: int = SEMANTIC_CACHE_TTL_SECONDS


@dataclass
class CacheHit:
    """A fresh, semantically-matching entry. Plain data — no ORM row."""
    entry_id: str
    question: str
    response: str
    similarity: float
    created_at: datetime
    tokens_in: int
    tokens_out: int


# ── Pure scoring (adapter-independent, unit-testable) ────
def best_match(matrix: np.ndarray, query_vec, threshold: float):
    """→ (row_index, score) of the closest row at/above `threshold`, or
    None. Cosine == dot product because all vectors are unit-norm. Kept
    free of DB/ORM so every adapter (and the self-test) can reuse it."""
    if matrix.size == 0:
        return None
    scores = matrix @ np.asarray(query_vec, dtype=np.float32)
    idx = int(np.argmax(scores))
    best = float(scores[idx])
    return (idx, best) if best >= threshold else None


# ── The port ─────────────────────────────────────────────
class SemanticCacheBackend(ABC):
    """Storage port. An adapter is free to use SQL, Redis, an ANN index —
    as long as it enforces the freshness contract in `lookup`."""

    @abstractmethod
    def lookup(self, workspace_id, embedding, knowledge_version,
               threshold, now) -> "CacheHit | None":
        """Nearest fresh entry (same workspace, same knowledge_version,
        not yet expired) with similarity ≥ threshold, else None."""

    @abstractmethod
    def store(self, key: CacheKey) -> None: ...

    @abstractmethod
    def invalidate_workspace(self, workspace_id) -> int:
        """Drop every entry for a workspace. → count removed."""

    @abstractmethod
    def size(self, workspace_id=None, now=None) -> int:
        """Live (unexpired) entry count, optionally scoped to a workspace."""

    @abstractmethod
    def purge_expired(self, now=None) -> int: ...


# ── Adapter 1: brute-force cosine over the SQL store ─────
class OrmBruteForceBackend(SemanticCacheBackend):
    """Loads a workspace's fresh entries and scores them in-process with
    numpy. O(entries) per lookup — fine at small scale, and the reason the
    port exists: swap this for pgvector/Redis when the cache grows."""

    def lookup(self, workspace_id, embedding, knowledge_version,
               threshold, now):
        rows = (SemanticCacheEntry.query
                .filter(SemanticCacheEntry.workspace_id == workspace_id,
                        SemanticCacheEntry.knowledge_version == knowledge_version,
                        SemanticCacheEntry.expires_at > now)
                .all())
        if not rows:
            return None
        matrix = np.vstack([unpack(r.question_embedding) for r in rows])
        m = best_match(matrix, embedding, threshold)
        if m is None:
            return None
        idx, score = m
        r = rows[idx]
        r.hit_count = (r.hit_count or 0) + 1
        db.session.commit()
        return CacheHit(entry_id=r.id, question=r.question, response=r.response,
                        similarity=score, created_at=r.created_at,
                        tokens_in=r.tokens_in, tokens_out=r.tokens_out)

    def store(self, key: CacheKey):
        db.session.add(SemanticCacheEntry(
            workspace_id=key.workspace_id, question=key.question,
            question_embedding=pack(key.embedding), response=key.response,
            repo_context=key.repo_context or {},
            knowledge_version=key.knowledge_version, model=key.model,
            tokens_in=key.tokens_in, tokens_out=key.tokens_out,
            expires_at=utcnow() + timedelta(seconds=key.ttl_seconds)))
        db.session.commit()

    def invalidate_workspace(self, workspace_id):
        n = (SemanticCacheEntry.query
             .filter_by(workspace_id=workspace_id)
             .delete(synchronize_session=False))
        db.session.commit()
        return n

    def size(self, workspace_id=None, now=None):
        q = SemanticCacheEntry.query
        if workspace_id is not None:
            q = q.filter_by(workspace_id=workspace_id)
        if now is not None:
            q = q.filter(SemanticCacheEntry.expires_at > now)
        return q.count()

    def purge_expired(self, now=None):
        now = now or utcnow()
        n = (SemanticCacheEntry.query
             .filter(SemanticCacheEntry.expires_at <= now)
             .delete(synchronize_session=False))
        db.session.commit()
        return n


# Adapter registry. Add "redis"/"pgvector" here later; select via
# SEMANTIC_CACHE_BACKEND. The façade below is all the pipeline sees.
_BACKENDS = {"orm": OrmBruteForceBackend}
_backend = None


def _get_backend() -> SemanticCacheBackend:
    global _backend
    if _backend is None:
        cls = _BACKENDS.get(SEMANTIC_CACHE_BACKEND, OrmBruteForceBackend)
        _backend = cls()
    return _backend


# ── Façade: the only surface the chat pipeline imports ───
def lookup(workspace_id, embedding, knowledge_version,
           threshold=None) -> "CacheHit | None":
    """Return a fresh, meaning-matching cached answer, or None. Never
    raises — a backend fault degrades to a miss so chat still runs."""
    if not SEMANTIC_CACHE_ENABLED:
        return None
    thr = SEMANTIC_CACHE_THRESHOLD if threshold is None else threshold
    try:
        return _get_backend().lookup(workspace_id, embedding,
                                     knowledge_version, thr, utcnow())
    except Exception:
        log.exception("semantic cache lookup failed — treating as miss")
        db.session.rollback()
        return None


def store(workspace_id, question, embedding, response, knowledge_version,
          repo_context=None, model="", tokens_in=0, tokens_out=0,
          ttl_seconds=None) -> None:
    """Persist a freshly-generated answer. Never raises — a store failure
    just means the next identical question is another miss."""
    if not SEMANTIC_CACHE_ENABLED:
        return
    try:
        _get_backend().store(CacheKey(
            workspace_id=workspace_id, question=question, embedding=embedding,
            response=response, knowledge_version=knowledge_version,
            repo_context=repo_context or {}, model=model,
            tokens_in=tokens_in, tokens_out=tokens_out,
            ttl_seconds=SEMANTIC_CACHE_TTL_SECONDS
            if ttl_seconds is None else ttl_seconds))
    except Exception:
        log.exception("semantic cache store failed — entry not cached")
        db.session.rollback()


def invalidate(workspace_id) -> int:
    """Drop a workspace's entries (called when its knowledge changes).
    The knowledge_version bump already makes them un-matchable; this
    reclaims the rows. Never raises."""
    if not SEMANTIC_CACHE_ENABLED:
        return 0
    try:
        return _get_backend().invalidate_workspace(workspace_id)
    except Exception:
        log.exception("semantic cache invalidate failed")
        db.session.rollback()
        return 0


def stats(workspace_id=None) -> dict:
    """Live-entry count for the AI Diagnostics 'Semantic Cache Size' card.
    Hit/miss *rates* are computed from Message rows in the diagnostics
    layer, not here (the cache doesn't see misses)."""
    try:
        return {"size": _get_backend().size(workspace_id, utcnow()),
                "backend": SEMANTIC_CACHE_BACKEND,
                "enabled": SEMANTIC_CACHE_ENABLED}
    except Exception:
        log.exception("semantic cache stats failed")
        db.session.rollback()
        return {"size": 0, "backend": SEMANTIC_CACHE_BACKEND,
                "enabled": SEMANTIC_CACHE_ENABLED}


# ── Self-test: pure scoring, no DB required (free) ───────
if __name__ == "__main__":
    # Three unit vectors; query is a near-paraphrase of #1.
    def unit(v):
        v = np.asarray(v, dtype=np.float32)
        return v / (np.linalg.norm(v) or 1.0)

    rows = np.vstack([unit([1, 0, 0]), unit([0, 1, 0]), unit([1, 1, 0])])
    q = unit([0.98, 0.02, 0])

    hit = best_match(rows, q, threshold=0.92)
    assert hit is not None and hit[0] == 0, hit
    print(f"paraphrase of row 0 -> matched row {hit[0]} @ {hit[1]:.3f}  (hit)")

    q2 = unit([0, 0, 1])            # orthogonal to everything
    miss = best_match(rows, q2, threshold=0.92)
    assert miss is None, miss
    print("orthogonal query      -> no match                    (miss)")

    empty = best_match(np.empty((0, 3), dtype=np.float32), q, 0.5)
    assert empty is None
    print("empty cache           -> no match                    (miss)")
    print("\nOK — semantic-cache scoring behaves; backend swappable via "
          "SEMANTIC_CACHE_BACKEND.")
