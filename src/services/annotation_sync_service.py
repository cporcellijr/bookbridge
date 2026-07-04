"""
Annotation hub — BookOrbit spoke.

The bridge stores canonical KOReader-format annotations (xpointer + highlighted
text) per (user, document md5) and relays them between devices via the exchange
endpoints in kosync_server. This service extends the relay to BookOrbit's web
reader: the bridge acts as one more "device" against BookOrbit's own
koreader-plugin exchange endpoint, so BookOrbit's server does every CFI <->
xpointer conversion and the bridge never needs a render engine.

Per user with BookOrbit KOReader-sync credentials configured
(BOOKORBIT_KOSYNC_USER / BOOKORBIT_KOSYNC_KEY), each cycle:
  1. collects the documents that have local annotations plus the user's linked
     books (so web-first highlights are pulled even before any device annotates),
  2. exchanges keys + changed entries with BookOrbit (key omission propagates
     deletions in both directions),
  3. applies BookOrbit's toApply delta into the canonical store (bumping
     versions so devices pick the changes up on their next exchange), and
  4. acks what landed.

Documents BookOrbit reports as ``unmatched`` (hash unknown to it) are cached
with a recheck TTL — a book added to BookOrbit later gets picked up on the
next probe instead of staying skipped until a bridge restart.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.utils.grimmory_cfi import GrimmoryCFIResolver
from src.utils.user_config import resolve_setting, _ALLOW_GLOBAL_FALLBACK_KEY

logger = logging.getLogger(__name__)

SPOKE_KEY = "@bookorbit"
BOOKLORE_SPOKE_KEY = "@booklore"
# Grimmory's web reader saves its highlight-with-note flow to a SECOND store
# (book_notes_v2, /api/v2/book-notes) with its own id space — synced as its
# own sub-spoke so web-reader notes reach devices too.
BOOKLORE_NOTES_SPOKE_KEY = "@booklore-notes"
_MAX_BOOKS_PER_CALL = 20
_MAX_CHANGES_PER_BOOK = 50
_MAX_PULL_ROUNDS = 5
_UNMATCHED_RECHECK_SECONDS = 6 * 3600

KOREADER_TO_GRIMMORY_COLOR = {
    "yellow": "#FFC107",
    "green": "#4ADE80",
    "cyan": "#38BDF8",
    "pink": "#F472B6",
    "orange": "#FB923C",
    "red": "#FB523C",
    "purple": "#F452FC",
    "blue": "#0248F8",
    "gray": "#AAAAAA",
    "white": "#FAFAFA",
}
GRIMMORY_TO_KOREADER_COLOR = {value: key for key, value in KOREADER_TO_GRIMMORY_COLOR.items()}
KOREADER_TO_GRIMMORY_STYLE = {
    "lighten": "highlight",
    "underscore": "underline",
    "strikeout": "strikethrough",
}
GRIMMORY_TO_KOREADER_STYLE = {value: key for key, value in KOREADER_TO_GRIMMORY_STYLE.items()}
DEFAULT_KOREADER_COLOR = "yellow"
DEFAULT_KOREADER_STYLE = "lighten"
DEFAULT_GRIMMORY_COLOR = KOREADER_TO_GRIMMORY_COLOR[DEFAULT_KOREADER_COLOR]
DEFAULT_GRIMMORY_STYLE = KOREADER_TO_GRIMMORY_STYLE[DEFAULT_KOREADER_STYLE]


class AnnotationSyncService:
    def __init__(self, database_service, ebook_parser=None, epub_cache_dir=None):
        self.database_service = database_service
        self.ebook_parser = ebook_parser
        self.epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir is not None else Path(
            os.environ.get("DATA_DIR", "/data")
        ) / "epub_cache"
        # {(user_id, md5): last_checked_epoch} — re-probed after the TTL.
        self._unmatched: dict[tuple[int | None, str], float] = {}
        self._lock = threading.Lock()

    def _is_unmatched(self, user_id, doc_md5: str) -> bool:
        checked_at = self._unmatched.get((user_id, doc_md5))
        if checked_at is None:
            return False
        if time.time() - checked_at >= _UNMATCHED_RECHECK_SECONDS:
            del self._unmatched[(user_id, doc_md5)]
            return False
        return True

    # ------------------------------------------------------------------
    # Cycle driver
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_account(value) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _truthy(value, default: bool = False) -> bool:
        if value in (None, ""):
            return default
        return str(value).strip().lower() in {"true", "1", "yes", "on"}

    @staticmethod
    def _ko_datetime_from_iso(value) -> str:
        text = str(value or "").strip()
        if not text:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", text)
            if match:
                return f"{match.group(1)} {match.group(2)}"
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _booklore_color_from_koreader(value) -> str:
        return KOREADER_TO_GRIMMORY_COLOR.get(str(value or "").strip().lower(), DEFAULT_GRIMMORY_COLOR)

    @staticmethod
    def _koreader_color_from_booklore(value) -> str:
        return GRIMMORY_TO_KOREADER_COLOR.get(str(value or "").strip().upper(), DEFAULT_KOREADER_COLOR)

    @staticmethod
    def _booklore_style_from_koreader(value) -> str:
        return KOREADER_TO_GRIMMORY_STYLE.get(str(value or "").strip().lower(), DEFAULT_GRIMMORY_STYLE)

    @staticmethod
    def _koreader_style_from_booklore(value) -> str:
        return GRIMMORY_TO_KOREADER_STYLE.get(str(value or "").strip().lower(), DEFAULT_KOREADER_STYLE)

    @staticmethod
    def _same_remote_anchor(payload: dict, remote: dict | None) -> bool:
        if not remote:
            return False
        return (
            str(payload.get("cfi") or "") == str(remote.get("cfi") or "")
            and str(payload.get("text") or "") == str(remote.get("text") or "")
            and str(payload.get("chapterTitle") or "") == str(remote.get("chapterTitle") or "")
        )

    def _annotation_owner_matches(self, user_id, creds: dict, kosync_user: str) -> bool:
        """Guard against relaying a user's highlights into another BookOrbit account.

        BookOrbit's KOReader sync username can be a separate row from the web
        username, and the public KOReader auth endpoint does not disclose the
        owning web user. To keep ownership explicit, allow the relay only when
        the KOReader username matches BOOKORBIT_USER, or when
        BOOKORBIT_KOSYNC_OWNER explicitly names that same BookOrbit user.
        """
        bookorbit_user = str(resolve_setting(creds, "BOOKORBIT_USER", "") or "").strip()
        if not bookorbit_user:
            logger.warning(
                "Annotation sync skipped for user %s: BOOKORBIT_USER is required "
                "so highlights cannot be written to an unknown BookOrbit account",
                user_id,
            )
            return False

        asserted_owner = str(resolve_setting(creds, "BOOKORBIT_KOSYNC_OWNER", "") or "").strip()
        effective_owner = asserted_owner or kosync_user
        if self._norm_account(effective_owner) == self._norm_account(bookorbit_user):
            return True

        if asserted_owner:
            logger.warning(
                "Annotation sync skipped for user %s: BOOKORBIT_KOSYNC_OWNER=%r "
                "does not match BOOKORBIT_USER=%r",
                user_id, asserted_owner, bookorbit_user,
            )
        else:
            logger.warning(
                "Annotation sync skipped for user %s: BOOKORBIT_KOSYNC_USER=%r "
                "does not match BOOKORBIT_USER=%r. Use KOReader sync credentials "
                "owned by this BookOrbit user, or set BOOKORBIT_KOSYNC_OWNER to "
                "the same BookOrbit username after verifying the KOReader account "
                "belongs to it.",
                user_id, kosync_user, bookorbit_user,
            )
        return False

    def run_cycle(self) -> dict:
        """Sync every configured user's annotations with BookOrbit once."""
        if not self._lock.acquire(blocking=False):
            logger.debug("Annotation sync cycle already running; skipping")
            return {"users": 0}
        try:
            users = self._enumerate_users()
            synced_users = 0
            for user_id, creds in users:
                synced_this_user = False
                kosync_user = str(resolve_setting(creds, "BOOKORBIT_KOSYNC_USER", "") or "").strip()
                kosync_key = BookOrbitClient.normalize_kosync_key(
                    resolve_setting(creds, "BOOKORBIT_KOSYNC_KEY", "")
                )
                if kosync_user and kosync_key and self._annotation_owner_matches(user_id, creds, kosync_user):
                    client = BookOrbitClient(credentials=creds)
                    if str(resolve_setting(creds, "BOOKORBIT_SERVER", "") or "").strip():
                        try:
                            self.sync_user(user_id, client, kosync_user, kosync_key)
                            synced_this_user = True
                        except Exception as e:
                            logger.error("BookOrbit annotation sync failed for user %s: %s", user_id, e, exc_info=True)

                if self._truthy(resolve_setting(creds, "BOOKLORE_ANNOTATION_SYNC", "false")):
                    client = BookloreClient(database_service=self.database_service, credentials=creds)
                    if client.is_configured():
                        try:
                            if self.sync_booklore_user(user_id, client):
                                synced_this_user = True
                        except Exception as e:
                            logger.error("Grimmory annotation sync failed for user %s: %s", user_id, e, exc_info=True)

                if synced_this_user:
                    synced_users += 1
            return {"users": synced_users}
        finally:
            self._lock.release()

    def _enumerate_users(self) -> list:
        """(user_id, credential-dict) pairs; admins fall back to global settings."""
        users = []
        try:
            for user in self.database_service.list_users() or []:
                if not getattr(user, "active", 1):
                    continue
                creds = dict(self.database_service.get_user_credentials(user.id) or {})
                creds[_ALLOW_GLOBAL_FALLBACK_KEY] = bool(getattr(user, "is_admin", False))
                users.append((user.id, creds))
        except Exception as e:
            logger.debug("Annotation sync could not enumerate users: %s", e)
        return users

    # ------------------------------------------------------------------
    # Per-user exchange
    # ------------------------------------------------------------------

    def _candidate_md5s(self, user_id) -> list[str]:
        md5s = set(self.database_service.get_annotation_md5s_for_user(user_id) or [])
        # Linked/active books too, so web-first highlights get pulled.
        try:
            linked = None
            if hasattr(self.database_service, "get_linked_abs_ids"):
                linked = self.database_service.get_linked_abs_ids(user_id)
                linked = set(linked) if linked is not None else None
            for book in self.database_service.get_books_by_status("active") or []:
                if linked is not None and book.abs_id not in linked:
                    continue
                doc_hash = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
                if doc_hash and len(doc_hash) == 32:
                    md5s.add(doc_hash)
        except Exception as e:
            logger.debug("Annotation sync book enumeration failed for user %s: %s", user_id, e)
        return sorted(md5s)

    def sync_user(self, user_id, client: BookOrbitClient, kosync_user: str, kosync_key: str) -> None:
        md5s = [
            m for m in self._candidate_md5s(user_id)
            if not self._is_unmatched(user_id, m)
        ]
        if not md5s:
            return

        for start in range(0, len(md5s), _MAX_BOOKS_PER_CALL):
            chunk = md5s[start:start + _MAX_BOOKS_PER_CALL]
            self._exchange_chunk(user_id, client, kosync_user, kosync_key, chunk)

    def _exchange_chunk(self, user_id, client, kosync_user, kosync_key, md5s: list[str]) -> None:
        for _round in range(_MAX_PULL_ROUNDS):
            books_payload = []
            uploaded_ids_by_hash = {}
            tombstone_ids_by_hash = {}
            for doc_md5 in md5s:
                state = self.database_service.get_annotation_spoke_state(user_id, doc_md5, SPOKE_KEY)
                changes = state["changes"][:_MAX_CHANGES_PER_BOOK]
                uploaded_ids_by_hash[doc_md5] = [c.pop("_id") for c in changes]
                tombstone_ids_by_hash[doc_md5] = state["pending_delete_acks"]
                for change in changes:
                    # BookOrbit's DTO rejects unknown/null-required fields.
                    change.pop("_spoke_server_id", None)
                    change.pop("_spoke_version", None)
                    change.pop("serverId", None)
                    change.pop("version", None)
                    for key in list(change.keys()):
                        if change[key] is None:
                            del change[key]
                books_payload.append({
                    "hash": doc_md5,
                    "keys": state["keys"],
                    "keysComplete": True,
                    "changes": changes,
                })

            if not books_payload:
                return

            response = client.koreader_exchange_annotations(kosync_user, kosync_key, books_payload)
            if response is None:
                logger.warning("Annotation exchange with BookOrbit failed for user %s", user_id)
                return

            for unmatched_hash in response.get("unmatched") or []:
                self._unmatched[(user_id, str(unmatched_hash).lower())] = time.time()

            more = False
            ack_books = []
            for result in response.get("results") or []:
                doc_md5 = str(result.get("hash") or "").lower()
                if not doc_md5:
                    continue
                to_apply = result.get("toApply") or {}
                acks = self.database_service.apply_spoke_annotations(
                    user_id, doc_md5, SPOKE_KEY,
                    adds=to_apply.get("add") or [],
                    edits=to_apply.get("edit") or [],
                    deletes=to_apply.get("delete") or [],
                )
                # Upload bookkeeping: BookOrbit ingested our changes + processed
                # our key-omission deletions on receipt.
                self.database_service.mark_spoke_annotations_uploaded(
                    user_id, SPOKE_KEY,
                    annotation_ids=uploaded_ids_by_hash.get(doc_md5) or [],
                    tombstone_ids=tombstone_ids_by_hash.get(doc_md5) or [],
                )
                if acks["applied"] or acks["deleted"]:
                    ack_books.append({
                        "hash": doc_md5,
                        "applied": acks["applied"],
                        "deleted": acks["deleted"],
                    })
                if result.get("more"):
                    more = True

            if ack_books:
                client.koreader_exchange_annotations_ack(kosync_user, kosync_key, ack_books)

            applied_any = bool(ack_books)
            if applied_any:
                logger.info(
                    "📝 Annotation sync: applied BookOrbit changes for user %s (%d book(s))",
                    user_id, len(ack_books),
                )
            if not more:
                return

    # ------------------------------------------------------------------
    # Grimmory / BookLore spoke
    # ------------------------------------------------------------------

    def _candidate_booklore_books(self, user_id) -> list[dict]:
        candidates = []
        try:
            linked = None
            if hasattr(self.database_service, "get_linked_abs_ids"):
                linked = self.database_service.get_linked_abs_ids(user_id)
                linked = set(linked) if linked is not None else None
            for book in self.database_service.get_books_by_status("active") or []:
                if linked is not None and book.abs_id not in linked:
                    continue
                source = str(getattr(book, "ebook_source", "") or "").strip().lower()
                if source not in {"booklore", "grimmory"}:
                    continue
                doc_hash = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
                book_id = str(getattr(book, "ebook_source_id", "") or "").strip()
                if not (doc_hash and len(doc_hash) == 32 and book_id):
                    continue
                candidates.append({
                    "doc_md5": doc_hash,
                    "book_id": book_id,
                    "filename": (
                        str(getattr(book, "original_ebook_filename", "") or "").strip()
                        or str(getattr(book, "ebook_filename", "") or "").strip()
                        or f"booklore_{book_id}.epub"
                    ),
                    "title": str(getattr(book, "abs_title", "") or "").strip(),
                })
        except Exception as e:
            logger.debug("Grimmory annotation book enumeration failed for user %s: %s", user_id, e)
        return candidates

    def _resolve_booklore_epub_path(self, client: BookloreClient, candidate: dict) -> Path | None:
        if self.ebook_parser is None:
            logger.warning("Grimmory annotation sync skipped: ebook parser is not configured")
            return None

        filename = candidate["filename"]
        try:
            return Path(self.ebook_parser.resolve_book_path(filename))
        except Exception:
            pass

        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.epub_cache_dir / filename
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path

        content = client.download_book(candidate["book_id"])
        if not content:
            logger.warning(
                "Grimmory annotation sync could not download EPUB for book %s",
                candidate["book_id"],
            )
            return None
        try:
            cache_path.write_bytes(content)
        except Exception as e:
            logger.warning("Grimmory annotation sync could not cache EPUB %s: %s", filename, e)
            return None
        return cache_path if cache_path.exists() and cache_path.stat().st_size > 0 else None

    def _booklore_payload_from_change(self, resolver: GrimmoryCFIResolver, book_id: str, change: dict) -> dict:
        return {
            "bookId": int(book_id),
            "cfi": resolver.xpointer_range_to_cfi(change.get("pos0"), change.get("pos1")),
            "chapterTitle": change.get("chapter"),
            "text": change.get("text") or "",
            "color": self._booklore_color_from_koreader(change.get("color")),
            "style": self._booklore_style_from_koreader(change.get("drawer")),
            "note": change.get("note"),
        }

    def _entry_from_booklore_note(self, resolver: GrimmoryCFIResolver, note: dict) -> dict | None:
        """Hub entry for a Grimmory web-reader note (book_notes_v2 row)."""
        try:
            pos0, pos1 = resolver.cfi_range_to_xpointers(note.get("cfi"))
        except Exception as e:
            logger.warning("Grimmory note CFI conversion failed for id %s: %s", note.get("id"), e)
            return None
        created = self._ko_datetime_from_iso(note.get("createdAt"))
        updated = self._ko_datetime_from_iso(note.get("updatedAt")) if note.get("updatedAt") else None
        return {
            "serverId": int(note["id"]),
            "version": 1,
            "datetime": created,
            "datetimeUpdated": updated if updated != created else None,
            "posFormat": "xpointer",
            "pos0": pos0,
            "pos1": pos1,
            "drawer": DEFAULT_KOREADER_STYLE,
            "color": self._koreader_color_from_booklore(note.get("color")),
            "text": note.get("selectedText"),
            "note": note.get("noteContent"),
            "chapter": note.get("chapterTitle"),
            "pageno": None,
        }

    def _entry_from_booklore_annotation(self, resolver: GrimmoryCFIResolver, annotation: dict) -> dict | None:
        try:
            pos0, pos1 = resolver.cfi_range_to_xpointers(annotation.get("cfi"))
        except Exception as e:
            logger.warning("Grimmory annotation CFI conversion failed for id %s: %s", annotation.get("id"), e)
            return None
        created = self._ko_datetime_from_iso(annotation.get("createdAt"))
        updated = self._ko_datetime_from_iso(annotation.get("updatedAt")) if annotation.get("updatedAt") else None
        return {
            "serverId": int(annotation["id"]),
            "version": 1,
            "datetime": created,
            "datetimeUpdated": updated,
            "posFormat": "xpointer",
            "pos0": pos0,
            "pos1": pos1,
            "drawer": self._koreader_style_from_booklore(annotation.get("style")),
            "color": self._koreader_color_from_booklore(annotation.get("color")),
            "text": annotation.get("text"),
            "note": annotation.get("note"),
            "chapter": annotation.get("chapterTitle"),
            "pageno": None,
        }

    def sync_booklore_user(self, user_id, client: BookloreClient) -> bool:
        did_work = False
        for candidate in self._candidate_booklore_books(user_id):
            if self.sync_booklore_book(user_id, client, candidate):
                did_work = True
        return did_work

    def sync_booklore_book(self, user_id, client: BookloreClient, candidate: dict) -> bool:
        book_path = self._resolve_booklore_epub_path(client, candidate)
        if not book_path:
            return False

        try:
            resolver = GrimmoryCFIResolver(self.ebook_parser, book_path)
        except Exception as e:
            logger.warning("Grimmory annotation resolver failed for %s: %s", candidate["filename"], e)
            return False

        doc_md5 = candidate["doc_md5"]
        book_id = candidate["book_id"]
        remote_annotations = client.get_annotations(book_id)
        if remote_annotations is None:
            return False
        remote_by_id = {
            int(item["id"]): item
            for item in remote_annotations
            if item.get("id") is not None
        }

        state = self.database_service.get_annotation_spoke_state(
            user_id,
            doc_md5,
            BOOKLORE_SPOKE_KEY,
            server_id_field="booklore_server_id",
            version_field="booklore_version",
            # Rows owned by a Grimmory web-reader note (book_notes_v2) are
            # synced by the notes sub-spoke; exporting them here would
            # duplicate every web note into the annotations store.
            exclude_if_set="booklore_note_id",
        )

        tombstone_acks = []
        for pending in state.get("pending_deletes") or []:
            remote_id = pending.get("serverId")
            if remote_id is not None and client.delete_annotation(remote_id):
                tombstone_acks.append(pending["_id"])

        uploaded_ids = []
        server_ids_by_annotation_id = {}
        for raw_change in (state.get("changes") or [])[:_MAX_CHANGES_PER_BOOK]:
            change = dict(raw_change)
            annotation_id = change.pop("_id", None)
            remote_id = change.pop("_spoke_server_id", None)
            change.pop("_spoke_version", None)
            if not annotation_id:
                continue

            try:
                payload = self._booklore_payload_from_change(resolver, book_id, change)
            except Exception as e:
                logger.warning("Grimmory annotation xpointer conversion failed for local id %s: %s", annotation_id, e)
                continue

            remote_id = int(remote_id) if remote_id is not None else None
            if remote_id is None:
                created = client.create_annotation(
                    book_id,
                    payload["cfi"],
                    payload["chapterTitle"],
                    payload["text"],
                    payload["color"],
                    payload["style"],
                    payload["note"],
                )
                if created and created.get("id") is not None:
                    uploaded_ids.append(annotation_id)
                    server_ids_by_annotation_id[str(annotation_id)] = int(created["id"])
                continue

            if self._same_remote_anchor(payload, remote_by_id.get(remote_id)):
                if client.update_annotation(remote_id, payload["color"], payload["style"], payload["note"]):
                    uploaded_ids.append(annotation_id)
                    server_ids_by_annotation_id[str(annotation_id)] = remote_id
                continue

            if client.delete_annotation(remote_id):
                created = client.create_annotation(
                    book_id,
                    payload["cfi"],
                    payload["chapterTitle"],
                    payload["text"],
                    payload["color"],
                    payload["style"],
                    payload["note"],
                )
                if created and created.get("id") is not None:
                    uploaded_ids.append(annotation_id)
                    server_ids_by_annotation_id[str(annotation_id)] = int(created["id"])

        if uploaded_ids or tombstone_acks:
            self.database_service.mark_spoke_annotations_uploaded(
                user_id,
                BOOKLORE_SPOKE_KEY,
                annotation_ids=uploaded_ids,
                tombstone_ids=tombstone_acks,
                server_id_field="booklore_server_id",
                version_field="booklore_version",
                synced_at_field="booklore_synced_at",
                server_ids_by_annotation_id=server_ids_by_annotation_id,
            )

        # Pull after push so Grimmory reflects local changes before merging.
        remote_annotations = client.get_annotations(book_id)
        if remote_annotations is None:
            return bool(uploaded_ids or tombstone_acks)

        adds = []
        remote_ids = set()
        for annotation in remote_annotations:
            if annotation.get("id") is None:
                continue
            remote_ids.add(int(annotation["id"]))
            entry = self._entry_from_booklore_annotation(resolver, annotation)
            if entry:
                adds.append(entry)

        known_ids = set(self.database_service.get_spoke_server_ids_for_book(
            user_id,
            doc_md5,
            server_id_field="booklore_server_id",
        ))
        deletes = [{"serverId": remote_id} for remote_id in sorted(known_ids - remote_ids)]

        if adds or deletes:
            self.database_service.apply_spoke_annotations(
                user_id,
                doc_md5,
                BOOKLORE_SPOKE_KEY,
                adds=adds,
                edits=[],
                deletes=deletes,
                server_id_field="booklore_server_id",
                version_field="booklore_version",
                synced_at_field="booklore_synced_at",
                # Grimmory positions are CFI round-trips — never let a pull
                # rewrite canonical identity (the ann_key cascade bug).
                trust_positions=False,
            )

        notes_did_work = self.sync_booklore_notes(user_id, client, doc_md5, book_id, resolver)
        return bool(uploaded_ids or tombstone_acks or adds or deletes or notes_did_work)

    def sync_booklore_notes(self, user_id, client: BookloreClient, doc_md5: str,
                            book_id: str, resolver: GrimmoryCFIResolver) -> bool:
        """Sync Grimmory's web-reader notes (book_notes_v2) for one book.

        This is the store Grimmory's own reader writes; it has its own id
        space, tracked in ``booklore_note_id``. The bridge never CREATES
        remote notes (device highlights go to the annotations store) — it
        pulls web-authored notes into the hub, writes device edits of those
        notes back, and propagates deletions both ways.
        """
        remote_notes = client.get_book_notes(book_id)
        if remote_notes is None:
            return False
        remote_by_id = {
            int(note["id"]): note
            for note in remote_notes
            if note.get("id") is not None
        }

        state = self.database_service.get_annotation_spoke_state(
            user_id,
            doc_md5,
            BOOKLORE_NOTES_SPOKE_KEY,
            server_id_field="booklore_note_id",
            version_field="booklore_version",
        )

        # Device deletions of web notes -> delete remotely.
        tombstone_acks = []
        for pending in state.get("pending_deletes") or []:
            remote_id = pending.get("serverId")
            if remote_id is not None and client.delete_book_note(remote_id):
                tombstone_acks.append(pending["_id"])
                remote_by_id.pop(int(remote_id), None)

        # Device edits of web-authored notes -> write back. Rows without a
        # note id belong to devices/the annotations store — never pushed here.
        uploaded_ids = []
        for raw_change in (state.get("changes") or [])[:_MAX_CHANGES_PER_BOOK]:
            change = dict(raw_change)
            annotation_id = change.pop("_id", None)
            remote_id = change.pop("_spoke_server_id", None)
            change.pop("_spoke_version", None)
            if not annotation_id or remote_id is None:
                continue
            if int(remote_id) not in remote_by_id:
                continue  # deleted remotely; the pull below tombstones it
            if client.update_book_note(
                int(remote_id),
                note_content=change.get("note") or "",
                color=self._booklore_color_from_koreader(change.get("color")),
            ):
                uploaded_ids.append(annotation_id)

        if uploaded_ids or tombstone_acks:
            self.database_service.mark_spoke_annotations_uploaded(
                user_id,
                BOOKLORE_NOTES_SPOKE_KEY,
                annotation_ids=uploaded_ids,
                tombstone_ids=tombstone_acks,
                server_id_field="booklore_note_id",
                version_field="booklore_version",
                synced_at_field="booklore_synced_at",
            )
            # Re-fetch so the merge below sees our just-written content.
            remote_notes = client.get_book_notes(book_id)
            if remote_notes is None:
                return True

        adds = []
        remote_ids = set()
        for note in remote_notes:
            if note.get("id") is None:
                continue
            remote_ids.add(int(note["id"]))
            entry = self._entry_from_booklore_note(resolver, note)
            if entry:
                adds.append(entry)

        known_ids = set(self.database_service.get_spoke_server_ids_for_book(
            user_id,
            doc_md5,
            server_id_field="booklore_note_id",
        ))
        deletes = [{"serverId": remote_id} for remote_id in sorted(known_ids - remote_ids)]

        if adds or deletes:
            self.database_service.apply_spoke_annotations(
                user_id,
                doc_md5,
                BOOKLORE_NOTES_SPOKE_KEY,
                adds=adds,
                edits=[],
                deletes=deletes,
                server_id_field="booklore_note_id",
                version_field="booklore_version",
                synced_at_field="booklore_synced_at",
                trust_positions=False,
            )
        return bool(uploaded_ids or tombstone_acks or adds or deletes)


def run_annotation_sync_daemon(service: AnnotationSyncService, interval_getter) -> None:
    """Daemon loop: run a sync cycle every N minutes (0 disables)."""
    while True:
        try:
            interval_mins = interval_getter()
        except Exception:
            interval_mins = 0
        if interval_mins and interval_mins > 0:
            try:
                service.run_cycle()
            except Exception as e:
                logger.error("Annotation sync cycle crashed: %s", e, exc_info=True)
            time.sleep(max(60, int(interval_mins * 60)))
        else:
            time.sleep(300)
