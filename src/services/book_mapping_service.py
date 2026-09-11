"""Book mapping service used by the shelf-watch flow.

Encapsulates the subset of `web_server.process_queue` logic needed to create a
`Book` record from an automatic match, without the Storyteller artifact / device
hash branches that only apply to user-driven approvals. The shelf-watch path
never has a Storyteller UUID, so omitting that branch keeps this code lean and
side-effect-free.
"""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from src.db.models import Book
from src.utils.series_metadata import resolve_series_for_book

logger = logging.getLogger(__name__)


class BookMappingService:
    """Creates `Book` records for shelf-watch auto-matches and ebook-only fallbacks."""

    def __init__(self, database_service, booklore_client, ebook_parser,
                 abs_client, sync_clients, bookorbit_client=None,
                 kavita_client=None, user_client_registry=None):
        self.database_service = database_service
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.kavita_client = kavita_client
        self.ebook_parser = ebook_parser
        self.abs_client = abs_client
        # `sync_clients` is a dict-like provider (DI Dict provider yields a dict)
        self.sync_clients = sync_clients
        self._user_client_registry = user_client_registry

    def _library_client_for_source(self, source_name: Optional[str]):
        """Pick the library client that hosts the ebook, by source label."""
        if source_name == 'BookOrbit':
            return self.bookorbit_client
        if source_name == 'Kavita':
            return self.kavita_client
        return self.booklore_client

    def _resolve_library_client_for_user(self, source_name: Optional[str],
                                          user_id: Optional[int] = None):
        """Pick the library client for *source_name*, scoped to *user_id* when
        a registry is available. Legacy callers without a user scope use the
        shared singleton; an explicit user's unavailable bundle never falls
        through to another user's client."""
        if user_id is None or self._user_client_registry is None:
            return self._library_client_for_source(source_name)
        try:
            bundle = self._user_client_registry.get_clients(user_id)
            if source_name == 'BookOrbit':
                return getattr(bundle, 'bookorbit_client', None)
            if source_name == 'Kavita':
                return getattr(bundle, 'kavita_client', None)
            return getattr(bundle, 'booklore_client', None)
        except Exception as exc:
            logger.warning("Shelf-watch: could not resolve %s client for user %s: %s", source_name, user_id, exc, exc_info=True)
            return None

    def _compute_kosync_id(self, ebook_filename: str, source_ebook_id: Optional[str],
                           source_name: Optional[str] = 'BookLore',
                           user_id: Optional[int] = None) -> Optional[str]:
        """Compute the KOSync document hash for a library-hosted ebook.

        Mirrors the library-API branch of `get_kosync_id_for_ebook` in
        `web_server.py`. Filesystem fallbacks are intentionally omitted: the
        shelf-watch flow is always anchored on a library book so we can rely on
        the source's download path (Grimmory or BookOrbit).

        When *user_id* is provided and a registry is available, downloads via
        the user's own library client instead of the global singleton.
        """
        if not source_ebook_id:
            return None
        client = self._resolve_library_client_for_user(source_name, user_id)
        if not client or not client.is_configured():
            return None
        try:
            content = client.download_book(source_ebook_id)
        except Exception as e:
            logger.warning(f"Shelf-watch: {source_name} download failed for kosync hash: {e}", exc_info=True)
            return None
        if not content:
            return None
        try:
            return self.ebook_parser.get_kosync_id_from_bytes(ebook_filename, content)
        except Exception as e:
            logger.warning(f"Shelf-watch: ebook parser failed to compute kosync hash: {e}", exc_info=True)
            return None

    def _resolve_series_metadata(self, book_like, user_id: Optional[int] = None) -> tuple:
        """Resolve (series_name, series_sequence) for a new mapping from its source library.

        The dashboard groups books into series cards purely on the persisted
        columns, so a mapping created without them never collapses into its
        series until an admin runs the backfill.
        """
        try:
            abs_client = self.abs_client
            bookorbit = self.bookorbit_client
            booklore = self.booklore_client
            kavita = self.kavita_client
            if user_id is not None and self._user_client_registry is not None:
                bundle = self._user_client_registry.get_clients(user_id)
                abs_client = getattr(bundle, 'abs_client', None) or abs_client
                bookorbit = getattr(bundle, 'bookorbit_client', None) or bookorbit
                booklore = getattr(bundle, 'booklore_client', None) or booklore
                kavita = getattr(bundle, 'kavita_client', None) or kavita
            return resolve_series_for_book(
                book_like,
                abs_client=abs_client,
                bookorbit_client=bookorbit,
                booklore_client=booklore,
                kavita_client=kavita,
                ebook_parser=self.ebook_parser,
            )
        except Exception as e:
            logger.warning(
                f"Shelf-watch: series lookup failed for '{getattr(book_like, 'abs_title', '')}': {e}",
                exc_info=True,
            )
            return None, None

    def _automatch_progress_trackers(self, book: Book, user_id: Optional[int] = None) -> None:
        """Run Hardcover/StoryGraph auto-match if configured. Mirrors process_queue lines 4142-4148."""
        try:
            if user_id is not None and self._user_client_registry is not None:
                bundle = self._user_client_registry.get_clients(user_id)
                sync_clients = dict(getattr(bundle, 'sync_clients', None) or {})
            else:
                sync_clients = dict(self.sync_clients) if self.sync_clients else {}
        except Exception:
            return
        hardcover = sync_clients.get('Hardcover')
        if hardcover and hardcover.is_configured():
            try:
                hardcover._automatch_hardcover(book)
            except Exception as e:
                logger.warning(f"Shelf-watch: Hardcover automatch failed for '{book.abs_id}': {e}", exc_info=True)
        storygraph = sync_clients.get('StoryGraph')
        if storygraph and storygraph.is_configured():
            try:
                storygraph._automatch_storygraph(book)
            except Exception as e:
                logger.warning(f"Shelf-watch: StoryGraph automatch failed for '{book.abs_id}': {e}", exc_info=True)

    def create_audio_mapping_from_match(
        self,
        *,
        audio_source: str,
        audio_source_id: str,
        audio_title: str,
        ebook_filename: str,
        audio_cover_url: Optional[str] = None,
        audio_duration: Optional[float] = None,
        audio_provider_book_id: Optional[str] = None,
        audio_provider_file_id: Optional[str] = None,
        ebook_source: Optional[str] = None,
        ebook_source_id: Optional[str] = None,
        booklore_ebook_id: Optional[str] = None,
        kosync_doc_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Optional[Book]:
        """Create or update a full sync mapping for a shelf-watch auto-match.

        Returns the saved `Book` or None on failure. ABS audio sources are added
        to the configured ABS collection; the Booklore shelf move is left to the
        orchestrator (it knows the source/destination shelf pair).
        """
        if not audio_source or not audio_source_id or not ebook_filename:
            logger.warning("Shelf-watch: create_audio_mapping_from_match missing required args")
            return None

        audio_source = str(audio_source).strip()
        audio_source_id = str(audio_source_id).strip()
        ebook_filename = str(ebook_filename).strip()

        # Callers that already know the KOSync hash (e.g. the filesystem read path) pass it
        # directly; library-anchored callers compute it from the source download.
        source_ebook_id = booklore_ebook_id or ebook_source_id
        kosync_doc_id = kosync_doc_id or self._compute_kosync_id(
            ebook_filename, source_ebook_id, ebook_source, user_id=user_id,
        )
        if not kosync_doc_id:
            logger.warning(
                f"Shelf-watch: could not compute kosync id for '{ebook_filename}' "
                f"(source_id={source_ebook_id}); skipping mapping"
            )
            return None

        if audio_source.lower() == 'booklore':
            bridge_key = f"booklore:{audio_source_id}"
            existing_book = (
                self.database_service.get_book(bridge_key)
                or self.database_service.get_book_by_audio_source("BookLore", audio_source_id)
            )
            target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
            target_book.abs_id = bridge_key
            target_book.audio_source = "BookLore"
            target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
            target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or f"/api/booklore/audiobook-cover/{audio_source_id}"
        elif audio_source.lower() == 'bookorbit':
            bridge_key = f"bookorbit:{audio_source_id}"
            existing_book = (
                self.database_service.get_book(bridge_key)
                or self.database_service.get_book_by_audio_source("BookOrbit", audio_source_id)
            )
            target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
            target_book.abs_id = bridge_key
            target_book.audio_source = "BookOrbit"
            target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
            target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url
        else:
            bridge_key = audio_source_id
            existing_book = self.database_service.get_book(bridge_key)
            target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
            target_book.abs_id = bridge_key
            target_book.audio_source = "ABS"
            target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
            target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url

        if existing_book and existing_book.kosync_doc_id:
            kosync_doc_id = existing_book.kosync_doc_id

        target_book.abs_title = audio_title or target_book.abs_title or bridge_key
        target_book.audio_source_id = audio_source_id
        target_book.audio_title = audio_title or target_book.audio_title or target_book.abs_title
        target_book.audio_duration = audio_duration if audio_duration is not None else target_book.audio_duration
        if audio_provider_file_id:
            target_book.audio_provider_file_id = str(audio_provider_file_id)
        target_book.ebook_filename = ebook_filename
        target_book.original_ebook_filename = ebook_filename
        target_book.ebook_source = ebook_source or target_book.ebook_source
        preserve_shared_orbit_id = False
        if user_id is not None and str(ebook_source or '').lower() == 'bookorbit' and existing_book:
            try:
                preserve_shared_orbit_id = self.database_service.has_user_bookorbit_link(
                    target_book.abs_id
                )
            except Exception:
                preserve_shared_orbit_id = False
        if not preserve_shared_orbit_id:
            target_book.ebook_source_id = ebook_source_id or target_book.ebook_source_id
        target_book.kosync_doc_id = kosync_doc_id
        target_book.status = "pending"
        target_book.sync_mode = "audiobook"
        target_book.duration = audio_duration if audio_duration is not None else target_book.duration

        if not target_book.series_name:
            _sname, _sseq = self._resolve_series_metadata(target_book, user_id=user_id)
            if _sname:
                target_book.series_name = _sname
                target_book.series_sequence = _sseq

        # A mapping for this exact ebook may already exist -- typically an
        # ebook-only entry created before the audiobook turned up. Carry over what
        # only that row knows before saving, then fold it in afterwards. Skipping
        # this leaves two rows claiming one KOSync hash, and since
        # KosyncDocument.linked_abs_id holds a single book, the loser is served but
        # can never receive progress while the device downloads the bytes twice.
        stale = (
            self.database_service.get_book_by_kosync_id(kosync_doc_id)
            if kosync_doc_id else None
        )
        if stale is not None and getattr(stale, "abs_id", None) != target_book.abs_id:
            for field in ("storyteller_uuid", "transcript_source", "transcript_file",
                          "abs_ebook_item_id"):
                if not getattr(target_book, field, None):
                    setattr(target_book, field, getattr(stale, field, None))

        saved_book = self.database_service.save_book(target_book)
        self.database_service.absorb_duplicate_mapping(saved_book)
        self._automatch_progress_trackers(saved_book, user_id=user_id)

        # ABS collection only for actual ABS audio sources — not BookOrbit
        if audio_source.lower() not in ('booklore', 'bookorbit'):
            try:
                from src.utils.user_config import user_setting
                abs_collection = user_setting('ABS_COLLECTION_NAME', 'Synced with KOReader')
                abs_client = self.abs_client
                if user_id is not None and self._user_client_registry is not None:
                    bundle = self._user_client_registry.get_clients(user_id)
                    abs_client = getattr(bundle, 'abs_client', None) or abs_client
                if abs_client is not None:
                    abs_client.add_to_collection(saved_book.abs_id, abs_collection)
            except Exception as e:
                logger.warning(f"Shelf-watch: failed to add '{saved_book.abs_id}' to ABS collection: {e}", exc_info=True)

        # Persist per-user BookOrbit link when an explicit user context exists
        if user_id is not None and (
            audio_source.lower() == 'bookorbit'
            or str(ebook_source or '').lower() == 'bookorbit'
        ):
            try:
                self.database_service.set_user_bookorbit_link(
                    user_id=user_id,
                    abs_id=saved_book.abs_id,
                    audio_id=(
                        str(audio_provider_book_id or audio_source_id)
                        if audio_source.lower() == 'bookorbit' else None
                    ),
                    ebook_id=str(ebook_source_id) if ebook_source_id else None,
                    title=audio_title,
                )
            except Exception as e:
                logger.debug("Shelf-watch: failed to set BookOrbit link for user %s: %s", user_id, e)

        # Claim the book for the current user
        if user_id is not None:
            try:
                self.database_service.link_user_book(user_id, saved_book.abs_id)
            except Exception:
                pass

        try:
            self.database_service.dismiss_suggestion(saved_book.abs_id)
            if saved_book.kosync_doc_id:
                self.database_service.dismiss_suggestion(saved_book.kosync_doc_id)
        except Exception:
            pass

        return saved_book

    def create_ebook_only_mapping(
        self,
        *,
        ebook_filename: str,
        ebook_title: Optional[str] = None,
        ebook_source: str = "BookLore",
        ebook_source_id: Optional[str] = None,
        booklore_ebook_id: Optional[str] = None,
        kosync_doc_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Optional[Book]:
        """Create an ebook-only mapping when no audio candidate was found.

        Uses the same generated abs_id pattern as the manual ebook-only flow in
        `web_server.py` (lines 880-904): `ebook-{kosync_doc_id[:16]}`.

        When *user_id* is provided and *ebook_source* is ``BookOrbit``, a
        ``UserBookOrbitLink`` is persisted and a ``UserBook`` claim is created
        so the mapping is attributed to the user's library.
        """
        if not ebook_filename:
            logger.warning("Shelf-watch: create_ebook_only_mapping missing ebook_filename")
            return None

        kosync_doc_id = kosync_doc_id or self._compute_kosync_id(
            ebook_filename, booklore_ebook_id or ebook_source_id, ebook_source,
            user_id=user_id,
        )
        if not kosync_doc_id:
            logger.warning(
                f"Shelf-watch: could not compute kosync id for ebook-only mapping '{ebook_filename}'"
            )
            return None

        existing_by_hash = self.database_service.get_book_by_kosync_id(kosync_doc_id)
        if existing_by_hash:
            logger.info(
                f"Shelf-watch: ebook-only mapping already exists for '{ebook_filename}' "
                f"(abs_id={existing_by_hash.abs_id}); reusing"
            )
            if user_id is not None:
                self.database_service.link_user_book(user_id, existing_by_hash.abs_id)
                if str(ebook_source or '').lower() == 'bookorbit' and ebook_source_id:
                    self.database_service.set_user_bookorbit_link(
                        user_id=user_id,
                        abs_id=existing_by_hash.abs_id,
                        ebook_id=str(ebook_source_id),
                        title=ebook_title,
                    )
            return existing_by_hash

        target_abs_id = f"ebook-{kosync_doc_id[:16]}"
        existing_by_id = self.database_service.get_book(target_abs_id)
        if existing_by_id:
            if user_id is not None:
                self.database_service.link_user_book(user_id, existing_by_id.abs_id)
                if str(ebook_source or '').lower() == 'bookorbit' and ebook_source_id:
                    self.database_service.set_user_bookorbit_link(
                        user_id=user_id,
                        abs_id=existing_by_id.abs_id,
                        ebook_id=str(ebook_source_id),
                        title=ebook_title,
                    )
            return existing_by_id

        inferred_title = ebook_title or Path(ebook_filename).stem or target_abs_id
        series_name, series_sequence = self._resolve_series_metadata(
            SimpleNamespace(
                abs_id=target_abs_id,
                abs_title=inferred_title,
                audio_source=None,
                audio_source_id=None,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id or booklore_ebook_id,
                ebook_filename=ebook_filename,
            ),
            user_id=user_id,
        )
        target_book = Book(
            abs_id=target_abs_id,
            abs_title=inferred_title,
            sync_mode="ebook_only",
            ebook_filename=ebook_filename,
            original_ebook_filename=ebook_filename,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
            kosync_doc_id=kosync_doc_id,
            status="pending",
            series_name=series_name,
            series_sequence=series_sequence,
        )

        saved_book = self.database_service.save_book(target_book)
        self._automatch_progress_trackers(saved_book, user_id=user_id)

        # Persist per-user BookOrbit link when the ebook source is BookOrbit
        if user_id is not None and ebook_source and ebook_source.lower() == 'bookorbit':
            try:
                self.database_service.set_user_bookorbit_link(
                    user_id=user_id,
                    abs_id=saved_book.abs_id,
                    ebook_id=str(ebook_source_id) if ebook_source_id else None,
                    title=inferred_title,
                )
            except Exception as e:
                logger.debug("Shelf-watch: failed to set BookOrbit ebook link for user %s: %s", user_id, e)

        # Claim the book for the current user
        if user_id is not None:
            try:
                self.database_service.link_user_book(user_id, saved_book.abs_id)
            except Exception:
                pass

        try:
            self.database_service.dismiss_suggestion(saved_book.abs_id)
            if saved_book.kosync_doc_id:
                self.database_service.dismiss_suggestion(saved_book.kosync_doc_id)
        except Exception:
            pass

        return saved_book
