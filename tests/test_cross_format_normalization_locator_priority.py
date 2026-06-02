import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Match existing tests that add project root for `src.*` imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, ServiceState, SyncResult
from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser


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


class _StubClient:
    def get_supported_sync_types(self):
        return {'audiobook', 'ebook'}

    def can_be_leader(self):
        return True

    def is_configured(self):
        return True

    def check_connection(self):
        return True

    def fetch_bulk_state(self):
        return None

    def supports_book(self, book):
        return True


class _SyncLoopClient(_StubClient):
    def __init__(self, supported_types=None, update_result=None):
        self._supported_types = supported_types or {'audiobook', 'ebook'}
        self.update_progress = MagicMock(
            return_value=update_result or SyncResult(0.0, True, {"pct": 0.0})
        )

    def get_supported_sync_types(self):
        return self._supported_types


def _manager_with_mocks():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()
    manager.alignment_service = MagicMock()
    manager.booklore_client = MagicMock()
    manager.booklore_client.find_book_by_filename.return_value = None
    manager.books_dir = None
    manager.epub_cache_dir = Path("/tmp/epub_cache")
    manager._sync_cycle_local_epub_cache = {}
    manager._sync_cycle_ebook_cache = {}
    # Make _get_local_epub return a dummy path so normalization can proceed
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager.sync_clients = {
        "ABS": _StubClient(),
        "KoSync": _StubClient(),
        "BookLore": _StubClient(),
    }
    return manager


def test_normalization_prefers_xpath_offset():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = 123
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.alignment_service.get_time_for_text.return_value = 555.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "KoSync": _state({"pct": 0.5, "xpath": "/body/DocFragment[1]/body/p[1]/text().0"}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["KoSync"] == 555.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 123
    manager.ebook_parser.resolve_xpath_to_index.assert_called_once()


def test_normalization_prefers_cfi_before_percent():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = 321
    manager.alignment_service.get_time_for_text.return_value = 777.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.4, "cfi": "epubcfi(/6/10!/4:0)"}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["BookLore"] == 777.0
    assert config["BookLore"].current["_normalized_ts"] == 777.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 321
    manager.ebook_parser.resolve_cfi_to_index.assert_called_once()


def test_normalization_falls_back_to_percent_when_no_locator():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.alignment_service.get_time_for_text.return_value = 888.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.4}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["BookLore"] == 888.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 400


def test_build_text_anchors_clamps_bounds():
    manager = SyncManager.__new__(SyncManager)
    full_text = "".join(chr(65 + (i % 26)) for i in range(300))

    prefix_start, suffix_start, window_start = manager._build_text_anchors(full_text, 0)
    assert len(prefix_start) == 0
    assert len(suffix_start) == 60
    assert len(window_start) == 120

    prefix_mid, suffix_mid, window_mid = manager._build_text_anchors(full_text, 150)
    assert len(prefix_mid) == 60
    assert len(suffix_mid) == 60
    assert len(window_mid) == 240

    prefix_end, suffix_end, window_end = manager._build_text_anchors(full_text, 9999)
    assert len(prefix_end) == 60
    assert len(suffix_end) == 1
    assert len(window_end) == 121


