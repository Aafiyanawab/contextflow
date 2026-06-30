import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"

VALID_INTENTS = [
    "infrastructure",
    "deployment",
    "monitoring",
    "security",
    "troubleshooting"
]

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Keyword Map ──────────────────────────────────────────
INTENT_KEYWORDS = {
    "infrastructure": [
        "terraform", "vpc", "ec2", "s3", "rds", "redis", "subnet",
        "route table", "security group", "eks", "ecr", "ecs",
        "lambda", "cloudwatch", "dynamodb", "load balancer", "alb", "nlb",
        "kubernetes", "helm", "ingress", "namespace", "pod", "cluster"
    ],
    "deployment": [
        "deploy", "deployment", "rollout", "release", "blue-green",
        "canary", "pipeline", "ci/cd", "github actions", "jenkins",
        "artifact", "build", "ship", "push", "publish"
    ],
    "monitoring": [
        "grafana", "prometheus", "cloudwatch", "alert", "dashboard",
        "metric", "log", "alarm", "notification", "observability",
        "trace", "monitor", "uptime"
    ],
    "security": [
        "policy", "role", "permission", "authentication",
        "authorization", "ssl", "tls", "certificate", "secret",
        "vault", "encryption", "firewall", "compliance", "audit"
    ],
    "troubleshooting": [
        "error", "issue", "failed", "failure", "debug", "debugging",
        "fix", "logs", "crash", "timeout", "latency", "slow", "broken"
    ]
}

# Greeting responses — each gets its own unique reply
GREETING_RESPONSES = {
    "hi": "Hey there! 👋 I'm ContextFlow. Connect a repo and ask me anything about your infrastructure.",
    "hello": "Hello! I'm ContextFlow, your AI infrastructure assistant. How can I help today?",
    "hey": "Hey! What can I help you build or troubleshoot today?",
    "yo": "Yo! Ready to help with your cloud infrastructure. What's up?",
    "sup": "Not much, just ready to help with your infrastructure! What's up with you?",
    "thanks": "You're welcome! Let me know if you need anything else.",
    "thank you": "Anytime! Happy to help with your cloud infrastructure questions.",
    "bye": "Goodbye! Come back anytime you need infrastructure help.",
    "goodbye": "See you later! 👋",
    "good morning": "Good morning! Ready to tackle some cloud infrastructure today?",
    "good afternoon": "Good afternoon! What are you working on?",
    "good evening": "Good evening! How can I assist with your infrastructure tonight?",
    "ok": "Got it! Let me know if you have any infrastructure questions.",
    "okay": "Sounds good! I'm here whenever you need help.",
    "how are you": "I'm running smoothly! Ready to help with your cloud infrastructure. What's on your mind?",
    "what's up": "Just here helping with cloud infra! What do you need?"
}


# ── Step 1: Rule-Based Detection ─────────────────────────
def detect_intent_rule_based(user_query: str):
    """
    Try to detect intent using keyword matching.
    Returns (intent, score, matched_greeting) or (None, 0, None) if unclear.
    """
    query = user_query.lower().strip()

    # Exact greeting match
    if query in GREETING_RESPONSES:
        return "general", 1, query

    # Partial greeting match (short messages only)
    if len(query.split()) <= 4:
        for greeting in GREETING_RESPONSES:
            if greeting in query:
                return "general", 1, greeting

    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in query]
        scores[intent] = len(matched)

    best_intent = max(scores, key=scores.get)
    best_score = scores[best_intent]

    if best_score == 0:
        return None, 0, None

    # Check for ambiguity
    top_scores = [k for k, v in scores.items() if v == best_score]
    if len(top_scores) > 1:
        return None, best_score, None

    return best_intent, best_score, None


# ── Step 2: OpenAI Fallback ──────────────────────────────
def detect_intent_openai(user_query: str) -> str:
    """
    Fallback — use OpenAI to classify ambiguous queries.
    Only called when rule-based returns None.
    """
    prompt = f"""You are an intent classifier for a cloud engineering AI platform.

Classify the following user query into exactly ONE of these categories:
- infrastructure
- deployment
- monitoring
- security
- troubleshooting

User query: "{user_query}"

Reply with only the category name, nothing else."""

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=10,
        temperature=0
    )

    intent = response.choices[0].message.content.strip().lower()

    if intent not in VALID_INTENTS:
        return "infrastructure"  # safe default

    return intent


# ── Main Function ────────────────────────────────────────
def get_intent(user_query: str) -> dict:
    """
    Main function — tries rule-based first, falls back to OpenAI.
    Returns intent + method used + matched greeting key (if any).
    """
    intent, score, matched_greeting = detect_intent_rule_based(user_query)

    if intent:
        return {
            "intent": intent,
            "method": "rule-based",
            "score": score,
            "greeting_key": matched_greeting
        }

    # Fallback to OpenAI
    intent = detect_intent_openai(user_query)
    return {
        "intent": intent,
        "method": "openai",
        "score": 0,
        "greeting_key": None
    }


# ── Test ─────────────────────────────────────────────────
if __name__ == "__main__":
    test_queries = [
        "Hi", "Hello", "Hey", "Thanks!", "How are you",
        "Create Terraform for Redis",
        "How do I deploy this service with blue-green strategy?",
        "Set up Grafana alerts for CloudWatch metrics",
        "Fix the error in my Lambda function logs",
        "What IAM permissions do I need for S3?",
    ]

    print("=== Intent Engine Test (Hybrid) ===\n")
    for query in test_queries:
        result = get_intent(query)
        print(f"Query:  {query}")
        print(f"Intent: {result['intent'].upper()} "
              f"(method: {result['method']}, greeting: {result['greeting_key']})\n")