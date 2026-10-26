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
from src.services.alignment_service import AlignmentService, ContentMatchRejected
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


def test_partial_omnibus_refusal_logs_counts_and_regional_evidence(service, caplog, monkeypatch):
    _guard_env(monkeypatch)
    ebook = _sequential_words(1000)
    segments = [{'start': 0, 'end': 100, 'text': _sequential_words(100)}]
    with pytest.raises(ContentMatchRejected) as refusal:
        service.align_and_store('partial-omnibus', segments, ebook)
    reason = str(refusal.value)
    assert 'ebook 1,000 / transcript 100 tokens = 10.00x' in reason
    assert 'ebook quartile overlap:' in reason
    assert ' / 0.0% / 0.0% / 0.0%' in reason
    assert 'no exact match' in reason
    assert 'Completed transcript retained' in reason
    assert 'do not appear to be the same work' not in reason
    assert reason in caplog.text
    assert service._get_alignment('partial-omnibus') is None


def test_overlap_diagnostics_use_the_same_samples_as_the_verdict():
    diagnostics = {}
    overlap = transcript_text_overlap(_sequential_words(100), _sequential_words(1000),
                                      diagnostics=diagnostics)
    assert diagnostics['sampled'] == 200
    assert diagnostics['matched'] / diagnostics['sampled'] == overlap
    assert diagnostics['quartiles'][0] > 0
    assert diagnostics['quartiles'][1:] == [0, 0, 0]


def test_overlap_survives_an_inline_word_join_in_the_ebook_text():
    """``_WORD_TOKEN_RE`` does not include
    ``INLINE_TEXT_JOINER``, so left in place it would split a bionic-reading
    mid-word join (e.g. ``"th<JOINER>e"``) back into two separate tokens
    ("th", "e") that never match the transcript's single "the" token --
    scoring a perfectly matching book as a mismatch. ``_word_tokens`` strips
    it first so this still scores as a full match."""
    from src.utils.ebook_dom_map import INLINE_TEXT_JOINER

    text = _sequential_words(200)
    fragmented_ebook_text = text.replace("word", f"wo{INLINE_TEXT_JOINER}rd")
    assert transcript_text_overlap(text, fragmented_ebook_text) == pytest.approx(1.0)


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
        with pytest.raises(ContentMatchRejected):
            service.align_and_store("mismatched_book", segments, ebook_text)
    assert service._get_alignment("mismatched_book") is None
    assert service.database_service.get_alignment_method("mismatched_book") is None


def test_recovery_requires_content_proof_even_when_normal_guards_are_disabled(service, monkeypatch):
    from unittest.mock import Mock

    service._save_alignment("book", [{"char": 0, "ts": 0}, {"char": 100, "ts": 10}],
                            "lexical", total_chars=100)
    service._generate_alignment_map_with_method = Mock(side_effect=AssertionError("must not align"))

    def weak_overlap(_transcript, _ebook, diagnostics=None):
        diagnostics.update(sampled=20)
        return 0.24

    monkeypatch.setattr(map_quality, "transcript_text_overlap", weak_overlap)
    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, ollama_guard="false", lexical_guard="false")
        with pytest.raises(ContentMatchRejected, match="required 25.0%"):
            service.align_and_store(
                "book", [{"start": 0, "end": 10, "text": "some transcript"}],
                "some ebook", require_recovery_content_match=True,
            )

    assert service._get_alignment("book") == [{"char": 0, "ts": 0}, {"char": 100, "ts": 10}]
    service._generate_alignment_map_with_method.assert_not_called()


@pytest.mark.parametrize("points", [
    [{"char": 0, "ts": 0}, {"char": float("nan"), "ts": 1}],
    [{"char": -1, "ts": 0}, {"char": 10, "ts": 1}],
])
def test_recovery_rejects_nonfinite_or_negative_map_positions(service, points):
    text = _sequential_words(300)
    service._generate_alignment_map_with_method = lambda *args, **kwargs: (points, "lexical", None)
    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, ollama_guard="false", lexical_guard="false")
        assert service.align_and_store(
            "invalid-recovery-map", [{"start": 0, "end": 10, "text": text,
                                      "words": [{"word": word, "start": 0, "end": 10}
                                                for word in text.split()]}], text,
            require_recovery_content_match=True,
        ) is False
    assert service._get_alignment("invalid-recovery-map") is None


