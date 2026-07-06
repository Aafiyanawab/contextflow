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

VALID_INTENTS = [
    "infrastructure",
    "deployment", 
    "monitoring",
    "security",
    "troubleshooting"
]