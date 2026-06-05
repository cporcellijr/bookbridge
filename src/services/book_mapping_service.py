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
from typing import Optional

from src.db.models import Book

logger = logging.getLogger(__name__)


class BookMappingService:
    """Creates `Book` records for shelf-watch auto-matches and ebook-only fallbacks."""

    def __init__(self, database_service, booklore_client, ebook_parser,
                 abs_client, sync_clients, bookorbit_client=None):
        self.database_service = database_service
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.ebook_parser = ebook_parser
        self.abs_client = abs_client
        # `sync_clients` is a dict-like provider (DI Dict provider yields a dict)
        self.sync_clients = sync_clients

    def _library_client_for_source(self, source_name: Optional[str]):
        """Pick the library client that hosts the ebook, by source label."""
        if source_name == 'BookOrbit':
            return self.bookorbit_client
        return self.booklore_client

    def _compute_kosync_id(self, ebook_filename: str, source_ebook_id: Optional[str],
                           source_name: Optional[str] = 'BookLore') -> Optional[str]:
        """Compute the KOSync document hash for a library-hosted ebook.

        Mirrors the library-API branch of `get_kosync_id_for_ebook` in
        `web_server.py`. Filesystem fallbacks are intentionally omitted: the
        shelf-watch flow is always anchored on a library book so we can rely on
        the source's download path (Grimmory or BookOrbit).
        """
        if not source_ebook_id:
            return None
        client = self._library_client_for_source(source_name)
        if not client or not client.is_configured():
            return None
        try:
            content = client.download_book(source_ebook_id)
        except Exception as e:
            logger.warning(f"Shelf-watch: {source_name} download failed for kosync hash: {e}")
            return None
        if not content:
            return None
        try:
            return self.ebook_parser.get_kosync_id_from_bytes(ebook_filename, content)
        except Exception as e:
            logger.warning(f"Shelf-watch: ebook parser failed to compute kosync hash: {e}")
            return None

    def _automatch_progress_trackers(self, book: Book) -> None:
        """Run Hardcover/StoryGraph auto-match if configured. Mirrors process_queue lines 4142-4148."""
        try:
            sync_clients = dict(self.sync_clients) if self.sync_clients else {}
        except Exception:
            return
        hardcover = sync_clients.get('Hardcover')
        if hardcover and hardcover.is_configured():
            try:
                hardcover._automatch_hardcover(book)
            except Exception as e:
                logger.warning(f"Shelf-watch: Hardcover automatch failed for '{book.abs_id}': {e}")
        storygraph = sync_clients.get('StoryGraph')
        if storygraph and storygraph.is_configured():
            try:
                storygraph._automatch_storygraph(book)
            except Exception as e:
                logger.warning(f"Shelf-watch: StoryGraph automatch failed for '{book.abs_id}': {e}")

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
        kosync_doc_id = kosync_doc_id or self._compute_kosync_id(ebook_filename, booklore_ebook_id, ebook_source)
        if not kosync_doc_id:
            logger.warning(
                f"Shelf-watch: could not compute kosync id for '{ebook_filename}' "
                f"(source_id={booklore_ebook_id}); skipping mapping"
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
        target_book.ebook_source_id = ebook_source_id or target_book.ebook_source_id
        target_book.kosync_doc_id = kosync_doc_id
        target_book.status = "pending"
        target_book.sync_mode = "audiobook"
        target_book.duration = audio_duration if audio_duration is not None else target_book.duration

        saved_book = self.database_service.save_book(target_book)
        self._automatch_progress_trackers(saved_book)

        if audio_source.lower() != 'booklore':
            try:
                abs_collection = os.environ.get('ABS_COLLECTION_NAME', 'Synced with KOReader')
                self.abs_client.add_to_collection(saved_book.abs_id, abs_collection)
            except Exception as e:
                logger.warning(f"Shelf-watch: failed to add '{saved_book.abs_id}' to ABS collection: {e}")

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
    ) -> Optional[Book]:
        """Create an ebook-only mapping when no audio candidate was found.

        Uses the same generated abs_id pattern as the manual ebook-only flow in
        `web_server.py` (lines 880-904): `ebook-{kosync_doc_id[:16]}`.
        """
        if not ebook_filename:
            logger.warning("Shelf-watch: create_ebook_only_mapping missing ebook_filename")
            return None

        kosync_doc_id = self._compute_kosync_id(
            ebook_filename, booklore_ebook_id or ebook_source_id, ebook_source
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
            return existing_by_hash

        target_abs_id = f"ebook-{kosync_doc_id[:16]}"
        existing_by_id = self.database_service.get_book(target_abs_id)
        if existing_by_id:
            return existing_by_id

        inferred_title = ebook_title or Path(ebook_filename).stem or target_abs_id
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
        )

        saved_book = self.database_service.save_book(target_book)
        self._automatch_progress_trackers(saved_book)

        try:
            self.database_service.dismiss_suggestion(saved_book.abs_id)
            if saved_book.kosync_doc_id:
                self.database_service.dismiss_suggestion(saved_book.kosync_doc_id)
        except Exception:
            pass

        return saved_book
