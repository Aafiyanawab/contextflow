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
                   url_for, Response, stream_with_context, abort, session, flash)
from dotenv import load_dotenv
from sqlalchemy import func, case, or_

from flask_migrate import Migrate

from app.models import (db, Workspace, KnowledgeSource, Document, Chunk,
                        Capsule, CapsuleChunk, Chat, Message, utcnow)
from app.github_discovery import discover_repo_context
from app.context_builder import (CONTEXT_LABELS, INTENT_CONTEXT_MAP,
                                 build_prompt_context, wants_summaries)
from app.intent_engine import (route_query, GREETING_RESPONSES,
                               detect_intent_rule_based)
from app.config import OPENAI_MODEL, estimate_cost
from app.auth import (init_auth, login_required, admin_required,
                      super_admin_required, check_csrf)
from app.models import User, Company, AuditLog
from app.capsules import refresh_workspace
from app.config import (RETRIEVAL_TOKEN_BUDGET, RETRIEVAL_K,
                        RETRIEVAL_K_WITH_SUMMARY, SUMMARY_CHUNK_BUDGET,
                        NAIVE_RAG_K)
from app.embeddings import (embed_query, search as vector_search,
                            capsule_chunk_ids)
from app.ingest.chunking import count_tokens
from app.ingest import ingest_upload_batch
from app.ingest.extract import ALLOWED_EXTENSIONS
from app.ingest.github_source import sync_github_documents
from app.ingest.repo_sync import sync_repository
from app.ratelimit import rate_limit
from app.storage import get_storage
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

# The AWS seam: get_storage() returns an S3-backed store when S3_BUCKET is
# set (production, or docker-compose via MinIO); otherwise it writes to
# local disk. Same four-method interface either way (see app/storage.py).
storage = get_storage(os.path.join(app.instance_path, "uploads"))


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


@app.errorhandler(403)
def forbidden(e):
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": "You don't have access to this."}), 403
    return render_template("403.html"), 403


@app.template_filter("ctxval")
def ctxval(value):
    return DISPLAY_VALUES.get(value, value)


@app.template_filter("usd")
def usd(amount):
    amount = amount or 0
    # Sub-dollar estimates need more precision to be meaningful.
    return f"${amount:,.4f}" if amount < 1 else f"${amount:,.2f}"


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


def usage_stats(workspace_id=None, user_id=None, scope_ids=None):
    q = db.session.query(
        func.count(Message.id),
        func.coalesce(func.sum(Message.tokens_in + Message.tokens_out), 0),
        func.coalesce(func.sum(case((Message.method == "rule-based", 1), else_=0)), 0),
        func.coalesce(func.sum(Message.context_tokens), 0),
        func.coalesce(func.sum(Message.naive_tokens), 0),
        func.coalesce(func.sum(Message.tokens_in), 0),
        func.coalesce(func.sum(Message.tokens_out), 0),
    ).filter(Message.role == "assistant")
    if workspace_id or user_id or scope_ids is not None:
        q = q.join(Chat, Message.chat_id == Chat.id)
    if workspace_id:
        q = q.filter(Chat.workspace_id == workspace_id)
    if user_id or scope_ids is not None:
        q = q.join(Workspace, Chat.workspace_id == Workspace.id)
    if user_id:
        q = q.filter(Workspace.user_id == user_id)
    if scope_ids is not None:
        q = q.filter(Workspace.user_id.in_(scope_ids or ["\0"]))
    (queries, tokens, free, ctx_tokens, naive_tokens,
     tokens_in, tokens_out) = q.one()
    free_pct = round(free * 100 / queries) if queries else 0
    # Measured context savings: naive-RAG baseline minus what capsule
    # routing actually sent, summed across this scope's exchanges.
    ctx_saved = int(naive_tokens) - int(ctx_tokens)
    ctx_reduction = round(ctx_saved * 100 / naive_tokens) if naive_tokens else 0
    return {"queries": queries, "tokens": int(tokens), "free_pct": free_pct,
            "savings": int(free) * CLASSIFIER_TOKENS_SAVED,
            "context_saved": ctx_saved, "context_reduction": ctx_reduction,
            "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
            "cost": estimate_cost(tokens_in, tokens_out)}


ROLE_LABELS = {"super_admin": "Super Admin", "company_admin": "Company Admin",
               "employee": "Employee"}


@app.template_filter("rolelabel")
def rolelabel(role):
    return ROLE_LABELS.get(role, role)


def audit(action, target=""):
    """Append a privileged-action record. Best-effort: recording the
    action must never break the action itself."""
    try:
        db.session.add(AuditLog(actor_id=g.user.id, actor_email=g.user.email,
                                action=action, target=str(target)[:200]))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _company_user_ids(admin):
    """User ids a Company Admin may see/manage: everyone sharing their
    company. Super Admins pass None (no scoping)."""
    if admin.is_super_admin:
        return None
    ids = [u.id for u in User.query.filter_by(company_id=admin.company_id).all()] \
        if admin.company_id else [admin.id]
    return ids


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

def _home_redirect():
    """Where an authenticated user belongs: admins to the dashboard,
    everyone else to their most recent chat/workspace, or onboarding
    when they have none yet."""
    if g.user.is_admin:
        return redirect(url_for("admin_overview"))
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


