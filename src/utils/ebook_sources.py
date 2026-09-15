"""Canonical ebook-source names and identity helpers."""

from typing import Optional


_SOURCE_NAMES = {
    "booklore": "Booklore",
    "grimmory": "Booklore",
    "bookorbit": "BookOrbit",
    "kavita": "Kavita",
    "bookfusion": "BookFusion",
    "abs": "ABS",
    "cwa": "CWA",
    "local file": "Local File",
}


def normalize_ebook_source(value) -> str:
    """Return the project-wide canonical spelling for an ebook source."""
    source = str(value or "").strip()
    if not source:
        return ""
    return _SOURCE_NAMES.get(source.lower(), source)


def source_name_variants(value) -> tuple[str, ...]:
    """Return lowercased stored spellings for one canonical ebook source."""
    canonical = normalize_ebook_source(value)
    if not canonical:
        return ()
    variants = {
        alias
        for alias, canonical_name in _SOURCE_NAMES.items()
        if canonical_name == canonical
    }
    variants.add(canonical.lower())
    return tuple(sorted(variants))


def is_grimmory_source(value) -> bool:
    """Whether *value* is a BookLore/Grimmory source-name variant."""
    return normalize_ebook_source(value) == "Booklore"


def is_storyteller_filename(value) -> bool:
    return str(value or "").lower().startswith("storyteller_")


def local_ebook_filename(book) -> Optional[str]:
    """Return the stable local/cache filename for a mapped ebook.

    ``ebook_filename`` may follow mutable source metadata.  The original name is
    retained as the local cache/device identity.  Storyteller artifacts remain the
    active local file because they are generated content rather than source
    metadata.
    """
    current = getattr(book, "ebook_filename", None)
    current = current if isinstance(current, str) and current else None
    if is_storyteller_filename(current):
        return current
    original = getattr(book, "original_ebook_filename", None)
    original = original if isinstance(original, str) and original else None
    return original or current
