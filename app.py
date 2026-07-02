import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from app.intent_engine import get_intent, GREETING_RESPONSES
from app.context_builder import build_enriched_prompt
from app.github_discovery import discover_repo_context
from openai import OpenAI
from dotenv import load_dotenv
import json

load_dotenv()

app = Flask(__name__)
app.secret_key = "contextflow-secret-key"

conversation_history = []
token_stats = {
    "total_queries": 0,
    "total_tokens": 0,
    "rule_based_count": 0,
    "openai_classified_count": 0
}

DEFAULT_GREETING_REPLY = "Hello! I'm ContextFlow. How can I help with your infrastructure today?"


@app.route("/")
def index():
    discovered = session.get("discovered_context", {})
    return render_template("index.html",
                           discovered=discovered,
                           stats=token_stats,
                           history=conversation_history)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        repo_url = request.form.get("repo_url")
        if repo_url:
            context = discover_repo_context(repo_url)
            session["discovered_context"] = context
            return render_template("setup.html",
                                   context=context,
                                   repo_url=repo_url,
                                   success=True)
    return render_template("setup.html")


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
    app.run(debug=True, port=5000)