@app.route("/")
def index():
    # Public landing for visitors; authenticated users skip straight home.
    if getattr(g, "user", None):
        return _home_redirect()
    return render_template("landing.html")


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
        chat_id = _ensure_first_chat(existing.workspace_id)
        return jsonify({"redirect": url_for("chat_view",
                                            ws_id=existing.workspace_id,
                                            chat_id=chat_id),
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

        chat_id = _ensure_first_chat(ws.id)
        done = {"type": "done",
                "redirect": url_for("chat_view", ws_id=ws.id, chat_id=chat_id),
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
        chat_id = _ensure_first_chat(ws_id)
        yield _sse({"type": "done",
                    "redirect": url_for("chat_view", ws_id=ws_id, chat_id=chat_id)})
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


def _ensure_first_chat(ws_id):
    """Return the id of a chat to open for this workspace, creating an
    empty one if none exists. Every creation path lands the user in a
    conversation (ChatGPT-style), never a setup screen."""
    chat = (Chat.query.filter_by(workspace_id=ws_id)
            .order_by(Chat.pinned.desc(), Chat.updated_at.desc()).first())
    if not chat:
        chat = Chat(workspace_id=ws_id, title="New chat")
        db.session.add(chat)
        db.session.commit()
    return chat.id


@app.route("/workspaces/create", methods=["POST"])
@login_required
def create_workspace_instant():
    """Create a workspace and its first chat, then drop straight into the
    conversation — knowledge is entirely optional and can be added later."""
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        n = Workspace.query.filter_by(user_id=g.user.id).count() + 1
        name = f"Workspace {n}"
    ws = Workspace(user_id=g.user.id, name=name)
    db.session.add(ws)
    db.session.flush()
    chat = Chat(workspace_id=ws.id, title="New chat")
    db.session.add(chat)
    db.session.commit()
    return redirect(url_for("chat_view", ws_id=ws.id, chat_id=chat.id))


@app.route("/w/<ws_id>/sources/connect", methods=["POST"])
@login_required
@rate_limit("scan", limit=10, per_seconds=600, message=SCAN_LIMIT_MESSAGE)
def connect_source(ws_id):
    """Attach a GitHub repository to an EXISTING workspace (Add Knowledge →
    Connect Repository). Same discovery + indexing pipeline as the initial
    scan; only difference is the source hangs off the current workspace."""
    ws = get_owned_workspace(ws_id)
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip().rstrip("/")
    if len(repo_url) > MAX_REPO_URL_CHARS or not GITHUB_URL_RE.match(repo_url):
        return jsonify({"error": "Enter a full GitHub repository URL, "
                                 "like https://github.com/org/repo"}), 400
    if any(s.uri == repo_url for s in ws.sources):
        return jsonify({"error": "That repository is already connected here."}), 400
    ws_pk = ws.id

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
            yield _sse(event)
        if "error" in outcome:
            yield _sse({"type": "error",
                        "message": _friendly_scan_error(outcome["error"])})
            return
        src = KnowledgeSource(workspace_id=ws_pk, type="github",
                              name=repo_url.split("/")[-1], uri=repo_url,
                              status="ready", profile=outcome["context"],
                              last_ingested_at=utcnow())
        db.session.add(src)
        db.session.commit()
        src_pk = src.id
        content_events = queue.Queue()

        def index_content():
            try:
                with app.app_context():
                    row = db.session.get(KnowledgeSource, src_pk)
                    sync_github_documents(
                        row, os.getenv("GITHUB_TOKEN"),
                        progress=lambda step, detail: content_events.put(
                            {"type": "step", "step": step, "detail": detail}))
            except Exception:
                pass
            content_events.put(None)

        threading.Thread(target=index_content, daemon=True).start()
        while True:
            event = content_events.get()
            if event is None:
                break
            yield _sse(event)
        # Knowledge changed → rebuild capsules (backend-only).
        try:
            with app.app_context():
                refresh_workspace(ws_pk)
        except Exception:
            pass
        yield _sse({"type": "done",
                    "redirect": url_for("workspace_view", ws_id=ws_pk)})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


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


# ── Document management (view / rename / delete / replace) ──

def _owned_document(ws_id, doc_id):
    ws = get_owned_workspace(ws_id)
    doc = db.session.get(Document, doc_id)
    if not doc or doc.workspace_id != ws.id:
        abort(404)
    return ws, doc


@app.route("/w/<ws_id>/documents/<doc_id>")
@login_required
def view_document(ws_id, doc_id):
    ws, doc = _owned_document(ws_id, doc_id)
    if not doc.storage_key:
        abort(404)
    try:
        data = storage.read(doc.storage_key)
    except Exception:
        abort(404)
    resp = app.response_class(
        data, mimetype=doc.mime or "application/octet-stream")
    disposition = "attachment" if request.args.get("download") == "1" else "inline"
    safe = doc.filename.replace('"', "").replace("\n", " ")
    resp.headers["Content-Disposition"] = f'{disposition}; filename="{safe}"'
    return resp


@app.route("/w/<ws_id>/documents/<doc_id>/rename", methods=["POST"])
@login_required
def rename_document(ws_id, doc_id):
    ws, doc = _owned_document(ws_id, doc_id)
    name = (request.form.get("filename") or "").strip()[:300]
    if name:
        # Preserve the extension so type detection / preview stay correct.
        ext = os.path.splitext(doc.filename)[1]
        if ext and not os.path.splitext(name)[1]:
            name += ext
        doc.filename = name
        db.session.commit()
    return redirect(url_for("workspace_view", ws_id=ws.id))


@app.route("/w/<ws_id>/documents/<doc_id>/delete", methods=["POST"])
@login_required
def delete_document(ws_id, doc_id):
    ws, doc = _owned_document(ws_id, doc_id)
    src = doc.source
    if doc.storage_key:
        storage.delete(doc.storage_key)
    db.session.delete(doc)  # cascades to chunks
    db.session.commit()
    # Drop the source once its last document is gone, so Knowledge stays tidy.
    if src and not src.documents:
        db.session.delete(src)
        db.session.commit()
    refresh_workspace(ws.id)
    return redirect(url_for("workspace_view", ws_id=ws.id))


@app.route("/w/<ws_id>/documents/<doc_id>/replace", methods=["POST"])
@login_required
@rate_limit("upload", limit=10, per_seconds=600, message=UPLOAD_LIMIT_MESSAGE)
def replace_document(ws_id, doc_id):
    ws, doc = _owned_document(ws_id, doc_id)
    files, err = _read_upload_files()
    if err:
        return jsonify({"error": err}), 400
    if len(files) != 1:
        return jsonify({"error": "Choose exactly one file."}), 400
    # Remove the old file, then ingest the replacement through the normal
    # pipeline (extract → chunk → embed → capsules rebuild inside the batch).
    if doc.storage_key:
        storage.delete(doc.storage_key)
    db.session.delete(doc)
    db.session.commit()
    for _ in ingest_upload_batch(ws.id, files, storage):
        pass
    return jsonify({"ok": True,
                    "redirect": url_for("workspace_view", ws_id=ws.id)})


# ── Workspace ────────────────────────────────────────────

@app.route("/w/<ws_id>")
@login_required
def workspace_view(ws_id):
    # User-facing dashboard: workspace name, knowledge sources, recent chats.
    # No capsules / detected stack / retrieval internals — those are admin-only.
    ws = get_owned_workspace(ws_id)
    return render_template("workspace.html", ws=ws, active_ws=ws)


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
                 .distinct().limit(12))]
        cards.append({"id": c.id, "title": c.title, "status": c.status,
                      "keywords": (c.keywords or [])[:12],
                      "concepts": c.concepts or [],
                      "summary": c.summary or "",
                      "tokens": c.token_count,
                      "chunks": len(c.memberships),
                      "doc_count": len(files),
                      "related": [titles[r] for r in (c.related or [])
                                  if r in titles],
                      "files": files,
                      "updated": timeago(c.updated_at)})
    return cards


@app.route("/w/<ws_id>/rescan", methods=["POST"])
@login_required
@rate_limit("scan", limit=10, per_seconds=600, message=SCAN_LIMIT_MESSAGE)
def rescan_workspace(ws_id):
    """Sync a connected repo through the incremental engine (head-commit guard
    + tree-diff); the engine falls back to a full index on a repo's first sync.
    Stack-profile re-discovery runs only when something actually changed, so an
    up-to-date repo costs one head-commit lookup and downloads nothing."""
    ws = get_owned_workspace(ws_id)
    src = ws.github_source
    if not src:
        abort(404)
    # Guard against overlapping syncs (double-click, second tab).
    if src.status == "ingesting":
        return jsonify({"error": "A sync is already in progress."}), 409
    src.status = "ingesting"
    db.session.commit()
    try:
        stats = sync_repository(src, os.getenv("GITHUB_TOKEN"))
    except Exception:
        src.status = "ready"
        db.session.commit()
        return jsonify({"ok": True, "index": "failed"})
    # Refresh the discovered stack profile only when the repo moved — an
    # "unchanged" sync stays zero-work. Best-effort: a discovery failure must
    # not undo an otherwise-successful content sync.
    if stats.get("status") != "unchanged":
        try:
            src.profile = discover_repo_context(src.uri)
        except Exception:
            pass
    src.status = "ready"
    db.session.commit()
    return jsonify({"ok": True, "index": stats})


@app.route("/w/<ws_id>/delete", methods=["POST"])
@login_required
def delete_workspace(ws_id):
    ws = get_owned_workspace(ws_id)
    storage.delete_workspace(ws.id)  # stored upload files aren't DB rows
    db.session.delete(ws)  # cascades to sources, documents, chats, messages
    db.session.commit()
    return redirect(url_for("index"))


def _new_chat_response(chat):
    """Either JSON (fetch — the sidebar/header New Chat, opened in place
    without a page reload) or a redirect (plain form post)."""
    url = url_for("chat_view", ws_id=chat.workspace_id, chat_id=chat.id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ws_id": chat.workspace_id, "chat_id": chat.id,
                        "url": url, "title": chat.title})
    return redirect(url)


