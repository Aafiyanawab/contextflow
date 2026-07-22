"""Knowledge Capsules: reusable knowledge domains, built automatically.

A capsule is NOT a cluster label. Clustering only gathers the raw
material; an LLM synthesis pass turns it into a knowledge domain:
a structured summary (facts and how concepts relate, with source
attributions), the keywords a person would use to ask about it (the
router's vocabulary), the named concepts it covers, and links to its
supporting chunks. Relationships between capsules are lightweight
`related` links from centroid similarity — deliberately not a graph.

refresh_workspace() is the single entry point, called at the end of
every ingestion (upload batch or GitHub sync). It is incremental:
new chunks attach to the nearest capsule (or seed new ones past a
distance threshold), only touched capsules re-synthesize, orphaned
membership rows are pruned, and empty capsules dissolve. Below
CAPSULE_FLOOR chunks the workspace keeps one "General" capsule and
skips clustering entirely.
"""
import json
import re

import numpy as np
from openai import OpenAI

from app.config import (OPENAI_API_KEY, OPENAI_MODEL, CAPSULE_FLOOR,
                        CAPSULE_MIN_CLUSTER, CAPSULE_MAX_K,
                        CAPSULE_ASSIGN_THRESHOLD, CAPSULE_SYNTHESIS_CHUNKS)
from app.embeddings import pack, unpack
from app.ingest.chunking import count_tokens
from app.models import db, Capsule, CapsuleChunk, Chunk, Workspace, utcnow
from app import semantic_cache

_client = OpenAI(api_key=OPENAI_API_KEY)

SYNTHESIS_PROMPT = """\
You are organizing a user's knowledge base. Below are excerpts that \
belong to one topic, drawn from the user's own documents.

Produce a JSON object with exactly these fields:
- "title": a short domain name for this topic (max 60 chars)
- "keywords": 8-15 lowercase search terms a person would actually type \
when asking about this topic
- "concepts": 3-10 named ideas/techniques/entities this topic covers
- "summary": 150-400 tokens of structured markdown stating what these \
sources actually SAY — key facts, definitions, and how the concepts \
relate to each other. Attribute facts to their source file where it \
helps (e.g. "per networking-notes.pdf"). Write it to be injected into \
a prompt as domain context, not as a table of contents.

Excerpts:
{excerpts}"""


def refresh_workspace(workspace_id, progress=None):
    """Bring the workspace's capsules in line with its chunks.
    → {"total", "synthesized"} stats. LLM failures leave capsules
    marked stale (retried on the next ingestion) — never raises."""
    def report(detail):
        if progress:
            progress(detail)

    chunks = (Chunk.query
              .filter(Chunk.workspace_id == workspace_id,
                      Chunk.embedding.isnot(None)).all())
    capsules = Capsule.query.filter_by(workspace_id=workspace_id).all()

    # Prune membership rows whose chunks were deleted with their source.
    valid_ids = {c.id for c in chunks}
    touched = set()
    for cap in capsules:
        for link in list(cap.memberships):
            if link.chunk_id not in valid_ids:
                db.session.delete(link)
                touched.add(cap.id)

    if not chunks:
        for cap in capsules:
            db.session.delete(cap)
        db.session.commit()
        return {"total": 0, "synthesized": 0}

    by_id = {c.id: c for c in chunks}
    linked = {link.chunk_id for cap in capsules for link in cap.memberships
              if link.chunk_id in valid_ids}
    unassigned = [c for c in chunks if c.id not in linked]

    # One-or-zero capsules over a floor-sized corpus is under-clustered —
    # whether it's the General floor capsule or a single small-source
    # capsule that a big upload just joined (mixed workspaces). Either
    # way, graduate to real domain clustering.
    under_clustered = (len(capsules) <= 1 and len(chunks) >= CAPSULE_FLOOR)
    if len(chunks) < CAPSULE_FLOOR:
        touched |= _ensure_general_capsule(workspace_id, chunks, capsules)
    elif not any(cap.centroid for cap in capsules) or under_clustered:
        # First build — or the corpus just outgrew a too-small capsule
        # set and graduates to real domain clustering.
        touched |= _full_build(workspace_id, chunks, capsules)
    elif unassigned:
        touched |= _assign_incremental(workspace_id, unassigned, capsules)

    db.session.flush()
    capsules = Capsule.query.filter_by(workspace_id=workspace_id).all()

    # Dissolve emptied capsules; recompute centroids of touched ones.
    for cap in list(capsules):
        member_ids = [l.chunk_id for l in cap.memberships]
        if not member_ids:
            db.session.delete(cap)
            capsules.remove(cap)
            continue
        if cap.id in touched or cap.status != "fresh":
            vecs = np.vstack([unpack(by_id[i].embedding) for i in member_ids])
            centroid = vecs.mean(axis=0)
            centroid /= (np.linalg.norm(centroid) or 1.0)
            cap.centroid = pack(centroid)
            cap.status = "stale"

    stale = [c for c in capsules if c.status != "fresh"]
    synthesized = 0
    for i, cap in enumerate(stale, start=1):
        report(f"synthesizing {i}/{len(stale)}")
        if _synthesize(cap, by_id):
            synthesized += 1

    _link_related(capsules)
    db.session.commit()

    # Knowledge just changed → bump the workspace's knowledge_version and
    # drop its semantic-cache entries. The bump alone makes prior entries
    # un-matchable (lookup filters on the version); invalidate() reclaims
    # the rows. Cache faults are swallowed and never affect capsules.
    ws = db.session.get(Workspace, workspace_id)
    if ws is not None:
        ws.knowledge_version = (ws.knowledge_version or 1) + 1
        db.session.commit()
        semantic_cache.invalidate(workspace_id)
    return {"total": len(capsules), "synthesized": synthesized}


