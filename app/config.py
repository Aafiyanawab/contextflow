import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"

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

VALID_INTENTS = [
    "infrastructure",
    "deployment", 
    "monitoring",
    "security",
    "troubleshooting"
]