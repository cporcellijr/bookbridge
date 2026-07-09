"""
Readest annotation spoke.

Bidirectionally syncs KOReader-format highlights/bookmarks stored in the
canonical koreader_annotations hub with the Readest cloud (Supabase-backed
at https://web.readest.com/api).

Push:  local annotations → Readest (using per-row readest_note_id / readest_synced_at)
Pull:  Readest notes  → local hub  (watermark per book stored in readest_sync_watermarks.json)

Book matching uses the full MD5 of the EPUB file (Readest's bookHash
convention), computed from the file on disk via the ebook_parser.

Color and style mappings mirror the Readest KOReader plugin:
  KOReader  ←→  Readest
  yellow    ←→  yellow
  red       ←→  red
  green     ←→  green
  blue      ←→  blue
  purple    ←→  violet
  orange    ←→  #ff8800
  cyan      ←→  #00bcd4
  olive     ←→  #808000
  gray      ←→  #9e9e9e

  KOReader drawer  ←→  Readest style
  lighten          ←→  highlight
  underscore       ←→  underline
  strikeout        ←→  squiggly
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import or_

from src.api.readest_client import ReadestClient
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

# KOReader color name → Readest color value
KO_TO_READEST_COLOR: dict[str, str] = {
    "yellow": "yellow",
    "red": "red",
    "green": "green",
    "blue": "blue",
    "purple": "violet",
    "orange": "#ff8800",
    "cyan": "#00bcd4",
    "olive": "#808000",
    "gray": "#9e9e9e",
    "pink": "#f472b6",
    "white": "#ffffff",
}
READEST_TO_KO_COLOR: dict[str, str] = {v: k for k, v in KO_TO_READEST_COLOR.items()}

KO_TO_READEST_STYLE: dict[str, str] = {
    "lighten": "highlight",
    "underscore": "underline",
    "strikeout": "squiggly",
    "invert": "highlight",
}
READEST_TO_KO_STYLE: dict[str, str] = {
    "highlight": "lighten",
    "underline": "underscore",
    "squiggly": "strikeout",
}

_DEFAULT_KO_COLOR = "yellow"
_DEFAULT_KO_STYLE = "lighten"
_MAX_PUSH_PER_CYCLE = 50


class ReadestAnnotationSync:
    """Spoke class — one instance shared across all users for a sync cycle."""

    def __init__(self, database_service, ebook_parser=None):
        self._db = database_service
        self._ebook_parser = ebook_parser
        self._watermarks: dict[str, int] = {}  # book_hash → last pull ms
        self._watermark_path = self._resolve_watermark_path()
        self._load_watermarks()

    # ------------------------------------------------------------------
    # Watermark persistence (JSON file, avoids an extra migration)
    # ------------------------------------------------------------------

    def _resolve_watermark_path(self) -> Path:
        data_dir = os.environ.get("DATA_DIR", "/data")
        return Path(data_dir) / "readest_sync_watermarks.json"

    def _load_watermarks(self) -> None:
        try:
            if self._watermark_path.exists():
                self._watermarks = json.loads(self._watermark_path.read_text())
        except Exception as e:
            logger.warning("Readest: could not load watermarks: %s", e)
            self._watermarks = {}

    def _save_watermarks(self) -> None:
        try:
            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            self._watermark_path.write_text(json.dumps(self._watermarks))
        except Exception as e:
            logger.warning("Readest: could not save watermarks: %s", e)

    @staticmethod
    def _watermark_key(user_id, book_hash: str) -> str:
        # Per-user: the same EPUB is one book_hash across the whole install, but
        # each user pulls their own Readest notes from their own account, so the
        # watermark must not be shared or the second user misses everything the
        # first already advanced past.
        return f"{user_id}:{book_hash}"

    def _get_watermark(self, user_id, book_hash: str) -> int:
        return int(self._watermarks.get(self._watermark_key(user_id, book_hash), 0))

    def _set_watermark(self, user_id, book_hash: str, ms: int) -> None:
        self._watermarks[self._watermark_key(user_id, book_hash)] = ms
        self._save_watermarks()

    # ------------------------------------------------------------------
    # Color / style helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ko_color_to_readest(color: Optional[str]) -> str:
        return KO_TO_READEST_COLOR.get(str(color or "").strip().lower(), "yellow")

    @staticmethod
    def _readest_color_to_ko(color: Optional[str]) -> str:
        return READEST_TO_KO_COLOR.get(str(color or "").strip().lower(), _DEFAULT_KO_COLOR)

    @staticmethod
    def _ko_style_to_readest(drawer: Optional[str]) -> str:
        return KO_TO_READEST_STYLE.get(str(drawer or "").strip().lower(), "highlight")

    @staticmethod
    def _readest_style_to_ko(style: Optional[str]) -> str:
        return READEST_TO_KO_STYLE.get(str(style or "").strip().lower(), _DEFAULT_KO_STYLE)

    # ------------------------------------------------------------------
    # Datetime helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ms_to_ko_datetime(ms: int) -> str:
        if not ms:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _ko_datetime_to_ms(dt_str: Optional[str]) -> int:
        if not dt_str:
            return int(time.time() * 1000)
        try:
            dt = datetime.strptime(str(dt_str).strip(), "%Y-%m-%d %H:%M:%S")
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return int(time.time() * 1000)

    @staticmethod
    def _iso_to_ms(value) -> int:
        """Parse an ISO-8601 timestamp (as the /sync GET returns) to epoch ms.

        The server hands back raw Postgres rows, so note timestamps arrive as
        snake_case ISO strings (``created_at``/``updated_at``/``deleted_at``),
        not the camelCase millisecond fields the client sends on push. Returns 0
        when the value is missing or unparseable.
        """
        text = str(value or "").strip()
        if not text:
            return 0
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _now_dt() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Book resolution
    # ------------------------------------------------------------------

    def _resolve_epub_path(self, book) -> Optional[Path]:
        if self._ebook_parser is None:
            return None
        filename = (
            str(getattr(book, "original_ebook_filename", "") or "").strip()
            or str(getattr(book, "ebook_filename", "") or "").strip()
        )
        if not filename:
            return None
        try:
            return Path(self._ebook_parser.resolve_book_path(filename))
        except Exception:
            return None

    def _candidate_books(self, user_id) -> list:
        try:
            linked = None
            if hasattr(self._db, "get_linked_abs_ids"):
                result = self._db.get_linked_abs_ids(user_id)
                linked = set(result) if result is not None else None
            books = self._db.get_books_by_status("active") or []
            if linked is not None:
                books = [b for b in books if b.abs_id in linked]
            return [b for b in books if str(getattr(b, "ebook_filename", "") or "").strip()]
        except Exception as e:
            logger.debug("Readest annotation: book enumeration failed for user %s: %s", user_id, e)
            return []

    # ------------------------------------------------------------------
    # Push (local → Readest)
    # ------------------------------------------------------------------

    def _build_push_payload(self, row, book_hash: str, meta_hash: str = "") -> Optional[dict]:
        pos0 = str(row.pos0 or "").strip()
        if not pos0:
            return None

        pos1 = str(row.pos1 or "").strip() or None

        if row.drawer:
            # Annotation (highlight/underline/strikeout)
            note_type = "annotation"
            note_id = row.readest_note_id or ReadestClient.derive_note_id(book_hash, note_type, pos0, pos1)
            return {
                "bookHash": book_hash,
                "metaHash": meta_hash,
                "id": note_id,
                "type": note_type,
                "xpointer0": pos0,
                "xpointer1": pos1,
                "text": row.text or "",
                "note": row.note or None,
                "style": self._ko_style_to_readest(row.drawer),
                "color": self._ko_color_to_readest(row.color),
                "page": row.pageno,
                "createdAt": self._ko_datetime_to_ms(row.datetime),
                "updatedAt": self._ko_datetime_to_ms(row.datetime_updated or row.datetime),
            }
        else:
            # Bookmark (no drawer, pos0 is the xpointer)
            note_type = "bookmark"
            note_id = row.readest_note_id or ReadestClient.derive_note_id(book_hash, note_type, pos0)
            return {
                "bookHash": book_hash,
                "metaHash": meta_hash,
                "id": note_id,
                "type": note_type,
                "xpointer0": pos0,
                "text": row.text or "",
                "note": row.note or None,
                "page": row.pageno,
                "createdAt": self._ko_datetime_to_ms(row.datetime),
                "updatedAt": self._ko_datetime_to_ms(row.datetime_updated or row.datetime),
            }

    def _push_for_book(self, user_id, client: ReadestClient, book, book_hash: str) -> int:
        """Push unsynced / changed local annotations to Readest. Returns push count."""
        from src.db.models import KoreaderAnnotation

        try:
            with self._db.get_session() as session:
                rows = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.md5 == book.kosync_doc_id,
                        KoreaderAnnotation.user_id == user_id,
                        KoreaderAnnotation.deleted == False,  # noqa: E712
                        or_(
                            KoreaderAnnotation.readest_synced_at == None,  # noqa: E711
                            KoreaderAnnotation.updated_at > KoreaderAnnotation.readest_synced_at,
                        ),
                    )
                    .limit(_MAX_PUSH_PER_CYCLE)
                    .all()
                )
                push_ids = [r.id for r in rows]
                notes = []
                id_to_note_id: dict[int, str] = {}
                for row in rows:
                    payload = self._build_push_payload(row, book_hash)
                    if payload:
                        notes.append(payload)
                        id_to_note_id[row.id] = payload["id"]

                # Tombstones: deleted locally but previously pushed to Readest
                tombstone_rows = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.md5 == book.kosync_doc_id,
                        KoreaderAnnotation.user_id == user_id,
                        KoreaderAnnotation.deleted == True,  # noqa: E712
                        KoreaderAnnotation.readest_note_id != None,  # noqa: E711
                        KoreaderAnnotation.readest_deleted_at == None,  # noqa: E711
                    )
                    .all()
                )
                tombstone_ids = [r.id for r in tombstone_rows]
                for row in tombstone_rows:
                    pos0 = str(row.pos0 or "").strip()
                    if not pos0:
                        continue
                    note_type = "annotation" if row.drawer else "bookmark"
                    notes.append({
                        "bookHash": book_hash,
                        "id": row.readest_note_id,
                        "type": note_type,
                        "xpointer0": pos0,
                        "text": "",
                        "createdAt": self._ko_datetime_to_ms(row.datetime),
                        "updatedAt": int(time.time() * 1000),
                        "deletedAt": int(time.time() * 1000),
                    })
        except Exception as e:
            logger.error("Readest push: DB query failed for user %s book %s: %s", user_id, book_hash, e)
            return 0

        if not notes:
            return 0

        if not client.push_notes(notes):
            return 0

        now_dt = self._now_dt()
        pushed = 0
        try:
            with self._db.get_session() as session:
                for ann_id in push_ids:
                    row = session.get(KoreaderAnnotation, ann_id)
                    if row is None:
                        continue
                    row.readest_note_id = id_to_note_id.get(ann_id, row.readest_note_id)
                    row.readest_synced_at = now_dt
                    pushed += 1
                for ann_id in tombstone_ids:
                    row = session.get(KoreaderAnnotation, ann_id)
                    if row is None:
                        continue
                    row.readest_deleted_at = now_dt
                session.commit()
        except Exception as e:
            logger.error("Readest push: DB update failed for user %s: %s", user_id, e)

        return pushed

    # ------------------------------------------------------------------
    # Pull (Readest → local)
    # ------------------------------------------------------------------

    def _pull_for_book(self, user_id, client: ReadestClient, book, book_hash: str) -> int:
        """Pull Readest notes into the local hub. Returns apply count."""
        since_ms = self._get_watermark(user_id, book_hash)
        notes = client.pull_notes(book_hash, since_ms)
        if notes is None:
            return 0
        if not notes:
            return 0

        from src.db.models import KoreaderAnnotation

        applied = 0
        new_watermark = since_ms
        now_dt = self._now_dt()

        try:
            with self._db.get_session() as session:
                for note in notes:
                    # The /sync GET returns raw Postgres rows: snake_case ISO
                    # timestamps, not the camelCase epoch-ms fields push sends.
                    created_ms = self._iso_to_ms(note.get("created_at"))
                    updated_ms = self._iso_to_ms(note.get("updated_at")) or created_ms
                    deleted_ms = self._iso_to_ms(note.get("deleted_at"))
                    # Advance the watermark by the newest of the row's timestamps so
                    # a tombstone (updated_at may predate deleted_at) isn't re-pulled.
                    row_ms = max(updated_ms, deleted_ms)
                    if row_ms > new_watermark:
                        new_watermark = row_ms

                    pos0 = str(note.get("xpointer0") or "").strip()
                    note_id = str(note.get("id") or "").strip()
                    if not pos0 or not note_id:
                        continue

                    # Handle server-side deletions (tombstones)
                    if deleted_ms:
                        self._apply_remote_delete(session, user_id, note_id, pos0, note, now_dt)
                        applied += 1
                        continue

                    pos1 = str(note.get("xpointer1") or "").strip() or None
                    drawer = self._readest_style_to_ko(note.get("style"))
                    color = self._readest_color_to_ko(note.get("color"))
                    text = str(note.get("text") or "").strip() or None
                    note_text = str(note.get("note") or "").strip() or None
                    ko_datetime = self._ms_to_ko_datetime(created_ms)
                    ko_datetime_updated = self._ms_to_ko_datetime(updated_ms) if updated_ms != created_ms else None
                    doc_md5 = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
                    ann_key = self._db.compute_annotation_key(ko_datetime, pos0)

                    # Find existing by readest_note_id first, then by identity key
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.readest_note_id == note_id,
                        )
                        .first()
                    )
                    if row is None:
                        row = (
                            session.query(KoreaderAnnotation)
                            .filter(
                                KoreaderAnnotation.md5 == doc_md5,
                                KoreaderAnnotation.user_id == user_id,
                                KoreaderAnnotation.ann_key == ann_key,
                            )
                            .first()
                        )

                    if row is not None:
                        # Update mutable fields only; never rewrite identity to avoid cascade
                        row.drawer = drawer
                        row.color = color
                        row.note = note_text
                        if text and not row.text:
                            row.text = text
                        row.readest_note_id = note_id
                        row.readest_synced_at = now_dt
                        if row.deleted:
                            row.deleted = False
                            row.deleted_at = None
                    else:
                        row = KoreaderAnnotation(
                            md5=doc_md5,
                            user_id=user_id,
                            ann_key=ann_key,
                            datetime=ko_datetime,
                            datetime_updated=ko_datetime_updated,
                            pos_format="xpointer",
                            pos0=pos0,
                            pos1=pos1,
                            drawer=drawer,
                            color=color,
                            text=text,
                            note=note_text,
                            pageno=note.get("page"),
                            source_device="readest",
                        )
                        row.readest_note_id = note_id
                        row.readest_synced_at = now_dt
                        session.add(row)

                    applied += 1
                session.commit()
        except Exception as e:
            logger.error("Readest pull: DB apply failed for user %s book %s: %s", user_id, book_hash, e)
            return applied

        if new_watermark > since_ms:
            self._set_watermark(user_id, book_hash, new_watermark)
        return applied

    def _apply_remote_delete(self, session, user_id, note_id: str, pos0: str, note: dict, now_dt: datetime) -> None:
        from src.db.models import KoreaderAnnotation

        row = (
            session.query(KoreaderAnnotation)
            .filter(
                KoreaderAnnotation.user_id == user_id,
                KoreaderAnnotation.readest_note_id == note_id,
            )
            .first()
        )
        if row is None:
            return
        if not row.deleted:
            row.deleted = True
            row.deleted_at = now_dt
        row.readest_deleted_at = now_dt

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def sync_user(self, user_id, creds: dict) -> bool:
        client = ReadestClient(credentials=creds, database_service=self._db)
        if not client.is_configured():
            return False

        books = self._candidate_books(user_id)
        if not books:
            return False

        did_work = False
        for book in books:
            epub_path = self._resolve_epub_path(book)
            if epub_path is None:
                continue
            book_hash = ReadestClient.compute_book_hash(epub_path)
            if not book_hash:
                logger.debug("Readest: could not hash EPUB for book %s", getattr(book, "abs_id", "?"))
                continue

            doc_md5 = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
            if not doc_md5:
                continue

            try:
                pushed = self._push_for_book(user_id, client, book, book_hash)
                pulled = self._pull_for_book(user_id, client, book, book_hash)
                if pushed or pulled:
                    logger.info(
                        "Readest annotation sync user=%s book=%s: pushed=%d pulled=%d",
                        user_id, book_hash[:8], pushed, pulled,
                    )
                    did_work = True
            except Exception as e:
                logger.error(
                    "Readest annotation sync failed for user %s book %s: %s",
                    user_id, book_hash[:8], e, exc_info=True,
                )

        return did_work
