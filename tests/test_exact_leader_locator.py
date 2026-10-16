"""Regression test: exact leader locator prevents text re-matching drift.

GitHub issue #523, part 1: when the leader's exact position (xpath or cfi) points
to a paragraph whose text also appears again shortly after, the old
get_locator_from_text path could pick the later copy, shifting the follower's
position.

This test suite verifies:
a) KoSync leads with an xpath; Grimmory follower receives a CFI resolving to the
   same index (not the later duplicate).
b) Grimmory leads with a CFI; KoSync follower's xpath resolves to the same index.
c) When the leader's file differs from epub_file_name, get_exact_locator returns
   None and the text-match path is still used (control).
d) Unit: KoSync and Grimmory get_exact_locator return None without xpath/cfi,
   and the interface default returns None.
"""
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import List
from unittest.mock import Mock, patch

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

# The leader's target paragraph text that also appears again shortly after.
# This duplication is what makes find_text_location pick the later copy.
LEADER_PARAGRAPH_TEXT = (
    "This is the leader's exact position in the book. It is unique enough, "
    "but it appears twice to test the re-matching drift."
)

# A second copy of the same text, appearing a bit later in the same chapter.
DUPLICATE_PARAGRAPH_TEXT = LEADER_PARAGRAPH_TEXT

# Filler text between the two copies to create a measurable offset.
FILLER_TEXTS = [
    "Some filler text to create distance between the two paragraphs.",
    "More filler to ensure the offset is significant.",
    "Even more filler to make the drift obvious in tests.",
]

# 0-based index of the LEADER paragraph among <body>'s direct <p> children:
# title, copyright, leader-paragraph, 3 fillers, duplicate-paragraph.
_LEADER_PARAGRAPH_INDEX = 2


def _paragraph_texts() -> List[str]:
    return [
        "Title Page",
        "Copyright 2024 by Someone. All rights reserved.",
        LEADER_PARAGRAPH_TEXT,
        *FILLER_TEXTS,
        DUPLICATE_PARAGRAPH_TEXT,
    ]


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


# KOReader xpath for the LEADER paragraph's text node (single spine item -> DocFragment[1];
# body's 3rd <p> child, 1-based -> p[3]).
XPATH_TO_LEADER = "/body/DocFragment[1]/body/p[3]/text().0"

# CFI pointing to the LEADER paragraph: spine_step=2 (single spine item),
# then element step 4 reaches <body>, then element step (2+1)*2=6 reaches p[3].
CFI_TO_LEADER = "epubcfi(/6/2!/4/6:0)"


class _ExactLocatorTestCase(unittest.TestCase):
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


class TestExactLocatorPreventsDrift(_ExactLocatorTestCase):
    """Test that find_text_location would actually drift on this fixture."""

    def test_find_text_location_picks_later_duplicate(self) -> None:
        """Verify the fixture actually triggers the bug: find_text_location on the
        leader's paragraph text returns the LATER duplicate, not the leader's index."""
        leader_index = self.parser.resolve_xpath_to_index(self.filename, XPATH_TO_LEADER)
        self.assertIsNotNone(leader_index)

        # Extract the text at the leader's xpath
        leader_text = self.parser.resolve_xpath(self.filename, XPATH_TO_LEADER)
        self.assertIsNotNone(leader_text)

        # Now ask find_text_location to find that text; it should pick the later copy
        locator = self.parser.find_text_location(self.filename, leader_text, hint_percentage=0.3)
        self.assertIsNotNone(locator)
        self.assertIsNotNone(locator.match_index)

        # The bug: match_index is > leader_index (drifted to the later duplicate)
        self.assertGreater(
            locator.match_index, leader_index,
            f"find_text_location did not drift: match_index={locator.match_index}, leader_index={leader_index}. "
            "The fixture may need adjustment."
        )


