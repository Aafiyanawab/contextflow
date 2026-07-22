# ── Context Builder v2 (Increment 6) ────────────────────
# Assembles the final prompt context from routed knowledge: the repo
# profile (one compact line), capsule summaries (only when the query
# shape warrants them), and scoped source chunks. Capsules always act
# as the routing index; their summaries are injected selectively so
# narrow factual questions don't pay for domain overviews.

BROAD_MARKERS = (
    "summarize", "summary", "overview", "overall", "explain",
    "compare", "difference", "differences", "themes", "relate",
    "relationship", "big picture", "high level", "introduction",
    "what do my", "what are the main", "walk me through",
)

# The canonical detected-stack keys. A repo profile ALSO carries
# inventory-only fields (owner, repo_name, package_manager, repo_size_kb,
# file_count) — those must never reach the model. Restricting the prompt
# stack line to these keys keeps profile enrichment out of prompts.
STACK_KEYS = ("cloud", "iac", "containerization", "orchestration",
              "cicd", "language", "framework")


def wants_summaries(query: str, route: dict, min_breadth=10):
    """Rules-first broad-vs-narrow call — no LLM. → (bool, reason).

    A summary earns its tokens only when it condenses real breadth: if
    the routed capsules together hold fewer than `min_breadth` chunks,
    scoped retrieval already covers essentially all of them and the
    summary is pure overhead (small corpora, the General floor capsule).
    """
    caps = route["capsules"]
    breadth = sum(len(c.memberships) for c in caps)
    if breadth and breadth < min_breadth:
        return False, "small knowledge set — scoped chunks already cover it"
    q = query.lower()
    if any(m in q for m in BROAD_MARKERS):
        return True, "broad / synthesis question"
    if route["method"] == "semantic":
        return True, "low-confidence routing — summary grounds the answer"
    if len(caps) > 1:
        return True, "question spans multiple domains"
    return False, "narrow factual question — scoped chunks only"


def build_prompt_context(profile, capsules, include_summaries, chunks):
    """→ context block string. Every part is optional; an empty block
    means the model answers from the system prompt + history alone."""
    parts = []
    if profile:
        stack = [CONTEXT_VALUES.get(profile[k], profile[k])
                 for k in STACK_KEYS if profile.get(k)]
        if stack:
            parts.append("User's stack: " + ", ".join(stack))
    if include_summaries:
        for cap in capsules:
            if cap.summary:
                parts.append(f"## Knowledge: {cap.title}\n{cap.summary}")
    if chunks:
        lines = []
        for c in chunks:
            where = c.document.filename
            page = (c.meta or {}).get("page")
            if page:
                where += f" p.{page}"
            lines.append(f"[{where}]\n{c.text}")
        parts.append("## Source excerpts from the user's knowledge\n"
                     + "\n\n".join(lines))
    return "\n\n".join(parts)


# Context rules — which discovered context is relevant per intent
INTENT_CONTEXT_MAP = {
    "general": [],
    "infrastructure": ["cloud", "iac", "containerization"],
    "deployment": ["orchestration", "cicd", "containerization", "cloud"],
    "monitoring": ["cloud", "cicd"],
    "security": ["cloud", "iac"],
    "troubleshooting": ["cloud", "language", "framework", "containerization"]
}

CONTEXT_LABELS = {
    "cloud": "Cloud Provider",
    "iac": "Infrastructure as Code",
    "containerization": "Containerization",
    "orchestration": "Orchestration",
    "cicd": "CI/CD",
    "language": "Programming Language",
    "framework": "Framework"
}

CONTEXT_VALUES = {
    "aws": "Amazon Web Services (AWS)",
    "terraform": "Terraform",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "github_actions": "GitHub Actions",
    "python": "Python",
    "flask": "Flask",
    "fastapi": "FastAPI"
}


def build_context(intent: str, discovered_context: dict) -> str:
    """
    Selects only the relevant context for the detected intent
    and builds a context string to inject into the prompt.
    """
    relevant_keys = INTENT_CONTEXT_MAP.get(intent, [])

    selected = {}
    for key in relevant_keys:
        if key in discovered_context:
            selected[key] = discovered_context[key]

    if not selected:
        return ""

    lines = ["Organizational Context (auto-discovered from repository):"]
    for key, value in selected.items():
        label = CONTEXT_LABELS.get(key, key)
        display_value = CONTEXT_VALUES.get(value, value)
        lines.append(f"- {label}: {display_value}")

    return "\n".join(lines)


# ── Test ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Simulate discovered context
    discovered = {
        "cloud": "aws",
        "iac": "terraform",
        "containerization": "docker",
        "cicd": "github_actions",
        "language": "python"
    }

    for intent in ["general", "infrastructure", "deployment", "troubleshooting"]:
        print(f"Intent: {intent.upper()}")
        print("-" * 50)
        print(build_context(intent, discovered) or "(no context injected)")
        print("=" * 50 + "\n")