def test_normalization_sets_high_low_confidence_by_source():
    manager = _manager_with_mocks()
    full_text = "abcdefghijklmnopqrstuvwxyz " * 80
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = (full_text, [])
    manager.alignment_service.get_time_for_text.return_value = 111.0
    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")

    # xpath -> high
    manager.ebook_parser.resolve_xpath_to_index.return_value = 150
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.ebook_parser.resolve_locator_id.return_value = None
    cfg_xpath = {
        "ABS": _state({"ts": 10.0}),
        "KoSync": _state({"pct": 0.1, "xpath": "/body/DocFragment[1]/body/p[1]/text().0"}),
    }
    manager._normalize_for_cross_format_comparison(book, cfg_xpath)
    assert cfg_xpath["KoSync"].current["_normalization_source"] == "xpath"
    assert cfg_xpath["KoSync"].current["_normalization_confidence"] == "high"

    # cfi -> high
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = 220
    manager.ebook_parser.resolve_locator_id.return_value = None
    cfg_cfi = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.1, "cfi": "epubcfi(/6/10!/4:0)"}),
    }
    manager._normalize_for_cross_format_comparison(book, cfg_cfi)
    assert cfg_cfi["BookLore"].current["_normalization_source"] == "cfi"
    assert cfg_cfi["BookLore"].current["_normalization_confidence"] == "high"

    # href_frag -> high
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.ebook_parser.resolve_locator_id.return_value = full_text[300:460]
    cfg_href = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.1, "href": "chapter.xhtml", "frag": "p1"}),
    }
    manager._normalize_for_cross_format_comparison(book, cfg_href)
    assert cfg_href["BookLore"].current["_normalization_source"] == "href_frag"
    assert cfg_href["BookLore"].current["_normalization_confidence"] == "high"

    # percent fallback -> low
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.ebook_parser.resolve_locator_id.return_value = None
    cfg_pct = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.1}),
    }
    manager._normalize_for_cross_format_comparison(book, cfg_pct)
    assert cfg_pct["BookLore"].current["_normalization_source"] == "percent_fallback"
    assert cfg_pct["BookLore"].current["_normalization_confidence"] == "low"


def test_normalization_uses_href_progression_when_fragment_lookup_fails():
    manager = _manager_with_mocks()
    full_text = "a" * 1000
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = (
        full_text,
        [{"href": "OEBPS/Text/part0083.xhtml", "start": 200, "end": 600}],
    )
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.ebook_parser.resolve_locator_id.return_value = None
    manager.alignment_service.get_time_for_text.return_value = 111.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state(
            {
                "pct": 0.1,
                "href": "OEBPS/Text/part0083.xhtml",
                "frag": "x_c079-sentence123",
                "chapter_progress": 0.5,
            }
        ),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["BookLore"] == 111.0
    assert config["BookLore"].current["_normalization_source"] == "href_progression"
    assert config["BookLore"].current["_normalization_confidence"] == "high"
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 400


def test_normalization_uses_cached_extract_once_per_book_per_cycle():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = 123
    manager.ebook_parser.resolve_cfi_to_index.return_value = 321
    manager.alignment_service.get_time_for_text.return_value = 500.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "KoSync": _state({"pct": 0.5, "xpath": "/body/DocFragment[1]/body/p[1]/text().0"}),
        "BookLore": _state({"pct": 0.5, "cfi": "epubcfi(/6/10!/4:0)"}),
    }

    manager._normalize_for_cross_format_comparison(book, config)
    manager._normalize_for_cross_format_comparison(book, config)

    assert manager.ebook_parser.extract_text_and_map.call_count == 1


def test_normalization_uses_client_specific_epub_contexts():
    manager = _manager_with_mocks()
    manager.sync_clients = {
        "ABS": _StubClient(),
        "Storyteller": _StubClient(),
        "BookLore": _StubClient(),
    }
    full_text = "abcdefghijklmnopqrstuvwxyz " * 100
    manager.ebook_parser.resolve_book_path.side_effect = lambda filename: filename
    manager.ebook_parser.extract_text_and_map.return_value = (
        full_text,
        [{"href": "chapter.xhtml", "start": 300, "end": 700}],
    )
    manager.ebook_parser.resolve_locator_id.return_value = full_text[340:460]
    manager.ebook_parser.resolve_cfi_to_index.return_value = 420
    manager.alignment_service.get_time_for_text.return_value = 123.0

    book = SimpleNamespace(
        abs_id="abs-ctx",
        transcript_file="DB_MANAGED",
        ebook_filename="storyteller_uuid.epub",
        original_ebook_filename="original.epub",
    )
    config = {
        "ABS": _state({"ts": 10.0}),
        "Storyteller": _state({"pct": 0.2, "href": "chapter.xhtml", "frag": "x_c001-sentence001"}),
        "BookLore": _state({"pct": 0.2, "cfi": "epubcfi(/6/10!/4:0)"}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["Storyteller"] == 123.0
    assert normalized["BookLore"] == 123.0
    manager.ebook_parser.resolve_locator_id.assert_any_call(
        "storyteller_uuid.epub",
        "chapter.xhtml",
        "x_c001-sentence001",
    )
    manager.ebook_parser.resolve_cfi_to_index.assert_called_once_with(
        "original.epub",
        "epubcfi(/6/10!/4:0)",
    )


def test_determine_leader_uses_locator_pct_when_raw_pct_is_inconsistent():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "BookLore": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name in {"KoSync", "BookLore"})
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 4124.7, "KoSync": 4086.2, "BookLore": 4113.3}
    )

    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.1015, "ts": 4124.7}),
        "KoSync": _state({"pct": 0.104255}),
        "BookLore": _state({"pct": 0.0, "cfi": "epubcfi(/6/16!/4/14:0)", "_locator_pct": 0.1010}),
    }

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "BookLore"
    assert leader_pct == 0.1010
    assert config["BookLore"].current["pct"] == 0.1010


