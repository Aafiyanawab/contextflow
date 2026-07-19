"""Repository Synchronization Engine.

The single owner of INCREMENTAL repository sync. Responsibilities:
  * head-commit guard    — skip everything when the repo hasn't moved
  * change detection     — tree-diff current blob SHAs vs what we stored
  * incremental apply    — download & process ONLY changed files
  * rename detection      — a deleted path + an added path sharing a blob SHA
  * metadata updates      — rename = move the row, keep chunks/embeddings/capsules
  * repository state      — advance the synced-commit baseline
  * capsule refresh       — only when chunks actually changed

The FULL/first index still lives in github_source.sync_github_documents; this
engine delegates to it for a repo's first sync (new or legacy — no baseline yet)
and runs incrementally thereafter. Change detection is head-commit-guard +
tree-diff (no GitHub Compare API — see docs/DECISIONS.md).
"""
import hashlib
import os

from github import Github

from app.ingest.github_source import (DOC_EXTENSIONS, select_repo_files,
                                      sync_github_documents)
from app.models import Document, db, utcnow


def sync_repository(source, github_token, progress=None):
    """Bring a connected repo's Documents in line with its current head, doing
    the least work possible. → stats dict with a "status" of:
        "full"      first sync (delegated to the full indexer)
        "unchanged" head matched the baseline — nothing downloaded
        "synced"    an incremental diff was applied
    """
    def report(step, detail=""):
        if progress:
            progress(step, detail)

    # ── First sync (new or legacy repo): no baseline yet. Delegate to the full
    #    indexer, which indexes everything AND records the baseline for next time.
    if not source.last_synced_commit_sha:
        report("content", "first sync — full index")
        stats = sync_github_documents(source, github_token, progress)
        stats["status"] = "full"
        return stats

    repo_name = "/".join(source.uri.rstrip("/").split("/")[-2:])
    repo = Github(github_token).get_repo(repo_name)
    default_branch = repo.default_branch
    head_sha = repo.get_branch(default_branch).commit.sha

    # ── Head-commit guard: a commit uniquely determines its tree, so an
    #    unchanged head means nothing changed. Finish without downloading.
    if head_sha == source.last_synced_commit_sha:
        source.last_ingested_at = utcnow()
        db.session.commit()
        report("content", "already up to date")
        return {"status": "unchanged", "added": 0, "modified": 0,
                "deleted": 0, "renamed": 0, "failed": 0, "chunks": 0}

    # ── Change detection: diff the current tree's blob SHAs against ours.
    #    Key by the stored (truncated) repo_path; keep the full path to fetch by.
    selected = select_repo_files(repo, ref=head_sha)
    current = {p[:300]: {"blob": b, "path": p} for p, _size, b in selected}
    existing = {d.repo_path: d for d in
                Document.query.filter_by(source_id=source.id).all() if d.repo_path}

    added, modified, renames, deleted = _classify(current, existing)

    n_added = n_modified = n_deleted = n_renamed = failed = new_chunks = 0

    # ── Renames (content unchanged): metadata-only move. Chunks, embeddings and
    #    capsule memberships are preserved — nothing is re-embedded.
    for old_path, new_path in renames:
        doc = existing[old_path]
        doc.repo_path = new_path
        doc.filename = new_path
        n_renamed += 1
    if renames:
        db.session.commit()
        report("content", f"{n_renamed} renamed")

    # ── Deletes: files that left the repo (or fell out of selection). Removing
    #    the Document cascades to its chunks and their capsule memberships.
    for path in deleted:
        db.session.delete(existing[path])
        n_deleted += 1
    if deleted:
        db.session.commit()

    # ── Adds + modifies: the ONLY paths that download file content.
    work = [("add", p) for p in added] + [("mod", p) for p in modified]
    for i, (op, path) in enumerate(work, start=1):
        blob_sha = current[path]["blob"]
        try:
            data = repo.get_contents(current[path]["path"], ref=head_sha).decoded_content
        except Exception:
            failed += 1  # transient fetch failure — retried next sync (see below)
            continue
        # A file that became binary or empty is no longer indexable: drop an
        # existing row (modify case), or just skip a new one (add case).
        drop = (b"\0" in data[:1024]) or (not data.decode("utf-8", errors="replace").strip())
        if drop:
            if op == "mod":
                db.session.delete(existing[path])
                n_deleted += 1
            continue

        text = data.decode("utf-8", errors="replace").strip()
        sha = hashlib.sha256(data).hexdigest()
        kind = "prose" if os.path.splitext(path)[1].lower() in DOC_EXTENSIONS else "code"

        if op == "add":
            doc = Document(source_id=source.id, workspace_id=source.workspace_id,
                           filename=path, repo_path=path, blob_sha=blob_sha,
                           mime="text/plain", size_bytes=len(data), sha256=sha,
                           text=text, status="extracted")
            db.session.add(doc)
            db.session.commit()
            n_added += 1
        else:  # "mod" — re-index in place, preserving the row (and its path).
            doc = existing[path]
            for chunk in list(doc.chunks):
                db.session.delete(chunk)  # cascades to capsule memberships
            doc.text, doc.sha256, doc.blob_sha = text, sha, blob_sha
            doc.size_bytes, doc.status = len(data), "extracted"
            db.session.commit()
            n_modified += 1

        _process_document(doc, kind=kind, context=path)
        new_chunks += len(doc.chunks)
        report("content", f"{i}/{len(work)} changed files")

    stats = {"status": "synced", "added": n_added, "modified": n_modified,
             "deleted": n_deleted, "renamed": n_renamed, "failed": failed,
             "chunks": new_chunks}

    # ── Capsules follow only when chunks actually changed — pure renames leave
    #    the knowledge base identical, so capsules are untouched.
    if n_added or n_modified or n_deleted:
        from app.capsules import refresh_workspace
        report("capsules", "organizing knowledge…")
        cap = refresh_workspace(
            source.workspace_id,
            progress=(lambda d: report("capsules", d)) if progress else None)
        stats["capsules"] = cap["total"]

    # ── Advance the baseline ONLY on a clean run. If a file fetch failed, we
    #    leave the old baseline so the next sync re-diffs from it and retries
    #    the failed file (already-applied changes are matched and skipped).
    source.last_ingested_at = utcnow()
    if failed == 0:
        source.last_synced_commit_sha = head_sha
        source.default_branch = default_branch
    db.session.commit()
    return stats


