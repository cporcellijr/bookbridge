import logging
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class CanonicalPosition:
    book_id: str
    service_name: str
    canonical_text_offset: Optional[int] = None
    canonical_audio_ms: Optional[int] = None
    raw_percentage: Optional[float] = None
    locator_json: Optional[dict] = None
    anchor_excerpt: Optional[str] = None
    variant_id: Optional[str] = None
    confidence: float = 0.0
    observed_at: Optional[float] = None


class CanonicalPositionService:
    AUDIO_TIMESTAMP_SERVICES = {"ABS", "BookLoreAudio"}

    def __init__(self, ebook_parser, alignment_service=None, variant_position_mapper=None):
        self.ebook_parser = ebook_parser
        self.alignment_service = alignment_service
        self.variant_position_mapper = variant_position_mapper

    @staticmethod
    def _coerce_float(value) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _primary_epub(book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    @staticmethod
    def _storyteller_epub(book) -> Optional[str]:
        current = getattr(book, "ebook_filename", None)
        if current and str(current).startswith("storyteller_"):
            return current
        storyteller_uuid = getattr(book, "storyteller_uuid", None)
        if storyteller_uuid:
            return f"storyteller_{storyteller_uuid}.epub"
        return current

    def _service_epub(self, book, service_name: str) -> Optional[str]:
        if service_name == "Storyteller":
            return self._storyteller_epub(book)
        return self._primary_epub(book)

    def _cached_text(self, filename: Optional[str]):
        if not filename:
            return "", []
        book_path = self.ebook_parser.resolve_book_path(filename)
        return self.ebook_parser.extract_text_and_map(book_path)

    def _excerpt_from_offset(self, filename: Optional[str], offset: Optional[int]) -> str:
        if not filename or offset is None:
            return ""
        try:
            full_text, _ = self._cached_text(filename)
        except Exception:
            return ""
        if not full_text:
            return ""
        safe_offset = max(0, min(int(offset), len(full_text) - 1))
        start = max(0, safe_offset - 120)
        end = min(len(full_text), safe_offset + 120)
        return " ".join(full_text[start:end].split())[:240]

    def _resolve_href_offset(self, filename: Optional[str], href: Optional[str], chapter_progress: Optional[float]) -> Optional[int]:
        if not filename or not href:
            return None
        try:
            _, spine_map = self._cached_text(filename)
        except Exception:
            return None
        href_norm = str(href).lower().strip()
        for item in spine_map or []:
            item_href = str(item.get("href", "")).lower().strip()
            if not item_href:
                continue
            if href_norm not in item_href and item_href not in href_norm:
                continue
            start = int(item.get("start", 0))
            end = int(item.get("end", start))
            if end <= start:
                return start
            progression = self._coerce_float(chapter_progress)
            if progression is not None:
                progression = max(0.0, min(progression, 1.0))
                return start + int((end - start) * progression)
            return start
        return None

    def _resolve_text_offset(self, book, service_name: str, state_data: dict) -> tuple[Optional[int], float, Optional[str], Optional[str]]:
        epub = self._service_epub(book, service_name)
        if not epub:
            return None, 0.0, None, None

        position = self._coerce_int(state_data.get("position"))
        if position is None:
            position = self._coerce_int(state_data.get("match_index"))
        if position is not None:
            return position, 0.97, "position", epub

        xpath = state_data.get("xpath")
        if xpath:
            try:
                offset = self.ebook_parser.resolve_xpath_to_index(epub, xpath)
            except Exception:
                offset = None
            if offset is not None:
                return int(offset), 0.95, "xpath", epub

        cfi = state_data.get("cfi")
        if cfi:
            try:
                offset = self.ebook_parser.resolve_cfi_to_index(epub, cfi)
            except Exception:
                offset = None
            if offset is not None:
                return int(offset), 0.95, "cfi", epub

        href = state_data.get("href")
        fragment = state_data.get("fragment") or state_data.get("frag")
        if fragment is None:
            fragments = state_data.get("fragments")
            if isinstance(fragments, list) and fragments:
                fragment = fragments[0]
        if href and fragment:
            try:
                text = self.ebook_parser.resolve_locator_id(epub, href, fragment)
                full_text, _ = self._cached_text(epub)
            except Exception:
                text = None
                full_text = ""
            if text and full_text:
                idx = full_text.find(text[:120])
                if idx >= 0:
                    return idx, 0.9, "href_fragment", epub

        href_offset = self._resolve_href_offset(epub, href, state_data.get("chapter_progress"))
        if href_offset is not None:
            return href_offset, 0.82, "href_progression", epub

        pct = self._coerce_float(state_data.get("pct"))
        try:
            full_text, _ = self._cached_text(epub)
        except Exception:
            full_text = ""
        if pct is not None and full_text:
            total_len = len(full_text)
            if total_len > 0:
                confidence = 0.35
                primary_epub = self._primary_epub(book)
                if service_name == "BookLore" and epub and primary_epub and epub == primary_epub:
                    confidence = 0.82
                return int(max(0, min(total_len - 1, pct * total_len))), confidence, "percentage", epub

        return None, 0.0, None, epub

    def resolve_state(self, book, service_name: str, state_data: Optional[dict], observed_at: Optional[float] = None) -> CanonicalPosition:
        state_data = state_data or {}
        raw_percentage = None
        for key in ("raw_pct", "_remote_pct"):
            raw_percentage = self._coerce_float(state_data.get(key))
            if raw_percentage is not None:
                break

        result = CanonicalPosition(
            book_id=getattr(book, "abs_id", ""),
            service_name=service_name,
            raw_percentage=raw_percentage,
            locator_json=state_data,
            observed_at=observed_at,
            variant_id=self._service_epub(book, service_name),
        )

        timestamp = self._coerce_float(state_data.get("ts"))
        if timestamp is not None and service_name in self.AUDIO_TIMESTAMP_SERVICES:
            result.canonical_audio_ms = max(int(round(timestamp * 1000.0)), 0)
            result.confidence = 1.0
            if self.alignment_service and getattr(book, "transcript_file", None):
                try:
                    char_offset = self.alignment_service.get_char_for_time(book.abs_id, timestamp)
                except Exception:
                    char_offset = None
                if char_offset is not None:
                    result.canonical_text_offset = int(char_offset)
                    primary_epub = self._primary_epub(book)
                    result.anchor_excerpt = self._excerpt_from_offset(primary_epub, result.canonical_text_offset)
            return result

        local_offset, confidence, source, variant_epub = self._resolve_text_offset(book, service_name, state_data)
        result.variant_id = variant_epub
        result.confidence = confidence
        if local_offset is None:
            return result

        result.anchor_excerpt = self._excerpt_from_offset(variant_epub, local_offset)
        canonical_offset = int(local_offset)

        primary_epub = self._primary_epub(book)
        if (
            service_name == "Storyteller"
            and variant_epub
            and primary_epub
            and variant_epub != primary_epub
            and self.variant_position_mapper
        ):
            mapped = self.variant_position_mapper.map_offset(
                book_id=getattr(book, "abs_id", ""),
                source_epub=variant_epub,
                target_epub=primary_epub,
                source_offset=local_offset,
                excerpt=result.anchor_excerpt,
            )
            if mapped and mapped.get("target_offset") is not None:
                canonical_offset = int(mapped["target_offset"])
                result.confidence = min(result.confidence, float(mapped.get("confidence") or 0.0))
                result.anchor_excerpt = self._excerpt_from_offset(primary_epub, canonical_offset)
                source = f"{source or 'variant'}_mapped"
            elif primary_epub:
                try:
                    full_text, _ = self._cached_text(primary_epub)
                except Exception:
                    full_text = ""
                pct = self._coerce_float(state_data.get("pct"))
                if full_text and pct is not None:
                    canonical_offset = int(max(0, min(len(full_text) - 1, pct * len(full_text))))
                    result.confidence = min(result.confidence or 0.35, 0.35)
                    result.anchor_excerpt = self._excerpt_from_offset(primary_epub, canonical_offset)
                    source = "storyteller_pct_fallback"

        result.canonical_text_offset = canonical_offset
        if self.alignment_service and getattr(book, "transcript_file", None):
            try:
                ts = self.alignment_service.get_time_for_text(
                    book.abs_id,
                    result.anchor_excerpt or "",
                    char_offset_hint=canonical_offset,
                )
            except Exception:
                ts = None
            if ts is not None:
                result.canonical_audio_ms = max(int(round(float(ts) * 1000.0)), 0)

        if source:
            state_data["_canonical_source"] = source
        if result.canonical_text_offset is not None:
            state_data["_canonical_text_offset"] = result.canonical_text_offset
        if result.canonical_audio_ms is not None:
            state_data["_canonical_audio_ms"] = result.canonical_audio_ms
        if result.anchor_excerpt:
            state_data["_anchor_excerpt"] = result.anchor_excerpt
        if result.canonical_text_offset is not None or result.canonical_audio_ms is not None:
            state_data["_canonical_confidence"] = result.confidence
        if result.variant_id:
            state_data["_variant_id"] = result.variant_id
        return result
