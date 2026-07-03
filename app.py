import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import queue
import re
import threading
from datetime import datetime, timezone

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, session, Response, stream_with_context, abort)
from dotenv import load_dotenv
from sqlalchemy import func, case

from app.models import db, Workspace, Chat, Message, utcnow
from app.github_discovery import discover_repo_context
from app.context_builder import (CONTEXT_LABELS, INTENT_CONTEXT_MAP,
                                 build_context, build_enriched_prompt)
from app.intent_engine import get_intent, GREETING_RESPONSES
from app.config import OPENAI_MODEL
from openai import OpenAI

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "contextflow-dev-only")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///contextflow.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
with app.app_context():
    db.create_all()

GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

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

# A rule-based classification avoids one OpenAI classifier call
# (~60 prompt + ~10 completion tokens). Used for the savings estimate.
CLASSIFIER_TOKENS_SAVED = 70
HISTORY_LIMIT = 12  # prior messages included per prompt (6 exchanges)


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


def usage_stats(workspace_id=None):
    q = db.session.query(
        func.count(Message.id),
        func.coalesce(func.sum(Message.tokens_in + Message.tokens_out), 0),
        func.coalesce(func.sum(case((Message.method == "rule-based", 1), else_=0)), 0),
    ).filter(Message.role == "assistant")
    if workspace_id:
        q = q.join(Chat, Message.chat_id == Chat.id).filter(Chat.workspace_id == workspace_id)
    queries, tokens, free = q.one()
    free_pct = round(free * 100 / queries) if queries else 0
    return {"queries": queries, "tokens": int(tokens), "free_pct": free_pct,
            "savings": int(free) * CLASSIFIER_TOKENS_SAVED}


@app.context_processor
def sidebar_context():
    workspaces = Workspace.query.order_by(Workspace.created_at.asc()).all()
    return {"sidebar_workspaces": workspaces, "global_usage": usage_stats()}


# ── Navigation ───────────────────────────────────────────

@app.route("/")
def index():
    last_chat = Chat.query.order_by(Chat.updated_at.desc()).first()
    if last_chat:
        return redirect(url_for("chat_view", ws_id=last_chat.workspace_id,
                                 chat_id=last_chat.id))
    ws = Workspace.query.order_by(Workspace.created_at.desc()).first()
    if ws:
        return redirect(url_for("workspace_view", ws_id=ws.id))
    return redirect(url_for("connect"))


@app.route("/setup")
def setup():
    return redirect(url_for("connect"))


# ── Connect repository ───────────────────────────────────

@app.route("/connect")
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
def scan_workspace():
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip().rstrip("/")
    if not GITHUB_URL_RE.match(repo_url):
        return jsonify({"error": "Enter a full GitHub repository URL, "
                                 "like https://github.com/org/repo"}), 400

    existing = Workspace.query.filter_by(repo_url=repo_url).first()
    if existing:
        return jsonify({"redirect": url_for("workspace_view", ws_id=existing.id),
                        "existing": True})

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

        ws = Workspace(repo_url=repo_url,
                       name=repo_url.split("/")[-1],
                       discovered_context=outcome["context"],
                       last_scanned_at=utcnow())
        db.session.add(ws)
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
def workspace_view(ws_id):
    ws = db.session.get(Workspace, ws_id)
    if not ws:
        abort(404)
    cells = [(key, OVERVIEW_LABELS.get(key, key), ws.discovered_context.get(key))
             for key in OVERVIEW_KEYS]
    return render_template("workspace.html", ws=ws, cells=cells,
                           ws_usage=usage_stats(ws.id), active_ws=ws)


@app.route("/w/<ws_id>/rescan", methods=["POST"])
def rescan_workspace(ws_id):
    ws = db.session.get(Workspace, ws_id)
    if not ws:
        abort(404)
    try:
        context = discover_repo_context(ws.repo_url)
    except Exception as e:
        return jsonify({"error": _friendly_scan_error(str(e))}), 502
    ws.discovered_context = context
    ws.last_scanned_at = utcnow()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/w/<ws_id>/delete", methods=["POST"])
