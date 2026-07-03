# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

ContextFlow: a Flask app that answers cloud/infra questions via OpenAI, injecting context
auto-discovered from a connected GitHub repo. Request flow: `docs/ARCHITECTURE.md`; product
background: `docs/PROJECT_CONTEXT.md`; planned work: `docs/ROADMAP.md`; design rationale:
`docs/DECISIONS.md`.

## Commands

```bash
pip install -r requirements.txt
python app.py                    # dev server on http://localhost:5000

# No test suite — each app/ module has an inline test block instead:
python -m app.intent_engine      # intent classification (free, no API calls for keyword path)
python -m app.context_builder    # prompt enrichment
python -m app.ai_client          # end-to-end, makes a real OpenAI call
```

Requires `OPENAI_API_KEY` and `GITHUB_TOKEN` in `.env`.

## Things that will bite you

- The OpenAI call is duplicated: streaming version inline in `app.py` `/chat`, non-streaming in
  `app/ai_client.py`. Changing the system prompt, model, or token limits requires updating both.
- `token_stats` and `conversation_history` in `app.py` are module-level globals — shared across all
  users, reset on restart. Only `discovered_context` (Flask session) is per-user.
- Every push to `main` deploys straight to EC2 via `.github/workflows/deploy.yml` — there is no
  test or lint gate in the pipeline.