def test_booklore_get_text_prefers_cfi_over_percentage():
    ebook_parser = MagicMock()
    ebook_parser.get_text_around_cfi.return_value = "cfi text"
    booklore_client = MagicMock()
    client = BookloreSyncClient(booklore_client, ebook_parser)
    state = _state({"pct": 0.0, "cfi": "epubcfi(/6/16!/4/14:0)"})
    book = SimpleNamespace(ebook_filename="book.epub")

    text = client.get_text_from_current_state(book, state)

    assert text == "cfi text"
    ebook_parser.get_text_around_cfi.assert_called_once_with("book.epub", "epubcfi(/6/16!/4/14:0)")
    ebook_parser.get_text_at_percentage.assert_not_called()


def test_determine_leader_ignores_stale_booklore_raw_delta():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "BookLore": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name in {"KoSync", "BookLore"})
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 23404.6, "KoSync": 23379.2, "BookLore": 23397.2}
    )

    book = SimpleNamespace(duration=40556, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.5763, "ts": 23404.6}),
        "KoSync": _state({"pct": 0.5894}),
        "BookLore": _state({"pct": 0.2980, "cfi": "epubcfi(/6/46!/4/16:0)", "_locator_pct": 0.5838}),
    }
    config["KoSync"].previous_pct = 0.5838
    config["BookLore"].previous_pct = 0.5838

    leader, _ = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "KoSync"


def test_single_non_abs_delta_must_be_ahead_on_normalized_timeline():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "BookLore": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name == "BookLore")
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 2844.3, "KoSync": 2829.8, "BookLore": 2829.8}
    )

    book = SimpleNamespace(duration=19967, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.1424530426, "ts": 2844.3}),
        "KoSync": _state({"pct": 0.142680}),
        "BookLore": _state({"pct": 0.105000, "_locator_pct": 0.142680, "_normalization_source": "cfi"}),
    }

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "ABS"
    assert leader_pct == config["ABS"].current["pct"]


def test_single_kosync_delta_behind_abs_does_not_lead():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "Hardcover": _Client(),
    }
    manager.cross_format_deadband_seconds = 2.0
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name == "KoSync")
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 15310.7, "KoSync": 7812.8, "Hardcover": 14600.0}
    )

    book = SimpleNamespace(duration=21175.4, transcript_file="DB_MANAGED", audio_source="ABS")
    config = {
        "ABS": _state({"pct": 0.7230, "ts": 15310.7}),
        "KoSync": _state(
            {
                "pct": 0.3913,
                "xpath": "/body/DocFragment[7]/body/div/p[706]/text()[1].0",
                "_normalization_source": "xpath",
            }
        ),
        "Hardcover": _state({"pct": 0.7344}),
    }
    config["KoSync"].previous_pct = 0.7360

    leader, leader_pct = manager._determine_leader(config, book, "abs-heart", "Heart the Lover")

    assert leader == "ABS"
    assert leader_pct == config["ABS"].current["pct"]


