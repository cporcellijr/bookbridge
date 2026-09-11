"""Tests for issue #426 Phase 2 — segmented alignment maps wired into
`AlignmentService._generate_alignment_map_with_method` behind the
`ALIGNMENT_SEGMENTED_MAPS` setting (docs/PLAN_OUT_OF_ORDER_NARRATION.md).

The permutation fixture below is the same shape as
`test_out_of_order_detection.TestFourPastMidnightPermutation` and
`test_out_of_order_detection.test_permuted_book_logs_out_of_order_warning`:
four uniquely-worded blocks (75/77/97/111 words) spined a-b-c-d but narrated
d-a-c-b, run through the real n-gram anchor finder rather than hand-built
anchor dicts, so the same LIS-collapses-to-two-blocks behavior applies here.
"""

import os
import random
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.alignment_service import AlignmentService
from src.sync_manager import SyncManager
from src.utils.polisher import Polisher

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Fixture: four-block permutation book (real anchors, real n-gram search)
# --------------------------------------------------------------------------- #

def _unique_words(prefix: str, count: int) -> List[str]:
    return [f"{prefix}{i:04d}" for i in range(count)]


def _segments_for(words: List[str], start_time: float, rng: random.Random,
                  word_seconds: float = 0.5,
                  jitter_seconds: float = 0.1) -> Tuple[List[Dict], float]:
    """One-word transcript segments, each `word_seconds` long, starting at
    `start_time`, each nudged by up to `jitter_seconds` of deterministic
    "measurement noise" drawn from the caller's own seeded `rng`. Returns
    (segments, next_start_time).

    A real transcript is never perfectly linear the way an evenly-spaced
    synthetic timeline is; without this, `select_anchors`'s retained anchors
    always land EXACTLY on the fitted line (residual ~0 to float precision),
    which cannot tell a correct, generous tolerance apart from a broken,
    near-zero one — both keep every anchor. `jitter_seconds` (0.1s) is well
    inside `segment_fit._RANSAC_TOLERANCE_SECONDS` (15s), so real fitting is
    unaffected; it exists only to make the tolerance's width load-bearing.
    The caller supplies `rng` (freshly seeded per book, not module-shared) so
    fixtures stay deterministic regardless of test order."""
    segments = []
    t = start_time
    for word in words:
        offset = rng.uniform(-jitter_seconds, jitter_seconds)
        start = t + offset
        end = start + word_seconds
        segments.append({"start": start, "end": end, "text": word})
        t += word_seconds
    return segments, t


def _char_ranges(blocks: List[List[str]]) -> List[Tuple[int, int]]:
    """Char (start, end) for each block within `" ".join(all words)`, matching
    how `_find_anchors` computes char offsets via `re.finditer(r'\\S+', ...)`."""
    ranges = []
    pos = 0
    for block in blocks:
        text = " ".join(block)
        ranges.append((pos, pos + len(text)))
        pos += len(text) + 1  # +1 for the space joining to the next block
    return ranges


class FourBlockBook:
    """A book with EPUB spine order a-b-c-d, narrated in whatever order the
    caller asks for. `full_text`/`spine_chapters` always reflect spine order;
    `segments` reflect `narration_order`."""

    SPINE_ORDER = ("a", "b", "c", "d")

    def __init__(self, narration_order: Tuple[str, str, str, str]):
        self.blocks = {
            "a": _unique_words("a", 75),
            "b": _unique_words("b", 77),
            "c": _unique_words("c", 97),
            "d": _unique_words("d", 111),
        }
        self.full_text = " ".join(w for name in self.SPINE_ORDER for w in self.blocks[name])
        ranges = _char_ranges([self.blocks[name] for name in self.SPINE_ORDER])
        self.char_range = dict(zip(self.SPINE_ORDER, ranges))
        self.spine_chapters = [{"start": start, "end": end} for start, end in ranges]

        rng = random.Random(426)  # fresh per book: deterministic regardless of test order
        segments = []
        t = 0.0
        for name in narration_order:
            block_segments, t = _segments_for(self.blocks[name], t, rng)
            segments.extend(block_segments)
        self.segments = segments