# ── Membership strategies ────────────────────────────────

def _ensure_general_capsule(workspace_id, chunks, capsules):
    """Small corpus: exactly one capsule holding every chunk."""
    general = next((c for c in capsules if c.slug == "general"), None)
    for cap in capsules:
        if cap is not general:
            db.session.delete(cap)
    if general is None:
        general = Capsule(workspace_id=workspace_id, slug="general",
                          title="General", status="stale")
        db.session.add(general)
        db.session.flush()
    have = {l.chunk_id for l in general.memberships}
    changed = False
    for chunk in chunks:
        if chunk.id not in have:
            db.session.add(CapsuleChunk(capsule_id=general.id,
                                        chunk_id=chunk.id))
            changed = True
    return {general.id} if (changed or general.status != "fresh") else set()


def _full_build(workspace_id, chunks, capsules):
    """First clustering pass over the whole corpus."""
    for cap in capsules:
        db.session.delete(cap)
    db.session.flush()
    X = np.vstack([unpack(c.embedding) for c in chunks])
    k = min(CAPSULE_MAX_K, max(3, round(len(chunks) ** 0.5)))
    assign = _kmeans(X, k)
    assign = _merge_small_clusters(X, assign)

    touched = set()
    for label in sorted(set(assign)):
        cap = Capsule(workspace_id=workspace_id,
                      slug=_unique_slug(workspace_id, f"topic-{label + 1}"),
                      title=f"Topic {label + 1}", status="stale")
        db.session.add(cap)
        db.session.flush()
        for idx in np.flatnonzero(assign == label):
            db.session.add(CapsuleChunk(capsule_id=cap.id,
                                        chunk_id=chunks[idx].id))
        touched.add(cap.id)
    return touched


def _assign_incremental(workspace_id, unassigned, capsules):
    """New chunks: nearest capsule if close enough, else outliers that
    may seed new capsules (or force-attach if too few to stand alone)."""
    caps = [c for c in capsules if c.centroid]
    centroids = np.vstack([unpack(c.centroid) for c in caps])
    touched, outliers = set(), []
    for chunk in unassigned:
        sims = centroids @ unpack(chunk.embedding)
        best = int(np.argmax(sims))
        if sims[best] >= CAPSULE_ASSIGN_THRESHOLD:
            db.session.add(CapsuleChunk(capsule_id=caps[best].id,
                                        chunk_id=chunk.id))
            touched.add(caps[best].id)
        else:
            outliers.append(chunk)

    if outliers and len(outliers) >= CAPSULE_MIN_CLUSTER:
        X = np.vstack([unpack(c.embedding) for c in outliers])
        k = max(1, round(len(outliers) ** 0.5 / 2))
        assign = _kmeans(X, k)
        for label in sorted(set(assign)):
            cap = Capsule(workspace_id=workspace_id,
                          slug=_unique_slug(workspace_id, "new-topic"),
                          title="New topic", status="stale")
            db.session.add(cap)
            db.session.flush()
            for idx in np.flatnonzero(assign == label):
                db.session.add(CapsuleChunk(capsule_id=cap.id,
                                            chunk_id=outliers[idx].id))
            touched.add(cap.id)
    elif outliers:
        for chunk in outliers:  # too few for a domain — attach to nearest
            sims = centroids @ unpack(chunk.embedding)
            best = int(np.argmax(sims))
            db.session.add(CapsuleChunk(capsule_id=caps[best].id,
                                        chunk_id=chunk.id))
            touched.add(caps[best].id)
    return touched


