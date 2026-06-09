"""Shared LLM helpers for verifying book-tracker matches (Hardcover, StoryGraph).

Both helpers degrade gracefully: if the Ollama client is missing/unconfigured or the
model returns something unusable, they fall back to the caller's existing behavior
(craft → returns the raw terms; judge → returns None so the caller writes nothing).
"""

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Structured-output schemas (Ollama >= 0.5). OllamaClient falls back to plain
# JSON mode automatically on servers that don't support them.
CRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "author": {"type": "string"},
    },
    "required": ["title", "author"],
}

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "choice": {"type": ["integer", "null"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["choice", "confidence"],
}


def tracker_match_enabled() -> bool:
    return os.environ.get("OLLAMA_TRACKER_MATCH", "false").lower() == "true"


def library_match_enabled() -> bool:
    """Gates LLM match rescue for library managers (Grimmory, BookOrbit)."""
    return os.environ.get("OLLAMA_LIBRARY_MATCH", "false").lower() == "true"


def _ready(ollama_client: Any) -> bool:
    return bool(ollama_client and ollama_client.is_configured())


def craft_search_terms(ollama_client: Any, title: str, author: str) -> Tuple[str, str]:
    """Ask the LLM for clean canonical {title, author} to search a book tracker.

    Strips subtitles, edition tags, narrator credits and series cruft so the search
    itself returns good candidates. Returns the original inputs on any failure.
    """
    if not _ready(ollama_client) or not title:
        return title, author

    prompt = (
        "You normalize messy audiobook metadata into a clean search query for a book "
        "catalog. Remove subtitles, edition/anniversary tags, format words (Unabridged, "
        "Audiobook), narrator credits, and series descriptors. Keep the canonical work "
        "title and the writing author (not the narrator).\n"
        f"Raw title: {title}\n"
        f"Raw author: {author}\n"
        'Respond ONLY with JSON: {"title": "<clean title>", "author": "<clean author>"}'
    )
    result = ollama_client.judge(prompt, schema=CRAFT_SCHEMA)
    if isinstance(result, dict):
        clean_title = (result.get("title") or "").strip()
        clean_author = (result.get("author") or "").strip()
        if clean_title:
            return clean_title, (clean_author or author)
    return title, author


def judge_best_candidate(
    ollama_client: Any,
    title: str,
    author: str,
    candidates: List[dict],
    min_confidence: float,
) -> Optional[int]:
    """Return the index of the candidate that is the same work, or None.

    `candidates` items must expose 'title' and (optionally) 'author'. Returns an index
    only when the model is confident (>= min_confidence); otherwise None so the caller
    writes nothing.
    """
    if not _ready(ollama_client) or not candidates:
        return None

    lines = [
        f"{i}. title: {c.get('title') or ''} | author: {c.get('author') or ''}"
        for i, c in enumerate(candidates)
    ]
    prompt = (
        "Decide which candidate book is the SAME WORK as the target (same book, any "
        "edition or translation), or none of them.\n"
        f"Target: title: {title} | author: {author}\n"
        "Candidates:\n" + "\n".join(lines) + "\n"
        'Respond ONLY with JSON: {"choice": <candidate number or null>, '
        '"confidence": <integer 0-100>}'
    )
    result = ollama_client.judge(prompt, schema=JUDGE_SCHEMA)
    if not isinstance(result, dict):
        return None

    choice = result.get("choice")
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    if isinstance(choice, int) and 0 <= choice < len(candidates) and confidence >= min_confidence:
        return choice
    return None


def rescue_from_catalog(
    ollama_client: Any,
    query: str,
    entries: List[dict],
    min_confidence: float,
    shortlist_size: int = 5,
    fuzzy_floor: float = 50.0,
) -> Optional[int]:
    """Pick the catalog entry that is the same work as `query`, or None.

    Shortlists `entries` (dicts with 'title' and optionally 'author') by fuzzy
    similarity so the judge sees a handful of plausible candidates, then asks the
    chat model to confirm one. Returns an index into `entries`.
    """
    if not _ready(ollama_client) or not query or not entries:
        return None
    from rapidfuzz import fuzz

    scored = []
    for i, entry in enumerate(entries):
        haystack = f"{entry.get('title') or ''} {entry.get('author') or ''}".strip()
        if not haystack:
            continue
        score = fuzz.token_set_ratio(query.lower(), haystack.lower())
        if score >= fuzzy_floor:
            scored.append((score, i))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    shortlist = [i for _, i in scored[:shortlist_size]]
    choice = judge_best_candidate(
        ollama_client, query, "", [entries[i] for i in shortlist], min_confidence
    )
    if choice is None:
        return None
    return shortlist[choice]


def best_semantic_window(
    ollama_client: Any,
    query: str,
    texts: List[str],
    threshold: float,
) -> Optional[Tuple[int, float]]:
    """Embed `query` and candidate `texts`; return (best_index, cosine) or None.

    Returns the index of the most semantically similar text only when its cosine
    similarity clears `threshold`; otherwise None so callers keep their fallback.
    """
    if not _ready(ollama_client) or not query or not texts:
        return None
    vectors = ollama_client.embed([query] + texts)
    if not vectors or len(vectors) != len(texts) + 1:
        return None

    from src.api.ollama_client import cosine_similarity

    query_vec = vectors[0]
    best_idx = None
    best_cos = 0.0
    for i, vec in enumerate(vectors[1:]):
        cos = cosine_similarity(query_vec, vec)
        if cos > best_cos:
            best_cos = cos
            best_idx = i
    if best_idx is not None and best_cos >= threshold:
        return best_idx, best_cos
    return None