class TestKoSyncLeaderExactLocator(_ExactLocatorTestCase):
    """KoSync leads with xpath; exact locator prevents drift to later duplicate."""

    def test_kosync_exact_locator_returns_correct_index(self) -> None:
        """KoSync's get_exact_locator must return a locator with match_index == leader_index."""
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename
        book.ebook_filename = None

        state = ServiceState(
            current={"pct": 0.3, "xpath": XPATH_TO_LEADER},
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("KoSync", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_kosync_client = Mock()
        client = KoSyncSyncClient(mock_kosync_client, self.parser)

        locator = client.get_exact_locator(book, state, self.filename)
        self.assertIsNotNone(locator)

        leader_index = self.parser.resolve_xpath_to_index(self.filename, XPATH_TO_LEADER)
        self.assertEqual(
            locator.match_index, leader_index,
            f"get_exact_locator returned match_index={locator.match_index}, expected {leader_index}"
        )

    def test_kosync_exact_locator_none_when_file_differs(self) -> None:
        """When epub_file_name differs from the book's file, get_exact_locator returns None."""
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename
        book.ebook_filename = None

        state = ServiceState(
            current={"pct": 0.3, "xpath": XPATH_TO_LEADER},
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("KoSync", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_kosync_client = Mock()
        client = KoSyncSyncClient(mock_kosync_client, self.parser)

        # Different filename -> None
        locator = client.get_exact_locator(book, state, "different.epub")
        self.assertIsNone(locator)

    def test_kosync_exact_locator_none_without_xpath(self) -> None:
        """KoSync returns None when state has no xpath."""
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename

        state = ServiceState(
            current={"pct": 0.3},  # No xpath
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("KoSync", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_kosync_client = Mock()
        client = KoSyncSyncClient(mock_kosync_client, self.parser)

        locator = client.get_exact_locator(book, state, self.filename)
        self.assertIsNone(locator)

    def test_kosync_exact_locator_declines_another_documents_xpath(self) -> None:
        """A GET answered from another hash (possibly another EPUB build) is not an
        exact position in this book's file, even when the XPath resolves (#523 review)."""
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from unittest.mock import Mock

        book = Mock()
        book.kosync_doc_id = "a" * 32
        book.original_ebook_filename = self.filename
        book.ebook_filename = None

        def state_for(metadata: dict):
            kosync_client = Mock()
            kosync_client.get_progress_with_metadata.return_value = (0.3, XPATH_TO_LEADER, metadata)
            client = KoSyncSyncClient(kosync_client, self.parser)
            return client, client.get_service_state(book, None)

        client, foreign = state_for({"_bridge_position_document": "b" * 32})
        self.assertEqual(foreign.current["_kosync_position_document"], "b" * 32)
        self.assertIsNone(client.get_exact_locator(book, foreign, self.filename))

        client, own = state_for({})
        locator = client.get_exact_locator(book, own, self.filename)
        self.assertIsNotNone(locator)
        self.assertEqual(
            locator.match_index,
            self.parser.resolve_xpath_to_index(self.filename, XPATH_TO_LEADER),
        )


class TestGrimmoryLeaderExactLocator(_ExactLocatorTestCase):
    """Grimmory leads with cfi; exact locator prevents drift to later duplicate."""

    def test_grimmory_exact_locator_returns_correct_index(self) -> None:
        """Grimmory's get_exact_locator must return a locator with match_index == leader_index."""
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename
        book.ebook_filename = None

        state = ServiceState(
            current={"pct": 0.3, "cfi": CFI_TO_LEADER},
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_booklore_client = Mock()
        client = BookloreSyncClient(mock_booklore_client, self.parser)

        locator = client.get_exact_locator(book, state, self.filename)
        self.assertIsNotNone(locator)

        leader_index = self.parser.resolve_cfi_to_index(self.filename, CFI_TO_LEADER)
        self.assertEqual(
            locator.match_index, leader_index,
            f"get_exact_locator returned match_index={locator.match_index}, expected {leader_index}"
        )

    def test_grimmory_exact_locator_none_when_file_differs(self) -> None:
        """When epub_file_name differs from the book's file, get_exact_locator returns None."""
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename

        state = ServiceState(
            current={"pct": 0.3, "cfi": CFI_TO_LEADER},
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_booklore_client = Mock()
        client = BookloreSyncClient(mock_booklore_client, self.parser)

        locator = client.get_exact_locator(book, state, "different.epub")
        self.assertIsNone(locator)

    def test_grimmory_exact_locator_none_without_cfi(self) -> None:
        """Grimmory returns None when state has no cfi."""
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        from src.sync_clients.sync_client_interface import ServiceState
        from unittest.mock import Mock

        book = Mock()
        book.original_ebook_filename = self.filename

        state = ServiceState(
            current={"pct": 0.3},  # No cfi
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        mock_booklore_client = Mock()
        client = BookloreSyncClient(mock_booklore_client, self.parser)

        locator = client.get_exact_locator(book, state, self.filename)
        self.assertIsNone(locator)



def _run_cycle_capturing_logs(manager) -> logging.Handler:
    """Run one sync cycle and return a handler holding every src.sync_manager record.

    Earlier tests in the full suite change this logger's level or disable it, so the
    capture forces DEBUG and enabled for the cycle and restores both afterwards.
    """
    handler = logging.Handler()
    handler.records = []
    handler.emit = lambda r: handler.records.append(r)
    sync_logger = logging.getLogger("src.sync_manager")
    old_level, old_disabled = sync_logger.level, sync_logger.disabled
    sync_logger.setLevel(logging.DEBUG)
    sync_logger.disabled = False
    sync_logger.addHandler(handler)
    try:
        manager.sync_cycle()
    finally:
        sync_logger.removeHandler(handler)
        sync_logger.setLevel(old_level)
        sync_logger.disabled = old_disabled
    return handler


class TestSyncCycleKoSyncLeader(BaseSyncCycleTestCase):
    """Full sync cycle: KoSync leads, Grimmory follower receives exact position."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-exact-locator',
            'abs_title': 'Exact Locator Test Book',
            'kosync_doc_id': 'test-kosync-doc-exact-locator',
            'ebook_filename': 'book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'unused_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        return {
            'kosync': {'pct': 0.20, 'last_updated': 1234567890},
            'booklore': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        # The leader paragraph is at index 2 of 7 top-level paragraphs, roughly 30%
        return 0.3

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 0.0, 'duration': 1000},
            'abs_in_progress': [],
            'kosync_progress': (self.get_expected_final_percentage(), XPATH_TO_LEADER),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),  # Grimmory starts with no position
        }

    def setUp(self) -> None:
        BaseSyncCycleTestCase.setUp(self)
        # Replace the dummy EPUB with the real crafted one
        _write_epub(Path(self.temp_dir) / 'books' / 'book.epub')

    def _build_manager(self):
        mocks = self.setup_common_mocks()
        mocks['abs_client'].is_configured.return_value = False
        mocks['storyteller_client'].is_configured.return_value = False

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

    def test_kosync_leader_grimmory_follower_exact_locator(self) -> None:
        """KoSync leads with xpath; Grimmory receives a CFI resolving to the same index."""
        manager, mocks = self._build_manager()

        # Capture logs to check locator_source
        handler = _run_cycle_capturing_logs(manager)

        # Check that exact_locator was used
        exact_locator_logs = [
            r for r in handler.records
            if "exact_locator" in r.getMessage()
        ]
        self.assertTrue(
            len(exact_locator_logs) > 0,
            "No exact_locator log found; the fix may not be active"
        )

        # Verify Grimmory was updated
        self.assertTrue(
            mocks['booklore_client'].update_progress.called,
            "Grimmory was never pushed a position"
        )
        _bl_id, pct, locator = mocks['booklore_client'].update_progress.call_args[0]

        # Verify the CFI resolves to the same index as the leader's xpath
        parser = EbookParser(
            books_dir=str(Path(self.temp_dir) / 'books'),
            epub_cache_dir=str(Path(self.temp_dir) / 'epub_cache'),
        )
        leader_index = parser.resolve_xpath_to_index("book.epub", XPATH_TO_LEADER)
        grimmory_index = parser.resolve_cfi_to_index("book.epub", locator.cfi)

        self.assertEqual(
            grimmory_index, leader_index,
            f"Grimmory CFI resolves to {grimmory_index}, expected {leader_index} (leader's xpath index)"
        )


class TestSyncCycleGrimmoryLeader(BaseSyncCycleTestCase):
    """Full sync cycle: Grimmory leads, KoSync follower receives exact position."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-grimmory-leader',
            'abs_title': 'Grimmory Leader Test Book',
            'kosync_doc_id': 'test-kosync-doc-grimmory-leader',
            'ebook_filename': 'book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'unused_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        return {
            # Grimmory's stored state is stale, so its fresh progress is the read event
            # that makes it leader (same setup as TestGrimmoryCfiLeaderReachesKoSyncCorrectly).
            'kosync': {'pct': 0.0, 'last_updated': 1234567890},
            'booklore': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "BookLore"

    def get_expected_final_percentage(self):
        return 0.3

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 0.0, 'duration': 1000},
            'abs_in_progress': [],
            'kosync_progress': (0.0, None),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (self.get_expected_final_percentage(), CFI_TO_LEADER),
        }

    def setUp(self) -> None:
        BaseSyncCycleTestCase.setUp(self)
        _write_epub(Path(self.temp_dir) / 'books' / 'book.epub')

    def _build_manager(self):
        mocks = self.setup_common_mocks()
        mocks['abs_client'].is_configured.return_value = False
        mocks['storyteller_client'].is_configured.return_value = False

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

    def test_grimmory_leader_kosync_follower_exact_locator(self) -> None:
        """Grimmory leads with cfi; KoSync receives an xpath resolving to the same index."""
        manager, mocks = self._build_manager()

        handler = _run_cycle_capturing_logs(manager)

        exact_locator_logs = [
            r for r in handler.records
            if "exact_locator" in r.getMessage()
        ]
        self.assertTrue(
            len(exact_locator_logs) > 0,
            "No exact_locator log found"
        )

        self.assertTrue(
            mocks['kosync_client'].update_progress.called,
            "KoSync was never pushed a position"
        )
        _ko_id, pct, xpath = mocks['kosync_client'].update_progress.call_args[0]

        parser = EbookParser(
            books_dir=str(Path(self.temp_dir) / 'books'),
            epub_cache_dir=str(Path(self.temp_dir) / 'epub_cache'),
        )
        leader_index = parser.resolve_cfi_to_index("book.epub", CFI_TO_LEADER)
        kosync_index = parser.resolve_xpath_to_index("book.epub", xpath)

        self.assertEqual(
            kosync_index, leader_index,
            f"KoSync xpath resolves to {kosync_index}, expected {leader_index} (leader's cfi index)"
        )


class TestInterfaceDefault(_ExactLocatorTestCase):
    """The base SyncClient interface returns None for get_exact_locator."""

    def test_interface_default_returns_none(self) -> None:
        from src.sync_clients.sync_client_interface import SyncClient, ServiceState
        from unittest.mock import Mock

        book = Mock()
        state = ServiceState(
            current={"pct": 0.3, "xpath": XPATH_TO_LEADER},
            previous_pct=0.2,
            delta=0.1,
            threshold=0.01,
            is_configured=True,
            display=("Test", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%",
        )

        client = SyncClient(self.parser)
        locator = client.get_exact_locator(book, state, self.filename)
        self.assertIsNone(locator)


if __name__ == "__main__":
    unittest.main()