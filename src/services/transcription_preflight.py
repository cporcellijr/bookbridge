"""Advisory EPUB/audio length measurement before full Whisper transcription (#528).

EPUB tokens/hour have no validated cross-format calibration against transcript
words/hour. Never classify a pairing as mismatched from this measurement alone.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from src.services.map_quality import _letter_token_count, _mostly_unspaced_script, _word_tokens


def pair_key(book: Any) -> str:
    """Stable identifier for the selected provider-side ebook/audio pairing."""
    fields = (
        getattr(book, "abs_id", None),
        getattr(book, "audio_source", None),
        getattr(book, "audio_source_id", None),
        getattr(book, "audio_provider_book_id", None),
        getattr(book, "audio_provider_file_id", None),
        getattr(book, "audio_duration", None),
        getattr(book, "duration", None),
        getattr(book, "ebook_source", None),
        getattr(book, "ebook_source_id", None),
        getattr(book, "original_ebook_filename", None)
        or getattr(book, "ebook_filename", None),
    )
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _valid_duration(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def metadata_duration(book: Any, item_details: dict | None = None) -> float | None:
    """Use catalog/provider duration only; do not acquire or probe audio."""
    media = (item_details or {}).get("media") if isinstance(item_details, dict) else None
    # Only the ABS metadata lookup identifies the selected ABS audiobook.
    # A Forge request may have ABS item_details even when its audio is BookOrbit.
    if (getattr(book, "audio_source", None) in (None, "", "ABS")
            and isinstance(media, dict) and "duration" in media):
        # A duration explicitly reported as zero/nonfinite is unreliable.
        # Do not silently replace it with an older saved estimate.
        return _valid_duration(media["duration"])
    # audio_duration is provider metadata captured on matching. Legacy duration
    # is accepted only for ABS items without a newer source duration.
    captured = getattr(book, "audio_duration", None)
    if captured is not None:
        if getattr(book, "audio_source_id", None) or getattr(book, "audio_source", None):
            return _valid_duration(captured)
        return None
    if (getattr(book, "audio_source", None) in (None, "", "ABS")
            and getattr(book, "abs_id", None)):
        return _valid_duration(getattr(book, "duration", None))
    return None


def evaluate_preflight(ebook_text: str, duration_seconds: Any) -> dict:
    """Return read-only evidence without a content-match verdict.

    Uses map_quality's existing Unicode tokenization but does not change its
    final lexical guard. Values are EPUB tokens, NOT ASR transcript word counts.
    """
    tokens = _word_tokens(ebook_text or "")
    count = _letter_token_count(tokens)
    duration = _valid_duration(duration_seconds)
    if not count or _mostly_unspaced_script(tokens):
        return {
            "token_count": count,
            "duration_seconds": duration,
            "epub_tokens_per_audio_hour": None,
            "assessment": "inconclusive",
            "explanation": (
                "EPUB text is empty or uses predominantly unspaced-script tokens; "
                "the word-rate measure is not reliable for this text."
            ),
        }
    if duration is None:
        return {
            "token_count": count, "duration_seconds": None,
            "epub_tokens_per_audio_hour": None, "assessment": "inconclusive",
            "explanation": "Audiobook duration is missing or unreliable; length cannot be compared.",
        }
    rate = count * 3600.0 / duration
    if not math.isfinite(rate):
        return {
            "token_count": count, "duration_seconds": None,
            "epub_tokens_per_audio_hour": None, "assessment": "inconclusive",
            "explanation": "Duration or length ratio cannot be measured reliably.",
        }
    return {
        "token_count": count,
        "duration_seconds": round(duration, 2),
        "epub_tokens_per_audio_hour": round(rate, 1),
        "assessment": "uncalibrated",
        "explanation": (
            "EPUB tokens per audio hour are not calibrated to transcript word rates. "
            "Catalog audio duration may also be inaccurate. Review the ebook edition/omnibus pairing; this is not a mismatch verdict."
        ),
    }


def evidence_identity(book: Any, ebook_text: str, item_details: dict | None = None) -> str:
    """Bind an acknowledgement to the exact selected pair, EPUB contents, and effective measured duration."""
    digest = hashlib.sha256()
    digest.update(pair_key(book).encode("ascii"))
    digest.update(b"\x00")
    digest.update((ebook_text or "").encode("utf-8"))
    digest.update(b"\x00")
    duration = metadata_duration(book, item_details)
    if duration is not None:
        digest.update(str(duration).encode("utf-8"))
    return digest.hexdigest()


def record_preflight(database_service: Any, book: Any, ebook_text: str,
                     item_details: dict | None = None) -> dict:
    """Compute evidence for every reader who has claimed the shared pairing."""
    evidence = evaluate_preflight(ebook_text, metadata_duration(book, item_details))
    # The book/transcript is catalog-wide, but the advisory and acknowledgement
    # must be independent for each reader who has actually claimed this pair.
    claimants = database_service.get_book_user_ids(book.abs_id)
    for claimant_id in claimants:
        database_service.save_transcription_preflight(
            abs_id=book.abs_id, user_id=claimant_id,
            identity=evidence_identity(book, ebook_text, item_details),
            pairing_key=pair_key(book), evidence=evidence,
        )
    return evidence
