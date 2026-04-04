import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.sync_client_interface import LocatorResult, ServiceState
from src.sync_manager import SyncManager


def _state(current: dict) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=0.0,
        delta=0.0,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
    )


class _LeaderClient:
    def can_be_leader(self):
        return True

    def get_supported_sync_types(self):
        return {'audiobook', 'ebook'}


class _DeterministicAlignmentService:
    def __init__(self, chars_per_second: float = 4.0):
        self.chars_per_second = chars_per_second

    def get_time_for_text(self, abs_id: str, query_text: str, char_offset_hint: int = None):
        if char_offset_hint is None:
            return None
        return float(char_offset_hint) / self.chars_per_second

    def get_char_for_time(self, abs_id: str, timestamp: float):
        return int(round(float(timestamp) * self.chars_per_second))


class _DeterministicParser:
    def __init__(self, text_len: int = 5000):
        self.full_text = "a" * text_len
        self.locator_roundtrip_tolerance = 2

    def _clamp(self, offset: int):
        return max(0, min(int(offset), len(self.full_text) - 1))

    def _xpath_for_offset(self, offset: int):
        idx = self._clamp(offset) + 1
        return f"/body/DocFragment[1]/body/p[{idx}]/text().0"

    def _cfi_for_offset(self, offset: int):
        idx = self._clamp(offset)
        return f"epubcfi(/6/2!/4/{idx}:0)"

    def resolve_book_path(self, filename):
        return filename

    def extract_text_and_map(self, filepath):
        return self.full_text, []

    def resolve_xpath_to_index(self, filename, xpath_str):
        if not xpath_str:
            return None
        m = re.search(r"/p\[(\d+)\]/text\(\)\.0$", xpath_str)
        if not m:
            return None
        return self._clamp(int(m.group(1)) - 1)

    def resolve_cfi_to_index(self, filename, cfi):
        if not cfi:
            return None
        m = re.search(r"/4/(\d+):", cfi)
        if not m:
            return None
        return self._clamp(int(m.group(1)))

    def resolve_locator_id(self, filename, href, frag):
        return None

    def get_sentence_level_ko_xpath(self, filename, percentage):
        offset = int(float(percentage) * (len(self.full_text) - 1))
        return self._xpath_for_offset(offset)

    def get_locator_from_char_offset(self, filename, char_offset: int):
        idx = self._clamp(char_offset)
        pct = idx / float(len(self.full_text) - 1)
        xpath = self._xpath_for_offset(idx)
        return LocatorResult(
            percentage=pct,
            xpath=xpath,
            perfect_ko_xpath=xpath,
            cfi=self._cfi_for_offset(idx),
            match_index=idx,
            href="chapter.xhtml",
            fragment="frag-1",
        )


def _manager_with_deterministic_mapping():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = _DeterministicParser()
    manager.alignment_service = _DeterministicAlignmentService(chars_per_second=4.0)
    manager.booklore_client = MagicMock()
    manager.booklore_client.find_book_by_filename.return_value = None
    manager.books_dir = None
    manager.epub_cache_dir = Path("/tmp/epub_cache")
    manager._sync_cycle_local_epub_cache = {}
    # Make _get_local_epub return a dummy path so normalization can proceed
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager.sync_clients = {"ABS": _LeaderClient(), "KoSync": _LeaderClient()}
    manager.cross_format_deadband_seconds = 2.0
    manager._sync_cycle_ebook_cache = {}
    return manager


def test_repeated_ebook_to_time_to_ebook_cycle_drift_bound():
    manager = _manager_with_deterministic_mapping()
    parser = manager.ebook_parser
    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    total_len = len(parser.full_text)

    start_offset = 1379
    current_offset = start_offset

    for _ in range(25):
        current_ts = manager.alignment_service.get_time_for_text(
            book.abs_id, "unused", char_offset_hint=current_offset
        )
        config = {
            "ABS": _state({"ts": current_ts}),
            "KoSync": _state(
                {
                    "pct": current_offset / float(total_len - 1),
                    "xpath": parser._xpath_for_offset(current_offset),
                }
            ),
        }
        normalized = manager._normalize_for_cross_format_comparison(book, config)
        assert normalized is not None

        locator, _ = manager._resolve_alignment_locator_from_abs_timestamp(book, normalized["KoSync"])
        assert locator is not None

        current_offset = parser.resolve_xpath_to_index(
            book.ebook_filename, locator.perfect_ko_xpath or locator.xpath
        )

    assert abs(current_offset - start_offset) <= 2


def test_repeated_time_to_locator_to_time_cycle_drift_bound():
    manager = _manager_with_deterministic_mapping()
    parser = manager.ebook_parser
    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    total_len = len(parser.full_text)

    start_ts = 321.75
    current_ts = start_ts

    for _ in range(25):
        locator, _ = manager._resolve_alignment_locator_from_abs_timestamp(book, current_ts)
        assert locator is not None

        current_offset = parser.resolve_xpath_to_index(
            book.ebook_filename, locator.perfect_ko_xpath or locator.xpath
        )
        config = {
            "ABS": _state({"ts": current_ts}),
            "KoSync": _state(
                {
                    "pct": current_offset / float(total_len - 1),
                    "xpath": locator.perfect_ko_xpath or locator.xpath,
                }
            ),
        }
        normalized = manager._normalize_for_cross_format_comparison(book, config)
        assert normalized is not None
        current_ts = normalized["KoSync"]

    assert abs(current_ts - start_ts) <= 1.0


def test_no_leader_micro_bounce_inside_deadband():
    manager = _manager_with_deterministic_mapping()
    manager._has_significant_delta = MagicMock(return_value=True)
    manager._normalize_for_cross_format_comparison = MagicMock(
        side_effect=[
            {"ABS": 1000.0, "KoSync": 1000.2},
            {"ABS": 1000.0, "KoSync": 1001.3},
            {"ABS": 1000.0, "KoSync": 1001.9},
            {"ABS": 1000.0, "KoSync": 1000.7},
            {"ABS": 1000.0, "KoSync": 1001.8},
            {"ABS": 1000.0, "KoSync": 1001.0},
        ]
    )
    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")

    leaders = []
    for i in range(6):
        config = {
            "ABS": _state({"pct": 0.2, "ts": 1000.0}),
            "KoSync": _state({"pct": 0.2 + (i * 0.0001), "_normalization_source": "xpath"}),
        }
        leader, _ = manager._determine_leader(config, book, "abs-1", "book")
        leaders.append(leader)

    assert leaders == ["ABS"] * 6


def test_leader_switch_outside_deadband():
    manager = _manager_with_deterministic_mapping()
    manager._has_significant_delta = MagicMock(return_value=True)
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 1000.0, "KoSync": 1003.5}
    )
    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.2, "ts": 1000.0}),
        "KoSync": _state({"pct": 0.35, "_normalization_source": "xpath"}),
    }

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "KoSync"
    assert leader_pct == config["KoSync"].current["pct"]

