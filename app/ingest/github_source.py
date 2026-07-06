"""GitHub source adapter: repository files → the common pipeline.

Selects a capped set of text-like files from the repo tree, materializes
each as a Document row under the workspace's github source, and runs the
same chunk/embed tail every upload goes through (process_document).
Originals stay in the repo, so nothing is written to file storage —
`document.text` is the working copy, refreshed on rescan.

sync_github_documents() is idempotent by content hash: unchanged files
are skipped, changed files are replaced, and files that left the repo
(or fell out of the selection) are removed — the index mirrors the repo.
"""
import hashlib
import os

from github import Github

from app.config import MAX_REPO_FILES, MAX_REPO_FILE_BYTES
from app.models import db, Document, utcnow

# Text-like repo content worth indexing. Docs sort first — they carry
# the most retrieval value per token.
DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb",
    ".php", ".c", ".h", ".cpp", ".cs", ".kt", ".swift", ".scala",
    ".tf", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".sql",
    ".html", ".css", ".json",
}
SPECIAL_FILES = {"dockerfile", "makefile", "jenkinsfile", "procfile"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
              "poetry.lock", "cargo.lock", "composer.lock"}


def _selectable(path, size):
    name = os.path.basename(path).lower()
    if name in SKIP_NAMES or name.endswith(".min.js"):
        return False
    if size is None or size == 0 or size > MAX_REPO_FILE_BYTES:
        return False
    ext = os.path.splitext(name)[1]
    return (ext in DOC_EXTENSIONS or ext in CODE_EXTENSIONS
            or name in SPECIAL_FILES)


def _sort_key(path):
    ext = os.path.splitext(path)[1].lower()
    return (0 if ext in DOC_EXTENSIONS else 1, path.count("/"), path)


def select_repo_files(repo):
    """→ capped, docs-first list of (path, size) from the repo tree."""
    tree = repo.get_git_tree("HEAD", recursive=True)
    candidates = [(item.path, item.size) for item in tree.tree
                  if item.type == "blob" and _selectable(item.path, item.size)]
    candidates.sort(key=lambda c: _sort_key(c[0]))
    return candidates[:MAX_REPO_FILES]


def sync_github_documents(source, github_token, progress=None):
    """Mirror the selected repo files into Document rows and run the
    shared pipeline tail on new/changed ones. → stats dict."""
    from app.ingest import process_document  # shared tail; avoid cycle

    def report(detail):
        if progress:
            progress("content", detail)

    repo_name = "/".join(source.uri.rstrip("/").split("/")[-2:])
    repo = Github(github_token).get_repo(repo_name)
    selected = select_repo_files(repo)

    existing = {d.sha256: d for d in Document.query.filter_by(
        source_id=source.id).all()}
    seen_shas, added, unchanged, chunks_made = set(), 0, 0, 0

    for i, (path, _size) in enumerate(selected, start=1):
        try:
            data = repo.get_contents(path).decoded_content
        except Exception:
            continue  # transient fetch failure — next rescan retries
        if b"\0" in data[:1024]:
            continue  # binary despite the extension
        sha = hashlib.sha256(data).hexdigest()
        seen_shas.add(sha)
        if sha in existing:
            unchanged += 1
            continue

        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        ext = os.path.splitext(path)[1].lower()
        kind = "prose" if ext in DOC_EXTENSIONS else "code"
        doc = Document(source_id=source.id, workspace_id=source.workspace_id,
                       filename=path[:300], mime="text/plain",
                       size_bytes=len(data), sha256=sha,
                       text=text, status="extracted")
        db.session.add(doc)
        db.session.commit()
        process_document(doc, kind=kind, context=path)
        chunks_made += len(doc.chunks)
        added += 1
        report(f"{i}/{len(selected)} files")

    # Files that changed or left the repo: their old rows go away.
    removed = 0
    for sha, doc in existing.items():
        if sha not in seen_shas:
            db.session.delete(doc)  # cascades to chunks
            removed += 1
    source.last_ingested_at = utcnow()
    db.session.commit()

    stats = {"files": added, "unchanged": unchanged, "removed": removed,
             "chunks": chunks_made}
    report(f"{added} new · {unchanged} unchanged · {removed} removed")
    return stats
