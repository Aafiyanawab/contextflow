"""Knowledge ingestion orchestrator.

Increment 2 scope: uploaded files → dedupe → store → extract → document
rows with status. Chunking/embedding attach here in Increment 3, and the
GitHub adapter joins the same pipeline in Increment 4 — every source
type flows through this module, there is no second pipeline.

ingest_upload_batch() is a generator of progress events, meant to run
inside a streamed SSE response; it opens all DB state itself (the same
rule post_message follows: nothing captured from the request scope).
"""
import hashlib

from app.config import EMBEDDING_MODEL
from app.models import db, Chunk, Document, KnowledgeSource, utcnow
from app.storage import make_document_key
from app.embeddings import embed_texts, pack
from app.ingest.chunking import chunk_document
from app.ingest.extract import extract_document, ExtractionError

UPLOAD_SOURCE_NAME = "Uploaded documents"


def get_or_create_upload_source(workspace_id):
    """One upload source per workspace; batches accumulate documents."""
    src = KnowledgeSource.query.filter_by(workspace_id=workspace_id,
                                          type="upload").first()
    if not src:
        src = KnowledgeSource(workspace_id=workspace_id, type="upload",
                              name=UPLOAD_SOURCE_NAME, status="ready")
        db.session.add(src)
        db.session.flush()
    return src


def ingest_upload_batch(workspace_id, files, storage):
    """files: list of (filename, bytes), already size/type-validated.
    Yields {"type": "file", name, status, detail} per file, then
    {"type": "summary", added, skipped, failed}."""
    source = get_or_create_upload_source(workspace_id)
    source.status = "ingesting"
    db.session.commit()

    added = skipped = failed = 0
    for filename, data in files:
        sha = hashlib.sha256(data).hexdigest()
        if Document.query.filter_by(workspace_id=workspace_id,
                                    sha256=sha).first():
            skipped += 1
            yield {"type": "file", "name": filename, "status": "skipped",
                   "detail": "already in this workspace"}
            continue

        doc = Document(source_id=source.id, workspace_id=workspace_id,
                       filename=filename[:300], size_bytes=len(data),
                       sha256=sha, status="pending")
        db.session.add(doc)
        db.session.flush()
        doc.storage_key = make_document_key(workspace_id, doc.id, filename)
        doc.mime = _mime_for(filename)
        storage.save(doc.storage_key, data)

        try:
            text, meta = extract_document(filename, data)
            doc.text, doc.meta, doc.status = text, meta, "extracted"
            db.session.commit()
        except ExtractionError as e:
            doc.status, doc.error = "error", str(e)
            failed += 1
            db.session.commit()
            yield {"type": "file", "name": filename, "status": "error",
                   "detail": str(e)}
            continue

        detail = _embed_document(doc)
        added += 1
        yield {"type": "file", "name": filename, "status": doc.status,
               "detail": detail}

    source.status = "ready"
    source.last_ingested_at = utcnow()
    db.session.commit()
    yield {"type": "summary", "added": added, "skipped": skipped,
           "failed": failed}


def _embed_document(doc):
    """Chunk + embed one extracted document. An embedding-API failure
    leaves the document at status "chunked" with the reason recorded —
    the text is safe and a later re-upload/reingest completes it."""
    pieces = chunk_document(doc.text)
    for p in pieces:
        db.session.add(Chunk(document_id=doc.id, workspace_id=doc.workspace_id,
                             seq=p["seq"], text=p["text"],
                             token_count=p["token_count"], meta=p["meta"]))
    doc.status = "chunked"
    db.session.commit()

    total_tokens = sum(p["token_count"] for p in pieces)
    try:
        vectors = embed_texts([p["embedding_text"] for p in pieces])
    except Exception:
        doc.error = "Embedding service unavailable — search will skip this file until it's re-added."
        db.session.commit()
        return f"{len(pieces)} chunks · embedding pending"

    chunks = (Chunk.query.filter_by(document_id=doc.id)
              .order_by(Chunk.seq).all())
    for chunk, vec in zip(chunks, vectors):
        chunk.embedding = pack(vec)
        chunk.embedding_model = EMBEDDING_MODEL
    doc.status = "embedded"
    db.session.commit()
    return f"{len(pieces)} chunks · {total_tokens:,} tokens"


def _mime_for(filename):
    from app.ingest.extract import MIME_BY_EXT
    import os
    return MIME_BY_EXT.get(os.path.splitext(filename)[1].lower())
