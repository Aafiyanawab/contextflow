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