@app.route("/w/<ws_id>/chats", methods=["POST"])
@login_required
def create_chat(ws_id):
    ws = get_owned_workspace(ws_id)
    title = (request.form.get("title") or "").strip()[:160]
    if not title:
        # No duplicate empty chats: if an untouched "New chat" already
        # exists, reuse it instead of piling up blanks (both New Chat
        # buttons hit this route, so this is the single dedup point).
        blank = next((c for c in ws.chats
                      if not c.messages and c.title == "New chat"), None)
        if blank:
            return _new_chat_response(blank)
        title = "New chat"
    new_chat = Chat(workspace_id=ws.id, title=title)
    db.session.add(new_chat)
    db.session.commit()
    return _new_chat_response(new_chat)


@app.route("/c/<chat_id>/rename", methods=["POST"])
@login_required
def rename_chat(chat_id):
    chat = get_owned_chat(chat_id)
    title = (request.form.get("title") or "").strip()[:160]
    if title:
        chat.title = title
        db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "title": chat.title})
    return redirect(request.referrer or
                    url_for("chat_view", ws_id=chat.workspace_id, chat_id=chat.id))


@app.route("/c/<chat_id>/pin", methods=["POST"])
@login_required
def pin_chat(chat_id):
    chat = get_owned_chat(chat_id)
    chat.pinned = not chat.pinned
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "pinned": chat.pinned})
    return redirect(request.referrer or
                    url_for("chat_view", ws_id=chat.workspace_id, chat_id=chat.id))


@app.route("/c/<chat_id>/duplicate", methods=["POST"])
@login_required
def duplicate_chat(chat_id):
    chat = get_owned_chat(chat_id)
    copy = Chat(workspace_id=chat.workspace_id,
                title=(chat.title[:150] + " (copy)")[:160])
    db.session.add(copy)
    db.session.flush()
    # Copy only the visible transcript (role + content). Orchestration/token
    # fields are intentionally left blank so usage stats aren't double-counted.
    for m in chat.messages:
        db.session.add(Message(chat_id=copy.id, role=m.role, content=m.content))
    db.session.commit()
    return redirect(url_for("chat_view", ws_id=copy.workspace_id, chat_id=copy.id))


@app.route("/c/<chat_id>/delete", methods=["POST"])
@login_required
def delete_chat(chat_id):
    chat = get_owned_chat(chat_id)
    ws_id = chat.workspace_id
    db.session.delete(chat)  # cascades to messages
    db.session.commit()
    nxt = (Chat.query.filter_by(workspace_id=ws_id)
           .order_by(Chat.pinned.desc(), Chat.updated_at.desc()).first())
    if nxt:
        return redirect(url_for("chat_view", ws_id=ws_id, chat_id=nxt.id))
    return redirect(url_for("workspace_view", ws_id=ws_id))


def _generate_title(text):
    """A short ChatGPT-style conversation title from the first message.
    Best-effort — the caller keeps its truncation fallback if this
    returns None (no API key, rate limit, etc.)."""
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content":
                       "Create a concise 2-5 word title summarizing what the "
                       "user wants. Title Case, no surrounding quotes, no "
                       "trailing punctuation."},
                      {"role": "user", "content": text[:500]}],
            max_tokens=16, temperature=0.2)
        title = (r.choices[0].message.content or "").strip().strip('"').strip()
        return title[:60] or None
    except Exception:
        return None


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
        naive = m.naive_tokens or 0
        reduction = round((naive - (m.context_tokens or 0)) * 100 / naive) \
            if naive else 0
        data["meta"] = {
            "intent": m.intent,
            "method": m.method,
            "matched": m.matched_keywords or [],
            "injected": m.injected_context or [],
            "withheld": m.withheld_context or [],
            "tokens_in": m.tokens_in,
            "tokens_out": m.tokens_out,
            "orchestration": {
                "capsules_used": m.capsules_used or [],
                "capsules_withheld": m.capsules_withheld or [],
                "chunks_used": m.chunks_used or [],
                "context_tokens": m.context_tokens or 0,
                "naive_tokens": naive,
                "reduction": reduction,
                "budget": RETRIEVAL_TOKEN_BUDGET,
                "summaries_injected": any(
                    c.get("summary_injected") for c in (m.capsules_used or [])),
            },
        }
    return data


