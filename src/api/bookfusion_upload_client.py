"""BookFusion upload client — Calibre integration API.

Uploads a local EPUB to BookFusion via the 3-step Calibre upload API
(init → S3 PUT → finalize). Uses HTTP Basic auth with the Calibre API key,
a separate credential from the reader-device-flow access token.
"""

from __future__ import annotations

import hashlib
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Any, List, Optional
from xml.etree import ElementTree

import requests

from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 60
_S3_TIMEOUT = 180
_FINALIZE_TIMEOUT = 60
_DEFAULT_API_URL = "https://www.bookfusion.com"
_CALIBRE_API_PATH = "/calibre-api/v1"

# Namespaces used in EPUB OPF parsing
_NAMESPACES = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf",
}


@dataclass
class BookFusionUploadResult:
    """Result of a book upload attempt."""

    status: str  # "created" | "duplicate" | "error"
    book_id: Optional[int] = None
    message: str = ""


class BookFusionUploadClient:
    """Upload EPUBs to BookFusion via the Calibre integration API (3-step upload).

    Authentication uses HTTP Basic with the Calibre API key (``BOOKFUSION_API_KEY``),
    which is a per-user credential separate from the reader-device access token.
    """

    def __init__(
        self,
        credentials: Optional[dict] = None,
        database_service=None,
        user_id: Optional[int] = None,
    ) -> None:
        self._creds = credentials
        self._db = database_service
        self._user_id = user_id
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Setting resolver (mirrors BookFusionClient._r)
    # ------------------------------------------------------------------

    def _r(self, key: str, default: str = "") -> str:
        return str(resolve_setting(self._creds, key, default) or default).strip()

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        raw = self._r("BOOKFUSION_API_URL", _DEFAULT_API_URL).rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = f"https://{raw}"
        return raw or _DEFAULT_API_URL

    def _calibre_api_url(self) -> str:
        return f"{self._base_url()}{_CALIBRE_API_PATH}"

    def _api_key(self) -> str:
        return self._r("BOOKFUSION_API_KEY")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """``True`` when a non-empty Calibre API key is resolvable."""
        return bool(self._api_key())

    def upload_epub(self, epub_path: str, metadata: dict) -> BookFusionUploadResult:
        """Run the 3-step upload pipeline for *epub_path*.

        Parameters
        ----------
        epub_path:
            Absolute or relative path to the EPUB file on disk.
        metadata:
            Dict with keys ``title``, ``summary``, ``language``, ``isbn``,
            ``issued_on``, ``authors`` (list[str]), ``tags`` (list[str]),
            ``series`` (list[dict{title, index}]).

        Returns
        -------
        BookFusionUploadResult
        """
        if not os.path.isfile(epub_path):
            return BookFusionUploadResult("error", message=f"File not found: {epub_path}")

        file_digest = self._compute_file_digest(epub_path)
        metadata_digest = self._compute_metadata_digest(metadata)

        # --- Step 1: init ---
        init_result = self._init_upload(os.path.basename(epub_path), file_digest)
        if init_result is None:
            return BookFusionUploadResult("error", message="Upload init failed (no response)")
        if init_result.get("status_code") == 422:
            # Duplicate — BookFusion already has a book with this exact file digest
            return BookFusionUploadResult("duplicate", message="Book is already in your BookFusion library")
        if init_result.get("status_code") != 201:
            body = str(init_result.get("body", ""))[:300]
            return BookFusionUploadResult("error", message=f"Upload init returned {init_result.get('status_code')}: {body}")

        s3_url = init_result.get("url")
        params = init_result.get("params")
        if not s3_url or not params:
            return BookFusionUploadResult("error", message="Upload init missing S3 url or params")

        # --- Step 2: S3 PUT ---
        s3_ok = self._s3_put(s3_url, params, epub_path)
        if not s3_ok:
            return BookFusionUploadResult("error", message="S3 upload failed")

        # --- Step 3: finalize ---
        finalize_result = self._finalize(
            key=params["key"],
            digest=file_digest,
            metadata_digest=metadata_digest,
            metadata=metadata,
        )
        if finalize_result is None:
            return BookFusionUploadResult("error", message="Finalize failed (no response)")

        if finalize_result.get("status_code") == 201:
            book_id = finalize_result.get("book_id")
            if book_id is not None:
                return BookFusionUploadResult("created", book_id=book_id, message="Upload successful")
            return BookFusionUploadResult("error", message="Finalize returned 201 but no book id")

        body = str(finalize_result.get("body", ""))[:300]
        return BookFusionUploadResult("error", message=f"Finalize returned {finalize_result.get('status_code')}: {body}")

    # ------------------------------------------------------------------
    # Private upload steps
    # ------------------------------------------------------------------

    def _init_upload(self, filename: str, digest: str) -> Optional[dict]:
        """POST /uploads/init with the EPUB filename and file digest.

        Returns a dict with ``status_code``, ``url``, ``params`` (from JSON body
        on 201), and ``body`` (raw text), or ``None`` on connection error.
        """
        api_key = self._api_key()
        if not api_key:
            logger.warning("BookFusion upload init skipped: no API key")
            return None
        url = f"{self._calibre_api_url()}/uploads/init"
        data = {"filename": filename, "digest": digest}
        try:
            resp = self.session.post(url, data=data, auth=(api_key, ""), timeout=_INIT_TIMEOUT)
        except Exception as exc:
            logger.error("BookFusion upload init failed: %s", exc)
            return None

        result: dict = {"status_code": resp.status_code, "body": resp.text}
        if resp.status_code == 201:
            try:
                body = resp.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                result["url"] = body.get("url")
                result["params"] = body.get("params")
        return result

    def _s3_put(self, s3_url: str, params: dict, epub_path: str) -> bool:
        """POST the EPUB file to the pre-signed S3 URL as multipart form.

        Returns ``True`` on HTTP 204.
        """
        try:
            with open(epub_path, "rb") as f:
                files = {"file": (os.path.basename(epub_path), f, "application/epub+zip")}
                resp = self.session.post(s3_url, data=params, files=files, timeout=_S3_TIMEOUT)
        except Exception as exc:
            logger.error("BookFusion S3 upload failed: %s", exc)
            return False

        if resp.status_code == 204:
            return True
        logger.warning("BookFusion S3 upload returned %s: %s", resp.status_code, resp.text[:200])
        return False

    def _finalize(
        self,
        key: str,
        digest: str,
        metadata_digest: str,
        metadata: dict,
    ) -> Optional[dict]:
        """POST /uploads/finalize with the S3 key, digests, and book metadata.

        Returns a dict with ``status_code``, ``book_id`` (on 201), and ``body``,
        or ``None`` on connection error.
        """
        api_key = self._api_key()
        if not api_key:
            logger.warning("BookFusion upload finalize skipped: no API key")
            return None
        url = f"{self._calibre_api_url()}/uploads/finalize"

        # Build form data as a list of tuples to support repeating fields
        data: list = [
            ("key", key),
            ("digest", digest),
            ("metadata[calibre_metadata_digest]", metadata_digest),
        ]

        # Scalar metadata fields — skip empty strings to avoid sending
        # ``isbn=""`` or ``issued_on=""`` which can cause date-parse rejections.
        for field in ("title", "summary", "language", "isbn", "issued_on"):
            val = metadata.get(field)
            if val not in (None, ""):
                data.append((f"metadata[{field}]", str(val)))

        # Repeating author list
        for author in metadata.get("authors") or []:
            if author:
                data.append(("metadata[author_list][]", str(author)))

        # Repeating tag list
        for tag in metadata.get("tags") or []:
            if tag:
                data.append(("metadata[tag_list][]", str(tag)))

        # Series (repeat per series item: title + index)
        for series_item in metadata.get("series") or []:
            series_title = series_item.get("title")
            if series_title:
                data.append(("metadata[series][][title]", str(series_title)))
            series_index = series_item.get("index")
            if series_index is not None:
                data.append(("metadata[series][][index]", str(series_index)))

        try:
            resp = self.session.post(
                url,
                data=data,
                auth=(api_key, ""),
                headers={"Accept": "application/json"},
                timeout=_FINALIZE_TIMEOUT,
            )
        except Exception as exc:
            logger.error("BookFusion upload finalize failed: %s", exc)
            return None

        result: dict = {"status_code": resp.status_code, "body": resp.text}
        if resp.status_code == 201:
            try:
                body = resp.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                raw_id = body.get("id")
                if raw_id is not None:
                    try:
                        result["book_id"] = int(raw_id)
                    except (ValueError, TypeError):
                        pass
        return result

    # ------------------------------------------------------------------
    # Digest computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_file_digest(epub_path: str) -> str:
        """SHA-256 of the EPUB file contents, read in 1 MiB chunks."""
        h = hashlib.sha256()
        chunk_size = 1024 * 1024
        with open(epub_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _compute_metadata_digest(metadata: dict) -> str:
        """SHA-256 of concatenated metadata fields, no separators, skip None.

        See §4 of the upload spec for the exact field order.
        """
        h = hashlib.sha256()

        def _update(v: Any) -> None:
            if v is None:
                return
            if isinstance(v, bytes):
                h.update(v)
            else:
                h.update(str(v).encode("utf-8"))

        # 1. Scalar fields in order
        _update(metadata.get("title"))
        _update(metadata.get("summary"))
        _update(metadata.get("language"))
        _update(metadata.get("isbn"))
        _update(metadata.get("issued_on"))

        # 2. Series: title then index (per series item)
        series_list = metadata.get("series")
        if series_list is not None:
            for item in series_list:
                _update(item.get("title"))
                _update(item.get("index"))

        # 3. Authors in order
        for author in metadata.get("authors") or []:
            _update(author)

        # 4. Tags in order
        for tag in metadata.get("tags") or []:
            _update(tag)

        # 5. Bookshelves — skipped (we send none)
        # 6. Cover — skipped for v1

        return h.hexdigest()


# ------------------------------------------------------------------
# Static EPUB metadata extractor
# ------------------------------------------------------------------


def extract_epub_metadata(epub_path: str) -> dict:
    """Parse a local EPUB file and return its Dublin Core metadata.

    Opens the EPUB (a ZIP archive), reads ``META-INF/container.xml`` to locate
    the package document (OPF), then extracts the Dublin Core fields.

    Returns a dict with keys:
    ``title``, ``summary``, ``language``, ``isbn``, ``issued_on``,
    ``authors`` (list[str]), ``tags`` (list[str]), ``series`` (list[dict]).
    """
    result: dict = {
        "title": "",
        "summary": "",
        "language": "",
        "isbn": "",
        "issued_on": "",
        "authors": [],
        "tags": [],
        "series": [],
    }

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            # --- Locate OPF via container.xml ---
            try:
                container_xml = zf.read("META-INF/container.xml")
            except KeyError:
                logger.warning("No META-INF/container.xml in EPUB: %s", epub_path)
                return result

            try:
                container_tree = ElementTree.fromstring(container_xml)
            except ElementTree.ParseError as exc:
                logger.warning("Failed to parse container.xml in %s: %s", epub_path, exc)
                return result

            # Find the first rootfile element
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile_el = container_tree.find(".//c:rootfile", ns)
            if rootfile_el is None:
                logger.warning("No rootfile element in container.xml of %s", epub_path)
                return result
            opf_path = rootfile_el.get("full-path", "").strip()
            if not opf_path:
                logger.warning("Empty rootfile full-path in container.xml of %s", epub_path)
                return result

            # --- Parse OPF ---
            try:
                opf_content = zf.read(opf_path)
            except KeyError:
                logger.warning("OPF file not found in EPUB: %s/%s", epub_path, opf_path)
                return result

            try:
                opf_tree = ElementTree.fromstring(opf_content)
            except ElementTree.ParseError as exc:
                logger.warning("Failed to parse OPF in %s: %s", epub_path, exc)
                return result

            # --- Extract Dublin Core metadata ---
            # dc:title
            title_el = opf_tree.find(".//dc:title", _NAMESPACES)
            if title_el is not None and title_el.text:
                result["title"] = title_el.text.strip()

            # dc:description → summary
            desc_el = opf_tree.find(".//dc:description", _NAMESPACES)
            if desc_el is not None and desc_el.text:
                result["summary"] = desc_el.text.strip()

            # dc:language
            lang_el = opf_tree.find(".//dc:language", _NAMESPACES)
            if lang_el is not None and lang_el.text:
                result["language"] = lang_el.text.strip()

            # dc:date → issued_on
            date_el = opf_tree.find(".//dc:date", _NAMESPACES)
            if date_el is not None and date_el.text:
                result["issued_on"] = date_el.text.strip()

            # ISBN: pick the first dc:identifier whose value looks like 10/13-digit ISBN
            for id_el in opf_tree.findall(".//dc:identifier", _NAMESPACES):
                if id_el.text:
                    cleaned = id_el.text.strip().replace("-", "").replace(" ", "")
                    if len(cleaned) in (10, 13) and cleaned.isdigit():
                        result["isbn"] = cleaned
                        break

            # dc:creator → authors (in document order)
            authors = []
            for creator_el in opf_tree.findall(".//dc:creator", _NAMESPACES):
                if creator_el.text:
                    authors.append(creator_el.text.strip())
            result["authors"] = authors

            # dc:subject → tags
            tags = []
            for subject_el in opf_tree.findall(".//dc:subject", _NAMESPACES):
                if subject_el.text:
                    tags.append(subject_el.text.strip())
            result["tags"] = tags

            # Series from OPF metadata (meta elements with name="calibre:series" etc.)
            # Also check for opf:role="aut" creator ordering — already in document order.
            # For series, look in the OPF metadata section for calibre meta elements.
            series_meta = {}
            for meta_el in opf_tree.findall(".//{http://www.idpf.org/2007/opf}meta"):
                name = meta_el.get("name", "").strip()
                content = meta_el.get("content", "").strip()
                if name == "calibre:series":
                    series_meta["title"] = content
                elif name == "calibre:series_index":
                    try:
                        series_meta["index"] = float(content)
                    except (ValueError, TypeError):
                        pass
            if series_meta.get("title"):
                result["series"] = [series_meta]

    except zipfile.BadZipFile as exc:
        logger.warning("Bad ZIP (not an EPUB?) %s: %s", epub_path, exc)
    except Exception as exc:
        logger.warning("Unexpected error extracting EPUB metadata from %s: %s", epub_path, exc)

    return result