OUT_OF_ORDER_NARRATION = ("d", "a", "c", "b")
IN_ORDER_NARRATION = ("a", "b", "c", "d")


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "segmented.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


@pytest.fixture(autouse=True)
def _clean_segmented_maps_env():
    """Every test sets this explicitly; never let one leak into the next."""
    original = os.environ.pop("ALIGNMENT_SEGMENTED_MAPS", None)
    yield
    os.environ.pop("ALIGNMENT_SEGMENTED_MAPS", None)
    if original is not None:
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = original


# --------------------------------------------------------------------------- #
# 1. Flag OFF: byte-identical to today, segments=None reaches _save_alignment
# --------------------------------------------------------------------------- #

class TestFlagOffIsByteIdenticalToToday:

    def test_spine_chapters_are_completely_inert_when_flag_is_off(self, service):
        """The compatibility guarantee, made airtight: with the flag off, an
        out-of-order book's map/method/segments must be identical whether or
        not real spine boundaries are even passed in. A mutation that drops
        or weakens the `segmented_maps_enabled()` gate flips this immediately,
        because the fixture genuinely is out of order."""
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "false"
        book = FourBlockBook(narration_order=OUT_OF_ORDER_NARRATION)

        with_spine = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=book.spine_chapters)
        without_spine = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=None)

        assert with_spine == without_spine
        alignment_map, method, map_segments = with_spine
        assert method == "lexical"
        assert map_segments is None
        assert len(alignment_map) > 0

    def test_align_and_store_persists_segments_none_when_flag_off(self, service):
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "false"
        os.environ["CONTENT_MATCH_GUARD"] = "false"
        book = FourBlockBook(narration_order=OUT_OF_ORDER_NARRATION)

        assert service.align_and_store(
            "book-flag-off", [dict(seg) for seg in book.segments], book.full_text,
            book.spine_chapters)

        assert service._get_segments("book-flag-off") is None
        assert service.database_service.get_alignment_method("book-flag-off") == "lexical"


# --------------------------------------------------------------------------- #
# 2. Flag ON + in-order book: still the LIS path, still no segments emitted
# --------------------------------------------------------------------------- #

class TestFlagOnInOrderBook:

    def test_in_order_book_stays_on_the_lis_path(self, service, caplog):
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "true"
        book = FourBlockBook(narration_order=IN_ORDER_NARRATION)

        with caplog.at_level("INFO", logger="src.services.alignment_service"):
            alignment_map, method, map_segments = service._generate_alignment_map_with_method(
                book.segments, book.full_text, spine_chapters=book.spine_chapters)

        assert map_segments is None
        assert method == "lexical"
        assert len(alignment_map) > 0
        assert "narration order matches spine order" in caplog.text
        assert "Monotonic LIS filter" in caplog.text
        assert "Segmented filter" not in caplog.text

    def test_in_order_book_map_matches_the_flag_off_map(self, service):
        """Not just "still uses the LIS" — the actual map must come out the
        same whether the flag is on or off, since fitting found nothing worth
        using."""
        book = FourBlockBook(narration_order=IN_ORDER_NARRATION)

        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "false"
        off_map, off_method, off_segments = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=book.spine_chapters)

        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "true"
        on_map, on_method, on_segments = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=book.spine_chapters)

        assert on_map == off_map
        assert on_method == off_method
        assert off_segments is None
        assert on_segments is None


# --------------------------------------------------------------------------- #
# 3. Flag ON + out-of-order book: segments emitted, more anchors retained,
#    segments_json persisted
# --------------------------------------------------------------------------- #

