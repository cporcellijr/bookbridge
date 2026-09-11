"""Regression tests: an alignment map speaks ONE EPUB's character space.

A book can carry two EPUBs whose text differs. `ebook_filename` is replaced by a
Storyteller artifact when a readalong is matched, and `original_ebook_filename`
preserves what was there before. The alignment map is fitted against whichever
file was current at forge time, so the map may be anchored to either one.

Resolving a client's position against one file and then looking that offset up
in a map fitted to the other is silently wrong by the difference between them.
Measured on the live library, of 29 books carrying two differing EPUBs, 18 are
decidable and they split BOTH ways:

    Jonathan Strange & Mr Norrell   map = ORIGINAL   artifact is 5,683 chars short -> 338.2s
    When the Moon Hits Your Eye     map = ARTIFACT   original is   655 chars short ->  49.0s

so no fixed choice of EPUB is correct. The map's own terminal anchor identifies
which file it speaks, and offsets are translated into that space by text.

Covers:
1. `get_map_terminal_char` returns the map's last anchor.
2. `_get_alignment_epub_filename` picks the EPUB whose length matches, in both
   directions, returns the sole candidate untouched when a book has one EPUB,
   and returns None rather than guessing when nothing matches.
3. `_translate_char_offset_between_epubs` recovers a front-matter shift in both
   directions, is identity for one EPUB, and returns None for a foreign text.
4. The headline regression: a KoSync offset is looked up in the map at the
   TRANSLATED offset, not the raw one.
"""

import json
import unittest
from unittest.mock import MagicMock

from src.db.models import Book
from src.services.alignment_service import AlignmentService
from src.sync_manager import SyncManager
from src.utils.polisher import Polisher


# A shared body of text, with the artifact carrying extra front matter. The
# shift is a constant for every offset after it -- exactly the real shape.
FRONT_MATTER = "PUBLISHER BOILERPLATE. " * 20          # 460 chars
BODY = "".join(f"Sentence number {i:05d} of the shared body text. " for i in range(400))
ORIGINAL_TEXT = BODY
ARTIFACT_TEXT = FRONT_MATTER + BODY
SHIFT = len(FRONT_MATTER)

ORIGINAL_EPUB = "Real Book (2019).epub"
ARTIFACT_EPUB = "storyteller_abcd1234.epub"


def _make_manager(alignment_service=None, texts=None):
    db = MagicMock()
    db.get_books_by_status.return_value = []
    parser = MagicMock()
    parser.locator_roundtrip_tolerance = 2

    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir="/tmp/epub_cache",
        data_dir="/tmp",
        books_dir="/tmp/books",
    )
    if texts is not None:
        manager._get_cached_ebook_text = lambda name: (texts[name], len(texts[name]))
        manager._get_local_epub = lambda name: f"/tmp/{name}" if name in texts else None
    return manager


def _make_book(ebook_filename=ARTIFACT_EPUB, original_ebook_filename=ORIGINAL_EPUB):
    return Book(
        abs_id="abs-1",
        abs_title="Two EPUBs",
        ebook_filename=ebook_filename,
        original_ebook_filename=original_ebook_filename,
        status="active",
    )


def _alignment_service_with_map(alignment_map):
    db = MagicMock()
    session = db.get_session()
    session.__enter__.return_value = session
    entry = MagicMock()
    entry.alignment_map_json = json.dumps(alignment_map)
    entry.segments_json = None
    session.query.return_value.filter_by.return_value.first.return_value = entry
    return AlignmentService(database_service=db, polisher=Polisher())


class TestMapTerminalChar(unittest.TestCase):
    """Coverage 1."""

    def test_returns_the_last_anchor_char(self):
        service = _alignment_service_with_map(
            [{"char": 0, "ts": 0.0}, {"char": 500, "ts": 50.0}, {"char": 9999, "ts": 900.0}]
        )
        self.assertEqual(service.get_map_terminal_char("abs-1"), 9999)

    def test_returns_none_without_a_map(self):
        service = AlignmentService(database_service=MagicMock(), polisher=Polisher())
        service._get_alignment = MagicMock(return_value=None)
        self.assertIsNone(service.get_map_terminal_char("abs-1"))


