"""Regression test: `resolve_xpath` / `get_text_around_cfi` must locate TEXT
via the same canonical `_to_index` resolver their index-returning twins use,
not a raw first-occurrence text search.

Real failure (book "Home Temptation: Volume 1", an EPUB that wraps each
opening curly quote in its own <span>):

1. KOReader leader, xpath
   ``/body/DocFragment[5]/body/div/p[61]/span[1]/text().0`` -- the target
   node is a <span> holding only "“". `resolve_xpath`'s anchor is that
   quote mark, whose FIRST occurrence in the chapter is the story's opening
   line of dialogue, so it returned text at 0.6% of the book instead of the
   reader's actual 12.2% (`resolve_xpath_to_index` already returned the
   correct 13630 for the same xpath). The sync then wrote BookOrbit to the
   wrong position:
   ``KoSync leads at 13.0100%`` -> ``Updated state data for 'BookOrbit':
   {'pct': 0.00632621746036019, 'cfi': 'epubcfi(/6/10!/4/2/16/2:0)'}``.

2. BookOrbit leader, CFI ``epubcfi(/6/10!/4/2,/276,/282/2/1:316)`` (a range
   CFI reducing to the range start ``/6/10!/4/2/276``). The walk reaches the
   right <p> (`“Wow, this place is packed” he yelled...`), but
   BeautifulSoup's ``separator=' '`` rendering inserts a space after the
   <span>, giving ``“ Wow...`` -- so the substring search misses
   entirely and `get_text_around_cfi` fell back to offset 0 (start of that
   spine item), tripping the collapse guard:
   ``Resolved locator collapsed to start-of-book (0%) while leader
   'BookOrbit' is at 29.5021% (source=fuzzy_text) -- treating as a failed
   locator resolution; skipping cross-client write to preserve existing
   progress``.

`resolve_cfi_to_index` already has the fix (commit f54f301,
`_canonical_offset_of_element`, document-order placement when the anchor is
missing or repeated in the BS4 chapter text). This test pins the mirrored fix
in `resolve_xpath` (via `resolve_xpath_to_index`) and `get_text_around_cfi`
(via `resolve_cfi_to_index`), plus the fallback path when a `_to_index`
resolver can't resolve at all.
"""
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import List
from unittest.mock import Mock, patch

from src.utils.ebook_dom_map import strip_inline_joiner
from src.utils.ebook_utils import EbookParser
from tests.base_sync_test import BaseSyncCycleTestCase

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

_XHTML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
    '<body>{body}</body></html>'
)

TITLE_TEXT = "Title Page"
COPYRIGHT_TEXT = "Copyright 2024 by Someone. All rights reserved."
# Both quote-opening paragraphs wrap the opening curly quote in its own
# <span>, exactly like the real book that triggered this bug.
EARLY_QUOTE_TEXT = (
    "<span>“</span>Try giving it some more gas this time, bro,” "
    "Jake said with a grin, gunning the engine before anyone could answer."
)
LATE_QUOTE_TEXT = (
    "<span>“</span>How are we going to make this work, mom?” "
    "Dave asked quietly, not sure he wanted to hear the answer."
)
FILLER_TEXTS = [
    "The road stretched on for miles with nothing but fields on either side.",
    "Nobody spoke for a while after that, each lost in their own thoughts.",
    "Eventually the silence gave way to the hum of tires on gravel.",
]

# 0-based index of the LATE paragraph among <body>'s direct <p> children:
# title, copyright, early-quote, 3 fillers, late-quote.
_LATE_PARAGRAPH_INDEX = 6


def _paragraph_texts() -> List[str]:
    return [TITLE_TEXT, COPYRIGHT_TEXT, EARLY_QUOTE_TEXT, *FILLER_TEXTS, LATE_QUOTE_TEXT]


def _write_epub(path: Path) -> None:
    body = "".join(f"<p>{text}</p>" for text in _paragraph_texts())
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        '<dc:identifier id="id">x</dc:identifier></metadata>'
        '<manifest><item id="i0" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="i0"/></spine></package>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", _XHTML.format(body=body))


# KOReader xpath for the LATE paragraph's opening-quote span (single spine
# item -> DocFragment[1]; body's 7th <p> child, 1-based -> p[7]).
XPATH_TO_LATE_SPAN = "/body/DocFragment[1]/body/p[7]/span[1]/text().0"

# CFI element steps mirror the xpath: spine_step=2 (single spine item),
# then element step 4 reaches <body> (its 2nd element child under <html>),
# then element step (index+1)*2 reaches the target <p> among body's children.
_LATE_STEP = (_LATE_PARAGRAPH_INDEX + 1) * 2
CFI_POINT_TO_LATE_PARAGRAPH = f"epubcfi(/6/2!/4/{_LATE_STEP}:0)"
# Range CFI (comma form): parent reaches <body> only, start suffix continues
# to the target <p> -- mirrors the real book's range CFI that reduced to its
# range-start path.
CFI_RANGE_TO_LATE_PARAGRAPH = f"epubcfi(/6/2!/4,/{_LATE_STEP},/{_LATE_STEP}/1:5)"