@app.route("/w/<ws_id>/c/<chat_id>")
@login_required
def chat_view(ws_id, chat_id):
    ws = get_owned_workspace(ws_id)
    active_chat = db.session.get(Chat, chat_id)
    if not active_chat or active_chat.workspace_id != ws.id:
        abort(404)
    # Minimal, user-facing payload: messages + suggestions only. The
    # orchestration snapshot (intent, capsules, chunks, tokens) stays in
    # the database for the admin AI tools — it is never sent to the user.
    payload = {
        "messages": [{"role": m.role, "content": m.content}
                     for m in active_chat.messages],
        "suggestions": build_suggestions(ws.context_profile),
        "postUrl": url_for("post_message", chat_id=active_chat.id),
        "newChatUrl": url_for("create_chat", ws_id=ws.id),
        "connectUrl": url_for("connect_source", ws_id=ws.id),
        "uploadUrl": url_for("upload_to_workspace", ws_id=ws.id),
        "wsId": ws.id,
    }
    # If this chat once drew on knowledge context (repo/docs) but the
    # workspace has since had all of it disconnected, show a gentle notice.
    # The conversation stays fully readable — only future turns lose context.
    context_notice = (not ws.sources) and any(
        m.chunks_used or m.capsules_used for m in active_chat.messages)
    return render_template("chat.html", ws=ws, chat=active_chat, payload=payload,
                           has_knowledge=bool(ws.sources),
                           context_notice=context_notice,
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


def _looks_like_greeting(query):
    """Same rule the router uses — checked before embedding so greetings
    never trigger an API call."""
    intent, _score, greeting, _kw = detect_intent_rule_based(query)
    return intent == "general" and greeting is not None


def _chunks_by_ids(ids):
    """Chunk rows for the given ids, preserving the ranked order."""
    if not ids:
        return []
    by_id = {c.id: c for c in Chunk.query.filter(Chunk.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


def _sum_chunk_tokens(ids):
    if not ids:
        return 0
    rows = (Chunk.query.with_entities(Chunk.token_count)
            .filter(Chunk.id.in_(ids)).all())
    return sum(r.token_count or 0 for r in rows)


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

    # ── Capsule-first routing (Context Engineering core) ──
    # 1. Greeting fast path routes without embedding.
    # 2. Otherwise embed the query ONCE and reuse it for both semantic
    #    routing and scoped retrieval.
    capsules = Capsule.query.filter_by(workspace_id=ws.id).all()
    is_greeting = _looks_like_greeting(query)
    query_vec = None
    if not is_greeting:
        try:
            query_vec = embed_query(query)
        except Exception as e:
            return jsonify({"error": _friendly_llm_error(str(e))}), 502

    route = route_query(query, capsules, query_vec)
    intent, method = route["intent"], route["method"]
    matched = route.get("matched_keywords") or []
    injected, withheld = context_split(intent, discovered)
    routed_capsules = route["capsules"]
    include_summaries, summary_reason = wants_summaries(query, route)

    # ── Scoped retrieval: only inside the routed capsules ──
    # Global fallback (whole workspace) fires only when nothing routed.
    # When a summary is injected it carries the domain breadth, so fewer
    # supporting chunks are pulled — the summary substitutes for them.
    scope_ids = None if method == "global-fallback" else \
        capsule_chunk_ids([c.id for c in routed_capsules])
    ret_k = RETRIEVAL_K_WITH_SUMMARY if include_summaries else RETRIEVAL_K
    ret_budget = SUMMARY_CHUNK_BUDGET if include_summaries else RETRIEVAL_TOKEN_BUDGET
    scoped_hits = []
    naive_tokens = 0
    if not is_greeting and (scope_ids or method == "global-fallback"):
        scoped_hits = vector_search(ws.id, query_vec, k=ret_k,
                                    chunk_ids=scope_ids,
                                    token_budget=ret_budget)
        # The naive-RAG baseline we measure against: global top-k, no
        # capsule scoping, no budget cap.
        naive_hits = vector_search(ws.id, query_vec, k=NAIVE_RAG_K)
        naive_tokens = _sum_chunk_tokens([h[0] for h in naive_hits])
    scoped_chunk_objs = _chunks_by_ids([h[0] for h in scoped_hits])
    score_by_id = {cid: s for cid, s in scoped_hits}

    context_block = build_prompt_context(discovered, routed_capsules,
                                          include_summaries, scoped_chunk_objs)
    context_tokens = count_tokens(context_block) if context_block else 0

    # Inspector snapshot (frozen with the message).
    cap_used = [{"title": c.title,
                 "summary_injected": include_summaries and bool(c.summary),
                 "tokens": c.token_count} for c in routed_capsules]
    cap_withheld = [{"title": c.title, "reason": r}
                    for c, r in route["withheld"]]
    chunks_meta = [{"filename": c.document.filename,
                    "page": (c.meta or {}).get("page"),
                    "tokens": c.token_count,
                    "score": round(score_by_id.get(c.id, 0), 3)}
                   for c in scoped_chunk_objs]
    reduction = round((naive_tokens - context_tokens) * 100 / naive_tokens) \
        if naive_tokens else 0
    orchestration = {
        "capsules_used": cap_used, "capsules_withheld": cap_withheld,
        "chunks_used": chunks_meta, "context_tokens": context_tokens,
        "naive_tokens": naive_tokens, "reduction": reduction,
        "budget": RETRIEVAL_TOKEN_BUDGET, "summary_reason": summary_reason,
        "summaries_injected": include_summaries}

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
            db.session.add(Message(
                chat_id=chat_pk, role="assistant", content=answer,
                intent=intent, method=method, matched_keywords=matched,
                injected_context=injected, withheld_context=withheld,
                capsules_used=cap_used, capsules_withheld=cap_withheld,
                chunks_used=chunks_meta, context_tokens=context_tokens,
                naive_tokens=naive_tokens,
                tokens_in=tokens_in, tokens_out=tokens_out))
            chat_row.updated_at = utcnow()
            db.session.commit()

        yield _sse({"type": "meta", "intent": intent, "method": method,
                    "matched": matched, "injected": injected,
                    "withheld": withheld, "orchestration": orchestration})

        # Greeting fast path: instant, free, no API call
        if is_greeting and method == "rule-based":
            reply = GREETING_RESPONSES.get(route.get("greeting_key"),
                                           DEFAULT_GREETING_REPLY)
            for word in reply.split(" "):
                yield _sse({"type": "token", "content": word + " "})
            persist_assistant(reply, 0, 0)
            yield _sse({"type": "done", "tokens_in": 0, "tokens_out": 0,
                        "title": chat_row.title,
                        "usage": usage_stats(ws_pk),
                        "user_usage": usage_stats(user_id=owner_pk)})
            return

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
        # ChatGPT-style title from the first exchange (fallback already set).
        if needs_title:
            better = _generate_title(query)
            if better:
                chat_row.title = better
                db.session.commit()
        yield _sse({"type": "done", "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "title": chat_row.title,
                    "usage": usage_stats(ws_pk),
                    "user_usage": usage_stats(user_id=owner_pk)})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Per-user aggregates (Profile + Admin reuse) ──────────

def _user_counts(user_id):
    """Sources / repos / capsules / documents / workspaces owned by one
    user, each a single grouped COUNT rather than N per-row queries."""
    ws = Workspace.query.filter_by(user_id=user_id).count()
    src = (KnowledgeSource.query.join(Workspace)
           .filter(Workspace.user_id == user_id).count())
    repos = (KnowledgeSource.query.join(Workspace)
             .filter(Workspace.user_id == user_id,
                     KnowledgeSource.type == "github").count())
    cap = (Capsule.query.join(Workspace, Capsule.workspace_id == Workspace.id)
           .filter(Workspace.user_id == user_id).count())
    docs = (Document.query.join(Workspace, Document.workspace_id == Workspace.id)
            .filter(Workspace.user_id == user_id).count())
    return {"workspaces": ws, "sources": src, "repos": repos,
            "capsules": cap, "documents": docs}


def _storage_bytes(user_id):
    total = (db.session.query(func.coalesce(func.sum(Document.size_bytes), 0))
             .join(Workspace, Document.workspace_id == Workspace.id)
             .filter(Workspace.user_id == user_id).scalar())
    return int(total or 0)


def _today_tokens(user_id=None, scope_ids=None):
    """Assistant tokens (in+out) since midnight UTC for one user or scope."""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = (db.session.query(func.coalesce(
            func.sum(Message.tokens_in + Message.tokens_out), 0))
         .join(Chat, Message.chat_id == Chat.id)
         .join(Workspace, Chat.workspace_id == Workspace.id)
         .filter(Message.role == "assistant", Message.created_at >= start))
    if user_id:
        q = q.filter(Workspace.user_id == user_id)
    if scope_ids is not None:
        q = q.filter(Workspace.user_id.in_(scope_ids or ["\0"]))
    return int(q.scalar() or 0)


def user_summary(u, recent_limit=8):
    stats = usage_stats(user_id=u.id)
    counts = _user_counts(u.id)
    recent = (Chat.query.join(Workspace)
              .filter(Workspace.user_id == u.id)
              .order_by(Chat.updated_at.desc()).limit(recent_limit).all())
    conversations = (Chat.query.join(Workspace)
                     .filter(Workspace.user_id == u.id).count())
    last_activity = recent[0].updated_at if recent else u.last_login_at
    return {"user": u, "stats": stats, "counts": counts, "recent": recent,
            "conversations": conversations, "last_activity": last_activity}


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        check_csrf()
        name = (request.form.get("name") or "").strip()[:120]
        company = (request.form.get("company_name") or "").strip()[:120]
        if name:
            g.user.name = name
        g.user.company_name = company or None
        db.session.commit()
        return redirect(url_for("profile"))
    return render_template("profile.html", active_nav="profile",
                           storage=_storage_bytes(g.user.id),
                           **user_summary(g.user))


@app.route("/profile/delete", methods=["POST"])
@login_required
def delete_account():
    check_csrf()
    u = g.user
    # Erase the user's stored files, then the row (workspaces/chats/docs
    # cascade via the ORM relationships).
    for doc in (Document.query.join(Workspace, Document.workspace_id == Workspace.id)
                .filter(Workspace.user_id == u.id).all()):
        if doc.storage_key:
            storage.delete(doc.storage_key)
    db.session.delete(u)
    db.session.commit()
    session.clear()
    return redirect(url_for("index"))


@app.route("/usage")
@login_required
def usage_view():
    stats = usage_stats(user_id=g.user.id)
    counts = _user_counts(g.user.id)
    # 14-day per-day token series for this user (Python bucketing, DB-agnostic).
    now = utcnow()
    since = now - timedelta(days=14)
    rows = (db.session.query(Message.created_at,
                             Message.tokens_in, Message.tokens_out)
            .join(Chat, Message.chat_id == Chat.id)
            .join(Workspace, Chat.workspace_id == Workspace.id)
            .filter(Workspace.user_id == g.user.id,
                    Message.role == "assistant",
                    Message.created_at >= since).all())
    daily = {}
    for created, ti, to in rows:
        d = _as_utc(created).date()
        daily[d] = daily.get(d, 0) + (ti or 0) + (to or 0)
    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    series = [{"label": d.strftime("%b %d"), "tokens": daily.get(d, 0)}
              for d in days]
    return render_template("usage.html", active_nav="usage", stats=stats,
                           counts=counts, storage=_storage_bytes(g.user.id),
                           today=_today_tokens(user_id=g.user.id), series=series)


# ── Admin dashboard ──────────────────────────────────────
# Every route is behind admin_required (Super + Company Admin) or
# super_admin_required (platform-wide). Company Admins see only their
# own company, enforced by _scope_ids() threaded through every query.

ACTIVE_WINDOW = timedelta(days=30)
APP_STARTED = utcnow()

# Which roles each admin role may assign. Company Admins can never mint
# a Super Admin, and never touch a Super Admin account.
ASSIGNABLE_ROLES = {
    "super_admin": ["super_admin", "company_admin", "employee"],
    "company_admin": ["company_admin", "employee"],
}


def _as_utc(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _scope_ids():
    """User ids the current admin may see. None = whole platform (Super
    Admin); a list for a Company Admin (their company's members)."""
    return _company_user_ids(g.user)


def _scope_label():
    if g.user.is_super_admin:
        return "Platform-wide"
    return (g.user.company.name if g.user.company else "Your account") + " (company)"


def _platform_totals(scope_ids):
    stats = usage_stats(scope_ids=scope_ids)
    active_since = utcnow() - ACTIVE_WINDOW
    uq = User.query
    wq = Workspace.query
    cq = Chat.query.join(Workspace, Chat.workspace_id == Workspace.id)
    dq = Document.query.join(Workspace, Document.workspace_id == Workspace.id)
    sq = KnowledgeSource.query.join(Workspace,
                                    KnowledgeSource.workspace_id == Workspace.id)
    kq = Capsule.query.join(Workspace, Capsule.workspace_id == Workspace.id)
    if scope_ids is not None:
        ids = scope_ids or ["\0"]
        uq = uq.filter(User.id.in_(ids))
        wq = wq.filter(Workspace.user_id.in_(ids))
        cq = cq.filter(Workspace.user_id.in_(ids))
        dq = dq.filter(Workspace.user_id.in_(ids))
        sq = sq.filter(Workspace.user_id.in_(ids))
        kq = kq.filter(Workspace.user_id.in_(ids))
    return {
        "users": uq.count(),
        "active_users": uq.filter(User.last_login_at >= active_since).count(),
        "companies": Company.query.count(),
        "workspaces": wq.count(),
        "conversations": cq.count(),
        "documents": dq.count(),
        "sources": sq.count(),
        "capsules": kq.count(),
        "stats": stats,
    }


def _admin_user_rows(search=None, scope_ids=None):
    q = User.query
    if scope_ids is not None:
        q = q.filter(User.id.in_(scope_ids or ["\0"]))
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(User.email.ilike(like), User.name.ilike(like)))
    users = q.order_by(User.created_at.asc()).all()
    companies = {c.id: c.name for c in Company.query.all()}
    ws_counts = dict(db.session.query(Workspace.user_id, func.count(Workspace.id))
                     .group_by(Workspace.user_id).all())
    chat_counts = dict(db.session.query(Workspace.user_id, func.count(Chat.id))
                       .join(Chat, Chat.workspace_id == Workspace.id)
                       .group_by(Workspace.user_id).all())
    tok = (db.session.query(Workspace.user_id,
                            func.coalesce(func.sum(Message.tokens_in), 0),
                            func.coalesce(func.sum(Message.tokens_out), 0))
           .join(Chat, Chat.workspace_id == Workspace.id)
           .join(Message, Message.chat_id == Chat.id)
           .filter(Message.role == "assistant")
           .group_by(Workspace.user_id).all())
    tok_map = {uid: (ti, to) for uid, ti, to in tok}
    rows = []
    for u in users:
        ti, to = tok_map.get(u.id, (0, 0))
        rows.append({"u": u,
                     "company": companies.get(u.company_id),
                     "workspaces": ws_counts.get(u.id, 0),
                     "conversations": chat_counts.get(u.id, 0),
                     "tokens": int(ti) + int(to),
                     "cost": estimate_cost(ti, to)})
    return rows


def _admin_workspace_rows(scope_ids=None):
    wq = Workspace.query
    if scope_ids is not None:
        wq = wq.filter(Workspace.user_id.in_(scope_ids or ["\0"]))
    workspaces = wq.all()
    owners = {u.id: u for u in User.query.all()}
    src_c = dict(db.session.query(KnowledgeSource.workspace_id,
                                  func.count(KnowledgeSource.id))
                 .group_by(KnowledgeSource.workspace_id).all())
    cap_c = dict(db.session.query(Capsule.workspace_id, func.count(Capsule.id))
                 .group_by(Capsule.workspace_id).all())
    doc_c = dict(db.session.query(Document.workspace_id, func.count(Document.id))
                 .group_by(Document.workspace_id).all())
    chat_c = dict(db.session.query(Chat.workspace_id, func.count(Chat.id))
                  .group_by(Chat.workspace_id).all())
    last_c = dict(db.session.query(Chat.workspace_id, func.max(Chat.updated_at))
                  .group_by(Chat.workspace_id).all())
    agg = (db.session.query(Chat.workspace_id,
                            func.coalesce(func.sum(Message.tokens_in), 0),
                            func.coalesce(func.sum(Message.tokens_out), 0),
                            func.coalesce(func.sum(Message.context_tokens), 0),
                            func.coalesce(func.sum(Message.naive_tokens), 0))
           .join(Message, Message.chat_id == Chat.id)
           .filter(Message.role == "assistant")
           .group_by(Chat.workspace_id).all())
    agg_map = {wid: (ti, to, ct, nt) for wid, ti, to, ct, nt in agg}
    rows = []
    for w in workspaces:
        ti, to, ct, nt = agg_map.get(w.id, (0, 0, 0, 0))
        reduction = round((nt - ct) * 100 / nt) if nt else 0
        rows.append({"ws": w, "owner": owners.get(w.user_id),
                     "sources": src_c.get(w.id, 0),
                     "capsules": cap_c.get(w.id, 0),
                     "documents": doc_c.get(w.id, 0),
                     "conversations": chat_c.get(w.id, 0),
                     "cost": estimate_cost(ti, to),
                     "reduction": reduction,
                     "last_activity": last_c.get(w.id)})
    rows.sort(key=lambda r: (_as_utc(r["last_activity"]) or datetime.min.replace(
        tzinfo=timezone.utc)), reverse=True)
    return rows


def _platform_analytics(scope_ids):
    """DB-agnostic time-bucketing: pull assistant-message token rows in
    the window and aggregate by day/month in Python (avoids SQLite vs
    Postgres date-function differences — the production seam)."""
    now = utcnow()

    def _rows(since):
        q = (db.session.query(Message.created_at, Message.tokens_in,
                              Message.tokens_out)
             .filter(Message.role == "assistant", Message.created_at >= since))
        if scope_ids is not None:
            q = (q.join(Chat, Message.chat_id == Chat.id)
                  .join(Workspace, Chat.workspace_id == Workspace.id)
                  .filter(Workspace.user_id.in_(scope_ids or ["\0"])))
        return q.all()

    rows = _rows(now - timedelta(days=30))
    daily = {}
    for created, ti, to in rows:
        d = _as_utc(created).date()
        b = daily.setdefault(d, [0, 0])
        b[0] += ti or 0
        b[1] += to or 0
    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    daily_series = [{"label": d.strftime("%b %d"),
                     "tokens": sum(daily.get(d, [0, 0])),
                     "cost": estimate_cost(*daily.get(d, [0, 0]))}
                    for d in days]

    m_rows = _rows(now - timedelta(days=185))
    monthly = {}
    for created, ti, to in m_rows:
        key = _as_utc(created).strftime("%Y-%m")
        b = monthly.setdefault(key, [0, 0])
        b[0] += ti or 0
        b[1] += to or 0
    months = []
    cur = now.replace(day=1)
    for _ in range(6):
        key = cur.strftime("%Y-%m")
        months.append({"label": cur.strftime("%b %Y"),
                       "tokens": sum(monthly.get(key, [0, 0])),
                       "cost": estimate_cost(*monthly.get(key, [0, 0]))})
        cur = (cur - timedelta(days=1)).replace(day=1)
    months.reverse()

    stats = usage_stats(scope_ids=scope_ids)
    # Single-model deployment: chat runs on OPENAI_MODEL. Present it as a
    # model-usage / provider-cost row (honest — embeddings aren't metered).
    model_usage = [{"model": OPENAI_MODEL, "provider": "OpenAI",
                    "requests": stats["queries"], "tokens": stats["tokens"],
                    "cost": stats["cost"]}]
    top_users = sorted(_admin_user_rows(scope_ids=scope_ids),
                       key=lambda r: r["tokens"], reverse=True)[:5]
    top_workspaces = sorted(_admin_workspace_rows(scope_ids=scope_ids),
                            key=lambda r: r["cost"], reverse=True)[:5]
    return {"daily": daily_series, "monthly": months, "model_usage": model_usage,
            "top_users": top_users, "top_workspaces": top_workspaces}


@app.route("/admin")
@admin_required
def admin_overview():
    return render_template("admin/overview.html", active_nav="admin",
                           admin_tab="overview", scope=_scope_label(),
                           totals=_platform_totals(_scope_ids()))


@app.route("/admin/users")
@admin_required
def admin_users():
    search = request.args.get("q", "").strip()
    return render_template("admin/users.html", active_nav="admin",
                           admin_tab="users", search=search,
                           assignable=ASSIGNABLE_ROLES.get(g.user.role, []),
                           companies=Company.query.order_by(Company.name).all(),
                           rows=_admin_user_rows(search, _scope_ids()))


@app.route("/admin/users/<user_id>")
@admin_required
def admin_user_detail(user_id):
    u = _manage_target(user_id, act=False)
    companies = {c.id: c.name for c in Company.query.all()}
    return render_template("admin/user_detail.html", active_nav="admin",
                           admin_tab="users", company=companies.get(u.company_id),
                           companies_list=Company.query.order_by(Company.name).all(),
                           **user_summary(u, recent_limit=10))


def _manage_target(user_id, act=True):
    """Fetch a user the current admin is allowed to view/manage. 404 if
    missing, 403 if outside a Company Admin's company or a Super Admin
    account they may not touch."""
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    scope = _scope_ids()
    if scope is not None and u.id not in scope:
        abort(403)
    if act and g.user.is_company_admin and u.is_super_admin:
        abort(403)
    return u


@app.route("/admin/users/<user_id>/deactivate", methods=["POST"])
@admin_required
def admin_deactivate_user(user_id):
    check_csrf()
    u = _manage_target(user_id)
    if u.id != g.user.id:
        u.active = False
        db.session.commit()
        audit("deactivate_user", u.email)
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/users/<user_id>/activate", methods=["POST"])
@admin_required
def admin_activate_user(user_id):
    check_csrf()
    u = _manage_target(user_id)
    u.active = True
    db.session.commit()
    audit("activate_user", u.email)
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/users/<user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id):
    check_csrf()
    u = _manage_target(user_id)
    role = request.form.get("role", "")
    if u.id != g.user.id and role in ASSIGNABLE_ROLES.get(g.user.role, []):
        u.role = role
        db.session.commit()
        audit("set_role", f"{u.email} → {role}")
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/workspaces")
@admin_required
def admin_workspaces():
    return render_template("admin/workspaces.html", active_nav="admin",
                           admin_tab="workspaces", scope=_scope_label(),
                           rows=_admin_workspace_rows(_scope_ids()))


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    return render_template("admin/analytics.html", active_nav="admin",
                           admin_tab="analytics", scope=_scope_label(),
                           data=_platform_analytics(_scope_ids()))


# ── Companies (Super Admin only) ──
# A company groups users; workspaces/knowledge belong to those users, not
# to the company directly. So a company's footprint is aggregated from its
# members' workspaces, and deleting a company moves members to Unassigned
# (never deleting their data).
def _company_metrics(member_ids):
    if not member_ids:
        return {"workspaces": 0, "documents": 0, "repositories": 0,
                "conversations": 0, "storage": 0, "tokens": 0, "cost": 0.0}
    inw = Workspace.user_id.in_(member_ids)
    documents = (Document.query.join(Workspace, Document.workspace_id == Workspace.id)
                 .filter(inw).count())
    repositories = (KnowledgeSource.query
                    .join(Workspace, KnowledgeSource.workspace_id == Workspace.id)
                    .filter(inw, KnowledgeSource.type == "github").count())
    conversations = (Chat.query.join(Workspace, Chat.workspace_id == Workspace.id)
                     .filter(inw).count())
    storage = (db.session.query(func.coalesce(func.sum(Document.size_bytes), 0))
               .join(Workspace, Document.workspace_id == Workspace.id)
               .filter(inw).scalar()) or 0
    ti, to = (db.session.query(
                func.coalesce(func.sum(Message.tokens_in), 0),
                func.coalesce(func.sum(Message.tokens_out), 0))
              .join(Chat, Message.chat_id == Chat.id)
              .join(Workspace, Chat.workspace_id == Workspace.id)
              .filter(inw, Message.role == "assistant").one())
    return {"workspaces": Workspace.query.filter(inw).count(),
            "documents": documents, "repositories": repositories,
            "conversations": conversations, "storage": int(storage),
            "tokens": int(ti) + int(to), "cost": estimate_cost(ti, to)}


def _company_admin(members):
    admins = [u for u in members if u.role == "company_admin"]
    return admins[0] if admins else None


def _company_rows():
    companies = Company.query.order_by(Company.name.asc()).all()
    members_by = {}
    for u in User.query.all():
        members_by.setdefault(u.company_id, []).append(u)
    rows = []
    for c in companies:
        members = members_by.get(c.id, [])
        rows.append({"c": c, "admin": _company_admin(members),
                     "members": len(members),
                     **_company_metrics([u.id for u in members])})
    return rows, len(members_by.get(None, []))


@app.route("/admin/companies")
@super_admin_required
def admin_companies():
    rows, unassigned = _company_rows()
    return render_template("admin/companies.html", active_nav="admin",
                           admin_tab="companies", rows=rows, unassigned=unassigned)


@app.route("/admin/companies/<company_id>")
@super_admin_required
def admin_company_detail(company_id):
    c = db.session.get(Company, company_id)
    if not c:
        abort(404)
    members = sorted(c.members, key=lambda u: (u.role != "company_admin",
                                               (u.email or "").lower()))
    member_ids = [u.id for u in members]
    ws_rows = _admin_workspace_rows(scope_ids=member_ids) if member_ids else []
    return render_template("admin/company_detail.html", active_nav="admin",
                           admin_tab="companies", c=c, members=members,
                           admin=_company_admin(members),
                           metrics=_company_metrics(member_ids),
                           workspaces=ws_rows[:20])


@app.route("/admin/companies/create", methods=["POST"])
@super_admin_required
def admin_create_company():
    check_csrf()
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        flash("Company name can't be empty.", "bad")
    elif Company.query.filter(func.lower(Company.name) == name.lower()).first():
        flash(f"A company named “{name}” already exists.", "bad")
    else:
        db.session.add(Company(name=name))
        db.session.commit()
        audit("create_company", name)
        flash(f"Company “{name}” created.", "good")
    return redirect(url_for("admin_companies"))


@app.route("/admin/companies/<company_id>/edit", methods=["POST"])
@super_admin_required
def admin_edit_company(company_id):
    check_csrf()
    c = db.session.get(Company, company_id)
    if not c:
        abort(404)
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        flash("Company name can't be empty.", "bad")
        return redirect(url_for("admin_company_detail", company_id=c.id))
    if Company.query.filter(func.lower(Company.name) == name.lower(),
                            Company.id != c.id).first():
        flash(f"Another company already uses the name “{name}”.", "bad")
        return redirect(url_for("admin_company_detail", company_id=c.id))
    changed = []
    if name != c.name:
        changed.append("renamed")
        c.name = name
    # Assign/change company admin: chosen member becomes company_admin;
    # any previous admin of THIS company is demoted to employee.
    admin_id = request.form.get("admin_id") or ""
    member_ids = {u.id for u in c.members}
    if admin_id and admin_id in member_ids:
        target = db.session.get(User, admin_id)
        if target and not target.is_super_admin and target.role != "company_admin":
            for u in c.members:
                if u.role == "company_admin" and u.id != admin_id:
                    u.role = "employee"
            target.role = "company_admin"
            changed.append("admin reassigned")
    db.session.commit()
    if changed:
        audit("edit_company", f"{c.name} ({', '.join(changed)})")
        flash("Company updated.", "good")
    return redirect(url_for("admin_company_detail", company_id=c.id))


@app.route("/admin/companies/<company_id>/delete", methods=["POST"])
@super_admin_required
def admin_delete_company(company_id):
    check_csrf()
    c = db.session.get(Company, company_id)
    if not c:
        abort(404)
    name = c.name
    # Move members to Unassigned and demote company admins — users and all
    # their workspaces/knowledge are preserved. Null the FK refs before the
    # delete so it's safe on an FK-enforcing database (Postgres).
    moved = 0
    for u in list(c.members):
        u.company_id = None
        if u.role == "company_admin":
            u.role = "employee"
        moved += 1
    db.session.flush()
    db.session.delete(c)
    db.session.commit()
    audit("delete_company", f"{name}" + (f" · {moved} user(s) → unassigned"
                                         if moved else ""))
    flash(f"Company “{name}” deleted." + (f" {moved} user(s) moved to "
          "Unassigned." if moved else ""), "good")
    return redirect(url_for("admin_companies"))


@app.route("/admin/users/<user_id>/company", methods=["POST"])
@super_admin_required
def admin_assign_company(user_id):
    check_csrf()
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    cid = request.form.get("company_id") or None
    u.company_id = cid
    db.session.commit()
    audit("assign_company", f"{u.email} → {cid or 'none'}")
    return redirect(request.referrer or url_for("admin_companies"))


# ── AI / Developer tools (the moved internals live here) ──
def _ai_diagnostics(ws_id):
    """Read-only retrieval-engine health for one workspace (Super Admin
    only). Does not touch RAG/embeddings — it just reports their state."""
    sources = KnowledgeSource.query.filter_by(workspace_id=ws_id).count()
    repos = KnowledgeSource.query.filter_by(workspace_id=ws_id,
                                            type="github").count()
    documents = Document.query.filter_by(workspace_id=ws_id).count()
    chunks = Chunk.query.filter_by(workspace_id=ws_id).count()
    embedded = Chunk.query.filter(Chunk.workspace_id == ws_id,
                                  Chunk.embedding.isnot(None)).count()
    capsules = Capsule.query.filter_by(workspace_id=ws_id).count()
    stale = Capsule.query.filter(Capsule.workspace_id == ws_id,
                                 Capsule.status != "fresh").count()
    last = (db.session.query(func.max(KnowledgeSource.last_ingested_at))
            .filter(KnowledgeSource.workspace_id == ws_id).scalar())
    coverage = round(embedded * 100 / chunks) if chunks else 0
    if chunks == 0:
        health = ("muted", "No index")
        index_status = ("muted", "Empty")
    elif embedded < chunks or stale:
        health = ("warn", "Degraded")
        index_status = ("warn", "Indexing")
    else:
        health = ("good", "Healthy")
        index_status = ("good", "Indexed")
    return {"sources": sources, "repositories": repos, "documents": documents,
            "chunks": chunks, "embedded": embedded, "capsules": capsules,
            "coverage": coverage, "last_indexed": last,
            "health": health, "index_status": index_status}


@app.route("/admin/ai")
@super_admin_required
def admin_ai_tools():
    """AI Diagnostics (Super Admin only): retrieval-engine health, indexed
    knowledge, and the Knowledge Index for any workspace. Read-only."""
    rows = _admin_workspace_rows(_scope_ids())
    ws_id = request.args.get("ws")
    selected = next((r for r in rows if r["ws"].id == ws_id), None) \
        or (rows[0] if rows else None)
    detail = None
    if selected:
        ws = selected["ws"]
        cells = [(k, OVERVIEW_LABELS.get(k, k), ws.context_profile.get(k))
                 for k in OVERVIEW_KEYS if ws.context_profile.get(k)]
        detail = {"ws": ws, "cells": cells, "capsules": _capsule_cards(ws.id),
                  "diag": _ai_diagnostics(ws.id)}
    return render_template("admin/ai_tools.html", active_nav="admin",
                           admin_tab="ai", rows=rows, detail=detail)


# ── Audit log & system (Super Admin only) ──
@app.route("/admin/audit")
@super_admin_required
def admin_audit():
    logs = (AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all())
    return render_template("admin/audit.html", active_nav="admin",
                           admin_tab="audit", logs=logs)


@app.route("/admin/system")
@super_admin_required
def admin_system():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    config = [
        ("Chat model", OPENAI_MODEL, True),
        ("OpenAI API key", "Configured" if os.getenv("OPENAI_API_KEY")
         else "Not set", bool(os.getenv("OPENAI_API_KEY"))),
        ("GitHub token", "Configured" if os.getenv("GITHUB_TOKEN")
         else "Not set", bool(os.getenv("GITHUB_TOKEN"))),
        ("GitHub OAuth login", "Enabled" if os.getenv("GITHUB_OAUTH_CLIENT_ID")
         else "Disabled", bool(os.getenv("GITHUB_OAUTH_CLIENT_ID"))),
        ("Google OAuth login", "Enabled" if os.getenv("GOOGLE_OAUTH_CLIENT_ID")
         else "Disabled", bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID"))),
        ("Email + password login", "Enabled", True),
    ]
    uptime = utcnow() - APP_STARTED
    health = {
        "db_ok": db_ok,
        "uptime": str(timedelta(seconds=int(uptime.total_seconds()))),
        "users": User.query.count(),
        "workspaces": Workspace.query.count(),
        "messages": Message.query.count(),
    }
    return render_template("admin/system.html", active_nav="admin",
                           admin_tab="system", config=config, health=health)


if __name__ == "__main__":
    # Debug must never default on — the Werkzeug debugger is an RCE console.
    # Opt in locally with FLASK_DEBUG=1; production leaves it unset.
    debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes", "on")
    app.run(host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "5000")),
            debug=debug)
