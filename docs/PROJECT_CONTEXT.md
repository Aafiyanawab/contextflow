# Project Context

## What ContextFlow is

ContextFlow is an AI assistant for cloud engineering questions that knows about *your* stack.
Instead of asking users to describe their environment in every prompt, it connects to a GitHub
repository once, auto-discovers the organizational context (cloud provider, IaC tool,
containerization, orchestration, CI/CD, language, framework), and silently injects the relevant
slice of that context into every AI prompt.

## The problem it solves

Generic LLM answers to infra questions are vague ("it depends on your cloud provider…"). Engineers
end up re-typing their stack details into every conversation. ContextFlow removes that friction:
ask "how should I deploy this service?" and the model already knows you run Docker on AWS with
GitHub Actions.

## How it's differentiated

- **Context is discovered, not declared** — scanning the repo's file tree and contents, not a
  settings form the user fills in.
- **Context is filtered by intent** — a troubleshooting question gets language/framework context; a
  deployment question gets CI/CD/orchestration context. The full discovered profile is never dumped
  wholesale into prompts (keeps prompts small and answers focused).
- **Cost-conscious by design** — a free rule-based keyword classifier handles most intent
  detection; OpenAI is only used as a fallback for ambiguous queries, and greetings never reach the
  API at all. The UI surfaces "free classifications" as a stat.

## Current state (MVP)

- Single-process Flask app, deployed as one Docker container on EC2 via GitHub Actions → ECR.
- Public GitHub repositories only (noted in the setup UI).
- No user accounts or auth — discovered context lives in the Flask browser session.
- No persistence — stats and history reset on every restart.
- No test suite — modules have inline `__main__` test blocks.
- Model: `gpt-4o-mini` for both classification fallback and answers.

## Key external dependencies

| Dependency | Used for |
|---|---|
| OpenAI API (`OPENAI_API_KEY`) | Answer generation + fallback intent classification |
| GitHub API via PyGithub (`GITHUB_TOKEN`) | Repository tree walk and file-content scanning |
| AWS (ECR, EC2, `ap-south-1`) | Build artifact registry and hosting |

## Where to look next

- `ARCHITECTURE.md` — request flow and module responsibilities
- `ROADMAP.md` — planned direction beyond the MVP
- `DECISIONS.md` — why things are built the way they are
