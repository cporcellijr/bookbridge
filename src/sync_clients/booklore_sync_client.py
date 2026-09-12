import os
from typing import Optional
import logging

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.utils.progress_metadata import parse_service_timestamp
from src.utils.ebook_sources import is_grimmory_source, normalize_ebook_source
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)

class BookloreSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_supported_sync_types(self) -> set:
        """Grimmory participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    @staticmethod
    def _mapped_book_id(book: Book) -> Optional[str]:
        source = getattr(book, "ebook_source", None)
        source_id = getattr(book, "ebook_source_id", None)
        if not isinstance(source, str) or not is_grimmory_source(source):
            return None
        if not isinstance(source_id, (str, int)) or not str(source_id).strip():
            return None
        return str(source_id).strip()

    def _resolve_legacy_book_id(self, book: Book, epub: Optional[str]) -> Optional[str]:
        """Resolve and safely backfill an exact legacy filename match."""
        if not epub:
            return None
        source = getattr(book, "ebook_source", None)
        if source is not None and (
            not isinstance(source, str)
            or (source.strip() and not is_grimmory_source(source))
        ):
            return None
        exact_resolver = getattr(self.booklore_client, "find_book_by_filename_exact", None)
        target = exact_resolver(epub) if callable(exact_resolver) else None
        if not isinstance(target, dict) or target.get("id") in (None, ""):
            return None

        book_id = str(target["id"])
        original_source = source
        book.ebook_source = normalize_ebook_source(source or "Booklore")
        book.ebook_source_id = book_id
        db = getattr(self.booklore_client, "db", None)
        claim = getattr(db, "backfill_ebook_source_id_if_unclaimed", None)
        if callable(claim):
            try:
                if not claim(book.abs_id, book_id, book.ebook_source):
                    logger.warning(
                        "Grimmory mapping source-id backfill refused for %s: id %s is already claimed",
                        getattr(book, "abs_id", "?"), book_id,
                    )
                    book.ebook_source = original_source
                    book.ebook_source_id = None
                    return None
                logger.info(
                    "Grimmory mapping source id backfilled for %s: %s (exact filename: %s)",
                    getattr(book, "abs_id", "?"), book_id, epub,
                )
            except Exception:
                logger.warning(
                    "Grimmory mapping source-id backfill failed for %s",
                    getattr(book, "abs_id", "?"), exc_info=True,
                )
                book.ebook_source = original_source
                book.ebook_source_id = None
                return None
        return book_id

    def supports_book(self, book: Book) -> bool:
        epub = self._resolve_epub_filename(book)

        # An explicit ebook source is authoritative. Match Grimmory regardless of
        # the tag variant it was saved under ('BookLore'/'Booklore'/'Grimmory');
        # never hijack a book explicitly owned by another ebook source (e.g.
        # BookOrbit), even if Grimmory hosts the same file.
        src = str(getattr(book, "ebook_source", None) or "").strip()
        if src:
            return is_grimmory_source(src) and bool(self._mapped_book_id(book) or epub)

        if not epub:
            return False

        # Otherwise (legacy/unsourced) only participate when the ebook can actually
        # be resolved against the Grimmory library cache.
        target = self.booklore_client.find_book_by_filename(epub, allow_refresh=False)
        return bool(target)

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # Prefer the original filename for tri-links, then fall back to the standard filename.
        epub = self._resolve_epub_filename(book)

        # Rich read when available (adds href + Grimmory's own lastReadTime and
        # readStatus); non-dict results (older/mocked clients) fall back to the
        # classic (pct, cfi) tuple.
        rich = None
        book_id = self._mapped_book_id(book) or self._resolve_legacy_book_id(book, epub)
        direct_rich_attempted = False
        if book_id and hasattr(self.booklore_client, "get_progress_rich_by_book_id"):
            direct_rich_attempted = True
            candidate = self.booklore_client.get_progress_rich_by_book_id(book_id)
            if isinstance(candidate, dict):
                rich = candidate
        elif hasattr(self.booklore_client, "get_progress_rich"):
            candidate = self.booklore_client.get_progress_rich(epub)
            if isinstance(candidate, dict):
                rich = candidate
        if rich is not None:
            bl_pct, bl_cfi = rich.get("pct"), rich.get("cfi")
        elif direct_rich_attempted:
            # The rich endpoint is the same GET /books/{id} used by the classic
            # read. A second call cannot improve identity resolution and turns a
            # 404/transient failure into needless duplicate traffic.
            bl_pct, bl_cfi = None, None
        else:
            if book_id and hasattr(self.booklore_client, "get_progress_by_book_id"):
                bl_pct, bl_cfi = self.booklore_client.get_progress_by_book_id(book_id)
            else:
                bl_pct, bl_cfi = self.booklore_client.get_progress(epub)

        if bl_pct is None:
            logger.debug("Grimmory percentage is None - returning no service state")
            return None

        # Get previous BookLore state
        prev_booklore_pct = prev_state.percentage if prev_state else 0

        delta = abs(bl_pct - prev_booklore_pct)

        current = {"pct": bl_pct, "cfi": bl_cfi}
        if rich is not None:
            if rich.get("href"):
                current["href"] = rich["href"]
            service_updated_at = parse_service_timestamp(rich.get("last_read_time"))
            if service_updated_at is not None:
                current["service_updated_at"] = service_updated_at
            if rich.get("status"):
                current["status"] = rich["status"]

        return ServiceState(
            current=current,
            previous_pct=prev_booklore_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        bl_pct = state.current.get('pct')
        bl_cfi = state.current.get('cfi')
        epub = self._resolve_epub_filename(book)
        if bl_cfi and epub and self.ebook_parser:
            txt = self.ebook_parser.get_text_around_cfi(epub, bl_cfi)
            if txt:
                return txt
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        # Prefer the original filename for updates too.
        epub = self._resolve_epub_filename(book)
        pct = request.locator_result.percentage
        book_id = self._mapped_book_id(book) or self._resolve_legacy_book_id(book, epub)
        if book_id and hasattr(self.booklore_client, "update_progress_by_book_id"):
            success = self.booklore_client.update_progress_by_book_id(book_id, pct, request.locator_result)
        else:
            success = self.booklore_client.update_progress(epub, pct, request.locator_result)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('BookLore', book.abs_id, pct)
            except ImportError:
                pass
        updated_state = {
            'pct': pct
        }
        if request.locator_result and request.locator_result.cfi:
            updated_state['cfi'] = request.locator_result.cfi
        return SyncResult(pct, success, updated_state)
