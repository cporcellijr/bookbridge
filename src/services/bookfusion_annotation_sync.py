"""BookFusion annotation spoke.

Relays KOReader-format hub annotations to BookFusion highlights and pulls
BookFusion highlights back into the canonical hub. BookFusion positions are
chapter-index plus UTF-16 offsets, so all placement goes through
``bookfusion_offsets``.
"""

import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

from src.api.bookfusion_client import BookFusionClient
from src.utils.bookfusion_offsets import BookFusionOffsetResolver, utf16_len

logger = logging.getLogger(__name__)

SPOKE_KEY = "@bookfusion"
_SERVER_ID_FIELD = "bookfusion_highlight_id"
_VERSION_FIELD = "bookfusion_version"
_SYNCED_AT_FIELD = "bookfusion_synced_at"
_MAX_PUSH_PER_BOOK = 50

KO_TO_BOOKFUSION_COLOR: dict[str, str] = {
    "yellow": "#FFFF33",
    "green": "#00AA66",
    "blue": "#0066FF",
    "red": "#FF3300",
    "orange": "#FF8800",
    "purple": "#EE00FF",
    "cyan": "#00FFEE",
    "olive": "#88FF77",
    "gray": "#808080",
}
_DEFAULT_KO_COLOR = "yellow"
_DEFAULT_KO_STYLE = "lighten"


