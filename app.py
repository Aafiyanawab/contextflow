import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import queue
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from flask import (Flask, g, render_template, request, jsonify, redirect,
                   url_for, Response, stream_with_context, abort)
from dotenv import load_dotenv
from sqlalchemy import func, case

from flask_migrate import Migrate

from app.models import (db, Workspace, KnowledgeSource, Document, Chunk,
                        Capsule, CapsuleChunk, Chat, Message, utcnow)
from app.github_discovery import discover_repo_context
from app.context_builder import CONTEXT_LABELS, INTENT_CONTEXT_MAP, build_context
from app.intent_engine import get_intent, GREETING_RESPONSES
from app.config import OPENAI_MODEL
from app.auth import init_auth, login_required
from app.capsules import refresh_workspace
from app.config import RETRIEVAL_TOKEN_BUDGET
from app.embeddings import embed_query, search as vector_search
from app.ingest import ingest_upload_batch
from app.ingest.extract import ALLOWED_EXTENSIONS
from app.ingest.github_source import sync_github_documents
from app.ratelimit import rate_limit
from app.storage import LocalStorage
from openai import OpenAI

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY is not set. Add a long random value to .env "
                       "(e.g. python -c \"import secrets; print(secrets.token_hex(32))\").")
# SQLite locally; production points DATABASE_URL at Postgres — the
# app never knows the difference (see docs/ARCHITECTURE.md).
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL",
                                                  "sqlite:///contextflow.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Uploads need real headroom (multipart only); every other body is
# still capped at 64 KB by cap_non_upload_bodies below.
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024
NON_UPLOAD_BODY_LIMIT = 64 * 1024
db.init_app(app)
# Schema is managed by Alembic migrations now (flask db upgrade), not
# create_all. render_as_batch is required for SQLite column drops.
migrate = Migrate(app, db, render_as_batch=True)

init_auth(app)

# The AWS seam: production swaps this for an S3-backed class with the
# same interface (see app/storage.py).
storage = LocalStorage(os.path.join(app.instance_path, "uploads"))


@app.before_request
def cap_non_upload_bodies():
    # Multipart uploads get the large limit; JSON/form bodies keep the
    # tight one so the abuse cap from the security phase still holds.
    if (request.content_length and request.content_length > NON_UPLOAD_BODY_LIMIT
            and not (request.mimetype or "").startswith("multipart/")):
        abort(413)

# Owner names cap at 39 chars and repo names at 100 on GitHub; 200 total
# is generous. The explicit length check matters because SQLite doesn't
# enforce the column's String(300).
GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_REPO_URL_CHARS = 200

# Overview grid: every category we can discover, in display order.
# Categories with no hit render dimmed — honesty about what wasn't found.
OVERVIEW_KEYS = ["cloud", "iac", "containerization", "orchestration",
                 "cicd", "language", "framework", "monitoring"]
OVERVIEW_LABELS = {**CONTEXT_LABELS, "monitoring": "Monitoring"}

DISPLAY_VALUES = {
    "aws": "AWS", "gcp": "GCP", "azure": "Azure",
    "terraform": "Terraform", "docker": "Docker", "kubernetes": "Kubernetes",
    "github_actions": "GitHub Actions",
    "python": "Python", "javascript": "JavaScript", "java": "Java",
    "flask": "Flask", "fastapi": "FastAPI", "django": "Django",
    "express": "Express", "react": "React", "spring": "Spring",
}

SYSTEM_PROMPT = ("You are ContextFlow, a senior cloud engineer assistant. "
                 "Provide specific, practical answers based on the "
                 "organizational context provided.")

DEFAULT_GREETING_REPLY = ("Hello! I'm ContextFlow. How can I help with "
                          "your infrastructure today?")

# A rule-based classification avoids one OpenAI classifier call
# (~60 prompt + ~10 completion tokens). Used for the savings estimate.
CLASSIFIER_TOKENS_SAVED = 70
HISTORY_LIMIT = 12  # prior messages included per prompt (6 exchanges)
MAX_QUERY_CHARS = 4000  # bounds prompt-token spend per message

SCAN_LIMIT_MESSAGE = ("Too many repository scans in a short time. "
                      "Try again in a few minutes.")
UPLOAD_LIMIT_MESSAGE = ("Too many uploads in a short time. "
                        "Try again in a few minutes.")
MAX_UPLOAD_FILES = 50
MAX_UPLOAD_FILE_BYTES = 10 * 1024 * 1024


