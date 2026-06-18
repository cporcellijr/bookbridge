from dataclasses import dataclass, field
from typing import Optional, Callable, Tuple

from src.db.models import Book, State

@dataclass
class ServiceState:
    # can contain xpath, ts, pct, href, frag
    current: dict
    previous_pct: float
    delta: float
    threshold: float
    is_configured: bool
    display: Tuple[str, str]
    value_formatter: Callable[[float], str]
    value_seconds_formatter: Callable[[float], str] = None

@dataclass
class LocatorResult:
    percentage: float
    xpath: Optional[str] = None
    match_index: Optional[int] = None
    cfi: Optional[str] = None
    href: Optional[str] = None
    fragment: Optional[str] = None
    perfect_ko_xpath: Optional[str] = None
    css_selector: Optional[str] = None
    chapter_progress: Optional[float] = None
    fragments: Optional[list] = None

@dataclass
class UpdateProgressRequest:
    locator_result: LocatorResult
    txt: Optional[str] = None
    # can be percentage or timestamp (ABS)
    previous_location: Optional[float] = None
    # When True, the ABS push credits the forward audio delta as listening time
    # instead of zero (used when an audiobook-companion leader like Storyteller
    # advances the position — see STORYTELLER_LISTENING_SESSIONS).
    credit_listening: bool = False

@dataclass
class SyncResult:
    # can be percentage or timestamp (ABS)
    location: Optional[float] = None
    success: bool = False
    updated_state: dict = field(default_factory=dict)

class SyncClient:
    """
    Base class for sync clients.
    
    Error Handling Convention:
    - Methods return None when data is not found (e.g., book doesn't exist)
    - Methods return SyncResult(success=False) for operational failures
    - Exceptions are only raised for unexpected errors (connection issues, etc.)
    """

    def __init__(self, ebook_parser):
        self.ebook_parser = ebook_parser

    def is_configured(self) -> bool:
        ...

    def check_connection(self):
        """
        Check if the client can connect to its service.
        Should raise an exception if connection fails.
        """
        ...

    def can_be_leader(self) -> bool:
        """
        Determine if this client can be the leader in the sync cycle.
        Most clients can be leaders, but some (like Hardcover) cannot provide
        text content and should never lead.
        """
        return True

    def fetch_bulk_state(self) -> Optional[dict]:
        """
        Pre-fetch all progress data in one API call.
        Returns a dict keyed by book identifier for quick lookup.
        Only implemented by clients that support bulk fetching (ABS, Storyteller).
        Default returns None (no bulk support).
        """
        return None

    def get_supported_sync_types(self) -> set:
        """
        Return set of sync types this client supports.
        Options: 'audiobook', 'ebook'
        Used for filtering which clients apply to which book sync modes.
        """
        return {'audiobook', 'ebook'}  # Default: supports both

    def supports_book(self, book: Book) -> bool:
        """Return True when this client is applicable to the provided book."""
        return True

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        """
        Args:
            bulk_context: Optional pre-fetched data to avoid redundant API calls
        """
        ...
    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        ...
    def get_fallback_text(self, book: Book, state: ServiceState) -> Optional[str]:
        """Optional method to return fallback text (e.g. previous segment) if primary match fails."""
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        ...

    def get_locator_from_text(self, txt: str, epub_file_name: str, hint_percentage: float) -> Optional[LocatorResult]:
        if not txt or not epub_file_name:
            return None
        locator_result: LocatorResult = self.ebook_parser.find_text_location(epub_file_name, txt, hint_percentage=hint_percentage)
        if not locator_result:
            return None
        # Add perfect_xpath if match_index is present, special case for KoSync
        perfect_xpath = None
        if locator_result.match_index is not None:
            perfect_xpath = self.ebook_parser.get_perfect_ko_xpath(epub_file_name, locator_result.match_index)
        # Return a new LocatorResult with perfect_xpath included
        return LocatorResult(
            percentage=locator_result.percentage,
            xpath=locator_result.xpath,
            match_index=locator_result.match_index,
            cfi=locator_result.cfi,
            href=locator_result.href,
            fragment=locator_result.fragment,
            perfect_ko_xpath=perfect_xpath,
            css_selector=locator_result.css_selector,
            chapter_progress=locator_result.chapter_progress,
            fragments=locator_result.fragments
        )