def test_recovery_refuses_timings_discarded_by_alignment(service):
    from unittest.mock import Mock

    text = _sequential_words(300)
    service._generate_alignment_map_with_method = Mock(side_effect=AssertionError("must not estimate"))
    segments = [{"start": 0, "end": 10, "text": text,
                 "words": [{"word": "unrelated", "start": 0, "end": 10}]}]
    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, ollama_guard="false", lexical_guard="false")
        assert service.align_and_store(
            "incomplete-recovery-timing", segments, text, require_recovery_content_match=True,
        ) is False
    service._generate_alignment_map_with_method.assert_not_called()
    assert service._get_alignment("incomplete-recovery-timing") is None


def test_recovery_content_proof_does_not_bypass_configured_guard(service, monkeypatch):
    calls = []

    def overlap(_transcript, _ebook, diagnostics=None):
        calls.append(None)
        if len(calls) == 1:
            diagnostics.update(sampled=20)
            return 0.30  # clears the mandatory recovery proof
        diagnostics.update(sampled=20, ebook_tokens=100, transcript_tokens=100,
                           quartiles=[0.0, 0.0, 0.0, 0.0])
        return 0.10  # fails the configured normal guard

    monkeypatch.setattr(map_quality, "transcript_text_overlap", overlap)
    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, min_overlap="0.20")
        with pytest.raises(ContentMatchRejected, match="need 20.0%"):
            service._verify_content_match(
                [{"text": "matching enough"}], "matching enough", abs_id="book",
                raise_on_mismatch=True, require_lexical_overlap=True,
            )

    assert len(calls) == 2


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
        with pytest.raises(ContentMatchRejected):
            service.align_and_store(abs_id, segments, ebook_text)
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


def test_embedding_pass_does_not_consult_the_lexical_guard(service, monkeypatch):
    """When the embedding similarity clears the threshold it decides alone; the
    lexical check only ever runs to second-guess a refusal."""
    service.ollama_client = _TopicOllama()
    spy = MagicMock(wraps=map_quality.transcript_text_overlap)
    monkeypatch.setattr(map_quality, "transcript_text_overlap", spy)

    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    ebook_text = "ocean " * 100

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp)
        ok = service._verify_content_match(segments, ebook_text, abs_id="x")

    assert ok is True
    spy.assert_not_called()


class _LowScoringEmbeddings:
    """An OpenAI text-embedding-3-small stand-in: every transcript passage scores
    0.38 against every book passage -- inside the 0.33-0.45 band #556 reported
    for correct pairings, below the 0.45 default."""

    provider = "openai"
    embed_model = "text-embedding-3-small"

    def is_configured(self) -> bool:
        return True

    def embed(self, texts: List[str]) -> List[List[float]]:
        half = len(texts) // 2  # the guard embeds transcript samples, then book samples
        return [[1.0, 0.0]] * half + [[0.38, (1 - 0.38 ** 2) ** 0.5]] * (len(texts) - half)


def test_low_embedding_score_is_overruled_by_lexical_overlap(tmp_path, caplog):
    """#556: a correct pairing whose embedding similarity lands below the
    threshold is stored when the transcript contains the ebook's own text,
    instead of being refused before alignment runs."""
    db, service = _service_with(tmp_path, _LowScoringEmbeddings(), "embed_low")
    try:
        ebook_text = _sequential_words(600)
        segments = [{"start": 0.0, "end": 300.0, "text": ebook_text}]

        with pytest.MonkeyPatch.context() as mp:
            _guard_env(mp, min_overlap="0.15")
            mp.setenv("OLLAMA_ALIGN_CONTENT_MIN_SIM", "0.45")
            with caplog.at_level("INFO", logger="src.services.alignment_service"):
                result = service.align_and_store("embed_low_book", segments, ebook_text)

        assert result is True
        assert service._get_alignment("embed_low_book") is not None
        assert ("Content-match guard: embedding similarity 0.38 < 0.45 "
                "(openai/text-embedding-3-small) overruled for embed_low_book") in caplog.text
    finally:
        db.db_manager.close()


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
            with pytest.raises(ContentMatchRejected):
                service.align_and_store("embed_fail_book", segments, ebook_text)
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


