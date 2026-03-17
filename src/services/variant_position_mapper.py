import hashlib
import json
import logging
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class VariantPositionMapper:
    """
    Map between alternate EPUB variants using cached anchor-excerpt lookups.

    This is intentionally lightweight: it caches successful excerpt-based
    remaps instead of building a full static alignment table for every book.
    """

    def __init__(self, ebook_parser, cache_dir: Path):
        self.ebook_parser = ebook_parser
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _file_fingerprint(book_path) -> str:
        path = Path(book_path)
        try:
            stat = path.stat()
            raw = f"{path.name}:{int(stat.st_mtime)}:{stat.st_size}"
        except OSError:
            raw = path.name
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_excerpt(text: Optional[str]) -> str:
        if not text:
            return ""
        collapsed = " ".join(str(text).split())
        return collapsed[:240].strip()

    def _cache_file(self, book_id: str, source_epub: str, target_epub: str) -> Path:
        try:
            source_path = self.ebook_parser.resolve_book_path(source_epub)
            target_path = self.ebook_parser.resolve_book_path(target_epub)
            source_fp = self._file_fingerprint(source_path)
            target_fp = self._file_fingerprint(target_path)
        except Exception:
            source_fp = hashlib.sha1(str(source_epub).encode("utf-8")).hexdigest()[:16]
            target_fp = hashlib.sha1(str(target_epub).encode("utf-8")).hexdigest()[:16]
        safe_book = hashlib.sha1(str(book_id).encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{safe_book}_{source_fp}_{target_fp}.json"

    def _load_cache(self, cache_file: Path) -> dict:
        if not cache_file.exists():
            return {}
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cache(self, cache_file: Path, payload: dict) -> None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            logger.debug("Variant map cache write failed for '%s': %s", cache_file, exc)

    def map_offset(
        self,
        *,
        book_id: str,
        source_epub: str,
        target_epub: str,
        source_offset: Optional[int],
        excerpt: Optional[str] = None,
    ) -> dict | None:
        if not source_epub or not target_epub:
            return None
        if source_epub == target_epub:
            return {
                "target_offset": source_offset,
                "confidence": 1.0 if source_offset is not None else 0.0,
                "excerpt": self._normalize_excerpt(excerpt),
                "cache_hit": False,
            }

        normalized_excerpt = self._normalize_excerpt(excerpt)
        if not normalized_excerpt and source_offset is None:
            return None

        cache_file = self._cache_file(book_id, source_epub, target_epub)
        cache = self._load_cache(cache_file)
        cache_key = hashlib.sha1(
            f"{source_offset}:{normalized_excerpt}".encode("utf-8")
        ).hexdigest()
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            cached = dict(cached)
            cached["cache_hit"] = True
            return cached

        target_offset = None
        confidence = 0.0
        exact_match = False

        try:
            target_book_path = self.ebook_parser.resolve_book_path(target_epub)
            target_text, _ = self.ebook_parser.extract_text_and_map(target_book_path)
        except Exception as exc:
            logger.debug("Variant map target load failed for '%s': %s", target_epub, exc)
            target_text = ""

        hint_pct = None
        if source_offset is not None:
            try:
                source_book_path = self.ebook_parser.resolve_book_path(source_epub)
                source_text, _ = self.ebook_parser.extract_text_and_map(source_book_path)
                if source_text:
                    hint_pct = max(0.0, min(float(source_offset) / float(len(source_text)), 1.0))
            except Exception:
                hint_pct = None

        if normalized_excerpt:
            if target_text:
                exact_index = target_text.find(normalized_excerpt)
                if exact_index >= 0:
                    target_offset = exact_index
                    confidence = 0.98
                    exact_match = True

            if target_offset is None:
                locator = self.ebook_parser.find_text_location(
                    target_epub,
                    normalized_excerpt,
                    hint_percentage=hint_pct,
                )
                if locator and locator.match_index is not None:
                    target_offset = int(locator.match_index)
                    confidence = 0.88 if locator.href else 0.8

        if target_offset is None and hint_pct is not None and target_text:
            target_offset = int(max(0, min(len(target_text) - 1, hint_pct * len(target_text))))
            confidence = 0.25

        if target_offset is None:
            return None

        result = {
            "target_offset": target_offset,
            "confidence": confidence,
            "excerpt": normalized_excerpt,
            "exact_match": exact_match,
            "cache_hit": False,
        }
        cache[cache_key] = result
        self._save_cache(cache_file, cache)
        return result
