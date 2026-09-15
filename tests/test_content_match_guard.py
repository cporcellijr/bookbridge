"""Tests for the non-LLM content-match guard (issue #426).

`AlignmentService._verify_content_match` was previously a permanent no-op on any
install without a configured Ollama client -- including this repo's own live
install (OLLAMA_ENABLED=false), which let eight mismatched audio/ebook pairings
get stored as maps that synced garbage positions silently before the user found
and deleted them. This file exercises the lexical n-gram fallback
(`map_quality.transcript_text_overlap`) and its wiring into the guard.
"""

from typing import List
from unittest.mock import MagicMock

import pytest

from src.db.database_service import DatabaseService
from src.services import map_quality
from src.services.alignment_service import AlignmentService
from src.services.map_quality import transcript_text_overlap
from src.utils.polisher import Polisher


def _sequential_words(count: int, start: int = 0) -> str:
    """`count` distinct tokens ("word0 word1 ...") so every 6-gram in the result
    is unique across the whole text -- no accidental overlap between disjoint
    ranges, and no accidental gap within a genuinely matching range."""
    return " ".join(f"word{i}" for i in range(start, start + count))


class _TopicOllama:
    """Stub embedding client: 'ocean' text embeds to [1,0], 'mountain' text to
    [0,1], anything else to [0.5, 0.5]. Mirrors tests/test_alignment_service.py."""

    def is_configured(self) -> bool:
        return True

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            low = (t or "").lower()
            if "ocean" in low:
                out.append([1.0, 0.0])
            elif "mountain" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out


@pytest.fixture
def service(tmp_path):
    """A real AlignmentService backed by a real (temp) SQLite DB, with no Ollama
    client -- exercises the lexical fallback end to end (the embedding path is
    unreachable), mirroring tests/test_map_publish_seam.py's fixture pattern."""
    db = DatabaseService(str(tmp_path / "content_match_guard.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def _guard_env(mp: pytest.MonkeyPatch, ollama_guard: str = "true",
               lexical_guard: str = "true", min_overlap: str = "0.25") -> None:
    mp.setenv("OLLAMA_ALIGN_CONTENT_GUARD", ollama_guard)
    mp.setenv("CONTENT_MATCH_GUARD", lexical_guard)
    mp.setenv("CONTENT_MATCH_MIN_OVERLAP", min_overlap)


# --------------------------------------------------------------------------- #
# transcript_text_overlap: the pure helper
# --------------------------------------------------------------------------- #

def test_overlap_identical_text_scores_near_one():
    text = _sequential_words(200)
    assert transcript_text_overlap(text, text) == pytest.approx(1.0)


def test_overlap_unrelated_text_scores_near_zero():
    ebook = _sequential_words(200, start=0)
    transcript = _sequential_words(200, start=100_000)
    assert transcript_text_overlap(transcript, ebook) == pytest.approx(0.0)


def test_overlap_samples_the_whole_book_not_just_the_head():
    """A transcript matching only the FIRST 10% of the ebook must score low --
    this must fail if the implementation samples only the head of the book."""
    ebook = _sequential_words(1000)
    transcript = _sequential_words(100)  # == ebook's first 100 tokens
    overlap = transcript_text_overlap(transcript, ebook)
    assert overlap < 0.3


def test_overlap_too_short_input_returns_one():
    """Too few tokens to sample meaningfully -- cannot judge, must not block."""
    assert transcript_text_overlap("hello world", _sequential_words(200)) == 1.0
    assert transcript_text_overlap(_sequential_words(200), "hello world") == 1.0


# --------------------------------------------------------------------------- #
# Wiring into AlignmentService._verify_content_match / align_and_store
# --------------------------------------------------------------------------- #

def test_align_and_store_refuses_mismatched_content_without_ollama(service):
    """The load-bearing regression test: with no Ollama configured, aligning an
    unrelated transcript against an ebook must be refused and nothing stored.
    Must fail if the lexical fallback is removed (`_verify_content_match` going
    back to `return True` whenever the embedding path is unavailable)."""
    ebook_text = _sequential_words(300, start=0)
    transcript_text = _sequential_words(300, start=100_000)
    segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp)
        result = service.align_and_store("mismatched_book", segments, ebook_text)

    assert result is False
    assert service._get_alignment("mismatched_book") is None
    assert service.database_service.get_alignment_method("mismatched_book") is None


