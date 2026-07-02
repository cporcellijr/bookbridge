"""Match routes enqueue tracker auto-match to a background worker instead of
blocking the response on EPUB downloads + the Ollama judge."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as ws


def _drain():
    """Wait for the (single, daemon) worker to finish all queued jobs."""
    ws._TRACKER_AUTOMATCH_QUEUE.join()


class TestDeferredTrackerAutomatch(unittest.TestCase):
    def test_enqueue_runs_both_trackers(self):
        book = SimpleNamespace(abs_id="abs1")
        hc = MagicMock(); hc.is_configured.return_value = True
        sg = MagicMock(); sg.is_configured.return_value = True

        ws._enqueue_tracker_automatch({"Hardcover": hc, "StoryGraph": sg}, book)
        _drain()

        hc._automatch_hardcover.assert_called_once_with(book)
        sg._automatch_storygraph.assert_called_once_with(book)

    def test_unconfigured_client_skipped(self):
        book = SimpleNamespace(abs_id="abs2")
        hc = MagicMock(); hc.is_configured.return_value = False
        sg = MagicMock(); sg.is_configured.return_value = True

        ws._enqueue_tracker_automatch({"Hardcover": hc, "StoryGraph": sg}, book)
        _drain()

        hc._automatch_hardcover.assert_not_called()
        sg._automatch_storygraph.assert_called_once_with(book)

    def test_one_failure_does_not_block_the_other(self):
        book = SimpleNamespace(abs_id="abs3")
        hc = MagicMock(); hc.is_configured.return_value = True
        hc._automatch_hardcover.side_effect = RuntimeError("boom")
        sg = MagicMock(); sg.is_configured.return_value = True

        ws._enqueue_tracker_automatch({"Hardcover": hc, "StoryGraph": sg}, book)
        _drain()

        hc._automatch_hardcover.assert_called_once_with(book)
        sg._automatch_storygraph.assert_called_once_with(book)  # still ran

    def test_no_book_is_noop(self):
        # Should not start the worker or raise.
        ws._enqueue_tracker_automatch({"Hardcover": MagicMock()}, None)
        _drain()


class TestBatchClaimsForUser(unittest.TestCase):
    """Batch processing claims each matched book to the acting user (so it shows on
    that user's dashboard), using the user id bound onto the worker thread."""

    def test_process_batch_queue_claims_to_bound_user(self):
        fake_saved = SimpleNamespace(abs_id="booklore:99")
        db = MagicMock()
        item = {"audio_source": "BookLore", "audio_source_id": "99", "audio_title": "T"}

        tok = ws.set_current_user_id(7)
        try:
            with patch.object(ws, "database_service", db), \
                 patch.object(ws, "_create_or_update_library_audio_mapping",
                              return_value=(fake_saved, None, None)):
                ws._process_batch_queue([item])
        finally:
            ws.reset_current_user_id(tok)

        db.link_user_book.assert_called_once_with(7, "booklore:99")

    def test_process_batch_queue_routes_bookorbit_audio_to_library_mapping(self):
        fake_saved = SimpleNamespace(abs_id="bookorbit:42")
        db = MagicMock()
        item = {"audio_source": "BookOrbit", "audio_source_id": "42", "audio_title": "T"}

        tok = ws.set_current_user_id(7)
        try:
            with patch.object(ws, "database_service", db), \
                 patch.object(ws, "_create_or_update_library_audio_mapping",
                              return_value=(fake_saved, None, None)) as mapping_mock:
                ws._process_batch_queue([item])
        finally:
            ws.reset_current_user_id(tok)

        self.assertEqual(mapping_mock.call_args.kwargs["audio_source"], "BookOrbit")
        db.link_user_book.assert_called_once_with(7, "bookorbit:42")

    def test_claim_is_noop_without_user(self):
        db = MagicMock()
        with patch.object(ws, "database_service", db):
            ws._claim_book_for_user_id(None, "abs-1")
        db.link_user_book.assert_not_called()


class TestAudioBridgeKeys(unittest.TestCase):
    """Bridge keys route non-ABS audio providers ('booklore:'/'bookorbit:')."""

    def test_build_bridge_key_per_source(self):
        self.assertEqual(ws._build_bridge_key("BookLore", "42"), "booklore:42")
        self.assertEqual(ws._build_bridge_key("BookOrbit", "42"), "bookorbit:42")
        self.assertEqual(ws._build_bridge_key("ABS", "li_abc"), "li_abc")

    def test_build_bridge_key_normalizes_prefixed_ids(self):
        self.assertEqual(ws._build_bridge_key(None, "bookorbit: 42"), "bookorbit:42")
        self.assertEqual(ws._build_bridge_key(None, "booklore:42"), "booklore:42")

    def test_audio_source_from_bridge_key(self):
        self.assertEqual(ws._audio_source_from_bridge_key("booklore:42"), "BookLore")
        self.assertEqual(ws._audio_source_from_bridge_key("bookorbit:42"), "BookOrbit")
        self.assertEqual(ws._audio_source_from_bridge_key("li_abc"), "ABS")
        self.assertEqual(ws._audio_source_from_bridge_key(""), "")


class TestSpawnUserBackground(unittest.TestCase):
    """Batch processing runs on a daemon thread (real run) but inline under tests."""

    def tearDown(self):
        ws._BACKGROUND_TASKS_SYNCHRONOUS = False

    def test_inline_when_synchronous_flag_set(self):
        ws._BACKGROUND_TASKS_SYNCHRONOUS = True
        called = []
        ws._spawn_user_background(lambda *a: called.append(a), 1, 2, label="t")
        self.assertEqual(called, [(1, 2)])  # ran inline, no thread

    def test_async_runs_on_thread(self):
        ws._BACKGROUND_TASKS_SYNCHRONOUS = False
        import threading
        done = threading.Event()
        seen = {}
        # uc()/current_user touch request context; tolerate their absence off-thread.
        try:
            ws._spawn_user_background(lambda: (seen.update(ran=True), done.set()), label="t")
        except RuntimeError:
            self.skipTest("no app/request context available for background spawn")
        self.assertTrue(done.wait(timeout=5))
        self.assertTrue(seen.get("ran"))


if __name__ == "__main__":
    unittest.main()
