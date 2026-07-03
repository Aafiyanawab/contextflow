# Roadmap

> **Status: proposed.** No roadmap had been written down before this document; the items below are
> derived from gaps and TODO-signals in the current codebase (the "MVP" markers in the UI, the
> unused `conversation_history` global, missing auth/persistence/tests). Treat this as a draft to
> confirm, reorder, or cut.

## Phase 1 — Hardening the MVP

- **Unify the OpenAI call path.** `/chat` in `app.py` and `app/ai_client.py` duplicate the
  request construction (system prompt, model, limits). Extract one shared client so prompt/model
  changes happen in one place.
- **Real test suite.** Convert the inline `__main__` blocks into pytest tests; the rule-based
  intent engine and context builder are pure functions and trivially testable without API keys.
- **CI gate.** Add a test/lint job to `deploy.yml` so pushes to `main` no longer deploy unverified.
- **Production server.** Replace `python app.py` (Flask dev server, `debug=True`) with gunicorn in
  the Docker image; move the hardcoded `app.secret_key` to an env var.

## Phase 2 — Multi-turn conversations

- Wire up the currently-unused `conversation_history` so follow-up questions carry prior context.
- Persist history per session (start with SQLite; the app currently has zero persistence).
- Per-session (not global) token/usage stats.

## Phase 3 — Deeper discovery

- **Private repositories** — OAuth GitHub app flow instead of a single server-side token
  (setup UI already says "Public repositories only for MVP").
- Broaden `DISCOVERY_RULES`: more languages (Go, Rust), more IaC (Pulumi, CloudFormation), more
  CI systems (GitLab CI, CircleCI); GCP/Azure hints already exist but are Terraform-only.
- Re-scan / refresh discovered context on demand rather than only at connect time.

## Phase 4 — Multi-user product

- User accounts and org-level shared context (one discovery, whole team benefits).
- Server-side context storage keyed to user/org instead of browser session cookies.
- Usage/cost dashboards built on persisted stats.

## Explicit non-goals (for now)

- Writing changes back to the connected repo — ContextFlow reads context, it doesn't act on repos.
- Supporting non-GitHub forges (GitLab, Bitbucket) before the GitHub path is solid.