def test_align_and_store_stores_matching_content_without_ollama(service):
    """False-positive guard: a genuine, well-matched pairing must not be
    blocked. The transcript narrates the ebook text verbatim -- the real
    (unmocked) n-gram anchoring runs and should easily find anchors."""
    ebook_text = _sequential_words(300)
    transcript_text = ebook_text
    segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp)
        result = service.align_and_store("matching_book", segments, ebook_text)

    assert result is True
    stored = service._get_alignment("matching_book")
    assert stored is not None
    assert len(stored) >= 2


@pytest.mark.parametrize("spelling", ["true", "on"])
def test_content_match_guard_blocks_on_true_and_on_spellings(service, spelling):
    """CLAUDE.md Sec 8: exercise both accepted truthy spellings for a new
    boolean setting -- catches a raw `== "true"` comparison."""
    ebook_text = _sequential_words(300, start=0)
    transcript_text = _sequential_words(300, start=100_000)
    segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]
    abs_id = f"blocked_{spelling}"

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, lexical_guard=spelling)
        result = service.align_and_store(abs_id, segments, ebook_text)

    assert result is False
    assert service._get_alignment(abs_id) is None


def test_content_match_guard_false_allows_mismatched_content(service):
    """CONTENT_MATCH_GUARD=false disables only the new lexical path -- the
    master switch (OLLAMA_ALIGN_CONTENT_GUARD) stays on."""
    ebook_text = _sequential_words(300, start=0)
    transcript_text = _sequential_words(300, start=100_000)
    segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, lexical_guard="false")
        result = service.align_and_store("guard_off", segments, ebook_text)

    assert result is True
    assert service._get_alignment("guard_off") is not None


def test_ollama_align_content_guard_false_disables_everything(service):
    """The master switch still disables the whole guard, lexical fallback
    included, for anyone who already turned it off -- preserving current
    behaviour for existing installs."""
    ebook_text = _sequential_words(300, start=0)
    transcript_text = _sequential_words(300, start=100_000)
    segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, ollama_guard="false", lexical_guard="true")
        result = service.align_and_store("master_off", segments, ebook_text)

    assert result is True
    assert service._get_alignment("master_off") is not None


def test_embedding_path_used_when_ollama_available_lexical_not_consulted(service, monkeypatch):
    """When Ollama IS available, the existing embedding-similarity behaviour
    runs unchanged and the new lexical path is never consulted."""
    service.ollama_client = _TopicOllama()
    spy = MagicMock(wraps=map_quality.transcript_text_overlap)
    monkeypatch.setattr(map_quality, "transcript_text_overlap", spy)

    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    ebook_text = "mountain " * 100

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp)
        ok = service._verify_content_match(segments, ebook_text, abs_id="x")

    assert ok is False  # unchanged embedding-guard behaviour: divergent topics blocked
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# An embedding path that renders no verdict must fall through to the lexical
# guard, not silently disable it. Previously an Ollama outage meant no content
# guard at all -- the same silent-garbage-map hole reached by another route.
# --------------------------------------------------------------------------- #

class _FailingOllama:
    """A CONFIGURED client whose embed() fails: Ollama down / OOM / malformed."""

    def __init__(self, result):
        self._result = result

    def is_configured(self) -> bool:
        return True

    def embed(self, texts: List[str]):
        return self._result


def _service_with(tmp_path, client, name):
    db = DatabaseService(str(tmp_path / f"{name}.db"))
    return db, AlignmentService(db, Polisher(), ollama_client=client)


@pytest.mark.parametrize("bad_result,label", [
    (None, "embed returns None"),
    ([], "embed returns an empty list"),
    ([[1.0, 0.0]], "embed returns too few vectors"),
])
def test_embed_failure_falls_through_to_lexical_guard(tmp_path, bad_result, label):
    """Ollama IS configured but embed() fails, and the content is mismatched:
    the lexical guard must still refuse. Fails if the embed()-failure leaf goes
    back to `return True`."""
    db, service = _service_with(tmp_path, _FailingOllama(bad_result), "embed_fail")
    try:
        ebook_text = _sequential_words(300, start=0)
        transcript_text = _sequential_words(300, start=100_000)
        segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

        with pytest.MonkeyPatch.context() as mp:
            _guard_env(mp)
            result = service.align_and_store("embed_fail_book", segments, ebook_text)

        assert result is False, f"guard must still refuse when {label}"
        assert service._get_alignment("embed_fail_book") is None
    finally:
        db.db_manager.close()