def _classify(current, existing):
    """Tree-diff into change classes.

    current:  {repo_path: {"blob": blob_sha, "path": full_path}}  (current tree)
    existing: {repo_path: Document}                               (indexed rows)

    → (added, modified, renames, deleted) where added/modified/deleted are
    repo_path keys and renames is a list of (old_path, new_path). A deleted
    path and an added path that share a blob SHA are paired as a pure rename
    (content unchanged); everything else is a genuine add/modify/delete.
    """
    added = [k for k in current if k not in existing]
    deleted = [k for k in existing if k not in current]
    modified = [k for k in current
                if k in existing and existing[k].blob_sha != current[k]["blob"]]

    # Pair renames by blob SHA (a deleted path whose bytes reappear at a new path).
    deleted_by_blob = {}
    for k in deleted:
        deleted_by_blob.setdefault(existing[k].blob_sha, []).append(k)
    renames, real_added = [], []
    for k in added:
        pool = deleted_by_blob.get(current[k]["blob"])
        if pool:
            renames.append((pool.pop(0), k))
        else:
            real_added.append(k)
    renamed_olds = {old for old, _ in renames}
    real_deleted = [k for k in deleted if k not in renamed_olds]
    return real_added, modified, renames, real_deleted


def _process_document(doc, kind, context):
    from app.ingest import process_document  # lazy: avoids an import cycle
    return process_document(doc, kind=kind, context=context)
