# Architecture

ContextFlow is a Flask AI engineering workspace. An authenticated user connects GitHub
repositories; each becomes a **workspace** whose auto-discovered context (cloud provider, IaC,
containerization, orchestration, CI/CD, language, framework) is injected — filtered by detected
intent — into every chat inside that workspace. The **Context Inspector** shows the orchestration
per exchange: detected intent, classification method, matched keywords, injected *and withheld*
context, and real token usage.

## Data model (`app/models.py`)

```
user 1─N oauth_identity              (provider + provider_uid, unique together)
user 1─N workspace 1─N chat 1─N message
workspace 1─N knowledge_source 1─N document 1─N chunk        (v2 foundations)
workspace 1─N capsule N─N chunk (capsule_chunk)              (populated from Inc 5)
```

- A workspace's GitHub connection lives in a `knowledge_source` row (`type="github"`);
  its discovered `profile` (JSON) is written at connect time and on explicit rescan.
  `Workspace.context_profile` is the one read path routes use.
- Assistant `message` rows store an **orchestration snapshot** (intent, method, matched_keywords,
  injected/withheld context, tokens_in/out) so the Inspector stays accurate even after a rescan
  changes the live workspace context (rescan is forward-only).
- All deletes cascade downward (user → workspaces → chats → messages).
- Flask-SQLAlchemy on `DATABASE_URL` (default: SQLite in `instance/contextflow.db`; production
  will point at Postgres). Schema is managed by Flask-Migrate/Alembic — `FLASK_APP=manage.py
  flask db upgrade`; never `create_all`.

## Authentication (`app/auth.py`)

GitHub OAuth via Authlib; adding Google later = one more `oauth.register()` block, one login
button, and identity rows link to the same user by verified email. Scope `read:user user:email`;
the access token is used once for the profile fetch and never stored. Sessions are Flask signed
cookies holding only `user_id` (`SECRET_KEY` required from env; HttpOnly; SameSite=Lax; Secure
when `FLASK_ENV=production`; 30-day lifetime). `load_user` puts `g.user` on every request.

`login_required` contract: requests marked `X-Requested-With: fetch` get 401 JSON (frontend
redirects to `/login?error=session_expired`); unauthenticated GETs redirect with `?next=`;
unauthenticated form posts redirect with a friendly "session expired" message.

**CSRF:** `check_csrf` (same file) rejects any unsafe-method request from a logged-in session
that doesn't echo the session's token — forms carry a hidden `csrf_token` input, fetch calls
send `X-CSRF-Token` (read from the meta tag in `base.html`). Fetch failures get 403 JSON;
form failures redirect like an expired session. Anonymous requests skip the check so
`login_required` answers instead.

**Ownership chokepoint:** `get_owned_workspace()` / `get_owned_chat()` in `app.py` are the only
authorization checks in the codebase — 404 (not 403) for anything the user doesn't own, so IDs
aren't probeable. Team workspaces / RBAC later extend these two functions, not the routes.

## Routes (`app.py`)

| Route | Purpose |
|---|---|
| `GET /` | redirect: user's most recent chat → newest workspace → `/connect` |
| `GET /login`, `GET /auth/github[/callback]`, `POST /logout` | auth (public) |
| `GET /connect` | connect page |
| `POST /workspaces/scan` | SSE: streams discovery progress steps, creates the workspace |
| `GET /w/<id>` | workspace overview: context grid, rescan/disconnect, chat list |
| `POST /w/<id>/rescan`, `POST /w/<id>/delete`, `POST /w/<id>/chats` | workspace actions |
| `GET /w/<id>/c/<id>` | chat view (three columns, history payload embedded as JSON) |
| `POST /c/<id>/messages` | SSE: the single message/orchestration/LLM pipeline |

The cost surfaces are rate-limited per user (`app/ratelimit.py`, in-memory sliding windows):
messages at 20/min, scan + rescan sharing 10 per 10 min, with 429 + `Retry-After` on excess.
Request bodies cap at 64 KB app-wide; repo URLs at 200 chars; messages at 4,000 chars.

## Message pipeline (`POST /c/<id>/messages`)

1. **Intent** (`app/intent_engine.py`) — free keyword matching first (returns matched keywords
   for the Inspector); OpenAI classifier fallback only for ambiguous queries; greetings
   short-circuit with canned replies and zero API cost. A classifier outage degrades to a
   default intent rather than failing the message — the answer call's error handling reports
   any real API problem.
2. **Context split** — `INTENT_CONTEXT_MAP` (`app/context_builder.py`) decides which discovered
   keys are injected vs withheld (withheld entries carry the reason, shown in the Inspector).
3. **Prompt** — system prompt + filtered context block + last 12 messages of this chat
   (multi-turn) + the new question.
4. **Stream** — one OpenAI call (`stream_options={"include_usage": true}` for real token
   counts), SSE events: `meta` → `token`* → `done` (with workspace-scoped and user-scoped usage
   rollups). The user message is persisted *before* the model call so failures never lose input.
5. **Persist** — both messages committed with the orchestration snapshot; chat auto-titles from
   the first question. Caveat: the generator may run under a different DB session than the
   request handler — mutate rows re-fetched by pk inside the generator only.

Usage statistics have one source of truth: `usage_stats(workspace_id=…, user_id=…)` — the
sidebar footer shows the user scope, the Inspector card the workspace scope, both updated live
from the same `done` event.

## Repo discovery (`app/github_discovery.py`)

One GitHub API tree walk plus a capped content sample (5 `.tf`, 10 code files) against
`DISCOVERY_RULES` / hint tables; no cloning. An optional `progress` callback feeds the connect
page's live step display (via a queue + worker thread in `scan_workspace`). Uses the server-side
`GITHUB_TOKEN`; public repos only.

## Frontend

Server-rendered Jinja on one design system (`static/css/app.css`, light theme, violet accent,
Inter). `base.html` = sidebar shell (workspaces → nested chats, usage footer, user chip + sign
out). Vanilla JS per page; chat history is embedded as a JSON payload and rendered client-side;
all content is HTML-escaped before light markdown formatting. Responsive: sidebar becomes a
drawer < 920px; Inspector becomes a slide-over < 1100px.

## CI/CD & deployment

`.github/workflows/deploy.yml`: push to `main` → Docker build → ECR (`ap-south-1`) → SSH to EC2,
pull and restart. No test/lint gate, no health check, Flask dev server in the container —
production hardening (gunicorn, gate, Terraform, OIDC) is the next phase; see `ROADMAP.md`.
