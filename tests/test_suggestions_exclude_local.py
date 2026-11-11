"""Regression tests for excluding filesystem ebooks from Suggestions only."""

import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from src import web_server as web
from src.services.ebook_discovery import _audio_fingerprints_by_key, build_discovery_snapshot, build_ebook_inventory
from src.services.suggestions_service import SuggestionsService


SETTING = "SUGGESTIONS_EXCLUDE_LOCAL_FILES"


def _candidate(source, source_id, title):
    return SimpleNamespace(
        name=f"{title}.epub", display_name=f"{title}.epub", title=title,
        authors="Ada Writer", source=source, source_id=source_id,
        language="", path=f"/books/{title}.epub",
    )


def _service(ebooks):
    return SuggestionsService(
        database_service=MagicMock(), container=MagicMock(), manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [], get_searchable_ebooks=lambda _q: ebooks,
        audiobook_matches_search=lambda *_args: False, get_abs_author=lambda _book: "",
        logger=MagicMock(),
    )


class TestSuggestionsExcludeLocal(TestCase):
    def test_candidate_pool_and_inventory_filter_local_rows_only_when_enabled(self):
        candidates = [
            _candidate("Local File", None, "Local Book"),
            _candidate("BookOrbit", "42", "Provider Book"),
        ]
        for value, expected in ((None, {"Local File", "BookOrbit"}), ("false", {"Local File", "BookOrbit"}), ("true", {"BookOrbit"}), ("on", {"BookOrbit"})):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop(SETTING, None)
                else:
                    os.environ[SETTING] = value
                pool = _service(candidates)._build_ebook_candidate_pool()
                self.assertEqual({row["source"] for row in pool}, expected)
                inventory = build_ebook_inventory(candidates)
                self.assertEqual({row["source"] for row in inventory.values()}, expected)

    def test_setting_changes_provider_decision_fingerprint_and_rescores_it(self):
        providers = [
            _candidate("BookOrbit", "42", "Provider Book"),
            _candidate("Kavita", "alias-1", "Provider Book"),
        ]
        first = None
        for value in ("false", "on"):
            with patch.dict(os.environ, {SETTING: value}):
                inventory = build_ebook_inventory(providers)
                snapshot = build_discovery_snapshot(
                    _service([]), inventory, [], previous=first,
                )
                self.assertEqual(snapshot["exclude_local_files"], value == "on")
                self.assertEqual(snapshot["stats"]["reused_cached"], 0)
                self.assertEqual(len(next(iter(inventory.values()))["aliases"]), 2)
                if first:
                    first_entry = next(iter(first["entries"].values()))
                    current_entry = next(iter(snapshot["entries"].values()))
                    self.assertNotEqual(first_entry["fingerprint"], current_entry["fingerprint"])
                    self.assertEqual(snapshot["stats"]["scanned_new"], 1)
                first = snapshot

    def test_discovery_keeps_bookorbit_title_index_but_skips_local_inventory(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            book_path = os.path.join(directory, "Local Title.epub")
            with zipfile.ZipFile(book_path, "w") as epub:
                epub.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
                epub.writestr("book.opf", (
                    '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<metadata><dc:title>Local Title</dc:title><dc:creator>Ada Writer</dc:creator></metadata></package>'
                ))
            orbit = MagicMock(
                is_configured=MagicMock(return_value=True),
                get_all_ebooks=MagicMock(return_value=[{
                    "id": 42, "title": "Local Title", "authors": "Ada Writer",
                    "ebookFormat": "epub", "fileName": None,
                }]),
                search_ebooks=MagicMock(return_value=[]), catalog_complete=True,
            )
            clients = SimpleNamespace(
                booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
                bookorbit_client=orbit,
                bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
                kavita_client=MagicMock(is_configured=MagicMock(return_value=False)),
                abs_client=MagicMock(is_configured=MagicMock(return_value=False), search_ebooks=MagicMock(return_value=[])),
                library_service=None,
            )
            source_states, inventory, metadata = {}, [], {}
            with patch.object(web, "EBOOK_DIR", Path(directory), create=True), \
                    patch.object(web, "uc", return_value=clients), patch.dict(os.environ, {SETTING: "true"}):
                found = web.get_searchable_ebooks(
                    "", inventory_candidates=inventory, source_states=source_states,
                    local_metadata=metadata,
                )
                manual = web.get_searchable_ebooks("Local Title")

            self.assertEqual([(book.source, book.name) for book in found], [("BookOrbit", "Local Title.epub")])
            self.assertEqual([book.source for book in inventory], ["BookOrbit"])
            local_role = source_states["Local File"]["roles"]["ebook"]
            self.assertFalse(local_role["configured"])
            self.assertIsNone(local_role["complete"])
            self.assertTrue(any(book.source == "Local File" for book in manual))

    def test_setting_toggle_drops_stale_cache_on_incomplete_scan_but_unchanged_scan_preserves_it(self):
        cached = {"audio": {"bridge_key": "audio", "matches": []}}
        for old_value, value, expected in (
            ("false", "true", {}), ("true", "false", {}), ("false", "false", cached),
        ):
            previous = {
                "ebook_discovery": {
                    "exclude_local_files": old_value == "true",
                    "generation": "old", "state": "complete", "entries": {"old": {"classification": "no_audio"}},
                },
                "scan_cache_by_abs": cached,
                "scan_cache_no_match_abs_ids": ["audio"],
                "scanned_at": 123,
            }
            service = _service([])

            def load_ebooks(_term, **kwargs):
                web._record_suggestions_source_state(
                    kwargs["source_states"], "Kavita", "ebook", True, complete=False,
                )
                return []

            with self.subTest(old=old_value, current=value), \
                    patch.dict(os.environ, {SETTING: value}), \
                    patch.object(web, "_get_suggestions_service", return_value=service), \
                    patch.object(web, "_load_persisted_suggestions_cache", return_value=previous), \
                    patch.object(web, "uc", return_value=SimpleNamespace()), \
                    patch.object(web, "get_suggestion_audiobooks", return_value=[]), \
                    patch.object(web, "get_searchable_ebooks", side_effect=load_ebooks):
                result = web.scan_library_suggestions(
                    cached_suggestions_by_abs=cached,
                    cached_no_match_abs_ids=["audio"],
                )

            self.assertFalse(result["discovery_complete"])
            self.assertEqual(result["cache_by_abs"], expected)
            self.assertEqual(result["no_match_abs_ids"], ["audio"] if expected else [])
            self.assertEqual(result["ebook_discovery"]["entries"], {} if not expected else previous["ebook_discovery"]["entries"])
            self.assertEqual(result["_exclude_setting_changed"], old_value != value)

    def test_incomplete_job_persists_cache_clear_only_after_setting_toggle(self):
        def run_job(changed):
            saved = {}
            result = {
                "discovery_complete": False, "cache_by_abs": {}, "no_match_abs_ids": [],
                "scanned_at": 123, "_exclude_setting_changed": changed,
                "ebook_discovery": {"state": "incomplete", "attempted_at": 123},
            }
            with patch.object(web, "scan_library_suggestions", return_value=result), \
                    patch.object(web, "_load_persisted_suggestions_cache", return_value={"scanned_at": 123}), \
                    patch.object(web, "_save_persisted_suggestions_cache", side_effect=lambda data: saved.update(data) or True), \
                    patch.dict(os.environ, {SETTING: "true"}):
                web._run_suggestions_scan_job("job")
            return saved

        changed_save = run_job(True)
        unchanged_save = run_job(False)
        self.assertEqual(changed_save["scan_cache_by_abs"], {})
        self.assertEqual(changed_save["scan_cache_no_match_abs_ids"], [])
        self.assertNotIn("scan_cache_by_abs", unchanged_save)

    def test_ordinary_scan_drops_legacy_local_suggestions_and_rescans_no_match_ids(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            books_dir = Path(directory) / "books"
            books_dir.mkdir()
            book_path = books_dir / "Local Title.epub"
            with zipfile.ZipFile(book_path, "w") as epub:
                epub.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
                epub.writestr("book.opf", (
                    '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<metadata><dc:title>Local Title</dc:title><dc:creator>Ada Writer</dc:creator></metadata></package>'
                ))

            local = _candidate("Local File", None, "Local Title")
            local_inventory = build_ebook_inventory([local])
            local_key, local_entry = next(iter(local_inventory.items()))
            local_entry.update({
                "classification": "no_audio", "fingerprint": "legacy",
                "checked_at": 1, "audio_keys": [], "search_complete": True,
            })
            source_clients = SimpleNamespace(
                booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
                bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
                bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
                kavita_client=MagicMock(is_configured=MagicMock(return_value=False)),
                abs_client=MagicMock(is_configured=MagicMock(return_value=False), search_ebooks=MagicMock(return_value=[])),
                library_service=None,
            )
            audio = [
                {"id": "audio-1", "title": "Local Title", "author": "Ada Writer", "audio_source": "ABS"},
                {"id": "audio-2", "title": "No Match", "author": "Unknown Writer", "audio_source": "ABS"},
            ]
            service = _service([local])
            service.database_service.get_all_books.return_value = []
            audio_fingerprints = _audio_fingerprints_by_key(service._build_audiobook_candidate_pool(audio))
            first_key, second_key = (service._audio_bridge_key(item) for item in audio)
            legacy_pair = {
                "bridge_key": first_key, "audio_source": "ABS", "audio_source_id": "audio-1",
                "audio_title": "Local Title", "audio_author": "Ada Writer",
                "matches": [{"source": "Local File", "name": "Local Title.epub", "score": 100}],
            }
            legacy_snapshot = {
                "schema_version": 1, "state": "complete", "generation": "legacy",
                "completed_at": 1, "attempted_at": 1, "entries": {local_key: local_entry},
                "audio_fingerprints_by_key": audio_fingerprints, "stats": {}, "source_states": {},
            }

            with patch.object(web, "DATA_DIR", data_dir, create=True), \
                    patch.object(web, "get_current_user_id", return_value=1), \
                    patch.object(web, "EBOOK_DIR", books_dir, create=True), \
                    patch.object(web, "_get_suggestions_service", return_value=service), \
                    patch.object(web, "uc", return_value=source_clients), \
                    patch.object(web, "get_suggestion_audiobooks", return_value=audio), \
                    patch.object(web, "_auto_match_suggestions", side_effect=lambda result, _user_id: result), \
                    patch.dict(os.environ, {SETTING: "false"}):
                self.assertTrue(web._save_persisted_suggestions_cache({
                    "scan_cache_by_abs": {first_key: legacy_pair},
                    "scan_cache_no_match_abs_ids": [second_key],
                    "scan_last_stats": {"reused_cached": 2},
                    "ebook_discovery": legacy_snapshot,
                }))
                saved_legacy = web._load_persisted_suggestions_cache()
                self.assertNotIn("exclude_local_files", saved_legacy["ebook_discovery"])

                with patch.dict(os.environ, {SETTING: "true"}):
                    web._run_suggestions_scan_job(
                        "exclude-local-on",
                        cached_suggestions_by_abs=saved_legacy["scan_cache_by_abs"],
                        cached_no_match_abs_ids=saved_legacy["scan_cache_no_match_abs_ids"],
                        full_refresh=False,
                    )
                excluded = web._load_persisted_suggestions_cache()
                self.assertEqual(excluded["scan_last_stats"]["scanned_new"], 2)
                self.assertEqual(excluded["scan_last_stats"]["reused_cached"], 0)
                self.assertEqual(excluded["scan_cache_no_match_abs_ids"], [first_key, second_key])
                self.assertFalse(any(
                    match.get("source") == "Local File"
                    for suggestion in excluded["scan_cache_by_abs"].values()
                    for match in suggestion.get("matches", [])
                ))
                self.assertNotIn(local_key, excluded["ebook_discovery"]["entries"])
                self.assertFalse(any(
                    entry.get("source") == "Local File"
                    for entry in excluded["ebook_discovery"]["entries"].values()
                ))

                with patch.dict(os.environ, {SETTING: "false"}):
                    web._run_suggestions_scan_job(
                        "exclude-local-off",
                        cached_suggestions_by_abs=excluded["scan_cache_by_abs"],
                        cached_no_match_abs_ids=excluded["scan_cache_no_match_abs_ids"],
                        full_refresh=False,
                    )
                restored = web._load_persisted_suggestions_cache()
                self.assertTrue(any(
                    match.get("source") == "Local File"
                    for suggestion in restored["scan_cache_by_abs"].values()
                    for match in suggestion.get("matches", [])
                ))