class TestFlagOnOutOfOrderBook:

    def test_segments_are_emitted_and_more_anchors_are_retained_than_the_lis(self, service, caplog):
        book = FourBlockBook(narration_order=OUT_OF_ORDER_NARRATION)

        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "false"
        lis_map, lis_method, lis_segments = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=book.spine_chapters)

        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "true"
        with caplog.at_level("INFO", logger="src.services.alignment_service"):
            seg_map, seg_method, seg_segments = service._generate_alignment_map_with_method(
                book.segments, book.full_text, spine_chapters=book.spine_chapters)

        # Sanity check on the fixture itself: the LIS really did collapse this
        # permutation (same shape as
        # TestFourPastMidnightPermutation.test_lis_can_only_keep_two_of_the_four_blocks).
        assert lis_segments is None

        assert seg_segments is not None
        assert len(seg_segments) == 4, "all four blocks must place"
        assert len(seg_map) > len(lis_map), (
            f"segmented map ({len(seg_map)} anchors) must retain more than "
            f"the LIS map ({len(lis_map)} anchors)"
        )
        # Every spine block's char range must be covered by exactly one
        # placed segment (order-preserving in char, not in ts).
        placed_char_starts = sorted(s.char_start for s in seg_segments)
        expected_char_starts = sorted(r[0] for r in book.char_range.values())
        assert placed_char_starts == expected_char_starts

        assert "narration order differs from spine order" in caplog.text
        assert "Segmented filter" in caplog.text

    def test_segments_json_is_persisted_through_align_and_store(self, service):
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "true"
        os.environ["CONTENT_MATCH_GUARD"] = "false"
        book = FourBlockBook(narration_order=OUT_OF_ORDER_NARRATION)

        assert service.align_and_store(
            "book-out-of-order", [dict(seg) for seg in book.segments], book.full_text,
            book.spine_chapters)

        stored_segments = service._get_segments("book-out-of-order")
        assert stored_segments is not None
        assert len(stored_segments) == 4
        for entry in stored_segments:
            assert set(entry.keys()) == {"char_start", "char_end", "ts_start", "ts_end"}


# --------------------------------------------------------------------------- #
# 4. Flag ON but no (real) spine boundaries: falls back to the LIS, no crash
# --------------------------------------------------------------------------- #

class TestFlagOnWithoutUsableBoundaries:

    @pytest.mark.parametrize("spine_chapters", [None, [], [{"start": 5, "end": 5}]],
                            ids=["none", "empty-list", "zero-width-only"])
    def test_falls_back_to_lis_without_crashing(self, service, spine_chapters):
        os.environ["ALIGNMENT_SEGMENTED_MAPS"] = "true"
        book = FourBlockBook(narration_order=OUT_OF_ORDER_NARRATION)

        alignment_map, method, map_segments = service._generate_alignment_map_with_method(
            book.segments, book.full_text, spine_chapters=spine_chapters)

        assert map_segments is None
        assert method == "lexical"
        assert len(alignment_map) > 0


# --------------------------------------------------------------------------- #
# 5. Setting registration + the recurring "on"-checkbox bug
# --------------------------------------------------------------------------- #