def test_embed_failure_does_not_block_matching_content(tmp_path):
    """False-positive guard for the outage path: a good pairing is still stored
    when the LLM is merely down."""
    db, service = _service_with(tmp_path, _FailingOllama(None), "embed_fail_ok")
    try:
        ebook_text = _sequential_words(300)
        segments = [{"start": 0.0, "end": 300.0, "text": ebook_text}]

        with pytest.MonkeyPatch.context() as mp:
            _guard_env(mp)
            result = service.align_and_store("embed_fail_match", segments, ebook_text)

        assert result is True
        assert service._get_alignment("embed_fail_match") is not None
    finally:
        db.db_manager.close()


def test_working_embedding_path_never_consults_the_lexical_guard(tmp_path):
    """When embeddings DO produce a verdict it stands alone -- the lexical
    fallback must not run, so the embedding path stays byte-for-byte as it was."""
    db, service = _service_with(tmp_path, _TopicOllama(), "embed_ok")
    try:
        # The topic keyword must appear in EVERY sampled passage, so interleave it
        # rather than prefixing once -- otherwise most passages embed to the stub's
        # neutral vector and the similarity says "match" for unrelated content.
        ebook_text = " ".join(f"mountain word{i}" for i in range(600))
        transcript_text = " ".join(f"ocean word{i}" for i in range(100_000, 100_600))
        segments = [{"start": 0.0, "end": 300.0, "text": transcript_text}]

        calls = []
        real = map_quality.transcript_text_overlap

        def _spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            _guard_env(mp)
            mp.setattr(map_quality, "transcript_text_overlap", _spy)
            result = service.align_and_store("embed_ok_book", segments, ebook_text)

        # The embedding verdict (ocean vs mountain => cosine 0.0 < 0.45) refuses.
        assert result is False
        assert calls == [], "lexical guard must not run when embeddings decided"
    finally:
        db.db_manager.close()


# --------------------------------------------------------------------------- #
# Calibration. These numbers come from running transcript_text_overlap over 28
# real book/transcript pairs on the live library (see the table in map_quality).
# The healthy range measured 0.300-0.850 -- the floor being Push (Unabridged), a
# correct pairing with unnarrated content -- while a genuine mismatch shares
# essentially no n-grams and lands at ~0.00. The default threshold has to sit in
# that band with margin on both sides.
# --------------------------------------------------------------------------- #

def test_default_threshold_sits_between_real_mismatch_and_real_match():
    """Pins the calibration: the default must allow the lowest legitimate overlap
    measured on real data and still refuse a near-zero one."""
    from src.utils.config_loader import DEFAULT_CONFIG

    default = float(DEFAULT_CONFIG["CONTENT_MATCH_MIN_OVERLAP"])
    lowest_real_match = 0.300   # Push (Unabridged), a correct pairing
    highest_real_mismatch = 0.04  # Let the Old Dreams Die, a wrong edition

    assert highest_real_mismatch < default < lowest_real_match, (
        f"CONTENT_MATCH_MIN_OVERLAP default {default} must sit between the highest "
        f"measured mismatch ({highest_real_mismatch}) and the lowest measured real "
        f"pairing ({lowest_real_match}); re-run the live overlap sweep before moving it"
    )
    # And keep real margin on both sides, not a hair's breadth.
    assert default <= lowest_real_match / 1.5
    assert default >= highest_real_mismatch * 2


def test_guard_allows_the_lowest_real_world_overlap_and_refuses_near_zero(service):
    """End to end at the shipped default: an overlap around the measured healthy
    floor is stored, a near-zero one is refused."""
    from src.utils.config_loader import DEFAULT_CONFIG

    default = DEFAULT_CONFIG["CONTENT_MATCH_MIN_OVERLAP"]
    ebook_text = _sequential_words(1000)

    # ~30% of the ebook's n-grams present -- the Push (Unabridged) case.
    partial = _sequential_words(300)
    assert transcript_text_overlap(partial, ebook_text) >= 0.25

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, min_overlap=default)
        assert service._verify_content_match(
            [{"start": 0.0, "end": 1.0, "text": partial}], ebook_text, abs_id="floor"
        ) is True

        unrelated = _sequential_words(1000, start=500_000)
        assert service._verify_content_match(
            [{"start": 0.0, "end": 1.0, "text": unrelated}], ebook_text, abs_id="zero"
        ) is False
