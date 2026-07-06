"""Structure-aware chunking: extracted text → retrieval units.

Splits at real boundaries in priority order — page breaks (\f, from PDF
extraction), markdown headings (from DOCX/MD extraction), then blank
lines — and accumulates blocks up to CHUNK_TARGET_TOKENS. A single
oversized block is sentence-split; nothing is ever cut mid-sentence.
Each chunk records its heading trail and page in meta, and exposes
embedding_text with the trail prepended (cheap contextual retrieval:
a few tokens per chunk for a real precision gain).
"""
import re

import tiktoken

from app.config import CHUNK_TARGET_TOKENS, CHUNK_MAX_BLOCK_TOKENS

_enc = tiktoken.get_encoding("cl100k_base")  # text-embedding-3-* tokenizer

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text, disallowed_special=()))


def _blocks(text):
    """Yield (block_text, heading_trail, page). Heading lines update the
    trail and are emitted as their own block so they stay with what
    follows them in the accumulator."""
    trail = {}  # level -> title
    page = 1
    has_pages = "\f" in text
    for page_text in text.split("\f"):
        for raw in re.split(r"\n\s*\n", page_text):
            block = raw.strip()
            if not block:
                continue
            m = _HEADING_RE.match(block.split("\n", 1)[0])
            if m:
                level = len(m.group(1))
                trail[level] = m.group(2).strip()
                for deeper in [k for k in trail if k > level]:
                    del trail[deeper]
            current = [trail[k] for k in sorted(trail)]
            yield block, current, (page if has_pages else None)
        page += 1


def _split_oversized(block, limit):
    """Sentence-accumulate an oversized block into <= limit-token parts."""
    parts, cur, cur_tokens = [], [], 0
    for sentence in _SENTENCE_RE.split(block):
        t = count_tokens(sentence)
        if cur and cur_tokens + t > limit:
            parts.append(" ".join(cur))
            cur, cur_tokens = [], 0
        cur.append(sentence)
        cur_tokens += t
    if cur:
        parts.append(" ".join(cur))
    return parts


def _code_blocks(text):
    """Code/config files: blank-line blocks, no heading parsing — a
    Python `# comment` must not be mistaken for a markdown heading."""
    for raw in re.split(r"\n\s*\n", text):
        block = raw.strip()
        if block:
            yield block, [], None


def chunk_document(text: str, kind: str = "prose", context: str = ""):
    """→ list of dicts: {seq, text, embedding_text, token_count, meta}.
    kind: "prose" (headings/pages structure) or "code" (blank-line
    blocks). context (e.g. the repo file path) prefixes embedding_text
    when there is no heading trail — same contextual-retrieval trick."""
    chunks = []
    cur, cur_tokens, cur_trail, cur_page = [], 0, [], None

    def flush():
        nonlocal cur, cur_tokens
        if not cur:
            return
        body = "\n\n".join(cur)
        trail = " > ".join(cur_trail) or context
        # Prepend the trail (or file-path context) for embedding unless
        # the chunk already starts with that heading text.
        embedding_text = (f"{trail}\n\n{body}"
                          if trail and not body.startswith("#") else body)
        meta = {}
        if cur_trail:
            meta["headings"] = list(cur_trail)
        if cur_page is not None:
            meta["page"] = cur_page
        chunks.append({"seq": len(chunks), "text": body,
                       "embedding_text": embedding_text,
                       "token_count": count_tokens(body), "meta": meta})
        cur, cur_tokens = [], 0

    block_iter = _code_blocks(text) if kind == "code" else _blocks(text)
    for block, trail, page in block_iter:
        pieces = ([block] if count_tokens(block) <= CHUNK_MAX_BLOCK_TOKENS
                  else _split_oversized(block, CHUNK_MAX_BLOCK_TOKENS))
        for piece in pieces:
            t = count_tokens(piece)
            # Flush at section/page boundaries — but only once the chunk
            # has substance, so documents made of many tiny sections
            # merge instead of producing confetti.
            boundary = (trail != cur_trail or page != cur_page) and cur_tokens >= 100
            if cur and (cur_tokens + t > CHUNK_TARGET_TOKENS or boundary):
                flush()
            cur.append(piece)
            cur_tokens += t
            cur_trail, cur_page = trail, page
    flush()
    return chunks
