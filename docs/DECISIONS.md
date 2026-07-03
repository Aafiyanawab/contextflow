# Decisions

Lightweight decision log. Add new entries at the bottom with a date and a one-line status.
(The early entries below were reconstructed from the code in July 2026, not written at decision
time — rationale is inferred where not documented.)

## 1. Hybrid intent classification: rules first, LLM fallback

**Status:** active · `app/intent_engine.py`

Keyword matching handles clear queries for free and instantly; OpenAI is called only when scoring
is ambiguous (no keyword hit, or a tie between intents). Greetings short-circuit before any API
call with canned responses. Rationale: most infra queries contain unambiguous vocabulary
("terraform", "deploy", "grafana"), so paying for classification on every request would be wasted
spend. Trade-off: the keyword lists need manual upkeep as tooling vocabulary evolves.

## 2. Intent-filtered context injection

**Status:** active · `app/context_builder.py` (`INTENT_CONTEXT_MAP`)

Only the discovered-context keys relevant to the detected intent are injected into the prompt,
rather than the full discovered profile. Rationale: smaller prompts, and answers stay focused on
what was asked (a monitoring question doesn't need to hear about the repo's framework). This
mapping is the product's core idea — change it deliberately.

## 3. Static-signal repo discovery, no cloning

**Status:** active · `app/github_discovery.py`

Discovery uses the GitHub API to walk the tree once and inspect a capped sample of file contents
(5 Terraform files, 10 code files) instead of cloning the repo. Rationale: no disk/state on the
server, bounded API usage, fast enough to run synchronously in the `/setup` request. Trade-off:
detection can miss signals outside the sampled files, and large repos make the synchronous scan
slow.

## 4. Context lives in the Flask session

**Status:** active, revisit in Phase 4 (see `ROADMAP.md`)

Discovered context is stored in the signed session cookie; nothing is persisted server-side.
Rationale: zero infrastructure for an MVP with no accounts. Consequence: context is per-browser,
lost when cookies clear, and can't be shared across a team.

## 5. `gpt-4o-mini` for everything

**Status:** active · `app/config.py`, `app/intent_engine.py`, `app.py`

One cheap model for both fallback classification and answer generation, `temperature=0.3`,
`max_tokens=1000` for answers. Rationale: cost — the product's pitch includes being economical, and
enriched prompts compensate for a smaller model. Note the model string is currently hardcoded in
three places; unifying it is a Phase 1 roadmap item.

## 6. SSE streaming with server-rendered frontend

**Status:** active · `app.py` `/chat`, `templates/index.html`

Responses stream as server-sent events consumed by vanilla JS over `fetch`; UI is Jinja templates
with no build step. Rationale: perceived latency matters for chat, and a bundler/framework is not
justified for two pages. Trade-off: client-side formatting is regex-based, not a real markdown
renderer.

## 7. Push-to-main deploys straight to EC2

**Status:** active, revisit in Phase 1 · `.github/workflows/deploy.yml`

Every push to `main` builds → ECR → SSH pull-and-restart on a single EC2 host. No staging
environment, no test gate, ~seconds of downtime during the container swap. Rationale: simplest
possible pipeline for a single-instance MVP. Accepted risk until a CI gate is added (Phase 1).
