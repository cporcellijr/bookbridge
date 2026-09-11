import re
import requests
import logging
import base64
from defusedxml import ElementTree as ET
from urllib.parse import quote

from src.utils.file_transfers import (
    IncompleteTransferError,
    response_declares_size,
    stream_response_to_path,
)
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

class CWAClient:
    def __init__(self, credentials: dict = None):
        # `credentials` (multi-user) overrides per-user keys (CWA_USERNAME/
        # PASSWORD/ENABLED); server URL stays global. None => global client.
        self._creds = credentials
        # Strip trailing slash and verify we don't duplicate /opds
        raw_url = resolve_setting(credentials, "CWA_SERVER", "").rstrip('/')
        if raw_url.endswith('/opds'):
            raw_url = raw_url[:-5]

        # Ensure scheme is present (case-insensitive check)
        if raw_url and not raw_url.lower().startswith(('http://', 'https://')):
            raw_url = f"http://{raw_url}"

        self.base_url = raw_url
        self._uuid_cache: dict[str, str] = {}

        # Sanitize credentials (strip whitespace)
        self.username = (resolve_setting(credentials, "CWA_USERNAME", "") or "").strip()
        self.password = (resolve_setting(credentials, "CWA_PASSWORD", "") or "").strip()
        
        if self.username:
            # Log masked username to confirm what we loaded
            # Show first 2 chars if possible, or just 1 if short
            masked = self.username[:2] + "***" if len(self.username) > 2 else "***"
            logger.debug(f"🔑 CWA Auth: Loaded credentials for user '{masked}'")
        else:
            logger.debug("CWA Auth: No username provided; using unauthenticated OPDS")

        self.session = requests.Session()
        
        # Standardize headers for all requests
        headers = {
            "User-Agent": "KOReader/2023.10",  # Spoof KOReader
            "Accept": "application/atom+xml,application/xml,application/xhtml+xml,text/xml;q=0.9,*/*;q=0.8",
        }

        # Force Pre-emptive Basic Auth
        # This sends credentials immediately without waiting for a 401 Challenge,
        # bypassing the "Redirect to Login Page" issue.
        if self.username and self.password:
            # We still set session.auth for compatibility, but the header takes precedence
            # REMOVED: self.session.auth = (self.username, self.password)
            
            # Manually construct the header
            user_pass = f"{self.username}:{self.password}"
            encoded_u = base64.b64encode(user_pass.encode()).decode()
            headers["Authorization"] = f"Basic {encoded_u}"
            
        self.session.headers.update(headers)
            
        self.timeout = 30
        self.search_template = None

    def _make_request(self, url, **kwargs):
        """Helper to make requests with cookies cleared to force Basic Auth."""
        try:
            # Clear cookies to prevent 'Guest' session sticking
            self.session.cookies.clear()
            # Merge kwargs with default timeout if not present
            kwargs.setdefault('timeout', self.timeout)
            return self.session.get(url, **kwargs)
        except Exception as e:
            logger.error(f"❌ CWA Request failed: {e}", exc_info=True)
            raise

    @property
    def enabled(self) -> bool:
        """Whether CWA is switched on, read per call.

        The client is a DI Singleton, so a flag captured in ``__init__`` would
        outlive the setting: ``resolve_setting`` enforces the install-wide service
        gate, and an admin switching CWA off in Settings must take effect without
        a restart.
        """
        return str(resolve_setting(self._creds, "CWA_ENABLED", "")).lower() == "true"

    def is_configured(self):
        """Check if CWA is enabled and configured."""
        return self.enabled and bool(self.base_url)

    def check_connection(self):
        """Check connection to CWA and validate response type."""
        if not self.is_configured():
            logger.warning("⚠️ CWA not configured (skipping)")
            return False

        try:
            url = f"{self.base_url}/opds"
            # Use helper
            r = self._make_request(url, timeout=5)
            
            # Check for soft login redirect (status 200 but HTML content)
            if r.status_code == 200:
                if r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                    logger.error("❌ CWA Connection Failed: Server returned HTML login page instead of XML. Authentication failed.")
                    return False

                get_persistent_condition_logger().resolve(
                    logger,
                    f"cwa_connection:{self.base_url}",
                    f"✅ CWA connection recovered at {self.base_url}",
                )
                logger.info(f"✅ Connected to CWA at {self.base_url}")
                return True

            elif r.status_code in [401, 403]:
                logger.error(f"❌ CWA Connection Failed: Unauthorized ({r.status_code}). Check credentials.")
                return False
            else:
                logger.error(f"❌ CWA Connection Failed: {r.status_code}")
                return False

        except Exception as e:
            get_persistent_condition_logger().warn(
                logger,
                f"cwa_connection:{self.base_url}",
                f"❌ CWA Connection Error: {e}",
                exc_info=True,
                level=logging.ERROR,
            )
            return False

    def _get_search_template(self):
        """
        Dynamically discover the search URL template from the OPDS root.
        Returns: URL template string (e.g. '/opds/search/{searchTerms}') or None.
        """
        if self.search_template:
            return self.search_template

        try:
            logger.debug(f"🔍 CWA: Discovering search endpoint from {self.base_url}/opds")
            # Use helper
            r = self._make_request(f"{self.base_url}/opds")
            
            # Check if we got an HTML login page disguised as 200 OK
            if r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                 logger.warning("⚠️ CWA Discovery Failed: Server returned HTML content. Likely authentication failure (Soft Redirect).")
                 return None

            if r.status_code != 200:
                logger.warning(f"⚠️ CWA OPDS Root failed {r.status_code}")
                return None

            root = ET.fromstring(r.text)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            
            # Find proper search link (prefer atom+xml)
            search_link = None
            
            # Helper to check link
            def is_valid_search_link(link_elem):
                return link_elem.get('rel') == 'search'
            
            # 1. Try standard Atom namespace with type check
            for link in root.findall('atom:link', ns):
                if is_valid_search_link(link):
                    l_type = link.get('type', '')
                    l_href = link.get('href')
                    if 'atom+xml' in l_type:
                        search_link = l_href
                        break # Found best match
                    elif not search_link and 'opensearch' not in l_type:
                        # Backup candidate (if not explicitly OSD)
                        search_link = l_href

            # 2. Fallback: Namespace-agnostic search
            if not search_link:
                for child in root:
                    if child.tag.endswith('link') and is_valid_search_link(child):
                        l_type = child.get('type', '')
                        l_href = child.get('href')
                        if 'atom+xml' in l_type:
                            search_link = l_href
                            break
                        elif not search_link and 'opensearch' not in l_type:
                            search_link = l_href

            if search_link:
                self.search_template = search_link
                # Ensure absolute URL
                if self.search_template and not self.search_template.startswith('http'):
                    self.search_template = f"{self.base_url}{self.search_template}"
                logger.info(f"✅ CWA: Discovered search template: {self.search_template}")
                return self.search_template

        except Exception as e:
            logger.error(f"❌ CWA Discovery Error: {e}", exc_info=True)
        
        return None

    def search_ebooks(self, query):
        """
        Search CWA via OPDS feed for ebook matches.
        Returns a list of dicts: {'title': str, 'author': str, 'download_url': str, 'ext': str}
        """
        if not self.is_configured():
            return []

        # Get search template (dynamic or fallback)
        template = self._get_search_template()
        
        if not template:
            # Fallback to legacy assumed standard if discovery fails
            safe_query = quote(query)
            search_url = f"{self.base_url}/opds/search?q={safe_query}"
            logger.warning("⚠️ CWA: Could not discover search template, falling back to legacy URL.")
        else:
            # Replace {searchTerms} with query
            # Note: We must encode the query, but the template syntax might vary.
            # Standard is {searchTerms}, we replace it.
            safe_query = quote(query)
            if "{searchTerms}" in template:
                search_url = template.replace("{searchTerms}", safe_query)
            else:
                 # If template doesn't have placeholder (weird), try appending
                 pass 
                 # Actually, let's assume if it returns a base URL, we append query?
                 # No, defined spec says it should have it.
                 # If missing, we might fail or try simple replace?
                 search_url = template.replace("{searchTerms}", safe_query)
        
        try:
            # Use helper
            r = self._make_request(search_url)
            
            if r.status_code != 200:
                logger.warning(f"⚠️ CWA Search failed {r.status_code}: {search_url}")
                return []
                
            return self._parse_opds(r.text)

        except Exception as e:
            logger.error(f"❌ CWA Search Error: {e}", exc_info=True)
            return []

    def _parse_opds(self, xml_content):
        """Parse Atom XML response from OPDS feed."""
        results = []
        try:
            # Check for HTML response (common if auth failed or 404 page returned as 200)
            if xml_content.lstrip().lower().startswith(('<!doctype html', '<html')):
                logger.warning("⚠️ CWA returned HTML content instead of XML. Check configuration/URL.")
                logger.debug(f"HTML Snippet: {xml_content[:200]}")
                return []

            # OPDS is Atom-based
            # Namespaces are annoying in ElementTree, ignore them or handle them
            # For simplicity, we'll try to handle standard Atom namespace
            namespaces = {
                'atom': 'http://www.w3.org/2005/Atom',
                'dcterms': 'http://purl.org/dc/terms/',
            }
            
            root = ET.fromstring(xml_content)
            
            entries = []
            # Check if root is a feed or an entry
            if root.tag.endswith('entry'):
                entries = [root]
            else:
                entries = root.findall('atom:entry', namespaces)

            for entry in entries:
                title_elem = entry.find('atom:title', namespaces)
                title = title_elem.text if title_elem is not None else "Unknown"
                
                author_elem = entry.find('atom:author/atom:name', namespaces)
                author = author_elem.text if author_elem is not None else "Unknown"

                language_elem = entry.find('dcterms:language', namespaces)
                language = (
                    language_elem.text.strip()
                    if language_elem is not None and language_elem.text
                    else ""
                )
                
                # Find EPUB link
                epub_link = None
                for link in entry.findall('atom:link', namespaces):
                    rel = link.get('rel')
                    mime = link.get('type')
                    href = link.get('href')
                    
                    if mime == "application/epub+zip" or (rel and "http://opds-spec.org/acquisition" in rel and mime == "application/epub+zip"):
                        epub_link = href
                        break
                
                if epub_link:
                    # Resolve relative URLs
                    if not epub_link.startswith('http'):
                         epub_link = f"{self.base_url}{epub_link}" if epub_link.startswith('/') else f"{self.base_url}/{epub_link}"

                    # Extract ID from entry (OPDS uses atom:id)
                    entry_id = None
                    import re

                    # 1. Try to extract ID from links (Most reliable for Calibre-Web)
                    # Look for /opds/book/123, /books/123, or /opds/download/123/ in any link
                    for link in entry.findall('atom:link', namespaces):
                        href = link.get('href', '')
                        # Regex matches /book/123, /books/123, or CWA's acquisition
                        # form /opds/download/123/epub/ anywhere in the path. Missing
                        # the download form here is what made every CWA match store a
                        # title slug instead of the Calibre id, forcing the ambiguous
                        # search that misresolved in #427.
                        id_match = re.search(r'/(?:book|books|download)/(\d+)', href)
                        if id_match:
                            entry_id = id_match.group(1)
                            break

                    # 2. Fallback: Extract from atom:id if link extraction failed
                    if not entry_id:
                        id_elem = entry.find('atom:id', namespaces)
                        if id_elem is not None and id_elem.text:
                            # STRICTER REGEX: Only match if the ID is purely numeric or ends in a slash-number
                            # Avoid matching UUIDs like ...ae11
                            match = re.search(r'(?:^|/)(\d+)$', id_elem.text)
                            if match:
                                entry_id = match.group(1)
                            else:
                                # Last resort: Clean the title
                                entry_id = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]
                        else:
                            entry_id = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]

                    results.append({
                        "id": entry_id,
                        "title": title,
                        "author": author,
                        "language": language,
                        "download_url": epub_link,
                        "ext": "epub",
                        "source": "CWA"
                    })
                    
            return results

        except Exception as e:
            logger.error(f"❌ Error parsing CWA OPDS: {e}", exc_info=True)
            logger.debug(f"Failed XML content (first 500 chars): {xml_content[:500]}")
            return []

    def get_book_by_id(self, cwa_id):
        """
        Fetch a specific book by its CWA ID. 
        Includes a fallback to direct download link construction if the server crashes (metadata page error).
        """
        if not self.is_configured(): return None
        if str(cwa_id or "").strip().lower() in {"", "none"}:
            return None
        # 1. Try standard OPDS lookup
        endpoints = [f"/opds/book/{cwa_id}", f"/opds/books/{cwa_id}"]
        
        for ep in endpoints:
            try:
                url = f"{self.base_url}{ep}"
                logger.debug(f"🔍 CWA: Trying direct ID lookup at {url}")
                
                # Use helper (Stateless)
                r = self._make_request(url)
                
                # If we get valid XML, parse it
                if r.status_code == 200 and not r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                    results = self._parse_opds(r.text)
                    if results:
                        for res in results:
                            if str(res['id']) == str(cwa_id):
                                return res
                        if len(results) == 1:
                            return results[0]
            except Exception as e:
                logger.warning(f"⚠️ CWA ID lookup failed for '{url}': {e}", exc_info=True)

        # 2. Fallback: Direct Download Link Construction
        # If the server crashed (Author DB error) or lookup failed, assume the ID is valid
        # and try to construct the download link blindly.
        get_persistent_condition_logger().warn(
            logger,
            "cwa_get_book_by_id_lookup_failed",
            f"⚠️ CWA metadata lookup failed for ID '{cwa_id}' — Attempting direct download fallback",
        )
        
        # Standard Calibre-Web OPDS download format: /opds/download/{id}/{format}/
        # We assume EPUB as it's the primary target
        fallback_url = f"{self.base_url}/opds/download/{cwa_id}/epub/"
        
        return {
            "id": cwa_id,
            "title": f"Unknown Book {cwa_id} (Fallback)",  # We don't know the title, but download might still work
            "author": "Unknown",
            "download_url": fallback_url,
            "ext": "epub",
            "source": "CWA_Fallback"
        }

    def download_ebook(self, download_url, output_path):
        """Download ebook file from URL to output_path."""
        try:
            logger.info(f"⬇️ CWA: Downloading ebook from {download_url}")
            # Clear cookies manually for download too
            self.session.cookies.clear()
            
            # identity encoding keeps Content-Length comparable with the bytes written.
            headers = {"Accept-Encoding": "identity"}
            with self.session.get(download_url, headers=headers, stream=True, timeout=120) as r:
                r.raise_for_status()
                try:
                    # Historical floor: smaller than 1 KiB is an error page, not an ebook.
                    return stream_response_to_path(
                        r,
                        output_path,
                        expected_size=response_declares_size(r),
                        min_size=1023,
                    )
                except IncompleteTransferError as e:
                    logger.warning(
                        f"⚠️ Downloaded file is too small ({e.actual_size} bytes), likely failed",
                        exc_info=True,
                    )
                    return False
        except Exception as e:
            logger.error(f"❌ CWA Download failed: {e}", exc_info=True)
            # A failed transfer must not destroy a previously valid destination; the
            # staged-file publication path only replaces the final file after success.
            return False

    def get_book_uuid(self, calibre_id: str) -> str | None:
        """Resolve a stored CWA book identifier to its Calibre UUID via OPDS search.

        ``calibre_id`` is whatever ``_parse_opds`` stored as the entry ``id`` when
        the book was matched: a numeric Calibre book id when one could be
        extracted, otherwise a title-derived slug. A CWA search on a title or
        series term legitimately returns many books (e.g. every entry in a
        series), so the correct entry must be *selected* rather than assumed to be
        first. Returning the first result blindly wrote progress to the wrong book
        (see issue #427). If a unique match cannot be identified we return
        ``None`` — skipping the sync is safer than corrupting another book's
        progress.
        """
        if calibre_id in self._uuid_cache:
            return self._uuid_cache[calibre_id]

        if not self.is_configured():
            return None

        key = str(calibre_id or "").strip()
        if not key:
            return None

        try:
            template = self._get_search_template()
            if not template:
                return None

            search_url = template.replace("{searchTerms}", quote(key))
            r = self._make_request(search_url, timeout=10)
            if r.status_code != 200:
                return None

            root = ET.fromstring(r.text)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}

            uuid_re = re.compile(
                r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                re.IGNORECASE,
            )
            want_numeric = key.isdigit()

            # Collect every candidate entry with the identifiers we can match on:
            # its UUID, the numeric book id embedded in its links, and the same
            # title slug that ``_parse_opds`` would have stored as the entry id.
            candidates = []  # list of (uuid, numeric_id, title_slug)
            for entry in root.findall('atom:entry', ns):
                id_elem = entry.find('atom:id', ns)
                uuid = None
                if id_elem is not None and id_elem.text:
                    m = uuid_re.search(id_elem.text)
                    if m:
                        uuid = m.group(1)
                if not uuid:
                    continue

                numeric_id = None
                for link in entry.findall('atom:link', ns):
                    href = link.get('href', '')
                    # CWA download links look like /opds/download/505/epub/ while
                    # classic Calibre-Web uses /book/123 or /books/123.
                    m = re.search(r'/(?:book|books|download)/(\d+)', href)
                    if m:
                        numeric_id = m.group(1)
                        break

                title_elem = entry.find('atom:title', ns)
                title = title_elem.text if (title_elem is not None and title_elem.text) else ""
                title_slug = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]

                candidates.append((uuid, numeric_id, title_slug))

            chosen = None

            # 1. Exact numeric id match — most reliable when the stored id is the
            #    Calibre book number.
            if want_numeric:
                for uuid, numeric_id, _slug in candidates:
                    if numeric_id == key:
                        chosen = uuid
                        break

            # 2. Title-slug match — covers the common CWA case where the stored id
            #    is the title-derived slug. Only accept it when it is unambiguous.
            #    Compared case-insensitively: the slug is derived from the title,
            #    and a capitalisation edit in Calibre must not orphan the mapping.
            #    Equality is the only safe test. A series routinely contains titles
            #    that prefix one another ("Dungeon Crawler Carl" and "Dungeon
            #    Crawler Carl: The Butcher's Masquerade"), and both slugs are cut to
            #    the same 30 chars, so any prefix/fuzzy relaxation here binds one
            #    book's progress to another — the exact corruption #427 reported.
            if chosen is None:
                key_cf = key.casefold()
                slug_matches = [c[0] for c in candidates if c[2] and c[2].casefold() == key_cf]
                if len(slug_matches) == 1:
                    chosen = slug_matches[0]

            # A lone search result is deliberately NOT accepted on its own. CWA's
            # search matches series and author terms too, so "one result" means
            # only that one book matched the query — not that it is this book. If
            # the stored slug no longer matches anything (the title was edited),
            # skipping the sync and logging is safer than binding to whatever came
            # back; the reader can re-match the book to repair it.

            if chosen is None:
                if candidates:
                    get_persistent_condition_logger().warn(
                        logger,
                        f"cwa_uuid_unresolved:{calibre_id}",
                        f"❌ CWA: Could not unambiguously resolve '{calibre_id}' to a "
                        f"single book ({len(candidates)} candidate(s) returned); skipping "
                        "CWA sync to avoid writing progress to the wrong book.",
                        level=logging.ERROR,
                    )
                # Do NOT cache the failure: this client is a DI Singleton, so a
                # cached None would wedge CWA sync for this book until the
                # process restarts, even after the user fixes their metadata.
                return None

            self._uuid_cache[calibre_id] = chosen
            get_persistent_condition_logger().resolve(
                logger,
                f"cwa_uuid_unresolved:{calibre_id}",
                f"✅ CWA: Resolved '{calibre_id}' to UUID {chosen} after prior failures",
            )
            logger.debug(f"📖 CWA: Resolved '{calibre_id}' -> UUID {chosen}")
            return chosen

        except Exception as e:
            logger.error(f"❌ CWA UUID resolution error for '{calibre_id}': {e}", exc_info=True)
            return None