class _TextAtLocatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        (tmp / "books").mkdir()
        (tmp / "cache").mkdir()
        self.parser = EbookParser(books_dir=str(tmp / "books"), epub_cache_dir=str(tmp / "cache"))
        self.filename = "book.epub"
        _write_epub(tmp / "books" / self.filename)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestResolveXpathUsesCanonicalOffset(_TextAtLocatorTestCase):
    def test_xpath_to_span_only_quote_lands_on_later_paragraph(self) -> None:
        """The old first-occurrence search would return the EARLY paragraph's
        text (the quote mark's first occurrence in the chapter); the fix
        must return the LATE paragraph instead, matching the canonical index
        resolver exactly."""
        expected_index = self.parser.resolve_xpath_to_index(self.filename, XPATH_TO_LATE_SPAN)
        self.assertIsNotNone(expected_index)

        text = self.parser.resolve_xpath(self.filename, XPATH_TO_LATE_SPAN)
        self.assertIsNotNone(text)
        self.assertIn("How are we going to make this work", text)
        self.assertNotIn("Try giving it some more gas", text)

        full_text, _ = self.parser.extract_text_and_map(self.parser.resolve_book_path(self.filename))
        expected_text = strip_inline_joiner(full_text[expected_index:expected_index + 600])
        self.assertEqual(text, expected_text)

    def test_fallback_used_when_canonical_resolver_returns_none(self) -> None:
        """When `resolve_xpath_to_index` can't resolve at all, `resolve_xpath`
        must still fall through to its historic text-search path rather than
        return None."""
        with patch.object(EbookParser, "resolve_xpath_to_index", return_value=None):
            text = self.parser.resolve_xpath(self.filename, XPATH_TO_LATE_SPAN)
        self.assertIsNotNone(text)
        # The fallback's first-occurrence search anchors on the bare quote
        # mark and lands on the EARLY paragraph -- pinning that this really
        # is the old code path, not a lucky coincidence.
        self.assertIn("Try giving it some more gas", text)


class TestGetTextAroundCfiUsesCanonicalOffset(_TextAtLocatorTestCase):
    def test_point_cfi_to_paragraph_lands_on_later_paragraph(self) -> None:
        """The <p> itself is the CFI target (no span descent): BS4's
        separator=' ' rendering inserts a space after the leading <span> that
        the paragraph's raw lxml text_content() doesn't have, so the old
        substring search misses and fell back to spine-item start (0%). The
        fix must still land on the LATE paragraph."""
        expected_index = self.parser.resolve_cfi_to_index(self.filename, CFI_POINT_TO_LATE_PARAGRAPH)
        self.assertIsNotNone(expected_index)

        text = self.parser.get_text_around_cfi(self.filename, CFI_POINT_TO_LATE_PARAGRAPH, context=50)
        self.assertIsNotNone(text)
        self.assertIn("How are we going to make this work", text)
        self.assertNotIn("Title Page", text)
        self.assertNotIn("Copyright", text)

        full_text, _ = self.parser.extract_text_and_map(self.parser.resolve_book_path(self.filename))
        expected_text = strip_inline_joiner(full_text[max(0, expected_index - 50):expected_index + 50])
        self.assertEqual(text, expected_text)

    def test_range_cfi_comma_form_lands_on_later_paragraph(self) -> None:
        text = self.parser.get_text_around_cfi(self.filename, CFI_RANGE_TO_LATE_PARAGRAPH, context=50)
        self.assertIsNotNone(text)
        self.assertIn("How are we going to make this work", text)
        self.assertNotIn("Title Page", text)
        self.assertNotIn("Copyright", text)

    def test_fallback_used_when_canonical_resolver_returns_none(self) -> None:
        """When `resolve_cfi_to_index` can't resolve at all, `get_text_around_cfi`
        must still fall through to its historic walk-and-search path rather
        than return None."""
        with patch.object(EbookParser, "resolve_cfi_to_index", return_value=None):
            text = self.parser.get_text_around_cfi(self.filename, CFI_POINT_TO_LATE_PARAGRAPH, context=50)
        self.assertIsNotNone(text)


