# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

ContextFlow: a Flask AI engineering workspace. Users sign in with GitHub OAuth; each connected
repository becomes a workspace whose auto-discovered context (cloud, IaC, CI/CD, language, …) is
injected — filtered by detected intent — into every chat in that workspace. Full request flow:
`docs/ARCHITECTURE.md`; product background: `docs/PROJECT_CONTEXT.md`; planned work:
`docs/ROADMAP.md`; design rationale: `docs/DECISIONS.md`.

## Commands

```bash
pip install -r requirements.txt
FLASK_APP=manage.py flask db upgrade   # create/upgrade the SQLite schema
python app.py                    # dev server on http://localhost:5000

# No test suite — pure modules have inline test blocks:
python -m app.intent_engine      # intent classification (1 OpenAI call for the ambiguous sample)
python -m app.context_builder    # context filtering, free
python -m app.github_discovery   # scans a hardcoded repo URL via the GitHub API
```

`.env` requires: `SECRET_KEY` (app refuses to boot without it), `OPENAI_API_KEY`,
`GITHUB_TOKEN` (repo scanning). Login providers are optional and shown only when configured:
`GITHUB_OAUTH_CLIENT_ID`/`_SECRET`, `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET`; email+password needs no
config. Providers live in a registry in `app/auth.py` — adding one is a `Provider` entry plus an
`oauth.register()` block.

## Things that will bite you

- **Schema changes go through Flask-Migrate** (`FLASK_APP=manage.py flask db migrate/upgrade`),
  never `db.create_all()` or deleting the DB — workspace memory is a product promise now.
  `manage.py` exists because `app.py` is shadowed by the `app/` package on import. The DB URL
  comes from `DATABASE_URL` (defaults to dev SQLite; production will point it at Postgres).
- **SSE generators outlive the request's DB session.** Never mutate ORM objects captured from
  the request scope inside a streamed generator — changes silently don't persist. Re-fetch by
  primary key inside the generator (see `post_message` in `app.py`).
- **Auth has a 401-vs-redirect contract.** Frontend fetch/SSE calls must send
  `X-Requested-With: fetch` and handle 401 by redirecting to `/login?error=session_expired`;
  everything else (page loads, form posts) gets redirected server-side. Never let a user see
  raw JSON.
- **Every POST needs the CSRF token** (`check_csrf` in `app/auth.py`) — new forms need
  `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`, new fetch calls the
  `X-CSRF-Token` header (from `base.html`'s meta tag), or logged-in requests 403. New inline
  `<script>` tags likewise need `nonce="{{ csp_nonce }}"` or the CSP silently blocks them.
- **Cost surfaces are rate-limited per user** in `app/ratelimit.py` (in-memory, per-process —
  limits multiply if you add workers). Messages 20/min; scan+rescan share 10 per 10 min.
- **All access control flows through `get_owned_workspace()` / `get_owned_chat()`** in `app.py`.
  Never query Workspace/Chat directly in a route — that chokepoint is where team workspaces and
  RBAC will plug in.
- **RBAC is three roles** — `super_admin` | `company_admin` | `employee` (`User.role`; see
  `is_admin`/`is_super_admin`). Guard admin routes with `@admin_required` (Super + Company
  Admin, 403 for employees) or `@super_admin_required` (companies, audit log, system). Company
  Admins are scoped to their `company_id` via `_scope_ids()` — thread it through any new admin
  query. **AI internals (Knowledge Capsules, retrieval, detected stack, intent, chunk/token
  data) are admin-only** now: never render them on user-facing workspace/chat pages — the
  `/admin/ai` tools are the one surface for them. Privileged mutations should call `audit()`.
- **Every push to `main` deploys straight to EC2** via `.github/workflows/deploy.yml` — there is
  no test or lint gate in the pipeline.
