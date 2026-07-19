import logging
import os
from typing import Optional

from src.api.bookfusion_client import BookFusionBookNotFound, BookFusionClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import (
    ServiceState,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils.ebook_utils import EbookParser
from src.utils.progress_metadata import parse_service_timestamp

logger = logging.getLogger(__name__)


class BookFusionSyncClient(SyncClient):
    """Progress sync client for BookFusion-hosted ebooks."""

    def __init__(
        self,
        bookfusion_client: BookFusionClient,
        ebook_parser: EbookParser,
        database_service=None,
        user_id: int = None,
    ) -> None:
        super().__init__(ebook_parser)
        self.client = bookfusion_client
        self._db = database_service
        self._user_id = user_id
        self.delta_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def check_connection(self) -> bool:
        return self.client.check_connection()

    def get_supported_sync_types(self) -> set:
        """BookFusion participates in both audiobook and ebook sync modes.

        Combined audiobook+ebook entries sync in 'audiobook' mode; advertising
        only 'ebook' excluded this client from them, so BookFusion progress was
        never read or written for tri-linked books. Mirrors the other
        ebook-capable clients (KoSync, Storyteller, Grimmory, BookOrbit, CWA).
        """
        return {"audiobook", "ebook"}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def _bookfusion_id(self, book: Book) -> Optional[str]:
        if self._db is not None and hasattr(self._db, "resolve_bookfusion_id"):
            resolved = self._db.resolve_bookfusion_id(self._user_id, book)
            if resolved not in (None, ""):
                return str(resolved)
        return None

    def supports_book(self, book: Book) -> bool:
        return bool(self._bookfusion_id(book))

    def _remove_stale_link(self, book: Book, book_id: str) -> None:
        """Remove a per-user link after BookFusion confirms the book is gone."""
        if self._db is None or self._user_id is None:
            return
        try:
            removed = self._db.delete_user_bookfusion_link(self._user_id, book.abs_id)
        except Exception as exc:
            logger.warning("Could not remove stale BookFusion link for '%s': %s", book.abs_id, exc)
            return
        if removed:
            logger.info(
                "Removed stale BookFusion link for '%s' after book %s returned 404",
                book.abs_id,
                book_id,
            )

    def fetch_bulk_state(self) -> Optional[dict]:
        if not self.client.is_configured():
            return None
        results: dict[str, dict] = {}
        page = 1
        per_page = 100
        while page <= 10000:
            books = self.client.search_books(page=page, per_page=per_page)
            if books is None:
                return results or None
            for item in books:
                book_id = item.get("id")
                pos = item.get("reading_position")
                if book_id is not None and isinstance(pos, dict):
                    results[str(book_id)] = pos
            if len(books) < per_page:
                break
            page += 1
        return results

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        book_id = self._bookfusion_id(book)
        if not book_id:
            return None
        rich = None
        if bulk_context:
            rich = bulk_context.get(str(book_id))
        if not isinstance(rich, dict):
            rich = self.client.get_reading_position(book_id)
        if not isinstance(rich, dict):
            return None

        try:
            pct = float(rich.get("percentage")) / 100.0
        except (TypeError, ValueError):
            return None
        pct = max(0.0, min(1.0, pct))

        current = {"pct": pct}
        service_updated_at = parse_service_timestamp(rich.get("updated_at"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at
        if rich.get("page_position_in_book") is not None:
            current["page_position_in_book"] = rich.get("page_position_in_book")

        prev_pct = prev_state.percentage if prev_state else 0.0
        return ServiceState(
            current=current,
            previous_pct=prev_pct,
            delta=abs(pct - prev_pct),
            threshold=self.delta_thresh,
            is_configured=self.client.is_configured(),
            display=("BookFusion", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        pct = state.current.get("pct")
        epub = self._resolve_epub_filename(book)
        if pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def _ensure_bf_epub(self, book_id: str) -> Optional[str]:
        """Return the filename of BookFusion's own cached EPUB, downloading if needed.

        Reading-position anchors (``chapter_index``/``cfi``) must be computed
        against BookFusion's copy of the book — a progress-spoke link sits on top
        of a different source EPUB whose chapter structure need not match. Best
        effort: returns ``None`` if the file can't be obtained.
        """
        if self.ebook_parser is None:
            return None
        filename = f"bookfusion_{book_id}.epub"
        cache_dir = getattr(self.ebook_parser, "epub_cache_dir", None)
        try:
            if cache_dir is not None and (cache_dir / filename).exists() and (cache_dir / filename).stat().st_size > 0:
                return filename
        except OSError:
            pass
        try:
            content = self.client.download_book(book_id)
        except Exception as exc:
            logger.warning("⚠️ BookFusion EPUB download failed for '%s': %s", book_id, exc)
            return None
        if not content or cache_dir is None:
            return None
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / filename).write_bytes(content)
            self.ebook_parser.invalidate_path_cache(filename)
        except Exception as exc:
            logger.warning("⚠️ Could not cache BookFusion EPUB '%s': %s", filename, exc)
            return None
        return filename

    def _reading_anchor(self, book_id: str, pct: float) -> Optional[dict]:
        """Compute a BookFusion navigation anchor for ``pct`` against BF's EPUB."""
        filename = self._ensure_bf_epub(book_id)
        if not filename:
            return None
        try:
            return self.ebook_parser.bookfusion_reading_anchor(filename, pct)
        except Exception as exc:
            logger.debug("BookFusion anchor computation failed for '%s': %s", book_id, exc)
            return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_id = self._bookfusion_id(book)
        if not book_id:
            return SyncResult(None, False, {})
        pct = max(0.0, min(1.0, float(request.locator_result.percentage)))
        payload = {
            "percentage": round(pct * 100.0, 4),
            "page_position_in_book": pct,
        }
        # BookFusion navigates reflowable EPUBs by cfi/chapter_index, not percentage.
        # Without a real anchor the reader opens at the stale chapter (usually the
        # start) and writes that position back, undoing every push.
        anchor = self._reading_anchor(book_id, pct)
        if anchor:
            payload["chapter_index"] = anchor["chapter_index"]
            payload["page_position_in_book"] = anchor["page_position_in_book"]
            if anchor.get("cfi"):
                payload["cfi"] = anchor["cfi"]
        try:
            result = self.client.set_reading_position(book_id, payload)
        except BookFusionBookNotFound:
            self._remove_stale_link(book, book_id)
            return SyncResult(pct, False, {"pct": pct})
        success = result is not None
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write("BookFusion", book.abs_id, pct)
            except ImportError:
                pass
        return SyncResult(pct, success, {"pct": pct})