class BookFusionAnnotationSync:
    """Spoke class for bidirectional BookFusion highlight sync."""

    def __init__(self, database_service, ebook_parser=None) -> None:
        self._db = database_service
        self._ebook_parser = ebook_parser
        self._offsets = BookFusionOffsetResolver(ebook_parser) if ebook_parser is not None else None

    def sync_user(self, user_id: int, creds: dict) -> bool:
        """Sync all linked BookFusion books for one user."""
        client = BookFusionClient(credentials=creds, database_service=self._db, user_id=user_id)
        if not client.is_configured():
            return False
        books = self._candidate_books(user_id)
        did_work = False
        for book in books:
            book_id = self._bookfusion_id(user_id, book)
            doc_md5 = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
            filename = self._resolve_epub_filename(book)
            if not book_id or not doc_md5 or not filename:
                continue
            try:
                pushed = self._push_for_book(user_id, client, book, book_id, doc_md5, filename)
                pulled = self._pull_for_book(user_id, client, book_id, doc_md5, filename)
                if pushed or pulled:
                    logger.info(
                        "BookFusion annotation sync user=%s book=%s: pushed=%d pulled=%d",
                        user_id,
                        book_id,
                        pushed,
                        pulled,
                    )
                    did_work = True
            except Exception as exc:
                logger.error("BookFusion annotation sync failed for user %s book %s: %s", user_id, book_id, exc, exc_info=True)
        return did_work

    def _candidate_books(self, user_id: int) -> list:
        try:
            linked = None
            if hasattr(self._db, "get_linked_abs_ids"):
                result = self._db.get_linked_abs_ids(user_id)
                linked = set(result) if result is not None else None
            books = self._db.get_books_by_status("active") or []
            if linked is not None:
                books = [b for b in books if b.abs_id in linked]
            return [b for b in books if self._bookfusion_id(user_id, b)]
        except Exception as exc:
            logger.debug("BookFusion annotation: book enumeration failed for user %s: %s", user_id, exc)
            return []

    def _bookfusion_id(self, user_id: int, book) -> Optional[str]:
        if hasattr(self._db, "resolve_bookfusion_id"):
            resolved = self._db.resolve_bookfusion_id(user_id, book)
            if resolved not in (None, ""):
                return str(resolved)
        return None

    @staticmethod
    def _resolve_epub_filename(book) -> Optional[str]:
        value = (
            str(getattr(book, "original_ebook_filename", "") or "").strip()
            or str(getattr(book, "ebook_filename", "") or "").strip()
        )
        return value or None

    def _push_for_book(self, user_id: int, client: BookFusionClient, book, book_id: str,
                       doc_md5: str, filename: str) -> int:
        state = self._db.get_annotation_spoke_state(
            user_id,
            doc_md5,
            SPOKE_KEY,
            server_id_field=_SERVER_ID_FIELD,
            version_field=_VERSION_FIELD,
        )
        pushed_ids: list[int] = []
        tombstone_ids: list[int] = []
        server_ids: dict[int, int] = {}
        versions: dict[int, int] = {}

        for tombstone in state.get("pending_deletes") or []:
            remote_id = tombstone.get("serverId")
            if remote_id is not None and client.delete_highlight(remote_id):
                tombstone_ids.append(int(tombstone["_id"]))

        for entry in (state.get("changes") or [])[:_MAX_PUSH_PER_BOOK]:
            payload = self._build_push_payload(book_id, filename, entry)
            if not payload:
                continue
            local_id = int(entry["_id"])
            remote_id = entry.get("_spoke_server_id")
            if remote_id:
                patch = {
                    "note": payload.get("note"),
                    "color": payload.get("color"),
                }
                if client.update_highlight(remote_id, patch):
                    pushed_ids.append(local_id)
                    versions[local_id] = self._version_from_payload({"updated_at": datetime.now(timezone.utc).isoformat()})
                continue
            created = client.create_highlight(payload)
            created_id = self._coerce_remote_id((created or {}).get("id"))
            if created_id is None:
                continue
            pushed_ids.append(local_id)
            server_ids[local_id] = created_id
            versions[local_id] = self._version_from_payload(created or {})

        if pushed_ids or tombstone_ids:
            self._db.mark_spoke_annotations_uploaded(
                user_id,
                SPOKE_KEY,
                pushed_ids,
                tombstone_ids=tombstone_ids,
                server_id_field=_SERVER_ID_FIELD,
                version_field=_VERSION_FIELD,
                synced_at_field=_SYNCED_AT_FIELD,
                server_ids_by_annotation_id=server_ids,
                versions_by_annotation_id=versions,
            )
        return len(pushed_ids) + len(tombstone_ids)

    def _pull_for_book(self, user_id: int, client: BookFusionClient, book_id: str,
                       doc_md5: str, filename: str) -> int:
        remote = client.pull_highlights(book_id)
        if remote is None:
            return 0
        known = set(self._db.get_spoke_server_ids_for_book(user_id, doc_md5, server_id_field=_SERVER_ID_FIELD) or [])
        remote_ids = {rid for rid in (self._coerce_remote_id(item.get("id")) for item in remote) if rid is not None}

        adds, edits = [], []
        for item in remote:
            entry = self._remote_to_entry(filename, item)
            if not entry:
                continue
            if entry["serverId"] in known:
                edits.append(entry)
            else:
                adds.append(entry)

        deletes = [{"serverId": rid} for rid in sorted(known - remote_ids)]
        result = self._db.apply_spoke_annotations(
            user_id,
            doc_md5,
            SPOKE_KEY,
            adds,
            edits,
            deletes,
            server_id_field=_SERVER_ID_FIELD,
            version_field=_VERSION_FIELD,
            synced_at_field=_SYNCED_AT_FIELD,
            trust_positions=True,
        )
        return len(result.get("applied") or []) + len(result.get("deleted") or [])

    def _build_push_payload(self, book_id: str, filename: str, entry: dict) -> Optional[dict]:
        if self._offsets is None:
            return None
        offsets = self._offsets.xpointer_to_offsets(filename, entry.get("pos0"), entry.get("pos1"))
        if not offsets:
            return None
        quote_text = str(entry.get("text") or "")
        if offsets["end_offset"] <= offsets["start_offset"] and quote_text:
            offsets["end_offset"] = offsets["start_offset"] + utf16_len(quote_text)
        if offsets["end_offset"] <= offsets["start_offset"]:
            return None
        return {
            "book_id": book_id,
            "chapter_index": offsets["chapter_index"],
            "start_offset": offsets["start_offset"],
            "end_offset": offsets["end_offset"],
            "quote_text": quote_text,
            "quote_prefix": offsets.get("quote_prefix") or "",
            "quote_suffix": offsets.get("quote_suffix") or "",
            "note": entry.get("note") or None,
            "color": self._ko_color_to_bookfusion(entry.get("color")),
        }

    def _remote_to_entry(self, filename: str, item: dict) -> Optional[dict]:
        if self._offsets is None:
            return None
        remote_id = self._coerce_remote_id(item.get("id"))
        if remote_id is None:
            return None
        try:
            chapter_index = int(item.get("chapter_index") or 0)
            start = int(item.get("start_offset"))
            end = int(item.get("end_offset"))
        except (TypeError, ValueError):
            return None
        resolved = self._offsets.offsets_to_xpointers(filename, chapter_index, start, end)
        if not resolved:
            return None
        created = self._ko_datetime_from_iso(item.get("added_at") or item.get("created_at") or item.get("updated_at"))
        updated = self._ko_datetime_from_iso(item.get("updated_at"))
        quote = str(item.get("quote_text") or resolved.get("text") or "").strip() or None
        return {
            "serverId": remote_id,
            "version": self._version_from_payload(item),
            "datetime": created,
            "datetime_updated": updated if updated != created else None,
            "pos0": resolved["pos0"],
            "pos1": resolved["pos1"],
            "drawer": _DEFAULT_KO_STYLE,
            "color": self._bookfusion_color_to_ko(item.get("color")),
            "text": quote,
            "note": str(item.get("note") or "").strip() or None,
            "chapter": str(item.get("chapter_title") or "").strip() or None,
        }

    @staticmethod
    def _coerce_remote_id(value) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ko_color_to_bookfusion(value: Optional[str]) -> str:
        return KO_TO_BOOKFUSION_COLOR.get(str(value or "").strip().lower(), KO_TO_BOOKFUSION_COLOR[_DEFAULT_KO_COLOR])

    @staticmethod
    def _bookfusion_color_to_ko(value: Optional[str]) -> str:
        rgb = BookFusionAnnotationSync._parse_hex(value)
        if rgb is None:
            return _DEFAULT_KO_COLOR
        best = _DEFAULT_KO_COLOR
        best_dist = float("inf")
        for name, hex_value in KO_TO_BOOKFUSION_COLOR.items():
            candidate = BookFusionAnnotationSync._parse_hex(hex_value)
            if candidate is None:
                continue
            dist = math.sqrt(sum((rgb[i] - candidate[i]) ** 2 for i in range(3)))
            if dist < best_dist:
                best = name
                best_dist = dist
        return best

    @staticmethod
    def _parse_hex(value: Optional[str]) -> Optional[tuple[int, int, int]]:
        text = str(value or "").strip()
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", text)
        if not match:
            return None
        raw = match.group(1)
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)

    @staticmethod
    def _ko_datetime_from_iso(value) -> str:
        text = str(value or "").strip()
        if not text:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _version_from_payload(payload: dict) -> int:
        for key in ("updated_at", "added_at", "created_at"):
            text = str(payload.get(key) or "").strip()
            if not text:
                continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(1, int(parsed.timestamp()))
            except ValueError:
                continue
        return max(1, int(datetime.now(timezone.utc).timestamp()))