def test_embedding_refusal_stands_when_lexical_overlap_also_fails(tmp_path):
    """An embedding refusal is second-guessed by the lexical check (#556), but a
    genuine mismatch shares no n-grams, so the refusal stands and says why."""
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
            with pytest.raises(ContentMatchRejected) as rejected:
                service.align_and_store("embed_ok_book", segments, ebook_text)

        # The embedding verdict (ocean vs mountain => cosine 0.0 < 0.45) refuses.
        assert len(calls) == 1
        assert "best passage similarity 0.00 < 0.45; ollama/?; lexical overlap 0.0%" in str(rejected.value)
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


# --------------------------------------------------------------------------- #
# Non-Latin scripts (issue #460). The tokenizer used to be `[a-z0-9']+`, so a
# Russian book tokenized to just its page numbers, years and `&gt;` remnants --
# enough tokens to pass the size floor, none of them in the transcript -- and
# every correct Russian pairing was refused at 3-7% overlap.
# --------------------------------------------------------------------------- #

_CYRILLIC = "абвгдежзиклмнопрстуфхцчшщэюя"


def _russian_words(count: int, start: int = 0) -> List[str]:
    """`count` distinct Cyrillic-only words (no digits), like `_sequential_words`."""
    words = []
    for i in range(start, start + count):
        n, word = i + 1, "сл"
        while n:
            word += _CYRILLIC[n % len(_CYRILLIC)]
            n //= len(_CYRILLIC)
        words.append(word)
    return words