class TestAlignmentEpubSelection(unittest.TestCase):
    """Coverage 2."""

    def setUp(self):
        self.texts = {ORIGINAL_EPUB: ORIGINAL_TEXT, ARTIFACT_EPUB: ARTIFACT_TEXT}

    def _manager_with_terminal(self, terminal):
        service = MagicMock()
        service.get_map_terminal_char.return_value = terminal
        return _make_manager(alignment_service=service, texts=self.texts)

    def test_picks_the_artifact_when_the_map_is_artifact_length(self):
        manager = self._manager_with_terminal(len(ARTIFACT_TEXT))
        self.assertEqual(manager._get_alignment_epub_filename(_make_book()), ARTIFACT_EPUB)

    def test_picks_the_original_when_the_map_is_original_length(self):
        """The Jonathan Strange direction: the map predates the artifact."""
        manager = self._manager_with_terminal(len(ORIGINAL_TEXT))
        self.assertEqual(manager._get_alignment_epub_filename(_make_book()), ORIGINAL_EPUB)

    def test_single_epub_book_returns_it_without_consulting_the_map(self):
        manager = self._manager_with_terminal(len(ORIGINAL_TEXT))
        book = _make_book(ebook_filename=ORIGINAL_EPUB, original_ebook_filename=None)
        self.assertEqual(manager._get_alignment_epub_filename(book), ORIGINAL_EPUB)
        manager.alignment_service.get_map_terminal_char.assert_not_called()

    def test_identical_filenames_are_deduped_to_one_candidate(self):
        manager = self._manager_with_terminal(len(ORIGINAL_TEXT))
        book = _make_book(ebook_filename=ORIGINAL_EPUB, original_ebook_filename=ORIGINAL_EPUB)
        self.assertEqual(manager._get_alignment_epub_filename(book), ORIGINAL_EPUB)
        manager.alignment_service.get_map_terminal_char.assert_not_called()

    def test_returns_none_rather_than_guessing_when_nothing_matches(self):
        """A map whose last anchor falls short of both files -- real for
        Storyteller-method maps. Callers keep their existing EPUB choice."""
        manager = self._manager_with_terminal(len(ORIGINAL_TEXT) - 5000)
        self.assertIsNone(manager._get_alignment_epub_filename(_make_book()))

    def test_returns_none_with_no_alignment_service(self):
        manager = _make_manager(alignment_service=None, texts=self.texts)
        self.assertIsNone(manager._get_alignment_epub_filename(_make_book()))


class TestOffsetTranslation(unittest.TestCase):
    """Coverage 3."""

    def setUp(self):
        self.texts = {ORIGINAL_EPUB: ORIGINAL_TEXT, ARTIFACT_EPUB: ARTIFACT_TEXT}
        self.manager = _make_manager(texts=self.texts)

    def test_original_to_artifact_recovers_the_front_matter_shift(self):
        for offset in (1000, 5000, 12000):
            with self.subTest(offset=offset):
                got = self.manager._translate_char_offset_between_epubs(
                    ORIGINAL_EPUB, ARTIFACT_EPUB, offset
                )
                self.assertEqual(got, offset + SHIFT)

    def test_artifact_to_original_recovers_it_in_reverse(self):
        for offset in (1000 + SHIFT, 5000 + SHIFT, 12000 + SHIFT):
            with self.subTest(offset=offset):
                got = self.manager._translate_char_offset_between_epubs(
                    ARTIFACT_EPUB, ORIGINAL_EPUB, offset
                )
                self.assertEqual(got, offset - SHIFT)

    def test_same_epub_is_identity_and_reads_nothing(self):
        manager = _make_manager(texts=self.texts)
        manager._get_cached_ebook_text = MagicMock(side_effect=AssertionError("must not read"))
        self.assertEqual(
            manager._translate_char_offset_between_epubs(ORIGINAL_EPUB, ORIGINAL_EPUB, 4242),
            4242,
        )

    def test_returns_none_for_a_genuinely_different_edition(self):
        texts = {ORIGINAL_EPUB: ORIGINAL_TEXT, "other.epub": "completely unrelated prose " * 500}
        manager = _make_manager(texts=texts)
        self.assertIsNone(
            manager._translate_char_offset_between_epubs(ORIGINAL_EPUB, "other.epub", 5000)
        )

    def test_offset_at_the_very_end_still_translates(self):
        got = self.manager._translate_char_offset_between_epubs(
            ORIGINAL_EPUB, ARTIFACT_EPUB, len(ORIGINAL_TEXT) - 1
        )
        self.assertEqual(got, len(ARTIFACT_TEXT) - 1)


