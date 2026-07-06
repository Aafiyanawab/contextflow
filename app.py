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

from app.models import db, Workspace, KnowledgeSource, Chat, Message, utcnow
from app.github_discovery import discover_repo_context
from app.context_builder import CONTEXT_LABELS, INTENT_CONTEXT_MAP, build_context
from app.intent_engine import get_intent, GREETING_RESPONSES
from app.config import OPENAI_MODEL
from app.auth import init_auth, login_required
from app.ratelimit import rate_limit
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
# Largest legitimate body is a chat message (4,000 chars of JSON);
# 64 KB bounds memory per request with room to spare.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
db.init_app(app)
# Schema is managed by Alembic migrations now (flask db upgrade), not
# create_all. render_as_batch is required for SQLite column drops.
migrate = Migrate(app, db, render_as_batch=True)

init_auth(app)

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
        db.session.add(KnowledgeSource(workspace_id=ws.id, type="github",
                                       name=ws.name, uri=repo_url,
                                       status="ready",
                                       profile=outcome["context"],
                                       last_ingested_at=utcnow()))
        db.session.commit()
        done = {"type": "done",
                "redirect": url_for("workspace_view", ws_id=ws.id),
                "found": len(outcome["context"]) - 1}  # minus the repo key
        yield f"data: {json.dumps(done)}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Workspace ────────────────────────────────────────────

@app.route("/w/<ws_id>")
@login_required
def workspace_view(ws_id):
    ws = get_owned_workspace(ws_id)
    cells = [(key, OVERVIEW_LABELS.get(key, key), ws.context_profile.get(key))
             for key in OVERVIEW_KEYS]
    return render_template("workspace.html", ws=ws, cells=cells,
                           ws_usage=usage_stats(ws.id), active_ws=ws)


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
    return jsonify({"ok": True})


@app.route("/w/<ws_id>/delete", methods=["POST"])
@login_required
def delete_workspace(ws_id):
    ws = get_owned_workspace(ws_id)
    db.session.delete(ws)  # cascades to chats and messages
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