class _CollectingHandler(logging.Handler):
    """Captures log records without depending on assertLogs (which requires
    at least one matching record, but the whole point here is that the
    collapse-guard warning must NOT fire)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestGrimmoryCfiLeaderReachesKoSyncCorrectly(BaseSyncCycleTestCase):
    """Ebook-only sync cycle with the REAL EbookParser on the crafted EPUB.

    Grimmory (internal key ``BookLore``) leads with the CFI pointing at the
    late paragraph -- the exact `get_text_around_cfi` bug (case 2 above).
    BookOrbit is not wired into `BaseSyncCycleTestCase`'s harness, but it
    calls the identical `get_text_around_cfi` (`booklore_sync_client.py:243`
    and `bookorbit_sync_client.py` both do), so Grimmory exercises the same
    choke point the real BookOrbit incident hit.

    Before the fix: `get_text_around_cfi` returns the title/copyright text at
    offset 0, `get_locator_from_text` fuzzy-matches that near the start of
    the book, and the collapse guard fires and skips the cross-client write.
    After the fix: it resolves near the leader's actual (late-paragraph)
    position, no collapse warning fires, and KoSync is pushed a matching
    high percentage instead of being left at its stale low one.
    """

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-cfi-span-anchor',
            'abs_title': 'CFI Span Anchor Test Book',
            'kosync_doc_id': 'test-kosync-doc-cfi-span-anchor',
            'ebook_filename': 'book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'unused_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        # KoSync's stored state already matches what this cycle's fetch
        # returns (delta 0) -- it is the stale sibling, not the leader.
        # Grimmory's stored state is far from its fresh fetched value, so it
        # reads as this cycle's change and becomes the leader.
        return {
            'kosync': {'pct': 0.20, 'last_updated': 1234567890},
            'booklore': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "BookLore"

    def get_expected_final_percentage(self):
        # The late paragraph is the last of 7 top-level paragraphs in the
        # fixture -- well past the midpoint of the book.
        return 0.7

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 0.0, 'duration': 1000},
            'abs_in_progress': [],
            'kosync_progress': (0.20, "/body/DocFragment[1]/body/p[2]/text().0"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (self.get_expected_final_percentage(), CFI_POINT_TO_LATE_PARAGRAPH),
        }

    def setUp(self) -> None:
        super().setUp()
        # Replace BaseSyncCycleTestCase's dummy placeholder ebook with the
        # real crafted EPUB so the REAL EbookParser has something to parse.
        _write_epub(Path(self.temp_dir) / 'books' / 'book.epub')

    def _build_manager(self):
        mocks = self.setup_common_mocks()
        # Ebook-only mapping: no audiobook leader in play.
        mocks['abs_client'].is_configured.return_value = False
        mocks['storyteller_client'].is_configured.return_value = False

        # The crux of this test: a REAL EbookParser on the real crafted EPUB,
        # not a Mock, so get_text_around_cfi runs its actual (fixed) logic.
        real_ebook_parser = EbookParser(
            books_dir=str(Path(self.temp_dir) / 'books'),
            epub_cache_dir=str(Path(self.temp_dir) / 'epub_cache'),
        )
        mocks['ebook_parser'] = real_ebook_parser

        from src.sync_manager import SyncManager
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        transcriber = Mock()

        abs_sync_client = ABSSyncClient(mocks['abs_client'], transcriber, real_ebook_parser)
        kosync_sync_client = KoSyncSyncClient(mocks['kosync_client'], real_ebook_parser)
        abs_ebook_sync_client = ABSEbookSyncClient(mocks['abs_client'], real_ebook_parser)
        storyteller_sync_client = StorytellerSyncClient(mocks['storyteller_client'], real_ebook_parser)
        booklore_sync_client = BookloreSyncClient(mocks['booklore_client'], real_ebook_parser)

        manager = SyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=real_ebook_parser,
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": abs_sync_client,
                "ABS eBook": abs_ebook_sync_client,
                "KoSync": kosync_sync_client,
                "Storyteller": storyteller_sync_client,
                "BookLore": booklore_sync_client,
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        return manager, mocks

    def test_no_collapse_guard_and_kosync_reaches_late_paragraph(self) -> None:
        manager, mocks = self._build_manager()

        handler = _CollectingHandler()
        sync_logger = logging.getLogger("src.sync_manager")
        sync_logger.addHandler(handler)
        try:
            manager.sync_cycle()
        finally:
            sync_logger.removeHandler(handler)

        collapse_warnings = [
            r.getMessage() for r in handler.records
            if "collapsed to start-of-book" in r.getMessage()
        ]
        self.assertEqual(
            collapse_warnings, [],
            f"Collapse guard fired unexpectedly (the pre-fix symptom): {collapse_warnings}",
        )

        self.assertTrue(
            mocks['kosync_client'].update_progress.called,
            "KoSync was never pushed a position",
        )
        _ko_id, pct, xpath = mocks['kosync_client'].update_progress.call_args[0]
        self.assertGreater(
            pct, 0.5,
            f"KoSync was pushed {pct:.4%}, near start-of-book instead of the late paragraph",
        )
        self.assertIsNotNone(xpath)


if __name__ == "__main__":
    unittest.main()
