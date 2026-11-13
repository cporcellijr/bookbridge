"""Per-reader display settings for the New interface (Account -> Display).

Values live in the reader's own credential rows (like ``UI_MODE``). A reader with
no saved value follows the server value in ``os.environ``, then ``DEFAULT_CONFIG``.
"""

import os
from typing import Mapping, Optional

from src.utils.config_loader import DEFAULT_CONFIG

_BOOL = ("true", "false")
_TRUE_SPELLINGS = frozenset({"true", "on", "1", "yes"})
_FALSE_SPELLINGS = frozenset({"false", "off", "0", "no"})
_HERO_ORDER = ("synopsis", "bars", "listened", "pages")
# Every valid stored value (including base-only) is an allowed configuration.
_HERO_CHOICES = ("",) + _HERO_ORDER + (
    "synopsis,bars", "synopsis,listened", "synopsis,pages",
    "bars,listened", "bars,pages", "listened,pages",
)

DISPLAY_SETTINGS: dict[str, tuple[str, ...]] = {
    "UI_THEME": ("auto", "day", "night"),
    "UI_LIBRARY_STYLE": ("cards", "faceout", "spines", "list"),
    "UI_SHELF_FINISH": ("theme", "wood", "dark", "plain"),
    "UI_COVER_SIZE": ("1", "2", "3", "4", "5"),
    "UI_SHOW_PROGRESS": _BOOL,
    "UI_SHOW_FINISHED": _BOOL,
    "UI_SHOW_FORMAT_STRIP": _BOOL,
    "UI_SHOW_RATINGS": _BOOL,
    "UI_SHOW_TITLES": _BOOL,
    "UI_SHOW_HERO": _BOOL,
    "UI_HERO_CONTENT": _HERO_CHOICES,
    "SERIES_SHOW_CURRENT_BOOK_DETAIL": _BOOL,
}


def normalize_display_value(key: str, raw: object) -> Optional[str]:
    """Return the stored form of ``raw`` for ``key``, or None if the key or value is not allowed."""
    allowed = DISPLAY_SETTINGS.get(key)
    if allowed is None or raw is None:
        return None
    value = str(raw).strip().lower()
    if key == "UI_HERO_CONTENT":
        # Empty is intentional: only the P16c base. Unknown/duplicate entries disappear.
        choices = set(part.strip() for part in value.split(","))
        return ",".join([piece for piece in _HERO_ORDER if piece in choices][:2])
    if allowed is _BOOL:
        if value in _TRUE_SPELLINGS:
            return "true"
        if value in _FALSE_SPELLINGS:
            return "false"
        return None
    return value if value in allowed else None


def resolve_display_settings(stored: Mapping[str, object]) -> dict[str, str]:
    """Every display setting for one reader: their value, else the server value, else the default."""
    resolved = {}
    for key in DISPLAY_SETTINGS:
        value = normalize_display_value(key, stored.get(key))
        if value is None:
            value = normalize_display_value(key, os.environ.get(key))
        resolved[key] = value if value is not None else DEFAULT_CONFIG[key]
    return resolved
