# [START FILE: abs-kosync-enhanced/hardcover_client.py]
"""
Hardcover.app GraphQL API Client

Handles book tracking, progress updates, and reading dates for Hardcover.app integration.

Key features:
- Auto-sets started_at when creating a new read
- Auto-sets finished_at when marking as finished (>99% progress)
- Supports ISBN and title/author search for book matching


"""

import os
import logging
import time
from typing import Optional, Dict
from datetime import date

import requests

from src.utils.string_utils import calculate_similarity, clean_book_title

logger = logging.getLogger(__name__)


class HardcoverRateLimitError(Exception):
    """Raised when Hardcover throttles a read query after bounded retries."""


class HardcoverClient:
    def __init__(self):
        self.api_url = "https://api.hardcover.app/v1/graphql"
        self.token = os.environ.get("HARDCOVER_TOKEN")
        self.user_id = None

        if self.token:
            self.token = self.token.strip()
            if self.token.lower().startswith("bearer "):
                self.token = self.token[7:].strip()

        if not self.token:
            logger.info("HARDCOVER_TOKEN not set")
            return

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "ABS-KoSync-Enhanced/5.9",
        }

    @staticmethod
    def _extract_me_payload(data) -> Optional[dict]:
        me = (data or {}).get("me")
        if isinstance(me, list):
            me = me[0] if me and isinstance(me[0], dict) else None
        elif not isinstance(me, dict):
            me = None
        return me

    @staticmethod
    def _is_read_only_graphql(query: str) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        return not normalized.lower().startswith("mutation")

    @staticmethod
    def _get_retry_delay(response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                delay = float(retry_after)
                if delay > 0:
                    return delay
            except (TypeError, ValueError):
                pass
        return float(2 ** (attempt - 1))

    def is_configured(self):
        enabled_val = os.environ.get("HARDCOVER_ENABLED", "").lower()
        if enabled_val == "false":
            return False
        return bool(self.token)

    def check_connection(self):
        """Test connection to Hardcover API by trying to get user ID."""
        if not self.is_configured():
            raise Exception("Hardcover not configured - HARDCOVER_TOKEN not set")

        user_id = self.get_user_id()
        if not user_id:
            raise Exception("Failed to fetch user ID from Hardcover API")

        logger.info(f"✅ Hardcover client connection verified, user id: {user_id}")
        return True

    def query(self, query: str, variables: Dict = None) -> Optional[Dict]:
        if not self.token:
            return None

        is_read_only = self._is_read_only_graphql(query)
        max_attempts = 3 if is_read_only else 1

        try:
            for attempt in range(1, max_attempts + 1):
                response = requests.post(
                    self.api_url,
                    json={"query": query, "variables": variables or {}},
                    headers=self.headers,
                    timeout=10,
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("data"):
                        return data["data"]
                    if data.get("errors"):
                        logger.error(f"❌ GraphQL errors: {data['errors']}")
                    return None

                if response.status_code == 429 and is_read_only:
                    if attempt < max_attempts:
                        delay = self._get_retry_delay(response, attempt)
                        logger.warning(
                            "⚠️ Hardcover rate limited read query. Retrying in %.1fs (attempt %d/%d).",
                            delay,
                            attempt,
                            max_attempts,
                        )
                        time.sleep(delay)
                        continue
                    raise HardcoverRateLimitError(
                        f"Hardcover read query throttled after {max_attempts} attempts"
                    )

                logger.error(f"❌ HTTP {response.status_code}: {response.text}")
                return None
        except HardcoverRateLimitError:
            raise
        except Exception as e:
            logger.error(f"❌ Hardcover query failed: {e}")

        return None

    def get_user_id(self) -> Optional[int]:
        if self.user_id:
            return self.user_id

        result = self.query("{ me { id } }")
        me = self._extract_me_payload(result)
        if me:
            self.user_id = me.get("id")
        return self.user_id

    def get_user_book(self, book_id):
        """Fetch the user's specific entry (UserBook) for a generic book_id."""
        # FIX: Prevent crash if book_id is None
        if not book_id:
            return None

        # Ensure we query for the current user as well as the book
        user_id = self.get_user_id()
        if not user_id:
            return None

        query = """
        query GetUserBook($book_id: Int!, $user_id: Int!) {
            user_books(where: {book_id: {_eq: $book_id}, user_id: {_eq: $user_id}}) {
                id
                status_id
            }
        }
        """
        try:
            response = self.query(
                query, {"book_id": int(book_id), "user_id": int(user_id)}
            )

            if response and "user_books" in response:
                books = response["user_books"]
                if books:
                    return books[0]

        except Exception as e:
            logger.error(f"âŒ Error fetching user book: {e}")

        return None

    def _extract_authors_from_cached(self, cached_contributors) -> list[str]:
        """
        Parses the JSON list of contributors from Hardcover API.
        Handles both formats: {'author': {'name': '...'}} or {'name': '...'}
        """
        if not cached_contributors or not isinstance(cached_contributors, list):
            return []

        authors = []
        for item in cached_contributors:
            if not isinstance(item, dict):
                continue
            
            # Case 1: {'author': {'name': 'Author Name'}}
            if "author" in item and isinstance(item["author"], dict):
                name = item["author"].get("name")
                if name:
                    authors.append(name)
            # Case 2: {'name': 'Author Name'}
            elif "name" in item:
                authors.append(item["name"])
        
        return authors

    def search_by_isbn(self, isbn: str) -> Optional[Dict]:
        """Search by ISBN-13 or ISBN-10."""
        isbn_key = "isbn_13" if len(str(isbn)) == 13 else "isbn_10"

        query = f"""
        query ($isbn: String!) {{
            editions(where: {{ {isbn_key}: {{ _eq: $isbn }} }}) {{
                id
                pages
                book {{
                    id
                    title
                    slug
                }}
            }}
        }}
        """

        result = self.query(query, {"isbn": str(isbn)})
        if result and result.get("editions") and len(result["editions"]) > 0:
            edition = result["editions"][0]
            return {
                "book_id": edition["book"]["id"],
                "slug": edition["book"].get("slug"),
                "edition_id": edition["id"],
                "pages": edition["pages"],
                "title": edition["book"]["title"],
            }
        return None

    def search_by_title_author(self, title: str, author: str = None) -> Optional[Dict]:
        """Search by title and author, returning the best fuzzy match."""
        # Clean the input title for better matching comparison
        clean_input_title = clean_book_title(title)
        clean_input_author = author.lower().strip() if author else ""

        # Construct search query
        search_query = f"{clean_input_title} {author or ''}".strip()

        query = """
        query ($query: String!) {
            search(
                query: $query, 
                per_page: 10, 
                page: 1, 
                query_type: "Book"
            ) {
                ids
            }
        }
        """

        result = self.query(query, {"query": search_query})
        if not result or not result.get("search") or not result["search"].get("ids"):
            return None

        book_ids = result["search"]["ids"]
        if not book_ids:
            return None

        # Fetch details for up to 10 books to compare
        book_query = """
        query ($ids: [Int!]) {
            books(where: { id: { _in: $ids }}) {
                id
                title
                slug
                cached_contributors
            }
        }
        """

        book_result = self.query(book_query, {"ids": book_ids})
        if not book_result or not book_result.get("books"):
            return None

        candidates = book_result["books"]
        best_match = None
        best_score = 0.0

        for book in candidates:
            # Score match
            candidate_title = clean_book_title(book["title"])
            title_score = calculate_similarity(clean_input_title, candidate_title)

            # Author Score
            author_score = 0.0
            if clean_input_author:
                # Get all authors for this book from cached_contributors
                authors = [
                    a.lower().strip()
                    for a in self._extract_authors_from_cached(book.get("cached_contributors"))
                ]
                if authors:
                    # Find best similarity among all authors
                    author_score = max(
                        calculate_similarity(clean_input_author, a) for a in authors
                    )
                else:
                    # If book has no authors and we provided one, penalize?
                    # For now, let's keep it 0.0
                    author_score = 0.0
            else:
                # If no author provided, author matching shouldn't hurt or help disproportionally
                author_score = 1.0

            # Combined Score logic:
            # Title is primary, but author acts as a strong multiplier/filter.
            # If author matches well (>0.8), we trust the match more.
            # If author is way off (<0.4), it's likely a different book with same title.

            if clean_input_author:
                # Weights: 60% Title, 40% Author
                score = (title_score * 0.6) + (author_score * 0.4)

                # Boost if author is an excellent match
                if author_score > 0.9:
                    score += 0.1
            else:
                score = title_score

            logger.debug(
                f"Matches for '{title}' by '{author}': '{book['title']}' (Score: {score:.2f}, Title: {title_score:.2f}, Author: {author_score:.2f})"
            )

            if score > best_score:
                best_score = score
                best_match = book

        # Threshold check
        if best_match and best_score > 0.5:
            logger.info(
                f"Selected best match: '{best_match['title']}' (Score: {best_score:.2f})"
            )

            edition = self.get_default_edition(best_match["id"])

            return {
                "book_id": best_match["id"],
                "slug": best_match.get("slug"),
                "edition_id": edition.get("id") if edition else None,
                "pages": edition.get("pages") if edition else None,
                "title": best_match["title"],
            }

        return None

    def get_default_edition(self, book_id: int) -> Optional[Dict]:
        """Get default edition for a book. Tries ebook, physical, then audiobook."""
        query = """
        query ($bookId: Int!) {
            books_by_pk(id: $bookId) {
                default_ebook_edition {
                    id
                    pages
                }
                default_physical_edition {
                    id
                    pages
                }
                default_audio_edition {
                    id
                    audio_seconds
                }
            }
        }
        """

        result = self.query(query, {"bookId": book_id})
        if result and result.get("books_by_pk"):
            book = result["books_by_pk"]
            if book.get("default_ebook_edition"):
                return book["default_ebook_edition"]
            elif book.get("default_physical_edition"):
                return book["default_physical_edition"]
            elif book.get("default_audio_edition"):
                return book["default_audio_edition"]

        return None

    def get_book_author(self, book_id: int) -> Optional[str]:
        """Fetch the primary author name for a book."""
        query = """
        query ($bookId: Int!) {
            books_by_pk(id: $bookId) {
                cached_contributors
            }
        }
        """
        result = self.query(query, {"bookId": book_id})
        if result and result.get("books_by_pk"):
            cached_contributors = result["books_by_pk"].get("cached_contributors", [])
            authors = self._extract_authors_from_cached(cached_contributors)
            if authors:
                return authors[0]
        return None

    def get_book_editions(self, book_id: int) -> list:
        """Fetch all editions for a book with format, pages, duration, and year."""
        query = """
        query ($bookId: Int!) {
            editions(where: { book_id: { _eq: $bookId } }) {
                id
                pages
                audio_seconds
                edition_format
                physical_format
                release_date
            }
        }
        """
        result = self.query(query, {"bookId": book_id})
        if result and result.get("editions"):
            editions = []
            for ed in result["editions"]:
                # Determine format label: prefer edition_format, fall back to physical_format
                format_label = ed.get("edition_format") or ed.get("physical_format")
                if not format_label:
                    # Infer format from available data
                    if ed.get("audio_seconds") and ed.get("audio_seconds") > 0:
                        format_label = "Audiobook"
                    elif ed.get("pages") and ed.get("pages") > 0:
                        format_label = "Book"
                    else:
                        format_label = "Unknown"
                # Normalize format label
                if format_label and format_label != "Unknown":
                    format_lower = format_label.lower()
                    if format_lower == "ebook":
                        format_label = "eBook"
                    else:
                        format_label = format_label.capitalize()
                # Extract year from release_date (format: "YYYY-MM-DD")
                release_date = ed.get("release_date")
                year = (
                    int(release_date[:4])
                    if release_date and len(release_date) >= 4
                    else None
                )

                editions.append(
                    {
                        "id": ed.get("id"),
                        "format": format_label,
                        "pages": ed.get("pages"),
                        "audio_seconds": ed.get("audio_seconds"),
                        "year": year,
                    }
                )
            return editions
        return []

    def resolve_book_from_input(self, input_str: str) -> Optional[Dict]:
        """
        Resolve a Hardcover book from a URL, numeric ID, or slug.
        Returns dict: { 'book_id', 'edition_id', 'pages', 'title' } or None.
        """
        if not input_str:
            return None

        from urllib.parse import urlparse

        s = input_str.strip()
        # If it's a URL, try to extract the last segment of the path
        try:
            parsed = urlparse(s)
            if parsed.scheme and parsed.netloc and parsed.path:
                path = parsed.path.rstrip("/")
                if "/" in path:
                    s = path.split("/")[-1]
                else:
                    s = path
        except Exception:
            pass

        # If it looks numeric, treat as book ID
        book = None
        if s.isdigit():
            try:
                book_id = int(s)
                query = """
                query ($id: Int!) {
                    books_by_pk(id: $id) {
                        id
                        title
                        slug
                        default_ebook_edition {
                            id
                            pages
                        }
                        default_physical_edition {
                            id
                            pages
                        }
                        default_audio_edition {
                            id
                            audio_seconds
                        }
                    }
                }
                """
                result = self.query(query, {"id": book_id})
                if result and result.get("books_by_pk"):
                    book = result["books_by_pk"]
                else:
                    return None
            except Exception as e:
                logger.error(f"âŒ resolve_book_from_input error (id): {e}")
                return None
        else:
            # Treat as slug
            slug = s
            query = """
            query ($slug: String!) {
                books(where: { slug: { _eq: $slug }}, limit: 1) {
                    id
                    title
                    slug
                    default_ebook_edition {
                        id
                        pages
                    }
                    default_physical_edition {
                        id
                        pages
                    }
                    default_audio_edition {
                        id
                        audio_seconds
                    }
                }
            }
            """
            result = self.query(query, {"slug": slug})
            if result and result.get("books") and len(result["books"]) > 0:
                book = result["books"][0]
            else:
                return None

        edition = None
        audio_seconds = None
        if book.get("default_ebook_edition"):
            edition = book["default_ebook_edition"]
        elif book.get("default_physical_edition"):
            edition = book["default_physical_edition"]
        elif book.get("default_audio_edition"):
            edition = book["default_audio_edition"]
            audio_seconds = edition.get("audio_seconds")

        return {
            "book_id": book.get("id"),
            "slug": book.get("slug"),
            "edition_id": edition.get("id") if edition else None,
            "pages": edition.get("pages") if edition else None,
            "audio_seconds": audio_seconds,
            "title": book.get("title"),
        }

    def find_user_book(self, book_id: int) -> Optional[Dict]:
        """Find existing user_book with read info."""
        query = """
        query ($bookId: Int!, $userId: Int!) {
            user_books(where: { book_id: { _eq: $bookId }, user_id: { _eq: $userId }}) {
                id
                status_id
                edition_id
                user_book_reads(order_by: {id: desc}, limit: 1) {
                    id
                    started_at
                    finished_at
                    progress_pages
                    progress_seconds
                }
            }
        }
        """

        result = self.query(query, {"bookId": book_id, "userId": self.get_user_id()})
        if result and result.get("user_books") and len(result["user_books"]) > 0:
            return result["user_books"][0]
        return None

    def update_status(
        self, book_id: int, status_id: int, edition_id: int = None
    ) -> Optional[Dict]:
        """
        Create/update user_book status.

        Status IDs:
        - 1: Want to Read
        - 2: Currently Reading
        - 3: Read (Finished)
        - 4: Did Not Finish
        """
        query = """
        mutation ($object: UserBookCreateInput!) {
            insert_user_book(object: $object) {
                error
                user_book {
                    id
                    status_id
                    edition_id
                }
            }
        }
        """

        update_args = {
            "book_id": int(book_id),
            "status_id": status_id,
            "privacy_setting_id": 1,
        }

        if edition_id:
            update_args["edition_id"] = int(edition_id)

        result = self.query(query, {"object": update_args})
        if result and result.get("insert_user_book"):
            error = result["insert_user_book"].get("error")
            if error:
                logger.error(f"âŒ Hardcover update_status error: {error}")
            return result["insert_user_book"].get("user_book")
        return None

    def _get_today_date(self) -> str:
        """Get today's date in YYYY-MM-DD format for Hardcover API."""
        return date.today().isoformat()

    def update_progress(
        self,
        user_book_id: int,
        page: int,
        edition_id: int = None,
        is_finished: bool = False,
        current_percentage: float = 0.0,
        audio_seconds: int = None,
    ) -> bool:
        """
        Update reading progress.
        Uses current_percentage > 0.02 (2%) to decide when to set 'started_at'.
        For audiobook editions, pass audio_seconds to use progress_seconds instead of progress_pages.
        """
        # First check if there's an existing read
        read_query = """
        query ($userBookId: Int!) {
            user_book_reads(where: { user_book_id: { _eq: $userBookId }}, order_by: {id: desc}, limit: 1) {
                id
                started_at
                finished_at
            }
        }
        """

        read_result = self.query(read_query, {"userBookId": user_book_id})
        today = self._get_today_date()

        # LOGIC: Only set started date if we are past 2%
        should_start = current_percentage > 0.02

        if (
            read_result
            and read_result.get("user_book_reads")
            and len(read_result["user_book_reads"]) > 0
        ):
            # --- UPDATE EXISTING READ ---
            existing_read = read_result["user_book_reads"][0]
            read_id = existing_read["id"]

            # Preserve existing dates
            started_at_val = existing_read.get("started_at")
            finished_at_val = existing_read.get("finished_at")

            # If no start date exists, and we passed 2%, set it to today
            if not started_at_val and should_start:
                started_at_val = today
                logger.info(
                    f"ðŸ”„ Hardcover: Setting started_at to '{today}' (Progress: {current_percentage:.1%})"
                )

            if is_finished and not finished_at_val:
                finished_at_val = today
                logger.info(f"ðŸ”„ Hardcover: Setting finished_at to '{today}'")

            # Use progress_seconds for audiobooks, progress_pages for page-based editions
            if audio_seconds and audio_seconds > 0:
                progress_seconds = int(audio_seconds * current_percentage)
                query = """
                mutation UpdateBookProgress($id: Int!, $seconds: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                    update_user_book_read(id: $id, object: {
                        progress_seconds: $seconds,
                        progress_pages: null,
                        edition_id: $editionId,
                        started_at: $startedAt,
                        finished_at: $finishedAt
                    }) {
                        error
                        user_book_read { id }
                    }
                }
                """
                variables = {
                    "id": read_id,
                    "seconds": progress_seconds,
                    "editionId": int(edition_id),
                    "startedAt": started_at_val,
                    "finishedAt": finished_at_val,
                }
            else:
                query = """
                mutation UpdateBookProgress($id: Int!, $pages: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                    update_user_book_read(id: $id, object: {
                        progress_pages: $pages,
                        progress_seconds: null,
                        edition_id: $editionId,
                        started_at: $startedAt,
                        finished_at: $finishedAt
                    }) {
                        error
                        user_book_read { id }
                    }
                }
                """
                variables = {
                    "id": read_id,
                    "pages": page,
                    "editionId": int(edition_id),
                    "startedAt": started_at_val,
                    "finishedAt": finished_at_val,
                }

            result = self.query(query, variables)

            if result and result.get("update_user_book_read"):
                if result["update_user_book_read"].get("error"):
                    return False
                return True
            return False

        else:
            # --- CREATE NEW READ ---
            # Apply logic to new reads too
            started_at_val = today if should_start else None
            finished_at_val = today if is_finished else None

            # Use progress_seconds for audiobooks, progress_pages for page-based editions
            if audio_seconds and audio_seconds > 0:
                progress_seconds = int(audio_seconds * current_percentage)
                query = """
                mutation InsertUserBookRead($id: Int!, $seconds: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                    insert_user_book_read(user_book_id: $id, user_book_read: {
                        progress_seconds: $seconds,
                        progress_pages: null,
                        edition_id: $editionId,
                        started_at: $startedAt,
                        finished_at: $finishedAt
                    }) {
                        error
                        user_book_read { id }
                    }
                }
                """
                variables = {
                    "id": user_book_id,
                    "seconds": progress_seconds,
                    "editionId": int(edition_id),
                    "startedAt": started_at_val,
                    "finishedAt": finished_at_val,
                }
            else:
                query = """
                mutation InsertUserBookRead($id: Int!, $pages: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                    insert_user_book_read(user_book_id: $id, user_book_read: {
                        progress_pages: $pages,
                        progress_seconds: null,
                        edition_id: $editionId,
                        started_at: $startedAt,
                        finished_at: $finishedAt
                    }) {
                        error
                        user_book_read { id }
                    }
                }
                """
                variables = {
                    "id": user_book_id,
                    "pages": page,
                    "editionId": int(edition_id),
                    "startedAt": started_at_val,
                    "finishedAt": finished_at_val,
                }

            result = self.query(query, variables)

            if result and result.get("insert_user_book_read"):
                if result["insert_user_book_read"].get("error"):
                    return False
                return True
            return False


# [END FILE]

