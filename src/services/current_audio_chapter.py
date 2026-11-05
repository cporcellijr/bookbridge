"""Presentation-only current audio chapter from stored book-relative seconds.

No provider progress reads, writes, or synthetic track chapters.
"""

import math
from typing import Optional


def real_chapter_rows(chapters: list[dict]) -> list[dict]:
    """Keep only finite, positive-duration chapter ranges and their genuine titles."""
    valid = []
    for row in chapters or []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and start >= 0 and end > start):
            continue
        title = row.get("title")
        title = title.strip() if isinstance(title, str) else None
        valid.append({"title": title or None, "start": start, "end": end})
    return sorted(valid, key=lambda item: item["start"])


def resolve_current_chapter(
    chapters: list[dict], position: Optional[float], source: str
) -> Optional[dict]:
    """Resolve [start, end) at stored absolute seconds; clamp beyond the final end."""
    try:
        seconds = float(position)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    rows = real_chapter_rows(chapters)
    for index, row in enumerate(rows):
        final = index == len(rows) - 1
        if row["start"] <= seconds < row["end"] or (final and seconds >= row["end"]):
            length = row["end"] - row["start"]
            return {
                "title": row["title"],
                "index": index + 1,
                "count": len(rows),
                "start": row["start"],
                "end": row["end"],
                "elapsed": min(seconds - row["start"], length),
                "length": length,
                "source": source,
            }
    return None
