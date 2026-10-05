import json
import logging
import os
import time
from typing import Optional, Tuple, Dict, Any

from src.api.hardcover_client import HardcoverClient, HardcoverRateLimitError
from src.db.models import Book, State, HardcoverDetails
from src.services.llm_matching import craft_search_terms, judge_best_candidate, tracker_match_enabled
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser, resolve_ebook_identifiers
from src.utils.logging_utils import sanitize_log_data
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

# Re-read candidate keys stored in State.locator_json for client 'hardcover'
REREAD_CANDIDATE_PCT_KEY = "reread_candidate_pct"
REREAD_CANDIDATE_FIRST_SEEN_KEY = "reread_candidate_first_seen"
# Confirmation margin: position must advance this far beyond the candidate anchor
# before a re-read is confirmed (2% of the book)
REREAD_CONFIRM_MARGIN = 0.02
# Minimum meaningful percentage: matches should_start threshold in hardcover_client.py
REREAD_MIN_PERCENTAGE = 0.02
# Completion threshold: matches is_finished = percentage > 0.99 in both progress paths
REREAD_COMPLETION_THRESHOLD = 0.99


class HardcoverSyncClient(SyncClient):
    """
    Hardcover sync client that handles both automating matching and progress sync.
    This integrates Hardcover as a proper sync client in the sync cycle.
    """

    def __init__(self, hardcover_client: HardcoverClient, ebook_parser: EbookParser, abs_client=None, database_service=None, ollama_client=None, booklore_client=None, bookorbit_client=None, kavita_client=None):
        super().__init__(ebook_parser)
        self.hardcover_client = hardcover_client
        self.abs_client = abs_client  # For fetching book metadata
        self.database_service = database_service
        self.ollama_client = ollama_client
        # Library clients let us read a library-hosted EPUB's embedded ISBN/author
        # when the file isn't on local disk (BookOrbit/Grimmory/Kavita ebook-only + ABS-linked).
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.kavita_client = kavita_client
        self._grimmory_list_sync_attempted = set()

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

    def _gather_match_metadata(self, book):
        """Resolve (title, author, isbn, asin) for matching, from ABS + the EPUB.

        ABS metadata is the primary source for linked audiobooks; the ebook's embedded
        identifiers supplement it (and are the only source for ebook-only mappings). The
        EPUB's real ISBN is preferred when ABS has none — ABS's ASIN is usually an Audible
        id that doesn't resolve a Hardcover edition, so the embedded ISBN is what nails
        niche titles that share a name with a more popular book.
        """
        title = author = isbn = asin = ""
        item = self.abs_client.get_item_details(book.abs_id) if self.abs_client else None
        if item:
            meta = item.get('media', {}).get('metadata', {}) or {}
            title = meta.get('title') or book.abs_title or ""
            author = meta.get('authorName') or ""
            isbn = meta.get('isbn') or ""
            asin = meta.get('asin') or ""

        # Supplement from the EPUB when there's no ABS item, or ABS lacks a usable ISBN.
        if not item or not isbn:
            ebook_meta = resolve_ebook_identifiers(
                self.ebook_parser,
                book,
                self.booklore_client,
                self.bookorbit_client,
                self.kavita_client,
            )
            title = title or ebook_meta.get('title') or book.abs_title or ""
            author = author or ebook_meta.get('author') or ""
            isbn = isbn or ebook_meta.get('isbn') or ""
            asin = asin or ebook_meta.get('asin') or ""

        return title, author, isbn, asin

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

        title, author, isbn, asin = self._gather_match_metadata(book)
        meta = {'title': title}  # keep log statements below working without an ABS item

        if not title:
            return

        # Try different search strategies in order of preference
        match = None
        matched_by = None
        first_rejected = None
        first_rejected_by = None

        # When the LLM judge is on, it OWNS title matching: only the precise ISBN/ASIN
        # searches run as plain strategies, and everything title-based is routed through
        # craft + judge below. This stops the fuzzy title search from committing a
        # same-title/wrong-author book (and short-circuiting the judge) before it runs.
        use_llm = (
            tracker_match_enabled()
            and self.ollama_client is not None
            and self.ollama_client.is_configured()
            and bool(title)
        )

        search_strategies = [
            (lambda: self.hardcover_client.search_by_isbn(isbn) if isbn else None, 'isbn', isbn),
            (lambda: self.hardcover_client.search_by_isbn(asin) if asin else None, 'asin', asin),
        ]
        # Judge off: fall back to the author-gated fuzzy title search (title+author first,
        # then blind title-only as a last resort).
        if not use_llm:
            search_strategies.append(
                (lambda: self.hardcover_client.search_by_title_author(title, author) if (title and author) else None, 'title_author', title and author)
            )
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
                        exc_info=True,
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
                        exc_info=True,
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
                    exc_info=True,
                )
                return
            conf_min = float(os.environ.get('OLLAMA_JUDGE_CONFIDENCE_MIN', 85))
            idx = judge_best_candidate(self.ollama_client, craft_title, craft_author, candidates, conf_min,
                                       isbn=(isbn or asin or ''))
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
                    exc_info=True,
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
            self._sync_grimmory_shelves_to_hardcover_lists(book, hardcover_details)
            logger.info(f"📚 Hardcover: '{sanitize_log_data(meta.get('title'))}' matched and set to Want to Read (matched by {matched_by})")
        else:
            logger.warning(f"⚠️ Hardcover: No match found for '{sanitize_log_data(meta.get('title'))}'")

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    def _projection_credentials(self) -> Optional[dict]:
        """The acting user's per-user credentials for this projection.

        The registry builds this sync client with the user's hardcover/booklore
        clients (same creds dict), so the toggle, prefix and shelf excludes resolve
        from the same account that owns the Hardcover writes and Grimmory reads.
        Falls back to the global config (creds None -> os.environ) for the global
        single-user/default cycle and for admins."""
        creds = getattr(self.hardcover_client, "_creds", None)
        if not isinstance(creds, dict):
            creds = getattr(self.booklore_client, "_creds", None) if self.booklore_client else None
        return creds if isinstance(creds, dict) else None

    def _grimmory_list_sync_mode(self, creds: Optional[dict]) -> str:
        mode = str(resolve_setting(creds, "HARDCOVER_GRIMMORY_LIST_SYNC", "off") or "off").strip().lower()
        return mode if mode in {"all", "magic", "shelf"} else "off"

    def _sync_grimmory_shelves_to_hardcover_lists(self, book, hardcover_details) -> int:
        """Project Grimmory shelf membership onto Hardcover lists for one matched book.

        Every setting resolves from the acting user's credentials, so each reader
        opts in and configures their own projection; regular users do not inherit
        the admin's global toggle, while admins/single-user fall back to it."""
        creds = self._projection_credentials()
        mode = self._grimmory_list_sync_mode(creds)
        if mode == "off":
            return 0
        if not self.booklore_client or not self.hardcover_client:
            return 0
        source_name = str(getattr(book, "ebook_source", "") or "").strip().lower()
        if source_name not in {"booklore", "grimmory"}:
            return 0
        source_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        if not source_id:
            return 0
        hardcover_book_id = getattr(hardcover_details, "hardcover_book_id", None)
        if not hardcover_book_id:
            return 0

        prefix = resolve_setting(creds, "HARDCOVER_GRIMMORY_LIST_PREFIX", "Grimmory: ")
        excluded_raw = resolve_setting(creds, "HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES", "")
        attempt_key = (
            str(getattr(book, "abs_id", "") or ""),
            str(hardcover_book_id),
            mode,
            prefix,
            excluded_raw,
        )
        if attempt_key in self._grimmory_list_sync_attempted:
            return 0

        excludes = self._split_csv(excluded_raw)
        sync_shelf = str(resolve_setting(creds, "BOOKLORE_SHELF_NAME", "") or "").strip()
        if sync_shelf and sync_shelf not in excludes:
            excludes.append(sync_shelf)

        try:
            if not self.booklore_client.is_configured():
                return 0
            shelf_mapping = self.booklore_client.get_book_shelf_mapping(
                mode=mode,
                excludes=excludes,
                target_book_ids=[source_id],
            )
        except Exception as e:
            logger.warning(
                "Hardcover: failed to read Grimmory shelves for '%s': %s",
                sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
                e,
                exc_info=True,
            )
            return 0

        shelves = (shelf_mapping or {}).get(source_id) or []
        added = 0
        for shelf_name in shelves:
            clean_shelf = str(shelf_name or "").strip()
            if not clean_shelf:
                continue
            list_name = f"{prefix}{clean_shelf}"
            try:
                if self.hardcover_client.ensure_book_on_list(
                    list_name,
                    int(hardcover_book_id),
                    edition_id=getattr(hardcover_details, "hardcover_edition_id", None),
                    description=f"Managed by BookBridge from Grimmory shelf '{clean_shelf}'.",
                ):
                    added += 1
            except Exception as e:
                logger.warning(
                    "Hardcover: failed to add '%s' to list '%s': %s",
                    sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
                    sanitize_log_data(list_name),
                    e,
                    exc_info=True,
                )

        self._grimmory_list_sync_attempted.add(attempt_key)
        if added:
            logger.info(
                "Hardcover: added '%s' to %d list(s) from Grimmory shelves",
                sanitize_log_data(getattr(book, "abs_title", None) or getattr(book, "abs_id", None)),
                added,
            )
        return added

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        """
        Hardcover doesn't provide text content, so return None.
        This client is primarily for progress synchronization.
        """
        return None

    def _handle_status_transition(self, book, hardcover_details, current_status, percentage, is_finished):
        """Handle status transitions and return the active read from the mutation."""
        updated_user_book = None
        # If finished and not already marked as Read (3), promote to Read
        if is_finished and current_status != 3:
            updated_user_book = self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                3,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Read")
            current_status = 3

        # If progress > 2% and currently "Want to Read" (1), promote to "Currently Reading" (2)
        elif percentage > 0.02 and current_status == 1:
            updated_user_book = self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                2,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Currently Reading")
            current_status = 2

        reads = (
            updated_user_book.get('user_book_reads') or []
            if isinstance(updated_user_book, dict)
            else []
        )
        active_read = reads[0] if reads else None
        return current_status, active_read

    def _read_reread_candidate(self, book: Book) -> Tuple[Optional[float], Optional[float]]:
        """
        Read the stored re-read candidate from the 'hardcover' state row.

        Returns (candidate_pct, first_seen) as floats, or (None, None) if no candidate exists
        or parsing fails. Any failure is logged at DEBUG level with exc_info=True.
        """
        try:
            state = self.database_service.get_state(book.abs_id, 'hardcover')
            if not state or not state.locator_json:
                return None, None

            locator_data = json.loads(state.locator_json)
            candidate_pct = locator_data.get(REREAD_CANDIDATE_PCT_KEY)
            first_seen = locator_data.get(REREAD_CANDIDATE_FIRST_SEEN_KEY)

            if candidate_pct is None or first_seen is None:
                return None, None

            return float(candidate_pct), float(first_seen)
        except Exception:
            logger.debug(
                "Failed to parse re-read candidate for book %s",
                sanitize_log_data(book.abs_title),
                exc_info=True,
            )
            return None, None

    def _decide_reread(
        self,
        book: Book,
        latest_read: Optional[Dict[str, Any]],
        percentage: float,
        is_finished: bool,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Decide whether a re-read is confirmed and return candidate metadata for state persistence.

        Args:
            book: The book being synced.
            latest_read: The most recent Hardcover read dict (may be None).
            percentage: Current position as a 0-1 fraction.
            is_finished: Whether the position is at completion (>0.99).

        Returns:
            (allow_new_read, candidate_meta) where:
            - allow_new_read: True if a new read should be created on Hardcover.
            - candidate_meta: Dict of keys to merge into updated_state; empty dict clears the candidate.
        """
        # Rule 1: No latest read, or latest read has no finished_at -> ordinary case
        if not latest_read or not latest_read.get("finished_at"):
            return False, {}

        # Rule 2: Read is finished AND (at completion or at/below minimum percentage)
        # This is the self-healing path for stale readers.
        if is_finished or percentage <= REREAD_MIN_PERCENTAGE:
            return False, {}

        # Rule 3: Read is finished and percentage is strictly between min and completion
        candidate_pct, first_seen = self._read_reread_candidate(book)

        if candidate_pct is None:
            # First sighting: record it, write nothing to Hardcover
            return False, {
                REREAD_CANDIDATE_PCT_KEY: percentage,
                REREAD_CANDIDATE_FIRST_SEEN_KEY: time.time(),
            }

        # Stored candidate exists
        if percentage > candidate_pct + REREAD_CONFIRM_MARGIN:
            # Position advanced across cycles -> confirmed re-read
            elapsed = time.time() - first_seen if first_seen else 0
            logger.info(
                "🔄 Hardcover: Re-read confirmed for '%s' (candidate %.2f%% -> current %.2f%%, first seen %.0fs ago)",
                sanitize_log_data(book.abs_title),
                candidate_pct * 100,
                percentage * 100,
                elapsed,
            )
            return True, {}

        # Position hasn't advanced far enough -> re-anchor to lower of the two
        # Keep original first_seen time
        return False, {
            REREAD_CANDIDATE_PCT_KEY: min(candidate_pct, percentage),
            REREAD_CANDIDATE_FIRST_SEEN_KEY: first_seen or time.time(),
        }

    @staticmethod
    def _is_preserved_read(latest_read: Optional[Dict[str, Any]], allow_new_read: bool) -> bool:
        """True when a completed read was left untouched, so nothing reached Hardcover."""
        return bool(latest_read and latest_read.get("finished_at") and not allow_new_read)

    def _preserved_hardcover_pct(self, book: Book) -> float:
        """The percentage Hardcover still holds while a completed read is preserved.

        Falls back to completion: the preserved read is finished, so 1.0 is the
        honest value when no state row records an earlier figure.
        """
        try:
            state = self.database_service.get_state(book.abs_id, 'hardcover')
        except Exception:
            logger.debug(
                "Failed to read preserved Hardcover state for book %s",
                sanitize_log_data(book.abs_title),
                exc_info=True,
            )
            return 1.0
        if state is not None and state.percentage is not None:
            return float(state.percentage)
        return 1.0

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
        self._sync_grimmory_shelves_to_hardcover_lists(book, hardcover_details)

        # Check if this is an audiobook edition
        audio_seconds = getattr(hardcover_details, 'hardcover_audio_seconds', None) or 0
        total_pages = hardcover_details.hardcover_pages or 0
        if audio_seconds <= 0 and total_pages == -1:
            # Already verified no valid edition exists; don't spend an API call on it.
            return SyncResult(None, False)

        # Get user book from Hardcover
        ub = self.hardcover_client.get_user_book(hardcover_details.hardcover_book_id)
        if not ub:
            logger.warning(
                f"⚠️ Hardcover: get_user_book returned None for book_id={hardcover_details.hardcover_book_id} "
                f"('{sanitize_log_data(book.abs_title)}') — skipping progress update"
            )
            return SyncResult(None, False)

        if audio_seconds > 0:
            return self._update_audiobook_progress(book, hardcover_details, ub, percentage, audio_seconds)

        # --- PAGE-BASED PATH ---
        # Attempt to refresh if pages are missing
        if total_pages <= 0:
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
        current_status, active_read = self._handle_status_transition(
            book, hardcover_details, current_status, percentage, is_finished
        )

        latest_read = active_read or self.hardcover_client.get_latest_read(ub['id'])
        allow_new_read, candidate_meta = self._decide_reread(
            book, latest_read, percentage, is_finished
        )

        # Update progress
        try:
            progress_kwargs = {
                'edition_id': hardcover_details.hardcover_edition_id,
                'is_finished': is_finished,
                'current_percentage': percentage,
                'allow_new_read': allow_new_read,
            }
            if latest_read:
                progress_kwargs['active_read'] = latest_read
            updated = self.hardcover_client.update_progress(
                ub['id'],
                page_num,
                **progress_kwargs,
            )
            if not updated:
                logger.error("❌ Hardcover progress update was not accepted")
                return SyncResult(None, False)

            if self._is_preserved_read(latest_read, allow_new_read):
                # Nothing reached Hardcover: the completed read still stands, so the
                # state row must not claim the incoming position.
                preserved_pct = self._preserved_hardcover_pct(book)
                return SyncResult(
                    preserved_pct,
                    True,
                    {'pct': preserved_pct, 'status': current_status, **candidate_meta},
                )

            # Calculate actual percentage from page number for state tracking
            actual_pct = min(page_num / total_pages, 1.0) if total_pages > 0 else percentage

            updated_state = {
                'pct': actual_pct,
                'pages': page_num,
                'total_pages': total_pages,
                'status': current_status,
                **candidate_meta,
            }

            return SyncResult(actual_pct, True, updated_state)

        except Exception as e:
            logger.error(f"❌ Failed to update Hardcover progress: {e}", exc_info=True)
            return SyncResult(None, False)

    def _update_audiobook_progress(self, book, hardcover_details, ub, percentage, audio_seconds):
        """Update Hardcover progress using progress_seconds for audiobook editions."""
        is_finished = percentage > 0.99
        current_status = ub.get('status_id')

        # Handle status transitions
        current_status, active_read = self._handle_status_transition(
            book, hardcover_details, current_status, percentage, is_finished
        )

        latest_read = active_read or self.hardcover_client.get_latest_read(ub['id'])
        allow_new_read, candidate_meta = self._decide_reread(
            book, latest_read, percentage, is_finished
        )

        try:
            progress_seconds = int(audio_seconds * percentage)
            progress_kwargs = {
                'edition_id': hardcover_details.hardcover_edition_id,
                'is_finished': is_finished,
                'current_percentage': percentage,
                'audio_seconds': audio_seconds,
                'allow_new_read': allow_new_read,
            }
            if latest_read:
                progress_kwargs['active_read'] = latest_read
            updated = self.hardcover_client.update_progress(
                ub['id'],
                0,  # No page number for audiobooks
                **progress_kwargs,
            )
            if not updated:
                logger.error("❌ Hardcover audiobook progress update was not accepted")
                return SyncResult(None, False)

            if self._is_preserved_read(latest_read, allow_new_read):
                # Nothing reached Hardcover: the completed read still stands, so the
                # state row must not claim the incoming position.
                preserved_pct = self._preserved_hardcover_pct(book)
                return SyncResult(
                    preserved_pct,
                    True,
                    {'pct': preserved_pct, 'status': current_status, **candidate_meta},
                )

            updated_state = {
                'pct': percentage,
                'progress_seconds': progress_seconds,
                'total_seconds': audio_seconds,
                'status': current_status,
                **candidate_meta,
            }

            return SyncResult(percentage, True, updated_state)

        except Exception as e:
            logger.error(f"❌ Failed to update Hardcover audiobook progress: {e}", exc_info=True)
            return SyncResult(None, False)