def test_single_storyteller_delta_with_href_progression_is_not_demoted():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "Storyteller": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name == "Storyteller")
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 31162.8, "Storyteller": 32417.8}
    )

    book = SimpleNamespace(duration=84898, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.3672900422, "ts": 31162.8}),
        "Storyteller": _state({"pct": 0.3819, "_normalization_source": "href_progression"}),
    }
    config["Storyteller"].previous_pct = 0.372752

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "Storyteller"
    assert leader_pct == config["Storyteller"].current["pct"]


def test_parse_cfi_components_supports_minimal_cfi():
    parser = EbookParser.__new__(EbookParser)

    spine_step, element_steps, char_offset = parser._parse_cfi_components("epubcfi(/6/26!/:0)")

    assert spine_step == 26
    assert element_steps == []
    assert char_offset == 0


def test_parse_cfi_components_supports_point_cfi_with_low_spine_step():
    parser = EbookParser.__new__(EbookParser)

    spine_step, _, char_offset = parser._parse_cfi_components("epubcfi(/6/4!/4/4/208:0)")

    assert spine_step == 4
    assert char_offset == 0


def test_parse_cfi_components_supports_range_cfi():
    parser = EbookParser.__new__(EbookParser)

    spine_step, element_steps, char_offset = parser._parse_cfi_components(
        "epubcfi(/6/4!/4/4,/114/1:174,/158/1:176)"
    )

    assert spine_step == 4
    assert char_offset == 174
    assert len(element_steps) > 0


def test_generate_cfi_never_emits_empty_element_path():
    parser = EbookParser.__new__(EbookParser)

    cfi = parser._generate_cfi(12, "plain text without body wrapper", 1)

    assert "!/:" not in cfi


def test_deadband_keeps_abs_as_leader_for_tiny_crossformat_gap():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {"ABS": _Client(), "KoSync": _Client()}
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: True)
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 1000.0, "KoSync": 1001.2}
    )

    config = {
        "ABS": _state({"pct": 0.2, "ts": 1000.0}),
        "KoSync": _state({"pct": 0.21, "_normalization_source": "xpath"}),
    }
    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "ABS"
    assert leader_pct == config["ABS"].current["pct"]


def test_deadband_allows_switch_when_delta_exceeds_threshold():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {"ABS": _Client(), "KoSync": _Client()}
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: True)
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 1000.0, "KoSync": 1002.6}
    )

    config = {
        "ABS": _state({"pct": 0.2, "ts": 1000.0}),
        "KoSync": _state({"pct": 0.21, "_normalization_source": "xpath"}),
    }
    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "KoSync"
    assert leader_pct == config["KoSync"].current["pct"]


def test_recent_external_kosync_percent_fallback_can_lead():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {"ABS": _Client(), "KoSync": _Client()}
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name == "KoSync")
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 28667.3, "KoSync": 30400.0}
    )

    config = {
        "ABS": _state({"pct": 0.415762, "ts": 28667.3}),
        "KoSync": _state(
            {
                "pct": 0.441,
                "_normalization_source": "percent_fallback",
                "_kosync_recent_external_put": True,
                "_kosync_last_put_device": "Kobo_monza",
                "_kosync_last_put_age_seconds": 247.0,
            }
        ),
    }
    book = SimpleNamespace(duration=68940, transcript_file="DB_MANAGED")

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "Leviathan Wakes")

    assert leader == "KoSync"
    assert leader_pct == config["KoSync"].current["pct"]


def test_stale_kosync_percent_fallback_is_demoted():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {"ABS": _Client(), "KoSync": _Client()}
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name == "KoSync")
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 28667.3, "KoSync": 30400.0}
    )

    config = {
        "ABS": _state({"pct": 0.415762, "ts": 28667.3}),
        "KoSync": _state({"pct": 0.441, "_normalization_source": "percent_fallback"}),
    }
    book = SimpleNamespace(duration=68940, transcript_file="DB_MANAGED")

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "Leviathan Wakes")

    assert leader == "ABS"
    assert leader_pct == config["ABS"].current["pct"]