# ── Security headers ─────────────────────────────────────
# A strict, nonce-based CSP: scripts run only if they carry the
# per-request nonce, so an injected <script> can't execute even if
# markup escaping is ever bypassed. Third-party origins are allowlisted
# to exactly what the app loads (Google Fonts, GitHub avatars).

@app.before_request
def set_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def inject_nonce():
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.after_request
def security_headers(resp):
    nonce = getattr(g, "csp_nonce", "")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' https://avatars.githubusercontent.com; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # HSTS only over TLS — sending it on today's plain-HTTP deploy would
    # lock browsers out. Becomes active automatically once TLS lands.
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    return resp


@app.errorhandler(413)
def request_too_large(e):
    # Keep the JSON-only-for-fetch contract even for bodies rejected
    # before a view runs.
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": "Request too large."}), 413
    return e


@app.template_filter("ctxval")
def ctxval(value):
    return DISPLAY_VALUES.get(value, value)


@app.template_filter("timeago")
def timeago(dt):
    if not dt:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 604800:
        return f"{seconds // 86400}d ago"
    return dt.strftime("%b %d, %Y")


def usage_stats(workspace_id=None, user_id=None):
    q = db.session.query(
        func.count(Message.id),
        func.coalesce(func.sum(Message.tokens_in + Message.tokens_out), 0),
        func.coalesce(func.sum(case((Message.method == "rule-based", 1), else_=0)), 0),
    ).filter(Message.role == "assistant")
    if workspace_id or user_id:
        q = q.join(Chat, Message.chat_id == Chat.id)
    if workspace_id:
        q = q.filter(Chat.workspace_id == workspace_id)
    if user_id:
        q = q.join(Workspace, Chat.workspace_id == Workspace.id) \
             .filter(Workspace.user_id == user_id)
    queries, tokens, free = q.one()
    free_pct = round(free * 100 / queries) if queries else 0
    return {"queries": queries, "tokens": int(tokens), "free_pct": free_pct,
            "savings": int(free) * CLASSIFIER_TOKENS_SAVED}


# ── Ownership layer ──────────────────────────────────────
# The single authorization chokepoint. Every workspace/chat access goes
# through here; team workspaces and RBAC later mean extending these two
# functions, not touching routes. 404 (not 403) so IDs aren't probeable.

def get_owned_workspace(ws_id):
    ws = db.session.get(Workspace, ws_id)
    if not ws or ws.user_id != g.user.id:
        abort(404)
    return ws


def get_owned_chat(chat_id):
    chat = db.session.get(Chat, chat_id)
    if not chat or chat.workspace.user_id != g.user.id:
        abort(404)
    return chat


@app.context_processor
def sidebar_context():
    if not getattr(g, "user", None):
        return {"sidebar_workspaces": [], "global_usage": usage_stats(user_id="none")}
    workspaces = (Workspace.query.filter_by(user_id=g.user.id)
                  .order_by(Workspace.created_at.asc()).all())
    return {"sidebar_workspaces": workspaces,
            "global_usage": usage_stats(user_id=g.user.id)}


# ── Navigation ───────────────────────────────────────────

@app.route("/")
@login_required
def index():
    last_chat = (Chat.query.join(Workspace)
                 .filter(Workspace.user_id == g.user.id)
                 .order_by(Chat.updated_at.desc()).first())
    if last_chat:
        return redirect(url_for("chat_view", ws_id=last_chat.workspace_id,
                                 chat_id=last_chat.id))
    ws = (Workspace.query.filter_by(user_id=g.user.id)
          .order_by(Workspace.created_at.desc()).first())
    if ws:
        return redirect(url_for("workspace_view", ws_id=ws.id))
    return redirect(url_for("connect"))


# ── Connect repository ───────────────────────────────────

@app.route("/connect")
@login_required
def connect():
    return render_template("connect.html")


def _friendly_scan_error(raw: str) -> str:
    lowered = raw.lower()
    if "404" in lowered or "not found" in lowered:
        return "Repository not found. Check the URL — private repositories aren't supported yet."
    if "rate limit" in lowered:
        return "GitHub API rate limit reached. Please try again in a few minutes."
    if "bad credentials" in lowered or "401" in lowered:
        return "GitHub token is invalid. Check GITHUB_TOKEN in the server's .env file."
    return "Scan failed. Verify the repository URL and try again."


