from pathlib import Path

from src.services.variant_position_mapper import VariantPositionMapper


class _FakeParser:
    def __init__(self, texts):
        self._texts = texts

    def resolve_book_path(self, filename):
        return filename

    def extract_text_and_map(self, book_path):
        return self._texts[str(book_path)], []

    def find_text_location(self, filename, excerpt, hint_percentage=None):
        text = self._texts[filename]
        idx = text.find(excerpt)
        if idx < 0:
            return None
        return type("Locator", (), {"match_index": idx, "href": "chapter.xhtml"})()


def test_variant_mapper_prefers_exact_excerpt_match(tmp_path: Path):
    excerpt = "the bridge canonical anchor excerpt"
    source = "aaa " + excerpt + " zzz"
    target = "111 " + excerpt + " 222"
    mapper = VariantPositionMapper(
        _FakeParser({"source.epub": source, "target.epub": target}),
        tmp_path / "variant_maps",
    )

    result = mapper.map_offset(
        book_id="book-1",
        source_epub="source.epub",
        target_epub="target.epub",
        source_offset=10,
        excerpt=excerpt,
    )

    assert result is not None
    assert result["target_offset"] == target.find(excerpt)
    assert result["confidence"] == 0.98
