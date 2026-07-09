"""
Hardcover annotation spoke.

Writes KOReader highlights (drawer=lighten) to the `private_notes` field on
the matching `user_books` record in Hardcover.  Hardcover has no per-highlight
API — `private_notes` is a free-form String on user_books and is the only
available annotation storage.

Each sync cycle collects all active (non-deleted) lighten highlights for a book
and formats them into a plain-text block that is written verbatim to
`private_notes`.  The last-written state is tracked via `hardcover_synced_at`
on each annotation row; if every annotation has `hardcover_synced_at` equal to
(or later than) its `updated_at` the book is skipped.

Deletions are handled implicitly: deleted rows are excluded from the formatted
block, so the next write will omit them.

Book matching re-uses HardcoverDetails already populated by the progress sync
client (hardcover_book_id). Books without a Hardcover match are silently
skipped.

Color mapping (KOReader name → display label in formatted block):
  yellow, red, green, blue, purple, orange, pink, cyan, olive, gray, white
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.api.hardcover_client import HardcoverClient
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_MAX_ANNOTATIONS = 500

# The bridge only manages the region between these markers inside private_notes,
# so a user's own notes above/below survive a rewrite.
_MANAGED_START = "<!-- BookBridge highlights (auto-generated) -->"
_MANAGED_END = "<!-- /BookBridge highlights -->"

KO_TO_HARDCOVER_COLOR: dict[str, str] = {
    "yellow": "yellow",
    "red": "red",
    "green": "green",
    "blue": "blue",
    "purple": "purple",
    "orange": "orange",
    "pink": "pink",
    "cyan": "cyan",
    "olive": "olive",
    "gray": "gray",
    "white": "white",
}


class HardcoverAnnotationSync:
    """Spoke class — one instance per sync cycle, shared across users."""

    def __init__(self, database_service):
        self._db = database_service

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ko_color_label(color: Optional[str]) -> str:
        return KO_TO_HARDCOVER_COLOR.get(str(color or "").strip().lower(), "")

    @staticmethod
    def _now_dt() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Book-level resolution
    # ------------------------------------------------------------------

    def _hardcover_details(self, book):
        try:
            return self._db.get_hardcover_details(book.abs_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_annotation(row) -> str:
        parts = []
        if row.pageno:
            parts.append(f"p.{row.pageno}")
        color = KO_TO_HARDCOVER_COLOR.get(str(row.color or "").strip().lower(), "")
        if color:
            parts.append(color)
        header = " | ".join(parts)
        lines = []
        if header:
            lines.append(f"[{header}]")
        if row.text:
            lines.append(f'"{row.text}"')
        if row.note:
            lines.append(f"Note: {row.note}")
        return "\n".join(lines)

    def _build_notes_block(self, rows) -> str:
        sections = []
        for row in rows:
            block = self._format_annotation(row)
            if block.strip():
                sections.append(block)
        return "\n\n---\n\n".join(sections)

    @staticmethod
    def _splice_managed(existing: Optional[str], block: str) -> str:
        """Insert/replace the bridge-managed block, preserving the user's own text.

        Only the region between the managed markers is ours; anything the user
        wrote above or below is kept verbatim. An empty block removes the managed
        region entirely (e.g. after the last highlight is deleted).
        """
        existing = existing or ""
        managed = f"{_MANAGED_START}\n{block}\n{_MANAGED_END}" if block.strip() else ""
        start = existing.find(_MANAGED_START)
        end = existing.find(_MANAGED_END)
        if start != -1 and end != -1 and end > start:
            pre = existing[:start].rstrip()
            post = existing[end + len(_MANAGED_END):].lstrip()
            parts = [p for p in (pre, managed, post) if p]
            return "\n\n".join(parts)
        if not managed:
            return existing
        if existing.strip():
            return existing.rstrip() + "\n\n" + managed
        return managed

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _needs_sync(self, rows) -> bool:
        """Return True if any row has been updated since last Hardcover sync."""
        for row in rows:
            if row.hardcover_synced_at is None:
                return True
            if row.updated_at and row.updated_at > row.hardcover_synced_at:
                return True
        return False

    def _sync_book(self, user_id, client: HardcoverClient, book) -> bool:
        from src.db.models import KoreaderAnnotation

        details = self._hardcover_details(book)
        if details is None:
            return False
        hardcover_book_id = getattr(details, "hardcover_book_id", None)
        if not hardcover_book_id:
            return False

        doc_md5 = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
        if not doc_md5:
            return False

        try:
            with self._db.get_session() as session:
                rows = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.md5 == doc_md5,
                        KoreaderAnnotation.user_id == user_id,
                        KoreaderAnnotation.deleted == False,  # noqa: E712
                        KoreaderAnnotation.drawer == "lighten",
                        KoreaderAnnotation.text != None,  # noqa: E711
                    )
                    .order_by(KoreaderAnnotation.pageno)
                    .limit(_MAX_ANNOTATIONS)
                    .all()
                )
                # Highlights deleted since their last push: the live-row query
                # can't see them, so without this a deletion-only change never
                # rewrites the block and the highlight lingers on Hardcover.
                deleted_since = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.md5 == doc_md5,
                        KoreaderAnnotation.user_id == user_id,
                        KoreaderAnnotation.deleted == True,  # noqa: E712
                        KoreaderAnnotation.hardcover_synced_at != None,  # noqa: E711
                        KoreaderAnnotation.deleted_at != None,  # noqa: E711
                        KoreaderAnnotation.deleted_at > KoreaderAnnotation.hardcover_synced_at,
                    )
                    .all()
                )

                if not self._needs_sync(rows) and not deleted_since:
                    return False

                # Only now that a change is confirmed do we spend Hardcover API
                # calls (it is rate-limited) — resolve the user_book and its
                # current private_notes so we can splice, not clobber.
                user_book_id, existing_notes = client.get_user_book_summary(int(hardcover_book_id))
                if not user_book_id:
                    return False

                notes_text = self._build_notes_block(rows)
                new_notes = self._splice_managed(existing_notes, notes_text)

                if new_notes != (existing_notes or ""):
                    if not client.update_private_notes(user_book_id, new_notes):
                        return False

                now_dt = self._now_dt()
                for row in rows:
                    row.hardcover_synced_at = now_dt
                for row in deleted_since:
                    row.hardcover_synced_at = now_dt

                session.commit()
        except Exception as e:
            logger.error(
                "Hardcover annotation sync failed for user %s book %s: %s",
                user_id, getattr(book, "abs_id", "?"), e, exc_info=True,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def sync_user(self, user_id, creds: dict) -> bool:
        client = HardcoverClient(credentials=creds)
        if not client.is_configured():
            return False

        try:
            books = self._db.get_books_by_status("active") or []
        except Exception as e:
            logger.debug("Hardcover annotation: book enumeration failed for user %s: %s", user_id, e)
            return False

        try:
            linked = None
            if hasattr(self._db, "get_linked_abs_ids"):
                result = self._db.get_linked_abs_ids(user_id)
                linked = set(result) if result is not None else None
            if linked is not None:
                books = [b for b in books if b.abs_id in linked]
        except Exception:
            pass

        did_work = False
        for book in books:
            try:
                if self._sync_book(user_id, client, book):
                    did_work = True
            except Exception as e:
                logger.error(
                    "Hardcover annotation sync error user %s book %s: %s",
                    user_id, getattr(book, "abs_id", "?"), e,
                )
        return did_work
