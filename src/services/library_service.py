"""
Library Service.
Handles high-level book management, bridging the gap between
AudioBookShelf (ABS), Grimmory (Metadata), and our local database.
"""

import glob
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional

from src.db.models import Book
from src.db.database_service import DatabaseService
from src.api.api_clients import ABSClient
from src.api.cwa_client import CWAClient
from src.services.suggestions_service import SuggestionsService
from src.utils.cache_paths import library_roots, safe_cache_path, safe_library_path
from src.utils.ebook_sources import (
    is_grimmory_source,
    is_storyteller_filename,
    local_ebook_filename,
    normalize_ebook_source,
)

logger = logging.getLogger(__name__)


class EbookInputError(ValueError):
    """An acquired ebook cannot provide usable text for alignment."""


class LibraryService:
    def __init__(self, database_service: DatabaseService, booklore_client, cwa_client: CWAClient, abs_client: ABSClient, epub_cache_dir: str, bookorbit_client=None, kavita_client=None):
        self.database_service = database_service
        self.booklore = booklore_client
        self.cwa_client = cwa_client
        self.abs_client = abs_client
        self.epub_cache_dir = epub_cache_dir
        self.bookorbit_client = bookorbit_client
        self.kavita_client = kavita_client
        
        if not os.path.exists(self.epub_cache_dir):
            try:
                os.makedirs(self.epub_cache_dir)
            except Exception:
                pass

    def get_syncable_books(self) -> List[Book]:
        """
        Returns a list of books that are active and candidates for synchronization.
        """
        # This wraps the low-level DB query
        return self.database_service.get_all_books()

    def acquire_ebook(self, abs_item: dict, book: Optional[Book] = None) -> Optional[str]:
        """
        Attempt to acquire an ebook for the given audiobook item.
        Priority Chain:
        0. Explicit mapping (book.ebook_source + book.ebook_source_id)
        1. ABS Direct Match (Audiobook item has ebook file)
        2. Grimmory (Curated DB Match)
        3. CWA (Automated Library Search via OPDS)
        4. ABS Search (Search other libraries for title)
        5. Filesystem (Fallback - handled by caller)
        
        Returns:
            Absolute path to the downloaded/found ebook, or None.
        """
        # Priority 0 — Explicit mapping (runs before abs_item guard so a mapping
        # with no usable ABS item can still resolve its ebook from the source library)
        if book and book.ebook_source and book.ebook_source_id:
            # Skip tri-linked mappings where ebook_filename names the Storyteller artifact
            # while ebook_source_id points at the source library book.
            if is_storyteller_filename(book.ebook_filename):
                logger.debug("   Priority 0 (Explicit mapping): Skipped — ebook_filename is a Storyteller artifact")
            else:
                # Map source to client
                client = None
                source_name = book.ebook_source
                normalized_source = normalize_ebook_source(source_name)
                # Cache identity survives provider outages and source-side renames.
                local_filename = local_ebook_filename(book)
                cache_path = safe_cache_path(self.epub_cache_dir, local_filename) if local_filename else None
                if cache_path and cache_path.is_file() and cache_path.stat().st_size > 1024:
                    logger.info(f"   ✅ Priority 0 (Explicit mapping): Using cached ebook: {cache_path}")
                    return str(cache_path)
                if normalized_source == "BookOrbit":
                    client = self.bookorbit_client
                elif is_grimmory_source(source_name):
                    client = self.booklore
                elif normalized_source == "Kavita":
                    client = self.kavita_client
                elif normalized_source == "CWA":
                    client = self.cwa_client
                # Any other source is not supported here
                
                if client and getattr(client, "is_configured", lambda: True)():
                    # ``ebook_filename`` is mutable external metadata. Keep using
                    # the original local cache name when a source-side rename is
                    # reconciled so filename drift alone does not redownload bytes
                    # or change the KoSync document identity.
                    local_filename = local_ebook_filename(book)
                    if local_filename:
                        cache_path = safe_cache_path(self.epub_cache_dir, local_filename)
                        if cache_path:
                            # Mounted library files take precedence over duplicate downloads.
                            for filename in dict.fromkeys((local_filename, book.ebook_filename)):
                                if not filename or safe_cache_path(self.epub_cache_dir, filename) is None:
                                    continue
                                for root in library_roots():
                                    try:
                                        for local_path in root.glob(f"**/{glob.escape(filename)}"):
                                            safe_path = safe_library_path(local_path)
                                            if safe_path and safe_path.is_file() and safe_path.stat().st_size > 1024:
                                                logger.info("Using mounted ebook: %s", safe_path)
                                                return str(safe_path)
                                    except OSError as e:
                                        logger.warning("Could not search ebook directory %s: %s", root, e, exc_info=True)

                            # Download from the source library
                            try:
                                logger.info(f"   📥 Priority 0 (Explicit mapping): Downloading from {source_name} ({book.ebook_source_id})")
                                if normalized_source == "CWA":
                                    target = client.get_book_by_id(book.ebook_source_id)
                                    if (target and str(target.get('id')) == str(book.ebook_source_id)
                                            and target.get('download_url')):
                                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                                        if (client.download_ebook(target['download_url'], str(cache_path))
                                                and cache_path.is_file() and self._is_epub_file(str(cache_path))):
                                            logger.info(f"   ✅ Priority 0 (Explicit mapping): Downloaded to {cache_path}")
                                            return str(cache_path)
                                    return None
                                content = client.download_book(book.ebook_source_id)
                                if content:
                                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                                    cache_path.write_bytes(content)
                                    if cache_path.stat().st_size > 1024:
                                        logger.info(f"   ✅ Priority 0 (Explicit mapping): Downloaded to {cache_path}")
                                        return str(cache_path)
                                    else:
                                        logger.warning(f"   Priority 0 (Explicit mapping): Downloaded content too small, falling through")
                                else:
                                    logger.warning(f"   Priority 0 (Explicit mapping): Empty download from {source_name} ({book.ebook_source_id}), falling through")
                            except Exception as e:
                                logger.warning(f"   Priority 0 (Explicit mapping): Download failed for {source_name} ({book.ebook_source_id}): {e}", exc_info=True)
                        else:
                            logger.debug("   Priority 0 (Explicit mapping): Skipped — safe_cache_path returned None")
                    else:
                        logger.debug("   Priority 0 (Explicit mapping): Skipped — empty ebook_filename")
                else:
                    logger.debug(f"   Priority 0 (Explicit mapping): Skipped — client for {source_name} not available or not configured")
                if normalized_source == "CWA":
                    # An unavailable chosen book must not become another ABS/CWA book.
                    return None
            if normalize_ebook_source(book.ebook_source) == "CWA":
                return None

        if not abs_item:
            return None

        item_id = abs_item.get('id')
        title = abs_item.get('media', {}).get('metadata', {}).get('title')
        author = abs_item.get('media', {}).get('metadata', {}).get('authorName')
        
        # Sanity check
        if not item_id or not title:
            return None

        logger.info(f"📚 Acquiring ebook for: {title} ({item_id})")
        logger.debug(f"   Author: {author}")
        logger.debug(f"   ABS Client available: {self.abs_client is not None}")
        logger.debug(f"   CWA Client available: {self.cwa_client is not None}, configured: {self.cwa_client.is_configured() if self.cwa_client else 'N/A'}")
        if self.bookorbit_client:
            logger.debug(f"   BookOrbit Client available: {self.bookorbit_client is not None}, configured: {self.bookorbit_client.is_configured() if self.bookorbit_client else 'N/A'}")

        # 1. ABS Direct Match
        if self.abs_client:
            ebooks = self.abs_client.get_ebook_files(item_id)
            logger.debug(f"   ABS Direct Check: Found {len(ebooks)} ebook file(s)")
            if ebooks:
                logger.info(f"   ✅ Priority 1 (ABS Direct): Found {len(ebooks)} ebook(s) in item.")
                output_path = self._acquire_abs_epub(ebooks, item_id, "direct")
                if output_path:
                    return output_path

        # 2. Grimmory (Curated)
        # Placeholder for curated DB lookup. 
        # Future: Check self.db.find_booklore_match(title, author)
        
        # 3. CWA (OPDS)
        if self.cwa_client and self.cwa_client.is_configured():
            target = self._find_cwa_match(title, author or '')
            if target:
                filename = f"{item_id}_cwa.epub"
                output_path = os.path.join(self.epub_cache_dir, filename)
                
                if (self.cwa_client.download_ebook(target['download_url'], output_path)
                        and self._is_epub_file(output_path)):
                    logger.info(f"   ⬇️ Downloaded CWA match to {output_path}")
                    return output_path

        # 4. ABS Search
        if self.abs_client:
             results = self.abs_client.search_ebooks(title)
             if results:
                 logger.info(f"   ✅ Priority 4 (ABS Search): Found {len(results)} matches for '{title}'")
                 # Try to find one with ebook files
                 for res in results:
                     # Check if author matches loosely
                     # ABS sends author: null for items with no author metadata,
                     # and dict.get() returns that None rather than the default.
                     res_author = res.get('author') or ''
                     if author and author.lower() not in res_author.lower() and res_author.lower() not in author.lower():
                         continue

                     target_files = self.abs_client.get_ebook_files(res['id'])
                     if target_files:
                         output_path = self._acquire_abs_epub(target_files, item_id, "abs_search")
                         if output_path:
                             return output_path

        return None

    @staticmethod
    def _is_epub_file(path: str) -> bool:
        """Reject non-EPUB downloads before returning a discovered attachment."""
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() or archive.read("mimetype") != b"application/epub+zip":
                    return False
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                rootfile = container.find(".//{*}rootfile")
                return bool(rootfile is not None and rootfile.get("full-path") in archive.namelist())
        except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
            return False

    def _acquire_abs_epub(self, files: list[dict], item_id: str, suffix: str) -> Optional[str]:
        """Try EPUB attachments only; a companion PDF must not shadow a later EPUB."""
        for target in files:
            if str(target.get('ext') or '').lower().lstrip('.') != 'epub':
                continue
            output_path = os.path.join(self.epub_cache_dir, f"{item_id}_{suffix}.epub")
            if self._is_epub_file(output_path):
                logger.info(f"   Using cached ebook: {output_path}")
                return output_path
            if (self.abs_client.download_file(target['stream_url'], output_path)
                    and self._is_epub_file(output_path)):
                label = "ABS search" if suffix == "abs_search" else "direct"
                logger.info(f"   ⬇️ Downloaded {label} match to {output_path}")
                return output_path
            logger.warning("   ABS ebook is not an EPUB archive; trying the next source: %s", output_path)
        return None

    def _find_cwa_match(self, title: str, author: str) -> Optional[dict]:
        """Search bounded title variants and accept only one matching CWA book ID."""
        def clean_title(value: str) -> str:
            return re.sub(r'\s*\((?:unabridged|abridged|audiobook|a novel)\)', '', value,
                          flags=re.IGNORECASE).strip().replace('–', '-').replace('—', '-')

        def title_parts(value: str) -> tuple[str, str]:
            parts = re.split(r":|\s-\s", clean_title(value), maxsplit=1)
            return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""

        def title_key(value: str) -> str:
            key = re.sub(r'[^\w]+', ' ', clean_title(value).casefold()).strip()
            return re.sub(r'^(?:the|a|an)\s+', '', key)

        clean = clean_title(title)
        base, _subtitle = title_parts(clean)
        authors = SuggestionsService._author_names(author)
        article_stripped = re.sub(r'^(?:the|a|an)\s+', '', base, flags=re.IGNORECASE)
        queries = dict.fromkeys((f"{clean} {authors[0]}" if authors else clean, clean, base,
                                 article_stripped,
                                 authors[0] if authors else ''))
        matches = {}
        result_counts = {}
        for query in queries:
            if not query:
                continue
            results = self.cwa_client.search_ebooks(query)
            result_counts[query] = len(results)
            for result in results:
                candidate_title = result.get('title') or ''
                exact = title_key(candidate_title) == title_key(clean)
                candidate_base, candidate_subtitle = title_parts(candidate_title)
                base_match = (
                    not exact
                    and bool(authors)
                    and title_key(candidate_base) == title_key(base)
                    and bool(_subtitle) and not candidate_subtitle
                )
                if not exact and not base_match:
                    continue
                if authors and SuggestionsService._author_score(author, result.get('author') or '') < 85:
                    continue
                if (result.get('id') is not None and result.get('download_url')
                        and str(result.get('ext') or '').lower() == 'epub'):
                    candidate_id = str(result['id'])
                    score = 2 if exact else 1
                    if candidate_id not in matches or score > matches[candidate_id][0]:
                        matches[candidate_id] = (score, result, query)
        if matches:
            best_score = max(score for score, _, _ in matches.values())
            best = [(result, query) for score, result, query in matches.values() if score == best_score]
            if len(best) == 1:
                result, query = best[0]
                logger.info(f"   ✅ Priority 3 (CWA): Found {result_counts[query]} matches for '{query}'")
                return result
            logger.warning("   CWA: Ambiguous matches for '%s'; explicit mapping required", title)
        return None

    def sync_library_books(self):
        """
        Main Routine: Synchronize our local library DB with external metadata sources (Grimmory).
        
        The new BookloreClient handles its own file-based caching internally.
        This method now simply triggers a cache refresh by calling get_all_books().
        """
        books = self.get_syncable_books()
        logger.info(f"📚 LibraryService: Syncing metadata for {len(books)} books...")
        
        # Check if Grimmory is configured
        if not self.booklore or not self.booklore.is_configured():
            logger.info("   Grimmory not configured, skipping library sync.")
            return
            
        logger.info("✅ Grimmory integration enabled - ebooks sourced from API")
        
        # Trigger cache refresh by calling get_all_books()
        # This will refresh the internal JSON-based cache if stale and PRUNE ghosts
        try:
            # Note: get_all_books() triggers _refresh_book_cache() if needed.
            # The client now handles persistence of new/pruned items internally.
            # We do NOT need to iterate and save everything again, as that is redundant
            # and could mask pruning operations.
            
            all_books = self.booklore.get_all_books()
            logger.info(f"   📚 Grimmory cache is active with {len(all_books)} books.")
            reconcile = getattr(self.booklore, "reconcile_mapping_filename_drift", None)
            if callable(reconcile):
                reconcile()
            
        except Exception as e:
            logger.error(f"   ❌ Library sync failed: {e}", exc_info=True)
