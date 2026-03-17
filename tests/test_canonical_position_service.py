from types import SimpleNamespace

from src.services.canonical_position_service import CanonicalPositionService


class _FakeParser:
    def resolve_book_path(self, filename):
        return filename

    def extract_text_and_map(self, _book_path):
        full_text = "a" * 2000
        return full_text, [{"href": "text/part0013.html", "start": 0, "end": len(full_text), "spine_index": 1}]


def test_storyteller_ts_is_not_treated_as_audio_progress():
    parser = _FakeParser()
    service = CanonicalPositionService(parser)
    book = SimpleNamespace(
        abs_id="book-1",
        original_ebook_filename="primary.epub",
        ebook_filename="storyteller_uuid.epub",
        storyteller_uuid="uuid",
        transcript_file="DB_MANAGED",
    )

    state = {
        "pct": 0.42,
        "ts": 1773764578921,
        "position": 321,
        "href": "text/part0013.html",
    }

    result = service.resolve_state(book, "Storyteller", state)

    assert result.canonical_text_offset == 321
    assert result.canonical_audio_ms is None
    assert state["_canonical_text_offset"] == 321
    assert "_canonical_audio_ms" not in state


def test_abs_ts_is_treated_as_audio_progress():
    parser = _FakeParser()
    service = CanonicalPositionService(parser)
    book = SimpleNamespace(
        abs_id="book-2",
        original_ebook_filename="primary.epub",
        ebook_filename="primary.epub",
        transcript_file=None,
    )

    result = service.resolve_state(book, "ABS", {"ts": 123.456})

    assert result.canonical_audio_ms == 123456
    assert result.canonical_text_offset is None


def test_booklore_primary_percentage_fallback_has_syncable_confidence():
    parser = _FakeParser()
    service = CanonicalPositionService(parser)
    book = SimpleNamespace(
        abs_id="book-3",
        original_ebook_filename="primary.epub",
        ebook_filename="primary.epub",
        transcript_file=None,
    )

    result = service.resolve_state(book, "BookLore", {"pct": 0.25})

    assert result.canonical_text_offset == 500
    assert result.confidence == 0.82