class TestSettingRegistration:

    def test_key_greps_in_all_four_touchpoints(self):
        config_loader_src = (REPO_ROOT / "src" / "utils" / "config_loader.py").read_text(encoding="utf-8")
        web_server_src = (REPO_ROOT / "src" / "web_server.py").read_text(encoding="utf-8")
        settings_html = (REPO_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

        # ALL_SETTINGS + DEFAULT_CONFIG.
        assert config_loader_src.count("ALIGNMENT_SEGMENTED_MAPS") >= 2
        assert "'ALIGNMENT_SEGMENTED_MAPS': 'false'" in config_loader_src

        # bool_keys in the POST /settings handler.
        assert "'ALIGNMENT_SEGMENTED_MAPS'" in web_server_src

        # The settings UI.
        assert 'name="ALIGNMENT_SEGMENTED_MAPS"' in settings_html
        assert "get_bool('ALIGNMENT_SEGMENTED_MAPS')" in settings_html

    def test_true_and_on_both_enable_it(self, monkeypatch):
        for spelling in ("true", "TRUE", "on", "On", "1", "yes"):
            monkeypatch.setenv("ALIGNMENT_SEGMENTED_MAPS", spelling)
            assert AlignmentService.segmented_maps_enabled() is True, spelling

    def test_false_and_unset_both_disable_it(self, monkeypatch):
        monkeypatch.setenv("ALIGNMENT_SEGMENTED_MAPS", "false")
        assert AlignmentService.segmented_maps_enabled() is False

        monkeypatch.delenv("ALIGNMENT_SEGMENTED_MAPS", raising=False)
        assert AlignmentService.segmented_maps_enabled() is False  # documented default


# --------------------------------------------------------------------------- #
# 6. sync_manager passes the EPUB spine, not the audiobook's own chapter
#    marks, into align_and_store
# --------------------------------------------------------------------------- #

def _build_sync_manager(tmp_path):
    """Minimal harness reaching the Whisper -> align_and_store call site
    (src/sync_manager.py, ~line 2543), modeled on
    `test_storyteller_priority_flow._build_manager`."""
    db = MagicMock()
    db.get_books_by_status.return_value = []
    db.update_latest_job.return_value = None
    db.get_latest_job.return_value = MagicMock(retry_count=0, progress=0.0)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {"media": {"chapters": []}}
    abs_client.get_audio_files.return_value = ["audio-1.m4b"]

    transcriber = MagicMock()
    transcriber.transcribe_from_smil = MagicMock(return_value=None)  # force the Whisper path
    transcriber.process_audio = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "x"}])

    ebook_parser = MagicMock()

    alignment_service = MagicMock()
    alignment_service.align_and_store.return_value = True

    manager = SyncManager(
        abs_client=abs_client,
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )

    epub_path = tmp_path / "book.epub"
    epub_path.write_text("dummy", encoding="utf-8")
    manager._get_local_epub = MagicMock(return_value=epub_path)
    return manager, ebook_parser, transcriber, alignment_service


def test_sync_manager_passes_epub_spine_not_audio_chapters_to_align_and_store(tmp_path):
    """Regression test (issue #426 phase 2): `align_and_store`'s fourth
    argument must be the EPUB spine chapters from `extract_text_and_map`, not
    the audiobook's own chapter marks from `get_chapters` — the two are
    unrelated position languages, and passing the audio chapters was a latent
    misnomer harmless only because nothing read the parameter before now."""
    manager, ebook_parser, transcriber, alignment_service = _build_sync_manager(tmp_path)

    spine_chapters = [{"start": 0, "end": 500}, {"start": 500, "end": 1000}]
    audio_chapters = [{"start": 0.0, "end": 999.0, "title": "audio-only chapter mark"}]
    ebook_parser.extract_text_and_map.return_value = ("ebook text " * 100, spine_chapters)

    audio = tmp_path / "a.m4b"
    audio.write_text("x", encoding="utf-8")
    adapter = MagicMock()
    adapter.get_audio_files.return_value = [{"local_path": str(audio)}]
    adapter.get_chapters.return_value = audio_chapters
    manager.audio_source_adapters = {"BookOrbit": adapter}

    book = Book(
        abs_id="spine-vs-audio-1", abs_title="Spine Vs Audio", ebook_filename="book.epub",
        kosync_doc_id="h", status="pending", duration=12.0,
        audio_source="BookOrbit", audio_source_id="5961", sync_mode="audiobook",
    )
    manager._run_background_job(book)

    assert alignment_service.align_and_store.call_count == 1
    args, kwargs = alignment_service.align_and_store.call_args
    passed_chapters = args[3] if len(args) > 3 else kwargs.get("spine_chapters")
    assert passed_chapters == spine_chapters
    assert passed_chapters != audio_chapters