class TestNormalizationUsesTheMapsSpace(unittest.TestCase):
    """Coverage 4 -- the headline regression.

    A client's offset resolved in the ORIGINAL must be looked up in the map at
    the ARTIFACT offset when the map is artifact-anchored. Before this fix the
    raw offset went straight into `get_time_for_text`."""

    def _run_normalization(self, terminal_char):
        """Drive the real `_normalize_for_cross_format_comparison` with KoSync
        reading the ORIGINAL and the map anchored per `terminal_char`."""
        alignment_service = MagicMock()
        alignment_service.get_map_terminal_char.return_value = terminal_char
        alignment_service.get_time_for_text.return_value = 1234.5

        texts = {ORIGINAL_EPUB: ORIGINAL_TEXT, ARTIFACT_EPUB: ARTIFACT_TEXT}
        manager = _make_manager(alignment_service=alignment_service, texts=texts)

        audio = MagicMock()
        audio.get_supported_sync_types.return_value = {"audiobook"}
        kosync = MagicMock()
        kosync.get_supported_sync_types.return_value = {"audiobook", "ebook"}
        manager.sync_clients = {"ABS": audio, "KoSync": kosync}
        manager._get_primary_audio_client_name = MagicMock(return_value="ABS")
        # KoSync reads the ORIGINAL, as `_get_epub_for_client` gives every
        # non-Storyteller client.
        manager._get_epub_for_client = lambda book, name: ORIGINAL_EPUB
        manager.ebook_parser.resolve_xpath_to_index.return_value = self.client_offset

        book = _make_book()
        book.transcript_file = "DB_MANAGED"
        config = {
            "ABS": MagicMock(current={"pct": 0.5, "ts": 1200.0}),
            "KoSync": MagicMock(current={"pct": 0.5, "xpath": "/body/p[1]/text().0"}),
        }
        manager._normalize_for_cross_format_comparison(book, config)
        return alignment_service

    def setUp(self):
        self.client_offset = 7000

    def test_client_offset_is_translated_into_an_artifact_anchored_map(self):
        """The regression: KoSync's offset is in the ORIGINAL, the map is in the
        ARTIFACT, so the lookup must use the SHIFTED offset."""
        alignment_service = self._run_normalization(terminal_char=len(ARTIFACT_TEXT))

        alignment_service.get_time_for_text.assert_called_once()
        hint = alignment_service.get_time_for_text.call_args.kwargs["char_offset_hint"]
        self.assertEqual(hint, self.client_offset + SHIFT)

    def test_offset_is_left_alone_when_the_map_matches_the_clients_epub(self):
        """Same book, map anchored to the ORIGINAL: no translation, and the
        raw offset is what the map is asked about."""
        alignment_service = self._run_normalization(terminal_char=len(ORIGINAL_TEXT))

        alignment_service.get_time_for_text.assert_called_once()
        hint = alignment_service.get_time_for_text.call_args.kwargs["char_offset_hint"]
        self.assertEqual(hint, self.client_offset)


if __name__ == "__main__":
    unittest.main(verbosity=2)
