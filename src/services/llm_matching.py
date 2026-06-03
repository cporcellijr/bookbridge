"""Shared LLM helpers for verifying book-tracker matches (Hardcover, StoryGraph).

Both helpers degrade gracefully: if the Ollama client is missing/unconfigured or the
model returns something unusable, they fall back to the caller's existing behavior
(craft → returns the raw terms; judge → returns None so the caller writes nothing).
"""

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def tracker_match_enabled() -> bool:
    return os.environ.get("OLLAMA_TRACKER_MATCH", "false").lower() == "true"


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
    result = ollama_client.judge(prompt)
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
    result = ollama_client.judge(prompt)
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
