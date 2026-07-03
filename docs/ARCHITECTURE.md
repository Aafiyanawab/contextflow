# Architecture

ContextFlow is a Flask app that answers cloud/infra questions via OpenAI, automatically injecting
organizational context it discovered from a connected GitHub repo (cloud provider, IaC tool,
container/orchestration tooling, CI/CD, language, framework). The same question gets a more
specific answer once a repo is connected, because relevant discovered facts are prepended to the
prompt.

## Request flow for `/chat` (`app.py`)

1. **Intent detection** (`app/intent_engine.py`) — `get_intent()` first tries free keyword matching
   (`detect_intent_rule_based`) against `INTENT_KEYWORDS` for the five valid intents
   (`infrastructure`, `deployment`, `monitoring`, `security`, `troubleshooting`) plus a `general`
   bucket for greetings. If keyword scoring is ambiguous (no match, or a tie between categories) it
   falls back to an OpenAI classification call (`detect_intent_openai`). Greetings short-circuit
   entirely — `app.py` streams a canned reply from `GREETING_RESPONSES` without ever calling
   OpenAI, so the "free classification" stat only counts truly free requests.

2. **Context selection** (`app/context_builder.py`) — `build_enriched_prompt()` looks up which
   discovered-context keys are relevant for the detected intent via `INTENT_CONTEXT_MAP`
   (e.g. `troubleshooting` pulls in cloud/language/framework/containerization but not CI/CD),
   formats only those into a context block, and prepends it to the user's question. This
   intent → relevant-context filtering is the core "ContextFlow" idea — don't dump all discovered
   facts into every prompt, only the ones relevant to what's being asked.

3. **Repo discovery** (`app/github_discovery.py`) — `discover_repo_context(repo_url)` is invoked
   from `/setup`, not from `/chat`. It walks the repo's full git tree once via PyGithub, then
   detects tools by three escalating signals per category (`DISCOVERY_RULES`): known filenames,
   known folder prefixes, or a 3+ file extension-count threshold. Cloud provider and framework are
   detected by grepping file contents (`CLOUD_PROVIDER_HINTS`, `FRAMEWORK_HINTS`) inside a capped
   sample of matching files (first 5 `.tf` files, first 10 code files) to bound API calls. Results
   are stored in the Flask `session` as `discovered_context` — the whole context pipeline is
   per-browser-session state, not persisted anywhere else.

4. **AI call** — `app.py`'s `/chat` route builds the streaming OpenAI call inline (SSE via
   `stream_with_context`), duplicating the request-construction logic that also lives in
   `app/ai_client.py` (a non-streaming variant used only by its own `__main__` test block).

## Frontend

Server-rendered Jinja (`templates/index.html`, `templates/setup.html`) with vanilla JS reading the
SSE stream and doing markdown-ish formatting client-side (regex-based code block / bold / newline
handling — no markdown library). No build step, no bundler, no JS framework.

## State

All in-process and ephemeral: `conversation_history` and `token_stats` in `app.py` are module-level
globals that reset on every server restart and are shared across all sessions (not per-user) — only
`discovered_context` is session-scoped. `conversation_history` is currently written nowhere and
only passed to the index template; there is no multi-turn memory in the AI calls.

## CI/CD & deployment

`.github/workflows/deploy.yml` builds the Docker image, pushes to ECR (`ap-south-1`), then SSHes
into an EC2 host to pull and run the new image on every push to `main`. There is no test or lint
step in the pipeline — pushing to `main` deploys directly. The container runs the Flask dev server
(`python app.py`, `debug=True`) on port 5000; there is no production WSGI server or reverse proxy
in the image.