def test_deadband_rollback_guard_skips_high_conf_locator_client():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    book = SimpleNamespace(duration=32385, transcript_file="DB_MANAGED")
    leader_state = _state({"pct": 0.713911, "ts": 23051.6})
    client_state = _state(
        {
            "pct": 0.7118,
            "_normalized_ts": 23052.9,
            "_normalization_source": "xpath",
        }
    )

    should_skip = manager._should_skip_deadband_rollback(
        book,
        "ABS",
        leader_state,
        "KoSync",
        client_state,
        "abs-1",
        "book",
    )

    assert should_skip is True


def test_deadband_rollback_guard_allows_percent_fallback_rewrite():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0

    book = SimpleNamespace(duration=32385, transcript_file="DB_MANAGED")
    leader_state = _state({"pct": 0.713911, "ts": 23051.6})
    client_state = _state(
        {
            "pct": 0.7118,
            "_normalized_ts": 23052.9,
            "_normalization_source": "percent_fallback",
        }
    )

    should_skip = manager._should_skip_deadband_rollback(
        book,
        "ABS",
        leader_state,
        "KoSync",
        client_state,
        "abs-1",
        "book",
    )

    assert should_skip is False


def test_sync_cycle_skips_deadband_rollback_to_high_conf_kosync():
    manager = SyncManager.__new__(SyncManager)
    manager.cross_format_deadband_seconds = 2.0
    manager.sync_delta_between_clients = 0.005
    manager.delta_chars_thresh = 2000
    manager._sync_cycle_ebook_cache = {}
    manager._sync_cycle_local_epub_cache = {}
    manager._last_library_sync = 0
    manager.library_service = None
    manager.booklore_client = None
    manager.alignment_service = None
    manager.ebook_parser = MagicMock()
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager._promote_alignment_backed_book = MagicMock(return_value=False)
    manager._record_local_reading_session = MagicMock()
    manager._record_grimmory_reading_session = MagicMock()

    abs_client = _SyncLoopClient(supported_types={"audiobook"})
    kosync_client = _SyncLoopClient()
    manager.sync_clients = {"ABS": abs_client, "KoSync": kosync_client}

    book = SimpleNamespace(
        abs_id="abs-1",
        abs_title="Rollback Guard",
        status="active",
        duration=32385,
        transcript_file="DB_MANAGED",
        ebook_filename="book.epub",
    )
    manager.database_service = MagicMock()
    manager.database_service.get_book.return_value = book
    manager.database_service.get_states_for_book.return_value = [
        SimpleNamespace(client_name="abs", last_updated=1000, timestamp=23000.0, percentage=0.70),
        SimpleNamespace(client_name="kosync", last_updated=1001, timestamp=None, percentage=0.7184),
    ]
    manager.database_service.save_state = MagicMock()

    config = {
        "ABS": _state({"pct": 0.713911, "ts": 23051.6}),
        "KoSync": _state(
            {
                "pct": 0.7118,
                "xpath": "/body/DocFragment[1]/body/p[153]/text().0",
                "_normalized_ts": 23052.9,
                "_normalization_source": "xpath",
            }
        ),
    }
    config["ABS"].previous_pct = 0.70
    config["ABS"].delta = 51.6
    config["ABS"].threshold = 60.0
    config["ABS"].value_seconds_formatter = lambda v: f"{v:.2f}s"
    config["KoSync"].previous_pct = 0.7184
    config["KoSync"].delta = 0.0066
    config["KoSync"].threshold = 0.005

    manager._fetch_states_parallel = MagicMock(return_value=config)
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 23051.6, "KoSync": 23052.9}
    )
    manager._resolve_alignment_locator_from_abs_timestamp = MagicMock(
        return_value=(
            LocatorResult(
                percentage=0.713911,
                xpath="/body/DocFragment[1]/body/p[153]/text().0",
                cfi="epubcfi(/6/4!/4/2:0)",
                match_index=713,
            ),
            "anchor text",
        )
    )

    manager._sync_cycle_internal(target_abs_id="abs-1")

    kosync_client.update_progress.assert_not_called()
    manager.database_service.save_state.assert_called_once()


