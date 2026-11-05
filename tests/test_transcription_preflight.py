"""Synthetic issue #528 length evidence. No reporter EPUB/audio files used."""
import math

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.alignment_service import AlignmentService, ContentMatchRejected
from src.services.transcription_preflight import (
    evaluate_preflight, evidence_identity, metadata_duration, pair_key, record_preflight,
)
from src.utils.polisher import Polisher


def _synthetic_tokens(count):
    return " ".join("word" for _ in range(count))


def test_trimmed_versus_omnibus_are_measured_not_falsely_classified():
    """Synthetic equal-duration editions are not threshold calibration."""
    trimmed = evaluate_preflight(_synthetic_tokens(7500), 3600)
    omnibus = evaluate_preflight(_synthetic_tokens(14900), 3600)
    assert (trimmed["token_count"], omnibus["token_count"]) == (7500, 14900)
    assert (trimmed["epub_tokens_per_audio_hour"],
            omnibus["epub_tokens_per_audio_hour"]) == (7500.0, 14900.0)
    assert trimmed["assessment"] == omnibus["assessment"] == "uncalibrated"
    assert "not a mismatch verdict" in omnibus["explanation"]


def test_plausible_pair_and_unicode_tokenization_without_chapter_heuristics():
    result = evaluate_preflight("L’amour don’t üni café — Привет 1984", 3600)
    assert result["token_count"] == 5
    assert result["assessment"] == "uncalibrated"
    assert "chapter" not in result
    assert evaluate_preflight(_synthetic_tokens(8000), 3600)["assessment"] == "uncalibrated"


@pytest.mark.parametrize("bad", [None, "", 0, -1, math.inf, -math.inf, math.nan, "broken"])
def test_invalid_or_missing_duration_is_inconclusive(bad):
    result = evaluate_preflight(_synthetic_tokens(100), bad)
    assert result["token_count"] == 100
    assert result["duration_seconds"] is None
    assert result["epub_tokens_per_audio_hour"] is None
    assert result["assessment"] == "inconclusive"


def test_untrusted_duration_never_invents_a_rate():
    book = Book(abs_id="x", audio_source="BookOrbit", duration=3600)
    assert metadata_duration(book) is None
    assert metadata_duration(book, {"media": {"duration": 3600}}) is None
    assert evaluate_preflight(_synthetic_tokens(50), metadata_duration(book))["assessment"] == "inconclusive"
    abs_book = Book(abs_id="abs", audio_source="ABS", audio_source_id="abs",
                    audio_duration=3600, duration=3600)
    for invalid in (None, 0, -10, math.inf, math.nan):
        assert metadata_duration(abs_book, {"media": {"duration": invalid}}) is None
    abs_book.audio_duration = math.nan
    assert metadata_duration(abs_book) is None


def test_audio_chapter_count_alone_does_not_affect_assessment():
    book = Book(abs_id="x", audio_source="ABS", audio_source_id="x", audio_duration=3600)
    tiny_toc = {"media": {"duration": 3600, "chapters": [{"title": "One"}]}}
    many_chapters = {"media": {"duration": 3600, "chapters": [
        {"title": str(i)} for i in range(50)
    ]}}
    a = evaluate_preflight(_synthetic_tokens(7500), metadata_duration(book, tiny_toc))
    b = evaluate_preflight(_synthetic_tokens(7500), metadata_duration(book, many_chapters))
    assert a == b
    assert a["assessment"] == "uncalibrated"


def test_pair_content_changes_reset_ack_and_user_isolation(tmp_path):
    db = DatabaseService(str(tmp_path / "proof.db"))
    try:
        alice = db.create_user("first-528", "pw", role="user")
        bob = db.create_user("second-528", "pw", role="user")
        book = Book(abs_id="pair", abs_title="Pair",
                    audio_source="ABS", audio_source_id="pair",
                    ebook_source="BookOrbit", ebook_source_id="42",
                    original_ebook_filename="trimmed.epub",
                    audio_duration=3600, user_id=alice.id)
        db.save_book(book)
        text = _synthetic_tokens(100)
        measured = record_preflight(db, book, text)
        assert measured["assessment"] == "uncalibrated"
        token = evidence_identity(book, text)
        assert not db.get_transcription_preflights(bob.id, ["pair"])
        assert not db.acknowledge_transcription_preflight(bob.id, "pair", token)
        assert db.acknowledge_transcription_preflight(alice.id, "pair", token)
        assert db.get_transcription_preflights(alice.id, ["pair"])["pair"]["acknowledged"]

        # Same catalog book, second reader: publication fans out, ack does not.
        db.link_user_book(bob.id, "pair")
        record_preflight(db, book, text)
        assert not db.get_transcription_preflights(bob.id, ["pair"])["pair"]["acknowledged"]
        assert db.get_transcription_preflights(bob.id, ["pair"])["pair"]["token_count"] == 100

        changed = " ".join(("different", text))
        record_preflight(db, book, changed)
        row = db.get_transcription_preflights(alice.id, ["pair"])["pair"]
        assert not row["acknowledged"]
        assert not db.acknowledge_transcription_preflight(alice.id, "pair", token)
        assert db.acknowledge_transcription_preflight(alice.id, "pair", row["identity"])

        book.ebook_source_id = "43"
        record_preflight(db, book, changed)
        row = db.get_transcription_preflights(alice.id, ["pair"])["pair"]
        assert row["pairing_key"] == pair_key(book)
        assert not row["acknowledged"]
    finally:
        db.db_manager.close()


def test_advisory_ack_does_not_override_final_lexical_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "true")
    monkeypatch.setenv("CONTENT_MATCH_GUARD", "true")
    monkeypatch.setenv("CONTENT_MATCH_MIN_OVERLAP", "0.25")
    db = DatabaseService(str(tmp_path / "guard.db"))
    try:
        owner = db.create_user("owner528", "pw", role="user")
        book = Book(abs_id="mismatch", abs_title="Mismatched",
                    ebook_filename="full.epub", audio_source="ABS",
                    duration=3600, user_id=owner.id)
        db.save_book(book)
        full = " ".join(f"word{i}" for i in range(1000))
        transcript_text = " ".join(f"word{i}" for i in range(100))
        record_preflight(db, book, full)
        token = evidence_identity(book, full)
        assert db.acknowledge_transcription_preflight(owner.id, book.abs_id, token)
        alignment = AlignmentService(db, Polisher())
        with pytest.raises(ContentMatchRejected):
            alignment.align_and_store(
                book.abs_id, [{"start": 0, "end": 100, "text": transcript_text}], full,
            )
        assert alignment._get_alignment(book.abs_id) is None
    finally:
        db.db_manager.close()