# ── Clustering primitives (numpy only — no sklearn) ─────

def _kmeans(X, k, iters=25, seed=7):
    rng = np.random.default_rng(seed)
    centroids = X[rng.choice(len(X), size=min(k, len(X)), replace=False)]
    assign = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        assign = (X @ centroids.T).argmax(axis=1)  # unit vectors: dot = cosine
        new = []
        for j in range(len(centroids)):
            members = X[assign == j]
            v = members.mean(axis=0) if len(members) else centroids[j]
            new.append(v / (np.linalg.norm(v) or 1.0))
        new = np.vstack(new)
        if np.allclose(new, centroids, atol=1e-5):
            break
        centroids = new
    return assign


def _merge_small_clusters(X, assign):
    """Clusters below CAPSULE_MIN_CLUSTER dissolve into their nearest
    big neighbor, then labels are compacted to 0..n."""
    labels, counts = np.unique(assign, return_counts=True)
    big = [l for l, n in zip(labels, counts) if n >= CAPSULE_MIN_CLUSTER]
    if not big:
        return np.zeros(len(assign), dtype=int)
    big_centroids = np.vstack([X[assign == l].mean(axis=0) for l in big])
    for l, n in zip(labels, counts):
        if n < CAPSULE_MIN_CLUSTER:
            for idx in np.flatnonzero(assign == l):
                assign[idx] = big[int(np.argmax(big_centroids @ X[idx]))]
    remap = {l: i for i, l in enumerate(sorted(set(assign)))}
    return np.array([remap[l] for l in assign])


# ── Synthesis ────────────────────────────────────────────

def _synthesize(cap, chunks_by_id):
    """One LLM call: cluster material → knowledge domain. Returns True
    on success; on failure the capsule stays stale for the next run."""
    member_ids = [l.chunk_id for l in cap.memberships]
    members = [chunks_by_id[i] for i in member_ids if i in chunks_by_id]
    centroid = unpack(cap.centroid)
    members.sort(key=lambda c: -float(unpack(c.embedding) @ centroid))
    excerpts = "\n\n---\n\n".join(
        f"[{c.document.filename}] {c.text[:800]}"
        for c in members[:CAPSULE_SYNTHESIS_CHUNKS])
    try:
        resp = _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user",
                       "content": SYNTHESIS_PROMPT.format(excerpts=excerpts)}],
            response_format={"type": "json_object"},
            max_tokens=700, temperature=0.3)
        data = json.loads(resp.choices[0].message.content)
        title = str(data.get("title") or cap.title)[:160]
        cap.title = title
        cap.keywords = [str(k).lower()[:60] for k in data.get("keywords", [])][:15]
        cap.concepts = [str(c)[:80] for c in data.get("concepts", [])][:10]
        cap.summary = str(data.get("summary") or "")[:4000]
        cap.token_count = count_tokens(cap.summary)
        if cap.slug.startswith(("topic-", "new-topic")):
            cap.slug = _unique_slug(cap.workspace_id, _slugify(title), cap.id)
        cap.status = "fresh"
        cap.version += 1
        cap.updated_at = utcnow()
        return True
    except Exception:
        return False  # stays stale; next ingestion retries


def _link_related(capsules):
    """Lightweight relationships: top-2 nearest capsules by centroid."""
    caps = [c for c in capsules if c.centroid]
    if len(caps) < 2:
        for c in caps:
            c.related = []
        return
    M = np.vstack([unpack(c.centroid) for c in caps])
    sims = M @ M.T
    np.fill_diagonal(sims, -1)
    for i, cap in enumerate(caps):
        order = np.argsort(sims[i])[::-1][:2]
        cap.related = [caps[j].id for j in order if sims[i][j] > 0.3]


def _slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:70] or "topic"


def _unique_slug(workspace_id, base, self_id=None):
    slug, n = base, 1
    while True:
        clash = Capsule.query.filter_by(workspace_id=workspace_id,
                                        slug=slug).first()
        if not clash or clash.id == self_id:
            return slug
        n += 1
        slug = f"{base}-{n}"
