# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

"""
from typing import Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag
from lxml import html
import hashlib
import logging
import os
import re
import glob
import posixpath
import threading
import rapidfuzz
import zipfile
import shutil
import tempfile
from pathlib import Path
from collections import OrderedDict
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

# Import epubcfi library for accurate CFI parsing
import epubcfi

class LRUCache:
    def __init__(self, capacity: int = 3):
        self.cache = OrderedDict()
        self.capacity = capacity
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def put(self, key, value):
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self.cache.clear()


class EbookParser:
    CRENGINE_FRAGILE_INLINE_TAGS = {
        "span", "em", "strong", "b", "i", "u", "a", "font", "small", "big", "sub", "sup"
    }
    CRENGINE_STRUCTURAL_TAGS = {
        "p", "div", "section", "article", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "header", "footer", "aside",
        "td", "th", "dt", "dd", "figcaption", "pre"
    }
    # Direct-child tags whose presence inside a <p> splits its direct text nodes
    # so that /p[M]/text().0 resolves to only the first fragment. Union of
    # CRENGINE_FRAGILE_INLINE_TAGS and the extra tags rejected by
    # kosync_sync_client._FRAGILE_INLINE_SEGMENT_RE — keeps the generator
    # and the downstream sanitizer in agreement on what counts as fragile.
    KOREADER_FRAGMENTING_P_CHILD_TAGS = CRENGINE_FRAGILE_INLINE_TAGS | {
        "mark", "abbr", "cite", "code", "q", "time", "s", "del", "ins"
    }

    def __init__(self, books_dir, epub_cache_dir=None, ollama_client=None):
        self.books_dir = Path(books_dir)
        self.epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir else Path("/data/epub_cache")
        self.ollama_client = ollama_client
        # Additional library folders to search for ebook files (multi-library
        # setups where a user's library is not under BOOKS_DIR). Comma- or
        # newline-separated container paths from EXTRA_EBOOK_DIRS.
        self.extra_book_dirs = self._parse_extra_book_dirs(os.getenv("EXTRA_EBOOK_DIRS", ""))

        cache_size = int(os.getenv("EBOOK_CACHE_SIZE", 3))
        self.cache = LRUCache(capacity=cache_size)
        self._media_overlay_ids_cache = LRUCache(capacity=cache_size)
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        self.useXpathSegmentFallback = os.getenv("XPATH_FALLBACK_TO_PREVIOUS_SEGMENT", "false").lower() == "true"
        self.locator_roundtrip_tolerance = int(os.getenv("LOCATOR_ROUNDTRIP_TOLERANCE_CHARS", 2))

        logger.info(
            f"✅ EbookParser initialized (cache={cache_size}, hash={self.hash_method}, "
            f"xpath_fallback={self.useXpathSegmentFallback}, extra_dirs={len(self.extra_book_dirs)})"
        )

    @staticmethod
    def _parse_extra_book_dirs(raw: str) -> list:
        """Parse EXTRA_EBOOK_DIRS into a list of Paths (comma/newline separated)."""
        if not raw:
            return []
        parts = re.split(r"[,\n]", raw)
        return [Path(p.strip()) for p in parts if p.strip()]

    def search_dirs(self) -> list:
        """All directories to search for ebook files: BOOKS_DIR + extra libraries."""
        return [self.books_dir, *self.extra_book_dirs]

    def resolve_book_path(self, filename):
        safe_name = glob.escape(filename)
        for d in self.search_dirs():
            try:
                if d.exists():
                    return next(d.glob(f"**/{safe_name}"))
            except StopIteration:
                continue

        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f

        if self.epub_cache_dir.exists():
            cached_path = self.epub_cache_dir / filename
            if cached_path.exists():
                return cached_path

        raise FileNotFoundError(f"Could not locate {filename}")

    def get_book_identifiers(self, filepath) -> set:
        """Return the set of normalized DC identifiers embedded in an EPUB.

        Used to link a hash-discovered library file to an existing mapping when
        filenames differ (e.g. a raw Calibre file vs the re-stamped CWA copy that
        share the same Calibre/ISBN identifier). Never raises.
        """
        path = Path(filepath)
        if not path.is_absolute():
            try:
                path = self.resolve_book_path(str(path))
            except FileNotFoundError:
                return set()
        try:
            book = epub.read_epub(str(path))
        except Exception as e:
            logger.debug(f"Could not read identifiers for '{path}': {e}")
            return set()
        ids = set()
        for value, _attrs in book.get_metadata("DC", "identifier"):
            norm = self._normalize_identifier(value)
            if norm:
                ids.add(norm)
        return ids

    @staticmethod
    def _normalize_identifier(raw) -> str:
        """Normalize an EPUB identifier for cross-file comparison.

        Strips common scheme prefixes (urn:uuid:, urn:isbn:, calibre:, isbn:) and
        lowercases, so the same work matches across library copies.
        """
        if not raw:
            return ""
        val = str(raw).strip().lower()
        for prefix in ("urn:uuid:", "urn:isbn:", "uuid:", "isbn:", "calibre:"):
            if val.startswith(prefix):
                val = val[len(prefix):]
                break
        return val.strip()

    def get_book_metadata(self, filename: str) -> dict:
        """Extract {title, author, isbn, asin} from an ebook's embedded metadata.

        Resolves `filename` under books_dir and reads the EPUB's Dublin Core fields.
        Used to match ABS-less (ebook-only) books to trackers. Returns empty strings
        for anything missing and never raises.
        """
        result = {"title": "", "author": "", "isbn": "", "asin": ""}
        if not filename:
            return result
        try:
            path = self.resolve_book_path(filename)
        except FileNotFoundError:
            return result
        try:
            book = epub.read_epub(str(path))
        except Exception as e:
            logger.warning(f"⚠️ Could not read EPUB metadata for '{filename}': {e}")
            return result

        titles = book.get_metadata("DC", "title")
        if titles and titles[0][0]:
            result["title"] = titles[0][0].strip()

        creators = book.get_metadata("DC", "creator")
        if creators and creators[0][0]:
            result["author"] = creators[0][0].strip()

        for value, attrs in book.get_metadata("DC", "identifier"):
            raw = (value or "").strip()
            if not raw:
                continue
            scheme = ""
            for k, v in (attrs or {}).items():
                if str(k).endswith("scheme"):
                    scheme = (v or "").upper()
                    break
            low = raw.lower()
            if not result["isbn"] and (scheme == "ISBN" or low.startswith("urn:isbn:")):
                result["isbn"] = re.sub(r"^urn:isbn:", "", low).replace("-", "").strip()
            elif not result["asin"] and (scheme == "AMAZON" or low.startswith("urn:amazon:")):
                result["asin"] = re.sub(r"^urn:amazon:", "", raw, flags=re.IGNORECASE).strip()

        return result

    def get_kosync_id(self, filepath):
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()
        
        md5 = hashlib.md5()
        try:
            file_size = os.path.getsize(filepath)
            with open(filepath, 'rb') as f:
                for i in range(-1, 11):
                    offset = 0 if i == -1 else 1024 * (4 ** i)
                    if offset >= file_size:
                        break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"❌ Error computing hash for {filepath}: {e}")
            return None

    def _compute_koreader_hash_from_bytes(self, content):
        md5 = hashlib.md5()
        try:
            file_size = len(content)
            for i in range(-1, 11):
                offset = 0 if i == -1 else 1024 * (4 ** i)
                if offset >= file_size: break

                chunk = content[offset:offset + 1024]
                if not chunk: break
                md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"❌ Error computing KOReader hash from bytes: {e}")
            return None

    def get_kosync_id_from_bytes(self, filename, content):
        if self.hash_method == "filename":
            return hashlib.md5(filename.encode('utf-8')).hexdigest()
        return self._compute_koreader_hash_from_bytes(content)

    def extract_cover(self, filepath, output_path):
        """
        Extract cover image from EPUB to output_path.
        Returns True if successful, False otherwise.
        """
        try:
            filepath = Path(filepath)
            # 1. Try to get cover from metadata using ebooklib
            try:
                book = epub.read_epub(str(filepath))
                # Check for cover item
                cover_item = None

                # Method A: get_item_with_id('cover') or similar
                # ebooklib doesn't have a standard 'get_cover' but often it's in the manifest

                # Method B: Iterate items
                for item in book.get_items():
                    if item.get_type() == ebooklib.ITEM_IMAGE:
                        # naive check: is it named "cover"?
                        if 'cover' in item.get_name().lower():
                            cover_item = item
                            break
                    if item.get_type() == ebooklib.ITEM_COVER:
                        cover_item = item
                        break

                if cover_item:
                    with open(output_path, 'wb') as f:
                        f.write(cover_item.get_content())
                    logger.debug(f"Extracted cover for {filepath.name}")
                    return True
            except Exception as e:
                logger.debug(f"ebooklib cover extraction failed for {filepath.name}: {e}")

            # 2. Fallback: ZipFile (if ebooklib fails or returns nothing)
            # (ebooklib is basically a zip wrapper anyway, but sometimes direct zip access is easier if we just want the file)
            # For now, let's stick to the attempt above. If valid EPUB, ebooklib should handle it.

            return False

        except Exception as e:
            logger.error(f"❌ Error extracting cover from '{filepath}': {e}")
            return False

    def _build_href_resolver(self, str_path):
        """Return a fn mapping an ebooklib OPF-relative href to its full archive
        path.

        ebooklib's `item.get_name()` is relative to the OPF file. When the OPF
        lives in a subdirectory (e.g. `OEBPS/package.opf`), the name omits that
        prefix (`xhtml/chapter3.xhtml`), but Readium/Storyteller key their
        reading-order and stored positions on the full archive path
        (`OEBPS/xhtml/chapter3.xhtml`). Pushing the bare name leaves the
        position unresolvable, so the reader opens at the cover. Resolve against
        the OPF directory (verified against the zip entries) so our hrefs match.
        """
        opf_dir = ""
        zip_names = set()
        try:
            with zipfile.ZipFile(str_path) as zf:
                zip_names = set(zf.namelist())
                container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
                match = re.search(r'full-path="([^"]+)"', container)
                if match:
                    opf_dir = posixpath.dirname(match.group(1))
        except Exception as e:
            logger.debug(f"href resolver: could not read container for '{str_path}': {e}")

        def resolve(name):
            if not name:
                return name
            if name in zip_names:
                return name
            if opf_dir:
                candidate = posixpath.normpath(posixpath.join(opf_dir, name))
                if not zip_names or candidate in zip_names:
                    return candidate
            return name

        return resolve

    def extract_text_and_map(self, filepath, progress_callback=None):
        """
        Used for fuzzy matching and general content extraction.
        Uses BeautifulSoup.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            filepath = self.resolve_book_path(filepath.name)
        str_path = str(filepath)

        cached = self.cache.get(str_path)
        if cached:
            if progress_callback: progress_callback(1.0)
            return cached['text'], cached['map']

        logger.info(f"Parsing EPUB: {filepath.name}")

        try:
            book = epub.read_epub(str_path)
            href_resolver = self._build_href_resolver(str_path)
            full_text_parts = []
            spine_map = []
            current_idx = 0

            total_spine = len(book.spine)

            for i, item_ref in enumerate(book.spine):
                if progress_callback:
                    progress_callback(i / total_spine)

                item = book.get_item_with_id(item_ref[0])
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), 'html.parser')
                    text = soup.get_text(separator=' ', strip=True)

                    start = current_idx
                    length = len(text)
                    end = current_idx + length

                    spine_map.append({
                        "start": start,
                        "end": end,
                        "char_len": length,
                        "spine_index": i + 1,
                        "href": href_resolver(item.get_name()),
                        "content": item.get_content()
                    })

                    full_text_parts.append(text)
                    current_idx = end + 1

            combined_text = " ".join(full_text_parts)
            self.cache.put(str_path, {'text': combined_text, 'map': spine_map})
            return combined_text, spine_map

        except Exception as e:
            logger.error(f"❌ Failed to parse EPUB '{filepath}': {e}")
            return "", []

    def get_text_at_percentage(self, filename, percentage):
        """Get text snippet at a given percentage through the book."""
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None

            target_pos = int(len(full_text) * percentage)
            # Grab a window of text around the calculated character position
            start = max(0, target_pos - 400)
            end = min(len(full_text), target_pos + 400)

            return full_text[start:end]
        except Exception as e:
            logger.error(f"❌ Error getting text at percentage: {e}")
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        """Calculate character difference between two percentages."""
        try:
            book_path = self.resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None
            total_len = len(full_text)
            return abs(int(total_len * percentage_prev) - int(total_len * percentage_new))
        except Exception as e:
            logger.error(f"❌ Error calculating character delta: {e}")
            return None

    # =========================================================================
    # STORYTELLER / READIUM / GENERAL UTILS
    # Uses BeautifulSoup for broad compatibility
    # =========================================================================

    def resolve_locator_id(self, filename, href, fragment_id):
        """
        Returns a text snippet starting at the element identified by href + #fragment_id.
        Useful for syncing from Storyteller or any Readium-based reader that uses DOM IDs.
        """
        try:
            if not href:
                logger.debug(f"resolve_locator_id: missing href for '{filename}'")
                return None
            if not fragment_id:
                logger.debug(f"resolve_locator_id: missing fragment_id for href='{href}' in '{filename}'")
                return None

            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            target_item = None
            for item in spine_map:
                if href in item['href'] or item['href'] in href:
                    target_item = item
                    break

            if not target_item: return None

            soup = BeautifulSoup(target_item['content'], 'html.parser')
            clean_id = fragment_id.lstrip('#')
            element = soup.find(id=clean_id)

            if not element: return None

            current_offset = 0
            found_offset = -1
            all_strings = soup.find_all(string=True)

            for s in all_strings:
                if s.parent == element or element in s.parents:
                    found_offset = current_offset
                    break
                text_len = len(s.strip())
                if text_len == 0:
                    continue
                current_offset += text_len

            if found_offset == -1:
                # Fallback
                elem_text = element.get_text(separator=' ', strip=True)
                chapter_text = soup.get_text(separator=' ', strip=True)
                found_offset = chapter_text.find(elem_text)

            if found_offset == -1: return None

            global_offset = target_item['start'] + found_offset
            start = max(0, global_offset)
            end = min(len(full_text), global_offset + 500)
            return full_text[start:end]

        except Exception as e:
            logger.error(f"❌ Error resolving locator ID '{fragment_id}' in '{filename}': {e}")
            return None

    def _generate_css_selector(self, target_tag):
        """Generate a Readium-compatible CSS selector."""
        if not target_tag: return ""
        segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if not isinstance(curr, Tag):
                curr = curr.parent
                continue
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    index += 1
                sibling = sibling.previous_sibling
            segments.append(f"{curr.name}:nth-child({index})")
            curr = curr.parent
        return " > ".join(reversed(segments))

    def _generate_cfi(self, spine_index, html_content, local_target_index):
        """Generate an EPUB CFI for Grimmory/Readium."""
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1

        if not target_tag:
            spine_step = (spine_index + 1) * 2
            return f"epubcfi(/6/{spine_step}!/4/2/1:0)"

        path_segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("4")
                break
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    index += 1
                sibling = sibling.previous_sibling
            path_segments.append(str(index * 2))
            curr = curr.parent

        spine_step = (spine_index + 1) * 2
        element_path = "/".join(reversed(path_segments))
        if not element_path:
            # Keep CFI parseable even if target text was attached to the document root.
            element_path = "4/2/1"
        return f"epubcfi(/6/{spine_step}!/{element_path}:0)"

    def _generate_xpath_bs4(self, html_content, local_target_index):
        """
        Original BS4 XPath generator (kept for fuzzy matching references).
        Returns: (xpath_string, target_tag_object, is_anchored)
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1

        if not target_tag: return "/body/div/p[1]", None, False

        path_segments = []
        curr = target_tag
        found_anchor = False

        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("body")
                break
            if curr.has_attr('id') and curr['id']:
                path_segments.append(f"*[@id='{curr['id']}']")
                found_anchor = True
                break
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name == curr.name:
                    index += 1
                sibling = sibling.previous_sibling
            path_segments.append(f"{curr.name}[{index}]")
            curr = curr.parent

        if not path_segments:
            return "/body/p[1]", target_tag, False

        xpath = "//" + "/".join(reversed(path_segments)) if found_anchor else "/" + "/".join(reversed(path_segments))
        xpath = xpath.rstrip("/")
        if xpath in ("", "/", "//", "/body", "//body"):
            xpath = "/body/p[1]"
            found_anchor = False
        return xpath, target_tag, found_anchor

    _SEMANTIC_WINDOW_CHARS = 1500
    _SEMANTIC_MAX_WINDOWS = 40
    _SEMANTIC_EMBED_MAX_CHARS = 4000  # keep embedded text under the model's token limit

    def _semantic_text_fallback(self, search_phrase: str, full_text: str,
                                hint_percentage: Optional[float]) -> int:
        """Embedding-based position rescue when fuzzy matching fails.

        Slices the hint neighborhood (or the whole book) into windows, embeds them
        alongside the search phrase, and returns a char index inside the most
        similar window — or -1 when disabled, unavailable, or below threshold.
        """
        client = self.ollama_client
        if not client or not client.is_configured():
            return -1
        if os.getenv("OLLAMA_EBOOK_TEXT_FALLBACK", "false").lower() != "true":
            return -1
        clean_search = " ".join((search_phrase or "").split())
        if not clean_search or not full_text:
            return -1

        try:
            threshold = float(os.getenv("OLLAMA_ALIGN_SIM_THRESHOLD", 0.72))
        except (TypeError, ValueError):
            threshold = 0.72

        total_len = len(full_text)
        region_start = 0
        region = full_text
        if hint_percentage is not None:
            region_start = int(max(0.0, hint_percentage - 0.10) * total_len)
            region_end = int(min(1.0, hint_percentage + 0.10) * total_len)
            if full_text[region_start:region_end].strip():
                region = full_text[region_start:region_end]
            else:
                region_start = 0

        size = max(self._SEMANTIC_WINDOW_CHARS, -(-len(region) // self._SEMANTIC_MAX_WINDOWS))
        windows = []
        for start in range(0, len(region), size):
            text = region[start:start + size]
            if text.strip():
                windows.append((region_start + start, text))
        if not windows:
            return -1

        from src.services.llm_matching import best_semantic_window

        embed_texts = [t[:self._SEMANTIC_EMBED_MAX_CHARS] for _, t in windows]
        best = best_semantic_window(client, clean_search, embed_texts, threshold)
        if best is None:
            return -1
        win_start, win_text = windows[best[0]]
        # Refine inside the winning window; fall back to its start.
        alignment = rapidfuzz.fuzz.partial_ratio_alignment(clean_search, win_text, score_cutoff=50)
        offset = alignment.dest_start if alignment else 0
        logger.info(
            f"🧠 Semantic position rescue: window at char {win_start} (cosine {best[1]:.2f})"
        )
        return win_start + offset

    def find_text_location(self, filename, search_phrase, hint_percentage=None) -> Optional[LocatorResult]:
        """
        Uses BS4 Engine. Good for fuzzy matching phrases from external apps.
        Returns: LocatorResult or None
        """
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None
            total_len = len(full_text)

            # [NEW] 0. Global Uniqueness Check (The "Anchor" Logic)
            # Try to find a 10-word sequence that appears EXACTLY once in the book.
            # This prevents jumping to duplicate phrases (e.g., "Chapter 1" in the ToC vs the actual chapter).
            clean_search = " ".join(search_phrase.split())
            words = clean_search.split()
            
            match_index = -1
            
            if len(words) >= 10:
                N = 10
                # Scan through the search phrase to find a unique anchor
                for i in range(len(words) - N + 1):
                    candidate = " ".join(words[i:i+N])
                    
                    # Check if this phrase exists exactly ONCE in the text
                    if full_text.count(candidate) == 1:
                        found_idx = full_text.find(candidate)
                        if found_idx != -1:
                            match_index = found_idx
                            logger.info(f"⚓ Found unique text anchor: '{candidate[:30]}...' at index {match_index}")
                            break
            
            # [End of NEW logic] - Continue to existing fallbacks

            # 1. Exact match (if anchor logic didn't find anything)
            if match_index == -1:
                match_index = full_text.find(search_phrase)

            # 2. Normalized match
            if match_index == -1:
                norm_content, norm_to_raw = self._normalize_with_map(full_text)
                norm_search = self._normalize(search_phrase)
                if norm_content and norm_search:
                    norm_index = norm_content.find(norm_search)
                    if norm_index != -1:
                        if norm_index < len(norm_to_raw):
                            match_index = norm_to_raw[norm_index]
                        else:
                            match_index = norm_to_raw[-1]

            # 3. Fuzzy match
            if match_index == -1:
                cutoff = self.fuzzy_threshold
                if hint_percentage is not None:
                    w_start = int(max(0, hint_percentage - 0.10) * total_len)
                    w_end = int(min(1.0, hint_percentage + 0.10) * total_len)
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text[w_start:w_end], score_cutoff=cutoff
                    )
                    if alignment: match_index = w_start + alignment.dest_start

                if match_index == -1:
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text, score_cutoff=cutoff
                    )
                    if alignment: match_index = alignment.dest_start

            # 4. Semantic fallback (optional, Ollama-gated)
            if match_index == -1:
                match_index = self._semantic_text_fallback(search_phrase, full_text, hint_percentage)

            if match_index != -1:
                percentage = match_index / total_len
                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']

                        # Use BS4 generator here for Rich Locators
                        xpath_str, target_tag, is_anchored = self._generate_xpath_bs4(item['content'], local_index)
                        css_selector = self._generate_css_selector(target_tag)
                        cfi = self._generate_cfi(item['spine_index'] - 1, item['content'], local_index)

                        # FIX: Handle double slashes gracefully
                        doc_frag_prefix = f"/body/DocFragment[{item['spine_index']}]"
                        if xpath_str.startswith('//'):
                            final_xpath = doc_frag_prefix + xpath_str[1:] # //id -> /DocFragment/id (or keep // if valid)
                        elif xpath_str.startswith('/'):
                            final_xpath = doc_frag_prefix + xpath_str
                        else:
                            final_xpath = f"{doc_frag_prefix}/{xpath_str}"
                        # Calculate chapter progress (critical for Storyteller)
                        chapter_len = len(item['content']) # Rough approximation using HTML length
                        if hasattr(item, 'get_content'): # double check if item object available or just dict
                             pass 
                        
                        # better: use start/end from map
                        spine_item_len = item['end'] - item['start']
                        chapter_progress = 0.0
                        if spine_item_len > 0:
                            chapter_progress = local_index / spine_item_len

                        perfect_ko = self.get_perfect_ko_xpath(filename, match_index)

                        overlay_ids = self.get_media_overlay_fragment_ids(book_path)
                        fragment_id = self.get_fragment_for_tag(target_tag, valid_ids=overlay_ids)

                        return LocatorResult(
                            percentage=percentage,
                            xpath=final_xpath,
                            perfect_ko_xpath=perfect_ko,
                            match_index=match_index,
                            cfi=cfi,
                            href=item['href'],
                            fragment=fragment_id,
                            css_selector=css_selector,
                            chapter_progress=chapter_progress
                        )

            return None
        except Exception as e:
            logger.error(f"❌ Error finding text in '{filename}': {e}")
            return None

    def get_media_overlay_fragment_ids(self, book_path) -> set:
        """
        Collect every text fragment id referenced by the EPUB's media-overlay
        SMIL files. Storyteller's readalong can only seek audio for these ids;
        anchoring a locator to any other id makes playback fall back to the
        start of the chapter. Returns an empty set for books without overlays.
        """
        str_path = str(book_path)
        cached = self._media_overlay_ids_cache.get(str_path)
        if cached is not None:
            return cached

        ids: set = set()
        try:
            with zipfile.ZipFile(str_path) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith('.smil'):
                        continue
                    content = zf.read(name).decode('utf-8', 'replace')
                    ids.update(re.findall(r'<text[^>]+src="[^"#]*#([^"]+)"', content))
        except Exception as e:
            logger.debug(f"Media overlay scan failed for '{str_path}': {e}")

        self._media_overlay_ids_cache.put(str_path, ids)
        return ids

    def get_fragment_for_tag(self, tag, valid_ids: Optional[set] = None):
        """
        Walks backwards from the given tag to find the nearest element with an id.
        Returns the id of the element if found, otherwise None.
        This id is used by the Storyteller to sync progress.

        When valid_ids is provided (media-overlay fragment ids), the nearest
        ancestor id in that set wins so Storyteller readalong can map the
        fragment to an audio clip; the innermost id is kept as a fallback.
        """
        fragment_id = None
        curr_tag = tag
        while curr_tag and curr_tag.name not in ['[document]', 'html', 'body']:
            if curr_tag.has_attr('id') and curr_tag['id']:
                candidate = curr_tag['id']
                if not valid_ids or candidate in valid_ids:
                    return candidate
                if fragment_id is None:
                    fragment_id = candidate
            curr_tag = curr_tag.parent
        return fragment_id

    def get_locator_from_char_offset(self, filename, char_offset: int) -> Optional[LocatorResult]:
        """
        Resolve a rich locator directly from a global character offset.
        This bypasses fuzzy text search entirely.
        """
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)
            if not full_text or not spine_map:
                return None

            total_len = len(full_text)
            if total_len <= 0:
                return None

            target_index = max(0, min(int(char_offset), total_len - 1))
            percentage = target_index / total_len

            target_item = next((item for item in spine_map if item['start'] <= target_index < item['end']), None)
            if not target_item:
                target_item = spine_map[-1]

            local_index = max(0, target_index - target_item['start'])
            perfect_ko = self.get_perfect_ko_xpath(filename, target_index)
            cfi = self._generate_cfi(target_item['spine_index'] - 1, target_item['content'], local_index)
            spine_item_len = max(1, target_item['end'] - target_item['start'])
            chapter_progress = local_index / spine_item_len

            return LocatorResult(
                percentage=percentage,
                xpath=perfect_ko,
                perfect_ko_xpath=perfect_ko,
                match_index=target_index,
                cfi=cfi,
                href=target_item.get('href'),
                fragment=None,
                css_selector=None,
                chapter_progress=chapter_progress,
            )
        except Exception as e:
            logger.error(f"âŒ Error resolving locator from char offset in '{filename}': {e}")
            return None

    def _normalize_with_map(self, text):
        """
        Normalize text and return a map from normalized char index -> raw char index.
        This prevents using normalized offsets as if they were raw offsets.
        """
        normalized_chars = []
        norm_to_raw = []
        for raw_idx, ch in enumerate(text):
            if ch.isalnum():
                normalized_chars.append(ch.lower())
                norm_to_raw.append(raw_idx)
        return "".join(normalized_chars), norm_to_raw

    def _normalize(self, text):
        normalized, _ = self._normalize_with_map(text)
        return normalized



    def _local_tag_name(self, node) -> str:
        tag = getattr(node, "tag", None)
        if not isinstance(tag, str):
            tag = getattr(node, "name", None)
        if not isinstance(tag, str):
            return ""
        if "}" in tag:
            tag = tag.split("}", 1)[1]
        return tag.lower()

    def _get_parent_node(self, node):
        if node is None:
            return None
        getparent = getattr(node, "getparent", None)
        if callable(getparent):
            return getparent()
        return getattr(node, "parent", None)

    def _nearest_crengine_anchor(self, node):
        current = node
        while current is not None:
            tag_name = self._local_tag_name(current)
            if tag_name == "body":
                return current
            if tag_name in self.CRENGINE_STRUCTURAL_TAGS:
                return current
            if tag_name in ("html", "document", "[document]"):
                break
            current = self._get_parent_node(current)
        return node

    def _p_has_fragmenting_inline_children(self, p_element) -> bool:
        if p_element is None:
            return False
        iterchildren = getattr(p_element, "iterchildren", None)
        if callable(iterchildren):
            children = iterchildren()
        else:
            children = getattr(p_element, "children", [])
        for child in children:
            if self._local_tag_name(child) in self.KOREADER_FRAGMENTING_P_CHILD_TAGS:
                return True
        return False

    def _iter_siblings(self, element, forward: bool):
        if element is None:
            return
        itersiblings = getattr(element, "itersiblings", None)
        if callable(itersiblings):
            for sib in itersiblings(preceding=not forward):
                yield sib
            return
        attr = "next_sibling" if forward else "previous_sibling"
        sib = getattr(element, attr, None)
        while sib is not None:
            yield sib
            sib = getattr(sib, attr, None)

    def _element_text_length(self, element) -> int:
        if element is None:
            return 0
        text_content = getattr(element, "text_content", None)
        if callable(text_content):
            try:
                return len((text_content() or "").strip())
            except Exception:
                return 0
        get_text = getattr(element, "get_text", None)
        if callable(get_text):
            try:
                return len(get_text(strip=True) or "")
            except Exception:
                return 0
        return 0

    def _is_clean_p_substitute(self, element) -> bool:
        if self._local_tag_name(element) != "p":
            return False
        if self._p_has_fragmenting_inline_children(element):
            return False
        return self._element_text_length(element) > 10

    def _find_clean_p_substitute(self, p_element):
        for sib in self._iter_siblings(p_element, forward=False):
            if self._is_clean_p_substitute(sib):
                return sib
        for sib in self._iter_siblings(p_element, forward=True):
            if self._is_clean_p_substitute(sib):
                return sib
        return None

    def _first_non_empty_direct_text_suffix(self, element) -> Optional[str]:
        if element is None:
            return None
        try:
            direct_text_nodes = element.xpath("text()")
            for i, node in enumerate(direct_text_nodes, start=1):
                if str(node).strip():
                    return "/text()" if i == 1 else f"/text()[{i}]"
        except Exception:
            pass

        if isinstance(element, Tag):
            text_nodes = [child for child in element.children if isinstance(child, str)]
            for i, node in enumerate(text_nodes, start=1):
                if str(node).strip():
                    return "/text()" if i == 1 else f"/text()[{i}]"
        return None

    def _build_inline_step(self, parent_el, inline_child) -> Optional[str]:
        """
        Return the XPath step for inline_child relative to parent_el.
        Returns '/span' or '/span[2]' — used when text lives inside an inline
        child element rather than directly under the structural block ancestor.
        Returns None if inline_child is not a direct child of parent_el.
        """
        try:
            tag = self._local_tag_name(inline_child)
            if not tag:
                return None
            direct_children = list(parent_el)
            if inline_child not in direct_children:
                return None
            siblings = [s for s in direct_children if self._local_tag_name(s) == tag]
            if len(siblings) == 1:
                return f"/{tag}"
            return f"/{tag}[{siblings.index(inline_child) + 1}]"
        except Exception:
            return None

    def _build_crengine_safe_text_xpath(self, element, spine_index, html_content) -> str:
        el_tag = self._local_tag_name(element)
        anchor = self._nearest_crengine_anchor(element)

        if self._local_tag_name(anchor) == "p" and self._p_has_fragmenting_inline_children(anchor):
            substitute = self._find_clean_p_substitute(anchor)
            if substitute is not None:
                logger.debug(
                    f"KOReader XPath: <p> in DocFragment[{spine_index}] has fragmenting "
                    f"inline children; substituting nearest clean <p> sibling"
                )
                anchor = substitute

        suffix = self._first_non_empty_direct_text_suffix(anchor)

        if not suffix:
            suffix = "/text()"

        xpath_base = self._build_xpath(anchor)
        return f"/body/DocFragment[{spine_index}]/{xpath_base}{suffix}.0"

    def _build_sentence_level_chapter_fallback_xpath(self, html_content, spine_index) -> str:
        """
        Build a safe sentence-level XPath anchored to the first readable text node
        in the chapter. This intentionally targets node starts (.0) instead of
        character-level offsets.
        """
        default_xpath = f"/body/DocFragment[{spine_index}]/body/p[1]/text().0"
        try:
            tree = html.fromstring(html_content)
        except Exception:
            return default_xpath

        sentence_tags = (
            "p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
            "blockquote", "figcaption", "dd", "dt", "td", "th",
            "div", "section", "article", "pre"
        )

        for tag in sentence_tags:
            for element in tree.iter(tag):
                suffix = self._first_non_empty_direct_text_suffix(element)
                if suffix:
                    xpath_base = self._build_xpath(element)
                    return f"/body/DocFragment[{spine_index}]/{xpath_base}{suffix}.0"

        for element in tree.iter():
            if self._local_tag_name(element) not in self.CRENGINE_STRUCTURAL_TAGS:
                continue
            suffix = self._first_non_empty_direct_text_suffix(element)
            if suffix:
                xpath_base = self._build_xpath(element)
                return f"/body/DocFragment[{spine_index}]/{xpath_base}{suffix}.0"

        return default_xpath

    def get_sentence_level_ko_xpath(self, filename, percentage) -> Optional[str]:
        """
        Resolve a sentence-level KOReader XPath from percentage.
        Returns node-start offset (.0), not word-level offsets.
        """
        try:
            book_path = self.resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None

            pct = float(percentage if percentage is not None else 0.0)
            pct = max(0.0, min(1.0, pct))
            position = int((len(full_text) - 1) * pct) if len(full_text) > 1 else 0
            xpath = self.get_perfect_ko_xpath(filename, position)

            # Truncate character offsets to node start for sentence-level syncing.
            if xpath:
                xpath = re.sub(r'(text\(\)(?:\[\d+\])?)\.\d+$', r'\1.0', xpath)

            return xpath
        except Exception as e:
            logger.error(f"Error generating sentence-level KOReader XPath: {e}")
            return None

    def get_perfect_ko_xpath(self, filename, position=0) -> Optional[str]:
        """
        Generate KOReader XPath for a specific character position in the book.
        Uses BeautifulSoup (Engine A) to perfectly align with the text extraction,
        eliminating parser drift compared to the old LXML offset logic.
        """
        try:
            # Get full text and spine mapping
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text or not spine_map:
                return None

            # Clamp position to valid range
            position = max(0, min(position, len(full_text) - 1))

            # Find which spine item contains this position
            target_item = next((item for item in spine_map
                              if item['start'] <= position < item['end']), spine_map[-1])

            local_pos = position - target_item['start']

            # Parse HTML content with BeautifulSoup
            soup = BeautifulSoup(target_item['content'], 'html.parser')
            
            # Find the exact text element matching the character count
            current_char_count = 0
            target_string = None
            target_offset = 0
            first_non_empty_string = None
            last_non_empty_string = None

            elements = soup.find_all(string=True)
            for string in elements:
                # Count lengths exactly like extract_text_and_map's get_text(strip=True)
                clean_text = string.strip()
                text_len = len(clean_text)
                
                if text_len == 0: 
                    continue

                if first_non_empty_string is None:
                    first_non_empty_string = string
                last_non_empty_string = string
                
                if current_char_count + text_len > local_pos:
                    target_string = string
                    # Calculate offset within the CLEAN string
                    clean_offset = local_pos - current_char_count
                    
                    # KOReader needs the offset within the RAW string (including leading whitespace etc)
                    # Find where the clean text starts inside the raw string to determine true offset
                    raw_text = str(string)
                    raw_start = raw_text.find(clean_text)
                    if raw_start == -1: 
                        raw_start = 0
                    
                    target_offset = raw_start + clean_offset
                    break
                
                current_char_count += text_len
                # extract_text_and_map uses separator=' ', adding exactly 1 space between words
                if current_char_count <= local_pos:
                    current_char_count += 1

            if target_string is None:
                target_string = last_non_empty_string or first_non_empty_string
                target_offset = 0

            if not target_string:
                logger.warning(f"⚠️ No matching text element found in spine {target_item['spine_index']}")
                return self._build_sentence_level_chapter_fallback_xpath(
                    target_item['content'],
                    target_item['spine_index']
                )

            target_tag = target_string.parent
            if not target_tag or target_tag.name == '[document]':
                return self._build_sentence_level_chapter_fallback_xpath(
                    target_item['content'],
                    target_item['spine_index']
                )

            # =================================================================
            # HYBRID ANCHOR MAPPING: BS4 -> LXML
            # 1. We have the exact mathematical text offset via BS4.
            # 2. We use the raw text as a unique "anchor" to find the exact
            #    same node in LXML's strictly structured tree.
            # 3. This guarantees perfect KOReader XPaths with zero parser drift.
            # =================================================================
            search_text = str(target_string)
            occurrence_index = 0
            
            # Count which occurrence of this exact text this is in the BS4 document
            for string in elements:
                if string is target_string:
                    break
                if str(string) == search_text:
                    occurrence_index += 1
                    
            tree = html.fromstring(target_item['content'])
            current_occurrence = 0
            
            for el in tree.iter():
                if el.text and el.text == search_text:
                    if current_occurrence == occurrence_index:
                        return self._build_crengine_safe_text_xpath(
                            el,
                            target_item['spine_index'],
                            target_item['content']
                        )
                    current_occurrence += 1
                    
                if el.tail and el.tail == search_text:
                    if current_occurrence == occurrence_index:
                        parent = el.getparent()
                        node_to_build = parent if parent is not None else el
                        return self._build_crengine_safe_text_xpath(
                            node_to_build,
                            target_item['spine_index'],
                            target_item['content']
                        )
                    current_occurrence += 1

            logger.warning(f"⚠️ Hybrid Anchor mapping failed for '{search_text}'. Falling back to BS4 structural path.")

            ancestor_p = target_tag
            while ancestor_p is not None and getattr(ancestor_p, "name", None) not in ("p", "body", "[document]", None):
                ancestor_p = getattr(ancestor_p, "parent", None)
            if (
                ancestor_p is not None
                and getattr(ancestor_p, "name", None) == "p"
                and self._p_has_fragmenting_inline_children(ancestor_p)
            ):
                substitute = self._find_clean_p_substitute(ancestor_p)
                if substitute is not None:
                    logger.debug(
                        f"KOReader XPath (BS4 fallback): <p> in DocFragment[{target_item['spine_index']}] "
                        f"has fragmenting inline children; substituting nearest clean <p> sibling"
                    )
                    target_tag = substitute

            # Build KOReader-compatible strictly positional XPath using BS4 (Fallback)
            path_segments = []
            curr = target_tag

            while curr and curr.name != '[document]':
                if curr.name == 'body':
                    path_segments.append("body")
                    break
                
                if curr.name in self.CRENGINE_FRAGILE_INLINE_TAGS:
                    curr = curr.parent
                    continue
                
                index = 1
                sibling = curr.previous_sibling
                while sibling:
                    if isinstance(sibling, Tag) and sibling.name == curr.name:
                        index += 1
                    sibling = sibling.previous_sibling
                
                path_segments.append(f"{curr.name}[{index}]")
                curr = curr.parent

            # Ensure the path starts with body
            if not path_segments or path_segments[-1] != 'body':
                path_segments.append('body')

            xpath = "/".join(reversed(path_segments))
            if xpath == "body":
                return self._build_sentence_level_chapter_fallback_xpath(
                    target_item['content'],
                    target_item['spine_index']
                )
            return f"/body/DocFragment[{target_item['spine_index']}]/{xpath}/text().0"

        except Exception as e:
            logger.error(f"❌ Error generating KOReader XPath: {e}")
            return None

    def _has_text_content(self, element):
        """Check if element directly contains text (not just in children)."""
        return element.text and element.text.strip() and len(element.text.strip()) > 0

    def _build_xpath(self, element):
        """Build XPath for an element, ensuring proper KOReader format."""
        parts = []
        current = element

        while current is not None and current.tag not in ['html', 'document']:
            tag_name = self._local_tag_name(current)

            if tag_name in self.CRENGINE_FRAGILE_INLINE_TAGS:
                current = current.getparent()
                continue
            
            # Get siblings of same tag to determine index
            parent = current.getparent()
            if parent is not None:
                siblings = [s for s in parent if self._local_tag_name(s) == tag_name]
                if len(siblings) > 1:
                    index = siblings.index(current) + 1
                    parts.insert(0, f"{tag_name}[{index}]")
                else:
                    parts.insert(0, tag_name)
            else:
                parts.insert(0, tag_name)
            current = parent

        # Clean up the path
        if parts and parts[0] == 'html':
            parts.pop(0)
        if not parts or parts[0] != 'body':
            parts.insert(0, 'body')

        # If we have no meaningful path, create a default
        if len(parts) <= 1:  # Just 'body' or empty
            parts = ['body', 'p[1]']

        return '/'.join(parts)

    def _iter_docfragment_candidates(self, spine_map, reported_spine_index):
        """
        Yield spine items ordered by proximity to the reported DocFragment index.
        KOReader can report fragment numbers that drift away from the physical
        EPUB spine because of internal chunking, so nearby items are valid
        fallbacks when the reported segment does not resolve cleanly.
        """
        by_index = {item.get("spine_index"): item for item in spine_map}
        seen = set()

        def _get_candidate(index):
            item = by_index.get(index)
            if item is None or index in seen:
                return None
            seen.add(index)
            return item

        exact_item = _get_candidate(reported_spine_index)
        if exact_item is not None:
            yield exact_item

        if not by_index:
            return

        max_distance = max(abs(index - reported_spine_index) for index in by_index.keys())
        for distance in range(1, max_distance + 1):
            previous_item = _get_candidate(reported_spine_index - distance)
            if previous_item is not None:
                yield previous_item

            next_item = _get_candidate(reported_spine_index + distance)
            if next_item is not None:
                yield next_item

    @staticmethod
    def _split_xpath_char_offset(relative_path: str):
        """Split a KOReader relative xpath into (clean_xpath, char_offset).

        KOReader appends the trailing character offset as ".NNN". It may sit on a
        text node ("/text().5", "/text()[2].5") OR directly on an element when the
        position is at an element boundary ("p[167].0"). All forms must be stripped
        before the path is handed to an XPath engine, otherwise lxml rejects the
        leftover ".0" as an "Invalid expression".
        """
        offset_match = re.search(r'(?:/text\(\)(?:\[\d+\])?)?\.(\d+)$', relative_path)
        offset = int(offset_match.group(1)) if offset_match else 0
        clean_xpath = re.sub(r'(?:/text\(\)(?:\[\d+\])?)?\.\d+$', '', relative_path)
        return clean_xpath, offset

    def _resolve_xpath_target_node(self, filename, spine_map, reported_spine_index, clean_xpath):
        """
        Resolve the XPath against the reported DocFragment first, then against
        nearby spine items when KOReader fragment numbering drifts.
        """
        candidate_items = list(self._iter_docfragment_candidates(spine_map, reported_spine_index))

        def _log_fallback(candidate_item):
            if candidate_item['spine_index'] != reported_spine_index:
                logger.info(
                    "KOReader DocFragment fallback mapped reported DocFragment[%s] to spine %s for '%s'",
                    reported_spine_index,
                    candidate_item['spine_index'],
                    Path(filename).name,
                )

        def _strict_resolve(tree):
            try:
                elements = tree.xpath(clean_xpath)
            except Exception as e:
                logger.debug(f"XPath query failed: {e}")
                elements = []

            if not elements and clean_xpath.startswith('./'):
                try:
                    elements = tree.xpath(clean_xpath[2:])
                except Exception:
                    pass

            if not elements:
                id_match = re.search(r"@id='([^']+)'", clean_xpath)
                if id_match:
                    try:
                        elements = tree.xpath(f"//*[@id='{id_match.group(1)}']")
                    except Exception:
                        pass

            return elements

        for candidate_item in candidate_items:
            tree = html.fromstring(candidate_item['content'])
            elements = _strict_resolve(tree)
            if elements:
                _log_fallback(candidate_item)
                return candidate_item, tree, elements[0]

        simple_path = re.sub(r'\[\d+]', '', clean_xpath)
        if simple_path != clean_xpath:
            for candidate_item in candidate_items:
                tree = html.fromstring(candidate_item['content'])
                try:
                    elements = tree.xpath(simple_path)
                except Exception:
                    elements = []
                if elements:
                    _log_fallback(candidate_item)
                    return candidate_item, tree, elements[0]

        logger.warning(f"Could not resolve XPath in {filename}: {clean_xpath}")
        return None, None, None

    def resolve_xpath(self, filename, xpath_str):
        """
        RESOLVER:
        Uses LXML to find the target element, then searches for its text in the
        BS4-generated full_text to ensure alignment (Fixes Parser Drift).
        """
        try:
            logger.debug(f"🔍 Resolving XPath (Hybrid): {xpath_str}")

            match = re.search(r'DocFragment\[(\d+)]', xpath_str)
            if not match:
                return None
            spine_index = int(match.group(1))

            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            # Parse path and offset. KOReader emits two forms for the trailing
            # character offset: "/text().NNN" (single text node) and
            # "/text()[N].MMM" (Nth text node when inline children split a
            # paragraph's text into multiple nodes). Both must be recognised.
            relative_path = xpath_str.split(f"DocFragment[{spine_index}]")[-1]
            clean_xpath, target_offset = self._split_xpath_char_offset(relative_path)

            if clean_xpath.startswith('/'):
                clean_xpath = '.' + clean_xpath

            target_item, tree, target_node = self._resolve_xpath_target_node(
                filename,
                spine_map,
                spine_index,
                clean_xpath,
            )
            if target_item is None or target_node is None:
                return None
            elements = [target_node]

            



            if not elements:
                logger.warning(f"⚠️ Could not resolve XPath in {filename}: {clean_xpath}")
                return None

            target_node = elements[0]

            # [NEW LOGIC STARTS HERE]
            # Instead of calculating offset via LXML iteration (which drifts),
            # grab the text and FIND it in the spine item content.
            
            # 1. Extract anchor text directly from target node content only.
            node_text = target_node.text_content().strip()
            clean_anchor = " ".join(node_text.split())
            if not clean_anchor:
                return None

            # 2. Find this anchor in the BS4 content (spine_map item)
            # We search specifically in this chapter's content to minimize false positives
            bs4_chapter_text = BeautifulSoup(target_item['content'], 'html.parser').get_text(separator=' ', strip=True)
            
            local_start_index = bs4_chapter_text.find(clean_anchor)
            
            if local_start_index != -1:
                # Found it! Calculate global position
                # Add target_offset (clamped to length of anchor)
                safe_offset = min(target_offset, len(clean_anchor))
                global_index = target_item['start'] + local_start_index + safe_offset
                
                # 3. Return text from the Main Source of Truth (full_text)
                start = max(0, global_index)
                end = min(len(full_text), global_index + 600) # Grab enough context
                return full_text[start:end]
            
            else:
                # Fallback: If exact match fails (rare), try the old calculation method
                # (This preserves old behavior if the new matching fails)
                logger.debug("Exact text match failed, falling back to LXML offset calculation")
                # Falling back to strict calculation (Logic from original implementation)
                
                preceding_len = 0
                found_target = False
                SEPARATOR_LEN = 1

                for node in tree.iter():
                    if node == target_node:
                        found_target = True
                        if node.text and target_offset > 0:
                            raw_segment = node.text[:min(len(node.text), target_offset)]
                            preceding_len += len(raw_segment.strip())
                        elif target_offset > 0:
                            preceding_len += target_offset
                        break

                    if node.text and node.text.strip():
                        preceding_len += (len(node.text.strip()) + SEPARATOR_LEN)
                    if node.tail and node.tail.strip():
                        preceding_len += (len(node.tail.strip()) + SEPARATOR_LEN)
                
                if found_target:
                     local_pos = preceding_len
                     global_offset = target_item['start'] + local_pos
                     start = max(0, global_offset)
                     end = min(len(full_text), global_offset + 500)
                     return full_text[start:end]

                return None

        except Exception as e:
            logger.error(f"❌ Error resolving XPath '{xpath_str}': {e}")
            return None

    def resolve_xpath_to_index(self, filename, xpath_str) -> Optional[int]:
        """
        Resolve KOReader XPath to canonical global character offset.
        Reuses the same hybrid XPath logic as resolve_xpath().
        """
        try:
            logger.debug(f"Resolving XPath->index (Hybrid): {xpath_str}")

            def _find_unique_index(haystack: str, needle: str):
                if not haystack or not needle:
                    return None, 0
                first = haystack.find(needle)
                if first == -1:
                    return None, 0
                second = haystack.find(needle, first + 1)
                if second != -1:
                    return None, 2
                return first, 1

            match = re.search(r'DocFragment\[(\d+)]', xpath_str)
            if not match:
                return None
            spine_index = int(match.group(1))

            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            relative_path = xpath_str.split(f"DocFragment[{spine_index}]")[-1]
            clean_xpath, target_offset = self._split_xpath_char_offset(relative_path)

            if clean_xpath.startswith('/'):
                clean_xpath = '.' + clean_xpath

            target_item, tree, target_node = self._resolve_xpath_target_node(
                filename,
                spine_map,
                spine_index,
                clean_xpath,
            )
            if target_item is None or target_node is None:
                return None
            bs4_chapter_text = BeautifulSoup(target_item['content'], 'html.parser').get_text(separator=' ', strip=True)

            elements = []
            try:
                elements = tree.xpath(clean_xpath)
            except Exception as e:
                logger.debug(f"XPath query failed: {e}")

            if not elements and clean_xpath.startswith('./'):
                try:
                    elements = tree.xpath(clean_xpath[2:])
                except Exception:
                    pass

            if not elements:
                id_match = re.search(r"@id='([^']+)'", clean_xpath)
                if id_match:
                    try:
                        elements = tree.xpath(f"//*[@id='{id_match.group(1)}']")
                    except Exception:
                        pass

            if not elements:
                simple_path = re.sub(r'\[\d+]', '', clean_xpath)
                try:
                    elements = tree.xpath(simple_path)
                except Exception:
                    pass

            if not elements:
                logger.warning(f"Could not resolve XPath in {filename}: {clean_xpath}")
                return None

            target_node = elements[0]

            node_text = target_node.text_content().strip()
            clean_anchor = " ".join(node_text.split())
            if not clean_anchor:
                return None
            chapter_len = max(0, target_item['end'] - target_item['start'])
            chapter_base = target_item['start']
            full_len = len(full_text)

            # Tier: exact unique match in BS4 coordinate space.
            if clean_anchor:
                local_start_index, match_count = _find_unique_index(bs4_chapter_text, clean_anchor)
                if local_start_index is not None:
                    safe_offset = min(max(target_offset, 0), len(clean_anchor))
                    local_offset = local_start_index + safe_offset
                    if chapter_len > 0:
                        local_offset = min(local_offset, chapter_len)
                    global_offset = min(full_len, chapter_base + local_offset)
                    logger.debug(
                        "XPath->index BS4 fallback tier=exact_unique local_start=%s safe_offset=%s local_offset=%s global_offset=%s",
                        local_start_index,
                        safe_offset,
                        local_offset,
                        global_offset,
                    )
                    return global_offset
                if match_count > 1:
                    logger.debug("XPath->index exact anchor is ambiguous in BS4 chapter text")
                else:
                    logger.debug("XPath->index exact anchor not found in BS4 chapter text")
            else:
                logger.debug("XPath->index clean anchor empty after extraction")

            # Tier: deterministic unique prefix match in BS4 coordinate space.
            if clean_anchor:
                prefix_lengths = [100, 80, 60, 50, 40, 30]
                for prefix_len in prefix_lengths:
                    if len(clean_anchor) < prefix_len:
                        continue
                    prefix = clean_anchor[:prefix_len].strip()
                    if not prefix:
                        continue
                    local_start_index, match_count = _find_unique_index(bs4_chapter_text, prefix)
                    if local_start_index is None:
                        if match_count > 1:
                            logger.debug(
                                "XPath->index prefix len=%s ambiguous in BS4 chapter text",
                                prefix_len,
                            )
                        continue

                    local_offset = local_start_index
                    if chapter_len > 0:
                        local_offset = min(local_offset, chapter_len)
                    global_offset = min(full_len, chapter_base + local_offset)
                    logger.debug(
                        "XPath->index BS4 fallback tier=substring_prefix_unique prefix_len=%s local_offset=%s global_offset=%s",
                        prefix_len,
                        local_offset,
                        global_offset,
                    )
                    return global_offset

            # Tier: deterministic unique normalized match with normalized->raw map.
            if clean_anchor:
                normalized_chapter_text, norm_to_raw = self._normalize_with_map(bs4_chapter_text)
                normalized_anchor = self._normalize(clean_anchor)
                normalized_candidates = [normalized_anchor]
                for prefix_len in [100, 80, 60, 50, 40, 30]:
                    if len(normalized_anchor) >= prefix_len:
                        normalized_candidates.append(normalized_anchor[:prefix_len])

                seen_candidates = set()
                for candidate in normalized_candidates:
                    if not candidate or candidate in seen_candidates:
                        continue
                    seen_candidates.add(candidate)

                    norm_index, match_count = _find_unique_index(normalized_chapter_text, candidate)
                    if norm_index is None:
                        if match_count > 1:
                            logger.debug(
                                "XPath->index normalized candidate len=%s ambiguous in normalized chapter text",
                                len(candidate),
                            )
                        continue
                    if norm_index >= len(norm_to_raw):
                        continue

                    local_offset = norm_to_raw[norm_index]
                    if chapter_len > 0:
                        local_offset = min(local_offset, chapter_len)
                    global_offset = min(full_len, chapter_base + local_offset)
                    logger.debug(
                        "XPath->index BS4 fallback tier=normalized_unique candidate_len=%s norm_index=%s local_offset=%s global_offset=%s",
                        len(candidate),
                        norm_index,
                        local_offset,
                        global_offset,
                    )
                    return global_offset

            # Tier: LXML-based position fallback (last resort when text-matching fails).
            # Mirrors the fallback in resolve_xpath(). Less precise than text-anchoring
            # but still produces an "xpath"-sourced offset, which is high-confidence.
            preceding_len = 0
            found_target = False
            SEPARATOR_LEN = 1
            for node in tree.iter():
                if node == target_node:
                    found_target = True
                    if node.text and target_offset > 0:
                        raw_segment = node.text[: min(len(node.text), target_offset)]
                        preceding_len += len(raw_segment.strip())
                    elif target_offset > 0:
                        preceding_len += target_offset
                    break
                if node.text and node.text.strip():
                    preceding_len += len(node.text.strip()) + SEPARATOR_LEN
                if node.tail and node.tail.strip():
                    preceding_len += len(node.tail.strip()) + SEPARATOR_LEN

            if found_target:
                local_offset = preceding_len
                if chapter_len > 0:
                    local_offset = min(local_offset, chapter_len)
                global_offset = min(full_len, chapter_base + local_offset)
                logger.debug(
                    "XPath->index tier=lxml_position_fallback local_offset=%s global_offset=%s",
                    local_offset,
                    global_offset,
                )
                return global_offset

            logger.debug("XPath->index BS4 fallback failed deterministically; returning None")
            return None

        except Exception as e:
            logger.error(f"Error resolving XPath->index '{xpath_str}': {e}")
            return None

    def _parse_cfi_components(self, cfi):
        """
        Parse CFI into (spine_step, element_steps, char_offset).
        Falls back for minimal CFIs like `epubcfi(/6/26!/:0)` that the library rejects.
        """
        try:
            parsed_cfi = epubcfi.parse(cfi)
            # epubcfi.parse() may return:
            # - Path (point CFI): has .steps/.offset
            # - PathRange (range CFI): has .parent/.start/.end
            # We normalize both to a "combined" start-path step list.
            if hasattr(parsed_cfi, "steps"):
                combined_steps = list(parsed_cfi.steps)
                parsed_offset = parsed_cfi.offset.value if getattr(parsed_cfi, "offset", None) else 0
            elif hasattr(parsed_cfi, "parent") and hasattr(parsed_cfi, "start"):
                parent_steps = list(getattr(parsed_cfi.parent, "steps", []) or [])
                start_steps = list(getattr(parsed_cfi.start, "steps", []) or [])
                combined_steps = parent_steps + start_steps
                start_offset = getattr(parsed_cfi.start, "offset", None)
                parsed_offset = start_offset.value if start_offset else 0
            else:
                raise ValueError(f"Unsupported parsed CFI type: {type(parsed_cfi).__name__}")

            spine_step = None
            element_steps = []

            redirect_idx = next(
                (i for i, step in enumerate(combined_steps) if step.__class__.__name__ == "Redirect"),
                None
            )

            if redirect_idx is not None:
                package_steps = [step for step in combined_steps[:redirect_idx] if hasattr(step, "index")]
                # The package marker is the leading `/6` (spine container); the spine item
                # is the last package step before the redirect. Identify it positionally so
                # CFIs whose spine step is literally 6 (e.g. `/6/6!/...`) parse correctly.
                if package_steps:
                    spine_step = int(package_steps[-1].index)
                element_steps = [step for step in combined_steps[redirect_idx + 1:] if hasattr(step, "index")]
            else:
                indexed_steps = [step for step in combined_steps if hasattr(step, "index")]
                # No redirect: skip a single leading `/6` package marker (by position, not
                # value-loop) so a spine item that is itself 6 is not discarded.
                if indexed_steps and indexed_steps[0].index == 6 and len(indexed_steps) >= 2:
                    spine_step = int(indexed_steps[1].index)
                    element_steps = list(indexed_steps[2:])
                elif indexed_steps:
                    spine_step = int(indexed_steps[0].index)
                    element_steps = list(indexed_steps[1:])

            char_offset = int(parsed_offset or 0)
            return spine_step, element_steps, char_offset
        except Exception as parse_err:
            fallback = re.match(
                r'^\s*epubcfi\(\s*/6/(\d+)(?:\[[^\]]*\])?!(?:/[^:)]*)?(?::(\d+))?\s*\)\s*$',
                str(cfi or "")
            )
            if not fallback:
                raise parse_err
            spine_step = int(fallback.group(1))
            char_offset = int(fallback.group(2) or 0)
            logger.debug(f"CFI fallback parser engaged for '{cfi}' (spine_step={spine_step}, offset={char_offset})")
            return spine_step, [], char_offset

    def get_text_around_cfi(self, filename, cfi, context=50):
        """
        Returns a text fragment of length 2*context centered on the position indicated by the CFI.
        Uses the epubcfi library for precise parsing.

        Example supported CFI: epubcfi(/6/16[chapter_6]!/4/2[book-columns]/2[book-inner]/268/4/2[kobo.134.3]/1:11)
        """
        try:
            spine_step, element_steps, char_offset = self._parse_cfi_components(cfi)

            if not spine_step:
                logger.error(f"❌ Could not extract spine step from CFI: '{cfi}'")
                return None

            # Load the EPUB and find the spine item
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            # Calculate spine index (CFI spine steps are 2x the actual index)
            cfi_spine_index = spine_step // 2
            spine_index = cfi_spine_index
            item = next((sp for sp in spine_map if sp.get('spine_index') == cfi_spine_index), None)
            if not item:
                logger.error(f"❌ Spine index {spine_index} out of range for CFI '{cfi}'")
                return None


            # Parse the HTML content with lxml for precise navigation
            tree = html.fromstring(item['content'])

            # Follow the CFI path precisely through the DOM
            current_element = tree
            text_count = 0

            logger.debug(f"Following CFI path with {len(element_steps)} steps")

            for i, step in enumerate(element_steps):
                if not hasattr(step, 'index'):
                    continue

                step_index = step.index
                step_assertion = getattr(step, 'assertion', None)

                logger.debug(f"Step {i}: index={step_index}, assertion={step_assertion}")

                if step_assertion:
                    # Look for element with specific ID or class
                    candidates = current_element.xpath(f".//*[contains(@id, '{step_assertion}') or contains(@class, '{step_assertion}')]")
                    if candidates:
                        current_element = candidates[0]
                        logger.debug(f"Found element with assertion: {step_assertion}")
                        continue

                # CFI uses 1-based indexing, even numbers for elements
                if step_index % 2 == 0:  # Even number = element
                    element_index = (step_index // 2) - 1
                    children = [child for child in current_element if hasattr(child, 'tag')]

                    if 0 <= element_index < len(children):
                        current_element = children[element_index]
                        logger.debug(f"Navigated to child element {element_index}: {current_element.tag}")
                    else:
                        logger.warning(f"⚠️ Element index {element_index} out of range (have {len(children)} children)")
                        break
                else:  # Odd number = text node
                    text_index = (step_index // 2)
                    # For text nodes, we need to count text content
                    text_nodes = []
                    for child in current_element:
                        if child.text and child.text.strip():
                            text_nodes.append(child.text.strip())
                        if child.tail and child.tail.strip():
                            text_nodes.append(child.tail.strip())

                    if 0 <= text_index < len(text_nodes):
                        # Calculate position up to this text node
                        text_count += sum(len(text) for text in text_nodes[:text_index])
                        logger.debug(f"Text node {text_index}, accumulated count: {text_count}")
                    break

            # Calculate text position within the current element
            if current_element is not None:
                # Get all text content up to the current element's position in the document
                soup = BeautifulSoup(item['content'], 'html.parser')
                chapter_text = soup.get_text(separator=' ', strip=True)

                # Find the current element's text in the chapter
                element_text = ""
                if hasattr(current_element, 'text_content'):
                    element_text = current_element.text_content()

                if element_text and len(element_text.strip()) > 5:
                    # Find where this element's content appears in the chapter
                    element_start = chapter_text.find(element_text.strip()[:50])
                    if element_start != -1:
                        local_offset = element_start + char_offset
                    else:
                        # Fallback: use text_count + char_offset
                        local_offset = text_count + char_offset
                else:
                    local_offset = text_count + char_offset
            else:
                local_offset = text_count + char_offset

            # Clamp to chapter bounds
            chapter_text = BeautifulSoup(item['content'], 'html.parser').get_text(separator=' ', strip=True)
            local_offset = min(max(0, local_offset), len(chapter_text))

            # Calculate global position
            global_offset = item['start'] + local_offset

            # Extract context
            start_pos = max(0, global_offset - context)
            end_pos = min(len(full_text), global_offset + context)

            snippet = full_text[start_pos:end_pos]
            logger.info(f"Snippet extracted: {snippet[:30]}...")
            return snippet

        except Exception as e:
            logger.error(f"❌ Error using epubcfi library for '{cfi}': {e}")
            return None

    def resolve_cfi_to_index(self, filename, cfi) -> Optional[int]:
        """
        Resolve CFI to canonical global character offset using the same parsing
        approach as get_text_around_cfi().
        """
        try:
            spine_step, element_steps, char_offset = self._parse_cfi_components(cfi)
            if not spine_step:
                logger.error(f"Could not extract spine step from CFI: '{cfi}'")
                return None

            book_path = self.resolve_book_path(filename)
            _, spine_map = self.extract_text_and_map(book_path)

            cfi_spine_index = spine_step // 2
            item = next((sp for sp in spine_map if sp.get('spine_index') == cfi_spine_index), None)
            if not item:
                logger.error(f"Spine index {cfi_spine_index} out of range for CFI '{cfi}'")
                return None

            tree = html.fromstring(item['content'])
            current_element = tree
            text_count = 0

            for step in element_steps:
                if not hasattr(step, 'index'):
                    continue

                step_index = step.index
                step_assertion = getattr(step, 'assertion', None)

                if step_assertion:
                    candidates = current_element.xpath(
                        f".//*[contains(@id, '{step_assertion}') or contains(@class, '{step_assertion}')]"
                    )
                    if candidates:
                        current_element = candidates[0]
                        continue

                if step_index % 2 == 0:
                    element_index = (step_index // 2) - 1
                    children = [child for child in current_element if hasattr(child, 'tag')]
                    if 0 <= element_index < len(children):
                        current_element = children[element_index]
                    else:
                        break
                else:
                    text_index = (step_index // 2)
                    text_nodes = []
                    for child in current_element:
                        if child.text and child.text.strip():
                            text_nodes.append(child.text.strip())
                        if child.tail and child.tail.strip():
                            text_nodes.append(child.tail.strip())

                    if 0 <= text_index < len(text_nodes):
                        text_count += sum(len(text) for text in text_nodes[:text_index])
                    break

            if current_element is not None:
                soup = BeautifulSoup(item['content'], 'html.parser')
                chapter_text = soup.get_text(separator=' ', strip=True)
                element_text = current_element.text_content() if hasattr(current_element, 'text_content') else ""

                if element_text and len(element_text.strip()) > 5:
                    element_start = chapter_text.find(element_text.strip()[:50])
                    if element_start != -1:
                        local_offset = element_start + char_offset
                    else:
                        local_offset = text_count + char_offset
                else:
                    local_offset = text_count + char_offset
            else:
                local_offset = text_count + char_offset

            chapter_text = BeautifulSoup(item['content'], 'html.parser').get_text(separator=' ', strip=True)
            local_offset = min(max(0, local_offset), len(chapter_text))
            return item['start'] + local_offset

        except Exception as e:
            logger.error(f"Error resolving CFI->index '{cfi}': {e}")
            return None