def _russian_pair(count: int = 3000, transcript_start: int = 0):
    """(transcript, ebook) shaped like the #460 report: the ebook carries page
    numbers every 25 words and stray `&gt;` markup; both narrate the year 1984."""
    transcript, ebook = [], []
    for i, word in enumerate(_russian_words(count)):
        ebook.append(word)
        if i % 40 == 0:
            ebook.append("1984")
        if i % 25 == 0:
            ebook.append(str(i // 25))
        if i % 300 == 0:
            ebook.append("&gt;")
    for i, word in enumerate(_russian_words(count, start=transcript_start)):
        transcript.append(word)
        if i % 40 == 0:
            transcript.append("1984")
    return " ".join(transcript), " ".join(ebook)


def test_russian_pairing_is_not_refused_by_the_lexical_guard(service, caplog):
    """#460 regression: a correct Russian pairing scored 0.0 (the report saw
    3.5-7%) and logged `Lexical content-match guard: audio/ebook n-gram overlap
    too low`. It must now clear the shipped default and store the map."""
    from src.utils.config_loader import DEFAULT_CONFIG

    transcript_text, ebook_text = _russian_pair()
    assert transcript_text_overlap(transcript_text, ebook_text) >= 0.5

    segments = [{"start": 0.0, "end": 3000.0, "text": transcript_text}]
    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, min_overlap=DEFAULT_CONFIG["CONTENT_MATCH_MIN_OVERLAP"])
        with caplog.at_level("WARNING"):
            ok = service._verify_content_match(segments, ebook_text, abs_id="768ec688")

    assert ok is True
    assert "Lexical content-match guard: audio/ebook n-gram overlap too low" not in caplog.text


def test_unrelated_russian_transcript_is_still_refused():
    """The guard must keep working for Russian, not just stop blocking it."""
    _, ebook_text = _russian_pair()
    unrelated, _ = _russian_pair(transcript_start=500_000)
    assert transcript_text_overlap(unrelated, ebook_text) == pytest.approx(0.0, abs=0.02)


def test_cyrillic_words_tokenize_and_yo_folds_to_ye():
    assert map_quality._word_tokens("Тана Френч, «В лесу» — ещё 1984 &gt;") == [
        "тана", "френч", "в", "лесу", "еще", "1984", "gt",
    ]
    assert map_quality._word_tokens("Ещё") == map_quality._word_tokens("еще")


def test_accents_and_curly_apostrophes_do_not_split_matches():
    assert map_quality._word_tokens("Café l’homme don’t") == ["cafe", "l'homme", "don't"]


def test_ascii_tokens_match_the_calibrated_pattern():
    """The 28-pair calibration was measured with `[a-z0-9']+`; plain ASCII text
    must tokenize exactly as it did then so those numbers still hold."""
    import re

    text = "It's 1984, Winston -- 'tis the_end o' the day; rock'n'roll? Dogs' bowls!"
    assert map_quality._word_tokens(text) == re.findall(r"[a-z0-9']+", text.lower())


def test_digit_only_tokens_cannot_judge():
    """Tokens with no letters (page numbers, years) never make a text judgeable."""
    ebook = " ".join(str(i) for i in range(300))
    transcript = " ".join(str(i) for i in range(1000, 1300))
    assert transcript_text_overlap(transcript, ebook) == 1.0


def test_unspaced_script_cannot_judge():
    """Chinese/Japanese/Thai have no spaces between words, so word n-grams can't
    line up with ASR output -- return 'cannot judge' instead of refusing."""
    ebook = "。".join(f"我们站在门口默默地看着第{i}片森林" for i in range(200))
    transcript = " ".join(f"他们在路上走了很久第{i}天" for i in range(200))
    assert transcript_text_overlap(transcript, ebook) == 1.0


def test_closing_curly_quote_stays_a_separator():
    """A word-edge `’` is a closing single quote, not an apostrophe: straightening
    it would glue it onto the word ("hello'") and miss the transcript's "hello"."""
    assert map_quality._word_tokens("‘Hello,’ she said. ‘Don’t.’") == [
        "hello", "she", "said", "don't",
    ]


def test_word_tokens_keeps_words_whole_across_format_chars():
    """Format characters (soft hyphen, word joiner, zero-width space) must not split words.
    The aligner's Polisher already deletes them, so the guard must do the same."""
    soft_hyphen = "\u00ad"
    word_joiner = "\u2060"
    zero_width_space = "\u200b"
    text_with_format = f"кро{soft_hyphen}вь гул{soft_hyphen}ко сло{word_joiner}во про{zero_width_space}бирался"
    text_plain = "кровь гулко слово пробирался"
    tokens_fmt = map_quality._word_tokens(text_with_format)
    tokens_plain = map_quality._word_tokens(text_plain)
    assert tokens_fmt == ["кровь", "гулко", "слово", "пробирался"]
    assert tokens_fmt == tokens_plain


def test_transcript_text_overlap_soft_hyphen_ebook():
    """A soft-hyphenated ebook must still match its plain transcript at 1.0 overlap."""
    diagnostics = {}
    words = _russian_words(600)
    transcript = " ".join(words)
    soft_hyphen = "\u00ad"
    ebook_words = []
    for w in words:
        if len(w) >= 4:
            mid = len(w) // 2
            ebook_words.append(w[:mid] + soft_hyphen + w[mid:])
        else:
            ebook_words.append(w)
    ebook_text = " ".join(ebook_words)
    overlap = transcript_text_overlap(transcript, ebook_text, diagnostics=diagnostics)
    assert overlap == 1.0
    assert diagnostics["ebook_tokens"] == diagnostics["transcript_tokens"]


def test_soft_hyphen_ebook_is_accepted_by_guard(service, caplog):
    """The lexical guard must accept a soft-hyphenated ebook; it should not log a warning."""
    from src.utils.config_loader import DEFAULT_CONFIG

    words = _russian_words(600)
    transcript = " ".join(words)
    soft_hyphen = "\u00ad"
    ebook_words = []
    for w in words:
        if len(w) >= 4:
            mid = len(w) // 2
            ebook_words.append(w[:mid] + soft_hyphen + w[mid:])
        else:
            ebook_words.append(w)
    ebook_text = " ".join(ebook_words)
    segments = [{"start": 0.0, "end": 1.0, "text": transcript}]

    with pytest.MonkeyPatch.context() as mp:
        _guard_env(mp, min_overlap=DEFAULT_CONFIG["CONTENT_MATCH_MIN_OVERLAP"])
        with caplog.at_level("WARNING"):
            ok = service._verify_content_match(segments, ebook_text, abs_id="soft-hyphen")
    assert ok is True
    assert "Lexical content-match guard: audio/ebook n-gram overlap too low" not in caplog.text
