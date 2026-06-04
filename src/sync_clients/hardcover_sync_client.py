import logging
import os
from typing import Optional

from src.api.hardcover_client import HardcoverClient, HardcoverRateLimitError
from src.db.models import Book, State, HardcoverDetails
from src.services.llm_matching import craft_search_terms, judge_best_candidate, tracker_match_enabled
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class HardcoverSyncClient(SyncClient):
    """
    Hardcover sync client that handles both automating matching and progress sync.
    This integrates Hardcover as a proper sync client in the sync cycle.
    """

    def __init__(self, hardcover_client: HardcoverClient, ebook_parser: EbookParser, abs_client=None, database_service=None, ollama_client=None):
        super().__init__(ebook_parser)
        self.hardcover_client = hardcover_client
        self.abs_client = abs_client  # For fetching book metadata
        self.database_service = database_service
        self.ollama_client = ollama_client

    def is_configured(self) -> bool:
        """Check if Hardcover is configured."""
        return self.hardcover_client.is_configured()

    def check_connection(self):
        """Check connection to Hardcover API."""
        return self.hardcover_client.check_connection()

    def can_be_leader(self) -> bool:
        """
        Hardcover cannot be a leader because it doesn't provide text content
        for synchronization. It only receives updates from other clients.
        """
        return False

    def get_supported_sync_types(self) -> set:
        """Hardcover supports both audiobook and ebook syncing (as a follower)."""
        return {'audiobook', 'ebook'}

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        """
        Since Hardcover can never be the leader, its service state is not used for
        leader selection or text extraction. Return None to indicate no state needed.
        Auto-matching and progress sync happen in update_progress when actually needed.
        """
        return None

    def _try_match_with_strategy(self, search_func, strategy_name, book_title):
        """Try a single search strategy and validate it has pages or audio_seconds."""
        match = search_func()
        if not match:
            return None, None

        pages = match.get('pages')
        if not pages or pages <= 0:
            logger.info(f"🔍 '{book_title}' could not find valid page count using '{strategy_name}' match")
            return None, match  # Return None for valid match, but keep rejected match

        return match, None  # Return valid match, no rejected match

    def _automatch_hardcover(self, book):
        """
        Match a book with Hardcover using various search strategies.
        Tries page-based editions first, falls back to audiobook editions.
        """
        if not self.hardcover_client.is_configured():
            return

        # Check if we already have hardcover details for this book
        existing_details = self.database_service.get_hardcover_details(book.abs_id)
        if existing_details:
            return  # Already matched

        item = self.abs_client.get_item_details(book.abs_id) if self.abs_client else None
        if item:
            meta = item.get('media', {}).get('metadata', {})
            isbn = meta.get('isbn')
            asin = meta.get('asin')
            title = meta.get('title')
            author = meta.get('authorName')
        else:
            # Ebook-only book (no ABS item): source identifiers from the EPUB itself.
            ebook_meta = self.ebook_parser.get_book_metadata(book.ebook_filename) if self.ebook_parser else {}
            title = ebook_meta.get('title') or book.abs_title
            author = ebook_meta.get('author')
            isbn = ebook_meta.get('isbn')
            asin = ebook_meta.get('asin')
            meta = {'title': title}  # keep log statements below working without an ABS item

        if not title:
            return

        # Try different search strategies in order of preference
        match = None
        matched_by = None
        first_rejected = None
        first_rejected_by = None

        # LLM tracker matching is a last-resort rescue only: the normal ISBN/ASIN and
        # title searches below always run first, so the LLM can add matches it would
        # otherwise miss but can never suppress a match the plain search already found.
        use_llm = (
            tracker_match_enabled()
            and self.ollama_client is not None
            and self.ollama_client.is_configured()
            and bool(title)
        )

        search_strategies = [
            (lambda: self.hardcover_client.search_by_isbn(isbn) if isbn else None, 'isbn', isbn),
            (lambda: self.hardcover_client.search_by_isbn(asin) if asin else None, 'asin', asin),
            (lambda: self.hardcover_client.search_by_title_author(title, author) if (title and author) else None, 'title_author', title and author),
        ]
        # Blind title-only fuzzy matching grabs the wrong book when many share a title;
        # with the LLM on we route title-only candidates through the judge below instead.
        if not use_llm:
            search_strategies.append(
                (lambda: self.hardcover_client.search_by_title_author(title, "") if title else None, 'title', title)
            )

        for search_func, strategy_name, condition in search_strategies:
            if not match and condition:
                try:
                    valid_match, rejected_match = self._try_match_with_strategy(search_func, strategy_name, book.abs_title)
                except HardcoverRateLimitError:
                    logger.warning(
                        "⚠️ Hardcover: Rate limited while matching '%s'; skipping automatch for now",
                        sanitize_log_data(meta.get('title')),
                    )
                    return
                if valid_match:
                    match = valid_match
                    matched_by = strategy_name
                    break
                elif rejected_match and not first_rejected:
                    first_rejected = rejected_match
                    first_rejected_by = strategy_name

        # If no page-based match found, check if first rejected match has an audiobook edition
        audio_seconds = None
        if not match and first_rejected:
            book_id = first_rejected.get('book_id')
            if book_id:
                try:
                    edition = self.hardcover_client.get_default_edition(book_id)
                except HardcoverRateLimitError:
                    logger.warning(
                        "⚠️ Hardcover: Rate limited while resolving editions for '%s'; skipping automatch for now",
                        sanitize_log_data(meta.get('title')),
                    )
                    return
                if edition and edition.get('audio_seconds') and edition['audio_seconds'] > 0:
                    match = first_rejected
                    matched_by = first_rejected_by
                    audio_seconds = edition['audio_seconds']
                    match['edition_id'] = edition['id']
                    match['pages'] = -1  # Sentinel: audiobook, no pages
                    logger.info(f"📚 Hardcover: '{sanitize_log_data(meta.get('title'))}' matched as audiobook ({audio_seconds}s)")

        # LLM rescue: clean the title, then list candidates title-only for maximum recall
        # (the judge uses the author for precision, so we deliberately don't constrain the
        # query by author here), and let the judge pick the one true book or write nothing.
        if not match and use_llm:
            try:
                craft_title, craft_author = craft_search_terms(self.ollama_client, title, author)
                candidates = self.hardcover_client.list_candidates_by_title_author(craft_title, "")
            except HardcoverRateLimitError:
                logger.warning(
                    "⚠️ Hardcover: Rate limited during LLM match for '%s'; skipping for now",
                    sanitize_log_data(meta.get('title')),
                )
                return
            conf_min = float(os.environ.get('OLLAMA_JUDGE_CONFIDENCE_MIN', 85))
            idx = judge_best_candidate(self.ollama_client, craft_title, craft_author, candidates, conf_min)
            if idx is None:
                logger.info(
                    f"🧠 Hardcover: LLM found no confident match for '{sanitize_log_data(title)}'; leaving for manual"
                )
                return
            try:
                match = self.hardcover_client.resolve_match_for_book(candidates[idx])
            except HardcoverRateLimitError:
                logger.warning(
                    "⚠️ Hardcover: Rate limited resolving LLM match for '%s'; skipping for now",
                    sanitize_log_data(meta.get('title')),
                )
                return
            if match:
                matched_by = 'title_author_llm'
                if match.get('audio_seconds'):
                    audio_seconds = match['audio_seconds']
                logger.info(
                    f"🧠 Hardcover: LLM matched '{sanitize_log_data(title)}' -> '{sanitize_log_data(match.get('title'))}'"
                )

        if match:
            hardcover_details = HardcoverDetails(
                abs_id=book.abs_id,
                hardcover_book_id=match.get('book_id'),
                hardcover_slug=match.get('slug'),
                hardcover_edition_id=match.get('edition_id'),
                hardcover_pages=match.get('pages'),
                hardcover_audio_seconds=audio_seconds,
                isbn=isbn,
                asin=asin,
                matched_by=matched_by
            )

            self.database_service.save_hardcover_details(hardcover_details)
            self.hardcover_client.update_status(int(match.get('book_id')), 1, match.get('edition_id'))
            logger.info(f"📚 Hardcover: '{sanitize_log_data(meta.get('title'))}' matched and set to Want to Read (matched by {matched_by})")
        else:
            logger.warning(f"⚠️ Hardcover: No match found for '{sanitize_log_data(meta.get('title'))}'")

    def set_manual_match(self, book_abs_id: str, input_str: str) -> bool:
        """
        Manually match an ABS book to a Hardcover book via URL, ID, or Slug.
        """
        if not self.hardcover_client.is_configured():
            logger.error("❌ Hardcover client not configured")
            return False

        # Resolve the input string to a Hardcover book
        match = self.hardcover_client.resolve_book_from_input(input_str)
        if not match:
            logger.error(f"❌ Could not resolve Hardcover book from '{input_str}'")
            return False

        # Try to get existing metadata from ABS for completeness
        isbn = None
        asin = None

        if self.abs_client:
            try:
                item = self.abs_client.get_item_details(book_abs_id)
                if item:
                    meta = item.get('media', {}).get('metadata', {})
                    isbn = meta.get('isbn')
                    asin = meta.get('asin')
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch ABS details during manual match: {e}")

        # Create/Update HardcoverDetails
        details = HardcoverDetails(
            abs_id=book_abs_id,
            hardcover_book_id=match['book_id'],
            hardcover_slug=match.get('slug'),
            hardcover_edition_id=match.get('edition_id'),
            hardcover_pages=match.get('pages'),
            hardcover_audio_seconds=match.get('audio_seconds'),
            isbn=isbn,
            asin=asin,
            matched_by='manual'
        )

        self.database_service.save_hardcover_details(details)
        logger.info(f"✅ Manually matched ABS {book_abs_id} to Hardcover {match['book_id']} ({match.get('title')})")

        # Trigger an initial status update to ensure it's tracked
        self.hardcover_client.update_status(match['book_id'], 1, match.get('edition_id'))
        return True

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        """
        Hardcover doesn't provide text content, so return None.
        This client is primarily for progress synchronization.
        """
        return None

    def _handle_status_transition(self, book, hardcover_details, current_status, percentage, is_finished):
        """Handle status transitions based on progress percentage."""
        # If finished and not already marked as Read (3), promote to Read
        if is_finished and current_status != 3:
            self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                3,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Read")
            return 3

        # If progress > 2% and currently "Want to Read" (1), promote to "Currently Reading" (2)
        elif percentage > 0.02 and current_status == 1:
            self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                2,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Currently Reading")
            return 2

        return current_status

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        """
        Update progress in Hardcover based on the incoming locator result.
        Performs auto-matching if needed before syncing progress.
        """
        if not self.is_configured() or not self.database_service:
            return SyncResult(None, False)

        # Ensure we have hardcover details (auto-match if needed)
        self._automatch_hardcover(book)

        percentage = request.locator_result.percentage

        # Get hardcover details for this book
        hardcover_details = self.database_service.get_hardcover_details(book.abs_id)
        if not hardcover_details or not hardcover_details.hardcover_book_id:
            return SyncResult(None, False)

        # Get user book from Hardcover
        ub = self.hardcover_client.get_user_book(hardcover_details.hardcover_book_id)
        if not ub:
            return SyncResult(None, False)

        # Check if this is an audiobook edition
        audio_seconds = getattr(hardcover_details, 'hardcover_audio_seconds', None) or 0

        if audio_seconds > 0:
            return self._update_audiobook_progress(book, hardcover_details, ub, percentage, audio_seconds)

        # --- PAGE-BASED PATH ---
        total_pages = hardcover_details.hardcover_pages or 0

        # Attempt to refresh if pages are missing
        if total_pages <= 0:
            if total_pages == -1:
                return SyncResult(None, False)  # Already verified no valid edition exists

            logger.info(f"Hardcover: Pages are 0 for {sanitize_log_data(book.abs_title)}, attempting to refresh details...")
            refreshed_edition = self.hardcover_client.get_default_edition(hardcover_details.hardcover_book_id)

            if refreshed_edition and refreshed_edition.get('pages'):
                # Found page-based edition
                total_pages = refreshed_edition['pages']
                hardcover_details.hardcover_pages = total_pages
                hardcover_details.hardcover_edition_id = refreshed_edition['id']
                self.database_service.save_hardcover_details(hardcover_details)
                logger.info(f"Hardcover: Updated page count to {total_pages}")
            elif refreshed_edition and refreshed_edition.get('audio_seconds') and refreshed_edition['audio_seconds'] > 0:
                # Found audiobook edition instead
                audio_seconds = refreshed_edition['audio_seconds']
                hardcover_details.hardcover_audio_seconds = audio_seconds
                hardcover_details.hardcover_edition_id = refreshed_edition['id']
                hardcover_details.hardcover_pages = -1
                self.database_service.save_hardcover_details(hardcover_details)
                logger.info(f"Hardcover: Found audiobook edition ({audio_seconds}s) for {sanitize_log_data(book.abs_title)}")
                return self._update_audiobook_progress(book, hardcover_details, ub, percentage, audio_seconds)
            else:
                logger.warning(f"⚠️ Hardcover Sync Skipped: {sanitize_log_data(book.abs_title)} still has 0 pages after refresh")
                hardcover_details.hardcover_pages = -1
                self.database_service.save_hardcover_details(hardcover_details)
                return SyncResult(None, False)

        page_num = int(total_pages * percentage)
        is_finished = percentage > 0.99
        current_status = ub.get('status_id')

        # Handle status transitions
        current_status = self._handle_status_transition(book, hardcover_details, current_status, percentage, is_finished)

        # Update progress
        try:
            self.hardcover_client.update_progress(
                ub['id'],
                page_num,
                edition_id=hardcover_details.hardcover_edition_id,
                is_finished=is_finished,
                current_percentage=percentage
            )

            # Calculate actual percentage from page number for state tracking
            actual_pct = min(page_num / total_pages, 1.0) if total_pages > 0 else percentage

            updated_state = {
                'pct': actual_pct,
                'pages': page_num,
                'total_pages': total_pages,
                'status': current_status
            }

            return SyncResult(actual_pct, True, updated_state)

        except Exception as e:
            logger.error(f"❌ Failed to update Hardcover progress: {e}")
            return SyncResult(None, False)

    def _update_audiobook_progress(self, book, hardcover_details, ub, percentage, audio_seconds):
        """Update Hardcover progress using progress_seconds for audiobook editions."""
        is_finished = percentage > 0.99
        current_status = ub.get('status_id')

        # Handle status transitions
        current_status = self._handle_status_transition(book, hardcover_details, current_status, percentage, is_finished)

        try:
            progress_seconds = int(audio_seconds * percentage)
            self.hardcover_client.update_progress(
                ub['id'],
                0,  # No page number for audiobooks
                edition_id=hardcover_details.hardcover_edition_id,
                is_finished=is_finished,
                current_percentage=percentage,
                audio_seconds=audio_seconds
            )

            updated_state = {
                'pct': percentage,
                'progress_seconds': progress_seconds,
                'total_seconds': audio_seconds,
                'status': current_status
            }

            return SyncResult(percentage, True, updated_state)

        except Exception as e:
            logger.error(f"❌ Failed to update Hardcover audiobook progress: {e}")
            return SyncResult(None, False)