def delete_workspace(ws_id):
    ws = db.session.get(Workspace, ws_id)
    if not ws:
        abort(404)
    db.session.delete(ws)  # cascades to chats and messages
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/w/<ws_id>/chats", methods=["POST"])
def create_chat(ws_id):
    ws = db.session.get(Workspace, ws_id)
    if not ws:
        abort(404)
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
def chat_view(ws_id, chat_id):
    ws = db.session.get(Workspace, ws_id)
    active_chat = db.session.get(Chat, chat_id)
    if not ws or not active_chat or active_chat.workspace_id != ws.id:
        abort(404)
    payload = {
        "messages": [serialize_message(m) for m in active_chat.messages],
        "usage": usage_stats(ws.id),
        "suggestions": build_suggestions(ws.discovered_context),
        "postUrl": url_for("post_message", chat_id=active_chat.id),
    }
    context_keys = [k for k in ws.discovered_context if k != "repo"]
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
def post_message(chat_id):
    active_chat = db.session.get(Chat, chat_id)
    if not active_chat:
        abort(404)
    ws = active_chat.workspace
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty message"}), 400

    discovered = ws.discovered_context or {}
    intent_result = get_intent(query)
    intent, method = intent_result["intent"], intent_result["method"]
    matched = intent_result.get("matched_keywords") or []
    injected, withheld = context_split(intent, discovered)

    is_first = len(active_chat.messages) == 0
    needs_title = is_first and active_chat.title == "New chat"
    history = [{"role": m.role, "content": m.content}
               for m in active_chat.messages[-HISTORY_LIMIT:]]
    chat_pk, ws_pk = active_chat.id, ws.id

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
                        "title": chat_row.title, "usage": usage_stats(ws_pk)})
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
                    "title": chat_row.title, "usage": usage_stats(ws_pk)})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Legacy (single-repo chat; removed in increment 3) ────

conversation_history = []
token_stats = {
    "total_queries": 0,
    "total_tokens": 0,
    "rule_based_count": 0,
    "openai_classified_count": 0
}

DEFAULT_GREETING_REPLY = "Hello! I'm ContextFlow. How can I help with your infrastructure today?"


@app.route("/legacy")
def legacy_index():
    discovered = session.get("discovered_context", {})
    return render_template("index.html",
                           discovered=discovered,
                           stats=token_stats,
                           history=conversation_history)


@app.route("/disconnect", methods=["POST"])
def disconnect():
    session.pop("discovered_context", None)
    return jsonify({"status": "disconnected"})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_query = data.get("query", "")

    if not user_query:
        return jsonify({"error": "No query provided"}), 400

    discovered = session.get("discovered_context", {})
    intent_result = get_intent(user_query)

    # ── Fast path: greetings get instant, free, hardcoded replies ──
    if intent_result["intent"] == "general" and intent_result["method"] == "rule-based":
        def generate_instant():
            meta = {"type": "meta", "intent": "general", "method": "rule-based"}
            yield f"data: {json.dumps(meta)}\n\n"

            greeting_key = intent_result.get("greeting_key")
            reply = GREETING_RESPONSES.get(greeting_key, DEFAULT_GREETING_REPLY)

            for word in reply.split(" "):
                payload = {"type": "token", "content": word + " "}
                yield f"data: {json.dumps(payload)}\n\n"

            token_stats["total_queries"] += 1
            token_stats["rule_based_count"] += 1
            done = {"type": "done", "total_tokens": 0}
            yield f"data: {json.dumps(done)}\n\n"

        return Response(
            stream_with_context(generate_instant()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # ── Normal path: real questions go to OpenAI ──
    enriched_prompt = build_enriched_prompt(
        user_query,
        intent_result["intent"],
        discovered
    )

    def generate():
        meta = {
            "type": "meta",
            "intent": intent_result["intent"],
            "method": intent_result["method"]
        }
        yield f"data: {json.dumps(meta)}\n\n"

        total_tokens = 0
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        try:
            with client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a senior cloud engineer assistant. "
                                   "Provide specific, practical answers based on "
                                   "the organizational context provided."
                    },
                    {
                        "role": "user",
                        "content": enriched_prompt
                    }
                ],
                max_tokens=1000,
                temperature=0.3,
                stream=True
            ) as stream:
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        total_tokens += 1
                        payload = {"type": "token", "content": token}
                        yield f"data: {json.dumps(payload)}\n\n"

        except Exception as e:
            error_str = str(e).lower()

            if "rate_limit" in error_str:
                error_msg = "⚠️ Too many requests right now. Please wait a moment and try again."
            elif "insufficient_quota" in error_str or "quota" in error_str:
                error_msg = "⚠️ Service temporarily unavailable — budget limit reached. Please contact the admin."
            elif "invalid_api_key" in error_str or "authentication" in error_str:
                error_msg = "⚠️ API configuration error. Please contact the admin."
            else:
                error_msg = "⚠️ I'm temporarily unable to process requests. Please try again in a few minutes."

            payload = {"type": "token", "content": error_msg}
            yield f"data: {json.dumps(payload)}\n\n"

            done = {"type": "done", "total_tokens": 0}
            yield f"data: {json.dumps(done)}\n\n"
            return

        token_stats["total_queries"] += 1
        token_stats["total_tokens"] += total_tokens
        if intent_result["method"] == "rule-based":
            token_stats["rule_based_count"] += 1
        else:
            token_stats["openai_classified_count"] += 1

        done = {"type": "done", "total_tokens": total_tokens}
        yield f"data: {json.dumps(done)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.route("/stats")
def stats():
    return jsonify({
        "stats": token_stats,
        "discovered_context": session.get("discovered_context", {})
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
