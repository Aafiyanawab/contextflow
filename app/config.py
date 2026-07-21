import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"

# USD per 1M tokens for the chat model — used only to render an
# *estimated* AI cost in the profile and admin dashboards. Embedding
# spend is negligible next to chat and is not modelled.
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60


def estimate_cost(tokens_in, tokens_out):
    return ((tokens_in or 0) * PRICE_INPUT_PER_1M
            + (tokens_out or 0) * PRICE_OUTPUT_PER_1M) / 1_000_000

# Embeddings / retrieval (Increment 3). 512 Matryoshka dimensions:
# 3x smaller vectors than the default 1536, negligible quality loss
# at workspace scale. Model+dims are stamped on every chunk so an
# upgrade can re-embed incrementally.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 512
CHUNK_TARGET_TOKENS = 500   # aim; blocks never split mid-sentence
CHUNK_MAX_BLOCK_TOKENS = 700  # oversized single blocks get sentence-split
RETRIEVAL_TOKEN_BUDGET = 2000  # retrieval fills a token budget, not a k

# GitHub content ingestion caps — bound API calls and embedding spend.
MAX_REPO_FILES = 50
MAX_REPO_FILE_BYTES = 200 * 1024

# Knowledge Capsules. Below the floor a workspace gets one "General"
# capsule (no clustering — small corpora don't need domain routing).
# Synthesis runs only on stale capsules, so LLM cost scales with
# changed knowledge, not corpus size.
CAPSULE_FLOOR = 40            # chunks needed before clustering kicks in
CAPSULE_MIN_CLUSTER = 4       # smaller clusters merge into neighbors
CAPSULE_MAX_K = 20
# Measured with text-embedding-3-small @512d: unrelated domains score
# ~0.30-0.45 against a broad centroid, same-domain content ~0.55+.
# 0.5 splits those bands.
CAPSULE_ASSIGN_THRESHOLD = 0.5  # below this similarity a chunk is an outlier
CAPSULE_SYNTHESIS_CHUNKS = 12    # representative chunks fed to synthesis

# Chat routing (Increment 6). Queries route to capsules first; global
# search is the last resort when nothing matches.
MAX_ROUTED_CAPSULES = 3
ROUTE_SEMANTIC_THRESHOLD = 0.35  # query-vs-centroid; queries score lower
                                 # than doc-vs-doc, hence below 0.5
RETRIEVAL_K = 6        # narrow queries: chunks carry the whole answer
RETRIEVAL_K_WITH_SUMMARY = 3  # broad queries: the summary carries breadth,
                              # chunks just supply specific evidence
SUMMARY_CHUNK_BUDGET = 1000   # tighter chunk budget when a summary is injected
NAIVE_RAG_K = 8        # the baseline we measure savings against

VALID_INTENTS = [
    "infrastructure",
    "deployment",
    "monitoring",
    "security",
    "troubleshooting"
]

# ── Password reset ───────────────────────────────────────
# Reset links are single-use and expire exactly 5 minutes after generation.
# Short by design: a reset grant is high-value, so its window is small.
PASSWORD_RESET_TTL_SECONDS = 300