def test_alignment_locator_roundtrip_regenerates_cfi_when_unstable():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager._sync_cycle_local_epub_cache = {}
    manager.ebook_parser.locator_roundtrip_tolerance = 2
    manager.ebook_parser.resolve_xpath_to_index.return_value = 250
    manager.ebook_parser.get_sentence_level_ko_xpath.return_value = "/body/DocFragment[1]/body/p[1]/text().0"
    manager.ebook_parser.resolve_cfi_to_index.side_effect = [260, 100]
    manager.ebook_parser.get_locator_from_char_offset.return_value = SimpleNamespace(
        cfi="epubcfi(/6/16!/4/2:0)"
    )

    locator = SimpleNamespace(
        percentage=0.5,
        xpath="/body/DocFragment[1]/body/p[99]/text().0",
        perfect_ko_xpath="/body/DocFragment[1]/body/p[99]/text().0",
        match_index=100,
        cfi="epubcfi(/6/2!/4/2:0)",
        href="chapter.xhtml",
        fragment=None,
        css_selector=None,
        chapter_progress=0.5,
        fragments=None,
    )
    book = SimpleNamespace(abs_id="abs-1", ebook_filename="book.epub")

    stable = manager._validate_and_stabilize_locator(book, 100, locator)

    assert stable.xpath is None
    assert stable.perfect_ko_xpath is None
    assert stable.cfi == "epubcfi(/6/16!/4/2:0)"


def test_roundtrip_prefers_sentence_xpath_before_percent_only():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager._sync_cycle_local_epub_cache = {}
    manager.ebook_parser.locator_roundtrip_tolerance = 2
    manager.ebook_parser.resolve_xpath_to_index.side_effect = [130, 101]
    manager.ebook_parser.get_sentence_level_ko_xpath.return_value = "/body/DocFragment[1]/body/p[10]/text().0"
    manager.ebook_parser.resolve_cfi_to_index.return_value = 100

    locator = SimpleNamespace(
        percentage=0.5,
        xpath="/body/DocFragment[1]/body/p[99]/text().0",
        perfect_ko_xpath="/body/DocFragment[1]/body/p[99]/text().0",
        match_index=100,
        cfi="epubcfi(/6/2!/4/2:0)",
        href="chapter.xhtml",
        fragment=None,
        css_selector=None,
        chapter_progress=0.5,
        fragments=None,
    )
    book = SimpleNamespace(abs_id="abs-1", ebook_filename="book.epub")

    stable = manager._validate_and_stabilize_locator(book, 100, locator)

    assert stable.xpath == "/body/DocFragment[1]/body/p[10]/text().0"
    assert stable.perfect_ko_xpath == "/body/DocFragment[1]/body/p[10]/text().0"
    assert stable.cfi == "epubcfi(/6/2!/4/2:0)"


def test_repeated_time_to_locator_roundtrip_stays_within_tolerance():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()
    manager._get_local_epub = lambda filename: Path(f"/tmp/{filename}")
    manager._sync_cycle_local_epub_cache = {}
    manager.ebook_parser.locator_roundtrip_tolerance = 2
    manager.ebook_parser.resolve_xpath_to_index.return_value = 101
    manager.ebook_parser.resolve_cfi_to_index.return_value = 99

    locator = SimpleNamespace(
        percentage=0.5,
        xpath="/body/DocFragment[1]/body/p[3]/text().0",
        perfect_ko_xpath="/body/DocFragment[1]/body/p[3]/text().0",
        match_index=100,
        cfi="epubcfi(/6/2!/4/2:0)",
        href="chapter.xhtml",
        fragment=None,
        css_selector=None,
        chapter_progress=0.5,
        fragments=None,
    )
    book = SimpleNamespace(abs_id="abs-1", ebook_filename="book.epub")

    for _ in range(5):
        stable = manager._validate_and_stabilize_locator(book, 100, locator)
        assert stable.xpath is not None
        assert stable.cfi is not None