@app.route("/workspaces/scan", methods=["POST"])
@login_required
@rate_limit("scan", limit=10, per_seconds=600, message=SCAN_LIMIT_MESSAGE)
def scan_workspace():
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip().rstrip("/")
    if len(repo_url) > MAX_REPO_URL_CHARS or not GITHUB_URL_RE.match(repo_url):
        return jsonify({"error": "Enter a full GitHub repository URL, "
                                 "like https://github.com/org/repo"}), 400

    existing = (KnowledgeSource.query.join(Workspace)
                .filter(Workspace.user_id == g.user.id,
                        KnowledgeSource.uri == repo_url).first())
    if existing:
        return jsonify({"redirect": url_for("workspace_view",
                                            ws_id=existing.workspace_id),
                        "existing": True})
    user_pk = g.user.id  # capture before the streamed generator runs

    events = queue.Queue()
    outcome = {}

    def progress(step, detail):
        events.put({"type": "step", "step": step, "detail": detail})

    def scan():
        try:
            outcome["context"] = discover_repo_context(repo_url, progress=progress)
        except Exception as e:
            outcome["error"] = str(e)
        events.put(None)

    def generate():
        threading.Thread(target=scan, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

        if "error" in outcome:
            payload = {"type": "error", "message": _friendly_scan_error(outcome["error"])}
            yield f"data: {json.dumps(payload)}\n\n"
            return

        ws = Workspace(user_id=user_pk, name=repo_url.split("/")[-1])
        db.session.add(ws)
        db.session.flush()  # need ws.id for the source row
        src = KnowledgeSource(workspace_id=ws.id, type="github",
                              name=ws.name, uri=repo_url,
                              status="ready",
                              profile=outcome["context"],
                              last_ingested_at=utcnow())
        db.session.add(src)
        db.session.commit()

        # Content indexing: repo files through the common pipeline.
        # Same worker-thread + queue pattern as discovery above; runs
        # under its own app context so it gets its own DB session.
        # A failure here degrades — the workspace still exists and
        # Rescan retries the index later.
        src_pk = src.id
        content_events = queue.Queue()
        content_outcome = {}

        def index_content():
            try:
                with app.app_context():
                    source_row = db.session.get(KnowledgeSource, src_pk)
                    content_outcome["stats"] = sync_github_documents(
                        source_row, os.getenv("GITHUB_TOKEN"),
                        progress=lambda step, detail: content_events.put(
                            {"type": "step", "step": step, "detail": detail}))
            except Exception:
                content_outcome["error"] = True
            content_events.put(None)

        threading.Thread(target=index_content, daemon=True).start()
        while True:
            event = content_events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
        if "error" in content_outcome:
            yield _sse({"type": "step", "step": "content",
                        "detail": "index failed — use Rescan to retry"})

        done = {"type": "done",
                "redirect": url_for("workspace_view", ws_id=ws.id),
                "found": len(outcome["context"]) - 1}  # minus the repo key
        yield f"data: {json.dumps(done)}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Upload knowledge sources ─────────────────────────────

def _read_upload_files():
    """Validate and read the request's files while they're still bound
    to the request. Whole-batch rejection on any invalid file — partial
    acceptance surprises users. Returns (files, error)."""
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return None, "No files selected."
    if len(uploads) > MAX_UPLOAD_FILES:
        return None, (f"Too many files — upload at most "
                      f"{MAX_UPLOAD_FILES} at a time.")
    files = []
    for f in uploads:
        # Folder uploads send relative paths; keep only the leaf name.
        # Storage keys are id-based, so this is display/extension only.
        name = os.path.basename(f.filename.replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return None, (f"\"{name}\" isn't a supported type. Use PDF, "
                          f"DOCX, TXT, or Markdown.")
        data = f.read()
        if len(data) > MAX_UPLOAD_FILE_BYTES:
            return None, f"\"{name}\" is larger than 10 MB."
        files.append((name, data))
    return files, None


def _upload_response(ws_id, files):
    """SSE stream of ingestion progress, ending with a redirect —
    the same shape the scan stream has."""
    def generate():
        for event in ingest_upload_batch(ws_id, files, storage):
            yield _sse(event)
        yield _sse({"type": "done",
                    "redirect": url_for("workspace_view", ws_id=ws_id)})
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/workspaces/upload", methods=["POST"])
@login_required
@rate_limit("upload", limit=10, per_seconds=600, message=UPLOAD_LIMIT_MESSAGE)
def upload_workspace():
    files, err = _read_upload_files()
    if err:
        return jsonify({"error": err}), 400
    name = (request.form.get("name") or "").strip()[:120] or "My documents"
    ws = Workspace(user_id=g.user.id, name=name)
    db.session.add(ws)
    db.session.commit()  # committed before streaming; generator re-queries
    return _upload_response(ws.id, files)


@app.route("/w/<ws_id>/sources/upload", methods=["POST"])
@login_required
@rate_limit("upload", limit=10, per_seconds=600, message=UPLOAD_LIMIT_MESSAGE)
def upload_to_workspace(ws_id):
    ws = get_owned_workspace(ws_id)
    files, err = _read_upload_files()
    if err:
        return jsonify({"error": err}), 400
    return _upload_response(ws.id, files)


@app.route("/w/<ws_id>/search", methods=["POST"])
@login_required
@rate_limit("search", limit=30, per_seconds=60)
def search_workspace(ws_id):
    """Retrieval preview: exactly what scoped vector search returns,
    with attribution and token accounting. This is the surface the
    capsule router plugs into — chat doesn't use retrieval yet."""
    ws = get_owned_workspace(ws_id)
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()[:500]
    if not query:
        return jsonify({"error": "Empty search"}), 400
    try:
        qvec = embed_query(query)
    except Exception as e:
        return jsonify({"error": _friendly_llm_error(str(e))}), 502
    hits = vector_search(ws.id, qvec, k=8,
                         token_budget=RETRIEVAL_TOKEN_BUDGET)
    by_id = {c.id: c for c in Chunk.query.filter(
        Chunk.id.in_([h[0] for h in hits])).all()} if hits else {}
    results = []
    for chunk_id, score in hits:
        chunk = by_id[chunk_id]
        results.append({
            "score": round(score, 3),
            "tokens": chunk.token_count,
            "snippet": chunk.text[:280],
            "filename": chunk.document.filename,
            "page": (chunk.meta or {}).get("page"),
            "headings": (chunk.meta or {}).get("headings", []),
        })
    return jsonify({"results": results,
                    "total_tokens": sum(r["tokens"] for r in results),
                    "budget": RETRIEVAL_TOKEN_BUDGET})


@app.route("/w/<ws_id>/sources/<src_id>/delete", methods=["POST"])
@login_required
def delete_source(ws_id, src_id):
    ws = get_owned_workspace(ws_id)
    src = db.session.get(KnowledgeSource, src_id)
    if not src or src.workspace_id != ws.id:
        abort(404)
    for doc in src.documents:
        if doc.storage_key:
            storage.delete(doc.storage_key)
    db.session.delete(src)  # cascades to documents and chunks
    db.session.commit()
    # Knowledge changed → capsules follow: memberships prune, emptied
    # capsules dissolve, shrunk ones re-synthesize.
    refresh_workspace(ws.id)
    return redirect(url_for("workspace_view", ws_id=ws.id))


# ── Workspace ────────────────────────────────────────────

@app.route("/w/<ws_id>")
@login_required
def workspace_view(ws_id):
    ws = get_owned_workspace(ws_id)
    cells = [(key, OVERVIEW_LABELS.get(key, key), ws.context_profile.get(key))
             for key in OVERVIEW_KEYS]
    return render_template("workspace.html", ws=ws, cells=cells,
                           capsules=_capsule_cards(ws.id),
                           ws_usage=usage_stats(ws.id), active_ws=ws)


def _capsule_cards(ws_id):
    """Capsule grid + detail payload. Read-only — users never create,
    edit, or delete capsules; ingestion maintains them."""
    caps = (Capsule.query.filter_by(workspace_id=ws_id)
            .order_by(Capsule.title.asc()).all())
    titles = {c.id: c.title for c in caps}
    cards = []
    for c in caps:
        files = [f for (f,) in (db.session.query(Document.filename)
                 .join(Chunk, Chunk.document_id == Document.id)
                 .join(CapsuleChunk, CapsuleChunk.chunk_id == Chunk.id)
                 .filter(CapsuleChunk.capsule_id == c.id)
                 .distinct().limit(8))]
        cards.append({"id": c.id, "title": c.title, "status": c.status,
                      "keywords": (c.keywords or [])[:12],
                      "concepts": c.concepts or [],
                      "summary": c.summary or "",
                      "tokens": c.token_count,
                      "chunks": len(c.memberships),
                      "related": [titles[r] for r in (c.related or [])
                                  if r in titles],
                      "files": files,
                      "updated": timeago(c.updated_at)})
    return cards


@app.route("/w/<ws_id>/rescan", methods=["POST"])
@login_required
@rate_limit("scan", limit=10, per_seconds=600, message=SCAN_LIMIT_MESSAGE)
def rescan_workspace(ws_id):
    ws = get_owned_workspace(ws_id)
    src = ws.github_source
    if not src:
        abort(404)
    try:
        context = discover_repo_context(src.uri)
    except Exception as e:
        return jsonify({"error": _friendly_scan_error(str(e))}), 502
    src.profile = context
    src.last_ingested_at = utcnow()
    db.session.commit()
    # Content re-index through the common pipeline: new/changed files
    # in, vanished files out. Profile already saved — an index failure
    # degrades rather than failing the rescan.
    try:
        stats = sync_github_documents(src, os.getenv("GITHUB_TOKEN"))
    except Exception:
        return jsonify({"ok": True, "index": "failed"})
    return jsonify({"ok": True, "index": stats})


@app.route("/w/<ws_id>/delete", methods=["POST"])
@login_required
def delete_workspace(ws_id):
    ws = get_owned_workspace(ws_id)
    storage.delete_workspace(ws.id)  # stored upload files aren't DB rows
    db.session.delete(ws)  # cascades to sources, documents, chats, messages
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/w/<ws_id>/chats", methods=["POST"])
@login_required
def create_chat(ws_id):
    ws = get_owned_workspace(ws_id)
    title = (request.form.get("title") or "").strip()[:160] or "New chat"
    new_chat = Chat(workspace_id=ws.id, title=title)
    db.session.add(new_chat)
    db.session.commit()
    return redirect(url_for("chat_view", ws_id=ws.id, chat_id=new_chat.id))


def context_split(intent, discovered):
    """Which discovered-context keys get injected for this intent,
    and which are deliberately withheld (with the reason)."""
    relevant = INTENT_CONTEXT_MAP.get(intent, [])
    injected, withheld = [], []
    for key, value in discovered.items():
        if key == "repo":
            continue
        entry = {"key": key,
                 "label": OVERVIEW_LABELS.get(key, key),
                 "value": DISPLAY_VALUES.get(value, value)}
        if key in relevant:
            injected.append(entry)
        else:
            entry["reason"] = ("greeting — no context needed" if intent == "general"
                               else f"not relevant to {intent}")
            withheld.append(entry)
    return injected, withheld


def build_suggestions(discovered):
    suggestions = []
    if discovered.get("iac") == "terraform":
        suggestions.append("Review our Terraform layout for best practices")
    if discovered.get("cicd") == "github_actions":
        suggestions.append("Add a test gate to our GitHub Actions pipeline")
    if discovered.get("containerization") == "docker":
        suggestions.append("Deploy this Docker service with zero downtime")
    if discovered.get("cloud") == "aws":
        suggestions.append("How can we reduce our AWS costs?")
    if not suggestions:
        suggestions = ["How should I structure my infrastructure?",
                       "Set up CI/CD for this repository"]
    return suggestions[:3]


def serialize_message(m):
    data = {"role": m.role, "content": m.content}
    if m.role == "assistant":
        data["meta"] = {
            "intent": m.intent,
            "method": m.method,
            "matched": m.matched_keywords or [],
            "injected": m.injected_context or [],
            "withheld": m.withheld_context or [],
            "tokens_in": m.tokens_in,
            "tokens_out": m.tokens_out,
        }
    return data


@app.route("/w/<ws_id>/c/<chat_id>")
@login_required
def chat_view(ws_id, chat_id):
    ws = get_owned_workspace(ws_id)
    active_chat = db.session.get(Chat, chat_id)
    if not active_chat or active_chat.workspace_id != ws.id:
        abort(404)
    payload = {
        "messages": [serialize_message(m) for m in active_chat.messages],
        "usage": usage_stats(ws.id),
        "suggestions": build_suggestions(ws.context_profile),
        "postUrl": url_for("post_message", chat_id=active_chat.id),
    }
    context_keys = [k for k in ws.context_profile if k != "repo"]
    return render_template("chat.html", ws=ws, chat=active_chat,
                           context_keys=context_keys, payload=payload,
                           active_ws=ws, active_chat=active_chat)


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _friendly_llm_error(raw: str) -> str:
    lowered = raw.lower()
    if "rate_limit" in lowered:
        return "Too many requests right now. Wait a moment and try again."
    if "insufficient_quota" in lowered or "quota" in lowered:
        return "Service temporarily unavailable — budget limit reached. Contact the admin."
    if "invalid_api_key" in lowered or "authentication" in lowered:
        return "API configuration error. Contact the admin."
    return "Temporarily unable to process requests. Try again in a few minutes."


@app.route("/c/<chat_id>/messages", methods=["POST"])
@login_required
@rate_limit("messages", limit=20, per_seconds=60)
def post_message(chat_id):
    active_chat = get_owned_chat(chat_id)
    ws = active_chat.workspace
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty message"}), 400
    if len(query) > MAX_QUERY_CHARS:
        return jsonify({"error": f"Message too long — keep it under "
                                 f"{MAX_QUERY_CHARS:,} characters."}), 400

    discovered = ws.context_profile
    intent_result = get_intent(query)
    intent, method = intent_result["intent"], intent_result["method"]
    matched = intent_result.get("matched_keywords") or []
    injected, withheld = context_split(intent, discovered)

    is_first = len(active_chat.messages) == 0
    needs_title = is_first and active_chat.title == "New chat"
    history = [{"role": m.role, "content": m.content}
               for m in active_chat.messages[-HISTORY_LIMIT:]]
    chat_pk, ws_pk, owner_pk = active_chat.id, ws.id, ws.user_id

    def generate():
        # The generator may run under a different session than the request
        # handler, so re-fetch the row here — mutations on objects captured
        # from the request scope would silently not persist.
        chat_row = db.session.get(Chat, chat_pk)

        # The user's message is recorded before the model is called,
        # so a failed API call never loses what they typed.
        db.session.add(Message(chat_id=chat_pk, role="user", content=query))
        if needs_title:
            chat_row.title = query[:57] + "…" if len(query) > 58 else query
        chat_row.updated_at = utcnow()
        db.session.commit()

        def persist_assistant(answer, tokens_in, tokens_out):
            db.session.add(Message(chat_id=chat_pk, role="assistant",
                                   content=answer, intent=intent, method=method,
                                   matched_keywords=matched,
                                   injected_context=injected,
                                   withheld_context=withheld,
                                   tokens_in=tokens_in, tokens_out=tokens_out))
            chat_row.updated_at = utcnow()
            db.session.commit()

        yield _sse({"type": "meta", "intent": intent, "method": method,
                    "matched": matched, "injected": injected, "withheld": withheld})

        # Greeting fast path: instant, free, no API call
        if intent == "general" and method == "rule-based":
            reply = GREETING_RESPONSES.get(intent_result.get("greeting_key"),
                                           DEFAULT_GREETING_REPLY)
            for word in reply.split(" "):
                yield _sse({"type": "token", "content": word + " "})
            persist_assistant(reply, 0, 0)
            yield _sse({"type": "done", "tokens_in": 0, "tokens_out": 0,
                        "title": chat_row.title,
                        "usage": usage_stats(ws_pk),
                        "user_usage": usage_stats(user_id=owner_pk)})
            return

        context_block = build_context(intent, discovered)
        system = SYSTEM_PROMPT + ("\n\n" + context_block if context_block else "")
        prompt_messages = ([{"role": "system", "content": system}]
                           + history
                           + [{"role": "user", "content": query}])

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        answer, tokens_in, tokens_out = "", 0, 0
        try:
            with client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=prompt_messages,
                max_tokens=1000,
                temperature=0.3,
                stream=True,
                stream_options={"include_usage": True},
            ) as stream:
                for chunk in stream:
                    if chunk.usage:
                        tokens_in = chunk.usage.prompt_tokens
                        tokens_out = chunk.usage.completion_tokens
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        answer += token
                        yield _sse({"type": "token", "content": token})
        except Exception as e:
            yield _sse({"type": "error", "message": _friendly_llm_error(str(e))})
            return

        persist_assistant(answer, tokens_in, tokens_out)
        yield _sse({"type": "done", "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "title": chat_row.title,
                    "usage": usage_stats(ws_pk),
                    "user_usage": usage_stats(user_id=owner_pk)})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    # Debug must never default on — the Werkzeug debugger is an RCE console.
    # Opt in locally with FLASK_DEBUG=1; production leaves it unset.
    debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes", "on")
    app.run(host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "5000")),
            debug=debug)
