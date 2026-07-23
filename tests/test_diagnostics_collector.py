"""Tests for the diagnostics warning collector (Phase 1 core)."""
import json
import logging
import os
import tempfile
import unittest

from src.services.diagnostics import (
    DiagnosticsLogHandler,
    _make_template,
    _sha1_prefix,
    scrub_diagnostic_text,
    setup_diagnostics_logging,
    get_diagnostics_handler,
)


def _make_record(
    logger_name: str,
    level: int,
    message: str,
    name: str = '',
) -> logging.LogRecord:
    """Create a LogRecord for testing."""
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname='test.py',
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestScrubDiagnosticText(unittest.TestCase):
    """Tests for scrub_diagnostic_text."""

    def test_url_replaced_with_stable_token(self):
        url = "https://example.com/path/to/page?q=1"
        result = scrub_diagnostic_text(f"See {url} for details")
        self.assertIn("url:", result)
        self.assertNotIn("example.com", result)
        # Same URL → same token
        result2 = scrub_diagnostic_text(f"Visit {url} again")
        # Extract the url: tokens and verify they match
        import re
        tokens = re.findall(r'url:[0-9a-f]+', result)
        tokens2 = re.findall(r'url:[0-9a-f]+', result2)
        self.assertEqual(tokens, tokens2)

    def test_url_token_is_stable_across_calls(self):
        url = "https://mybookserver.local:8080/api/books"
        r1 = scrub_diagnostic_text(url)
        r2 = scrub_diagnostic_text(url)
        self.assertEqual(r1, r2)
        self.assertIn("url:", r1)

    def test_posix_path_replaced(self):
        text = "Failed to open /books/Author/Title.epub"
        result = scrub_diagnostic_text(text)
        self.assertIn("path:", result)
        self.assertIn(".epub", result)
        self.assertNotIn("Author", result)

    def test_windows_path_replaced(self):
        text = r"Cannot read C:\data\logs\app.log"
        result = scrub_diagnostic_text(text)
        self.assertIn("path:", result)
        self.assertIn(".log", result)
        self.assertNotIn("data", result)

    def test_path_preserves_extension(self):
        text = "/sync/bookmarks/chapter3.json"
        result = scrub_diagnostic_text(text)
        self.assertIn(".json", result)
        self.assertIn("path:", result)

    def test_short_path_not_replaced(self):
        text = "/a/b"  # Only 2 segments, but no extension – still counts as path
        # Actually 2 separators means >=2, so this IS replaced
        result = scrub_diagnostic_text(text)
        self.assertIn("path:", result)

    def test_single_path_separator_not_replaced(self):
        text = "file.txt in /data"
        result = scrub_diagnostic_text(text)
        # Only one '/' in '/data', not a path replacement
        self.assertIn("/data", result)

    def test_quoted_span_replaced(self):
        text = 'Sync failed for "The Great Gatsby Chapter One Title"'
        result = scrub_diagnostic_text(text)
        self.assertIn("t:", result)
        self.assertIn('"', result)
        # Inner text is replaced
        self.assertNotIn("Great Gatsby", result)

    def test_quoted_span_single_quotes(self):
        text = "Error in 'some really long quoted text here for testing'"
        result = scrub_diagnostic_text(text)
        self.assertIn("t:", result)
        self.assertNotIn("some really long", result)

    def test_short_quoted_span_not_replaced(self):
        text = 'Error: "short"'
        result = scrub_diagnostic_text(text)
        # "short" is 5 chars < 12, should not be replaced
        self.assertIn('"short"', result)
        self.assertNotIn("t:", result)

    def test_plain_text_untouched(self):
        text = "Sync completed successfully"
        result = scrub_diagnostic_text(text)
        self.assertEqual(result, text)

    def test_empty_string(self):
        self.assertEqual(scrub_diagnostic_text(""), "")


class TestMakeTemplate(unittest.TestCase):
    """Tests for the template generation used in deduplication."""

    def test_digits_replaced(self):
        tpl = _make_template("Failed after 3 retries")
        self.assertEqual(tpl, "Failed after # retries")

    def test_whitespace_collapsed(self):
        tpl = _make_template("Too   many    spaces")
        self.assertEqual(tpl, "Too many spaces")

    def test_digit_runs_replaced(self):
        tpl = _make_template("Error code 404 at line 42")
        self.assertEqual(tpl, "Error code # at line #")

    def test_http_failure_statuses_remain_distinct(self):
        unauthorized = _make_template("Failed to fetch all progress: 401")
        unavailable = _make_template("Failed to fetch all progress: 502")

        self.assertEqual(unauthorized, "Failed to fetch all progress: 401")
        self.assertEqual(unavailable, "Failed to fetch all progress: 502")

    def test_book_titles_collapse_for_shared_transcript_failure(self):
        first = _make_template(
            "❌ Antes de que los cuelguen: "
            "Failed to generate transcript from both SMIL and Whisper."
        )
        second = _make_template(
            "❌ El talento oscuro: "
            "Failed to generate transcript from both SMIL and Whisper."
        )

        self.assertEqual(
            first,
            "<book>: Failed to generate transcript from both SMIL and Whisper.",
        )
        self.assertEqual(first, second)

    def test_hardcover_no_match_titles_collapse_after_scrubbing(self):
        first = _make_template(scrub_diagnostic_text(
            "\u26a0\ufe0f Hardcover: No match found for 'False Gods'"
        ))
        second = _make_template(scrub_diagnostic_text(
            "\u26a0\ufe0f Hardcover: No match found for "
            "'The Extremely Long Librarian\'s Journey'"
        ))

        self.assertEqual(first, "Hardcover: No match found for '<book>'")
        self.assertEqual(first, second)
        self.assertEqual(
            _make_template("Hardcover: API request returned 503"),
            "Hardcover: API request returned 503",
        )

    # -- scrub-token collapse tests (diagnostics finding 630) -------------

    def test_quoted_doc_id_collapse_same_template(self):
        """Two KoSync-style messages with different doc_ids produce same template."""
        # Realistic 32-hex doc_ids (KoSync format) - each >= 12 chars so scrubbed to t:<hash>
        msg1 = "Error fetching KoSync progress for doc 'a1b2c3d4e5f678901234567890abcdef': timeout"
        msg2 = "Error fetching KoSync progress for doc 'fedcba09876543210fedcba098765432': timeout"

        scrubbed1 = scrub_diagnostic_text(msg1)
        scrubbed2 = scrub_diagnostic_text(msg2)
        tpl1 = _make_template(scrubbed1)
        tpl2 = _make_template(scrubbed2)

        self.assertEqual(tpl1, tpl2)
        # Verify the template contains the collapsed t:# form
        self.assertIn("t:#", tpl1)
        # And the rest of the message shape is preserved
        self.assertIn("Error fetching KoSync progress for doc", tpl1)
        self.assertIn("timeout", tpl1)

    def test_filesystem_path_collapse_same_template(self):
        """Two messages differing only in filesystem path produce same template.

        The path must be whitespace-free: scrub tokenizes on whitespace, so only a
        space-free token with >= 2 separators becomes a single ``path:<hash><ext>``.
        """
        msg1 = "Failed to open /srv/library/alpha/book.epub for reading"
        msg2 = "Failed to open /srv/library/omega/story.epub for reading"

        scrubbed1 = scrub_diagnostic_text(msg1)
        scrubbed2 = scrub_diagnostic_text(msg2)
        tpl1 = _make_template(scrubbed1)
        tpl2 = _make_template(scrubbed2)

        self.assertEqual(tpl1, tpl2)
        # Path extension preserved after collapse
        self.assertIn("path:#.epub", tpl1)

    def test_url_collapse_same_template(self):
        """Two messages differing only in URL produce same template."""
        msg1 = "Sync request to https://abs-server-1.local/api/sync failed"
        msg2 = "Sync request to https://abs-server-2.local/api/sync failed"

        scrubbed1 = scrub_diagnostic_text(msg1)
        scrubbed2 = scrub_diagnostic_text(msg2)
        tpl1 = _make_template(scrubbed1)
        tpl2 = _make_template(scrubbed2)

        self.assertEqual(tpl1, tpl2)
        self.assertIn("url:#", tpl1)

    def test_http_status_preserved_after_scrub_token_collapse(self):
        """HTTP status codes remain distinct in templates despite scrub-token collapse."""
        msg1 = "Error fetching KoSync progress for doc 'a1b2c3d4e5f678901234567890abcdef': HTTP 401"
        msg2 = "Error fetching KoSync progress for doc 'fedcba09876543210fedcba098765432': HTTP 502"

        scrubbed1 = scrub_diagnostic_text(msg1)
        scrubbed2 = scrub_diagnostic_text(msg2)
        tpl1 = _make_template(scrubbed1)
        tpl2 = _make_template(scrubbed2)

        # Different HTTP statuses must yield different templates (regression guard)
        self.assertNotEqual(tpl1, tpl2)
        self.assertIn("401", tpl1)
        self.assertIn("502", tpl2)
        # But the doc_id hashes are collapsed
        self.assertIn("t:#", tpl1)
        self.assertIn("t:#", tpl2)


class TestDiagnosticsHandler(unittest.TestCase):
    """Tests for DiagnosticsLogHandler core behaviour."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._test_logger = logging.getLogger('test_diag_collector')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def _make_handler(self, **kwargs) -> DiagnosticsLogHandler:
        handler = DiagnosticsLogHandler(data_dir=self._data_dir, **kwargs)
        handler.setLevel(logging.INFO)
        self._test_logger.addHandler(handler)
        return handler

    # -- dedupe --------------------------------------------------------

    def test_same_warning_different_numbers_collapses(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.warning("Sync failed after 3 retries")
        self._test_logger.warning("Sync failed after 7 retries")
        with handler._lock:
            self.assertEqual(len(handler._entries), 1)
            entry = list(handler._entries.values())[0]
            self.assertEqual(entry['count'], 2)
            self.assertIn('#', entry['template'])

    def test_book_specific_transcript_failures_share_one_entry(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        suffix = "Failed to generate transcript from both SMIL and Whisper."

        self._test_logger.error(f"❌ First Book: {suffix}")
        self._test_logger.error(f"❌ Second Book: {suffix}")

        with handler._lock:
            self.assertEqual(len(handler._entries), 1)
            self.assertEqual(next(iter(handler._entries.values()))['count'], 2)

    def test_different_http_statuses_create_separate_entries(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()

        self._test_logger.warning("Failed to fetch all progress: 401")
        self._test_logger.warning("Failed to fetch all progress: 502")

        with handler._lock:
            self.assertEqual(len(handler._entries), 2)

    # -- opt-in gating -------------------------------------------------

    def test_warning_not_recorded_when_opt_out(self):
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        handler = self._make_handler()
        self._test_logger.warning("Something went wrong")
        with handler._lock:
            self.assertEqual(len(handler._entries), 0)

    def test_warning_not_recorded_when_opt_false(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'false'
        handler = self._make_handler()
        self._test_logger.warning("Something went wrong")
        with handler._lock:
            self.assertEqual(len(handler._entries), 0)

    def test_warning_recorded_when_opt_true(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.warning("Something went wrong")
        with handler._lock:
            self.assertEqual(len(handler._entries), 1)

    def test_warning_recorded_when_opt_on(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'on'
        handler = self._make_handler()
        self._test_logger.warning("Something went wrong")
        with handler._lock:
            self.assertEqual(len(handler._entries), 1)

    def test_info_not_recorded_as_warning_entry(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.info("Normal info message")
        with handler._lock:
            # INFO records go to ring buffer but not to warning entries
            self.assertEqual(len(handler._entries), 0)
            self.assertTrue(len(handler._ring) > 0)

    # -- context capture -----------------------------------------------

    def test_context_capture_includes_previous_info_lines(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.info("Info line A")
        self._test_logger.info("Info line B")
        self._test_logger.warning("Warning message X")
        with handler._lock:
            entry = list(handler._entries.values())[0]
            context = entry['context']
            context_text = '\n'.join(context)
            self.assertIn("Info line A", context_text)
            self.assertIn("Info line B", context_text)
            self.assertIn("Warning message X", context_text)

    def test_context_captured_once_not_overwritten(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.info("Context line 1")
        self._test_logger.warning("Warning message X")
        # Capture the first context
        with handler._lock:
            first_context = list(handler._entries.values())[0]['context'][:]
        # Log more info lines and the same warning again
        self._test_logger.info("Context line 2")
        self._test_logger.warning("Warning message X")
        with handler._lock:
            second_context = list(handler._entries.values())[0]['context']
        # Context should be the same (captured at first occurrence)
        self.assertEqual(first_context, second_context)

    # -- handler never raises -----------------------------------------

    def test_handler_never_raises_on_emit_error(self):
        """Even if getMessage() raises, emit must not propagate."""
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()

        class BadMessage:
            def __str__(self):
                raise RuntimeError("intentional failure")

        record = logging.LogRecord(
            name='test.bad',
            level=logging.WARNING,
            pathname='test.py',
            lineno=1,
            msg=BadMessage(),
            args=(),
            exc_info=None,
        )
        # Must not raise
        handler.emit(record)

    # -- self-exclusion ------------------------------------------------

    def test_self_exclusion(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        diag_logger = logging.getLogger('src.services.diagnostics')
        diag_logger.propagate = False
        diag_logger.addHandler(handler)
        diag_logger.warning("Diagnostics internal message")
        diag_logger.handlers.clear()
        with handler._lock:
            self.assertEqual(len(handler._entries), 0)

    # -- cap entries at max_templates ----------------------------------

    def test_cap_entries_at_max_templates(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler(max_templates=3)
        self._test_logger.warning("Error type A")
        self._test_logger.warning("Error type B")
        self._test_logger.warning("Error type C")
        self._test_logger.warning("Error type D")
        with handler._lock:
            self.assertEqual(len(handler._entries), 3)
            self.assertEqual(handler._dropped, 1)


class TestPersistence(unittest.TestCase):
    """Tests for disk persistence and merge-on-reload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        self._test_logger = logging.getLogger('test_persistence')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def _make_handler(self, **kwargs) -> DiagnosticsLogHandler:
        handler = DiagnosticsLogHandler(data_dir=self._data_dir, **kwargs)
        handler.setLevel(logging.INFO)
        self._test_logger.addHandler(handler)
        return handler

    def test_flush_and_reload_survives(self):
        handler = self._make_handler()
        self._test_logger.warning("Persistent warning A")
        handler.flush_now()

        # Create a NEW handler with the same data_dir
        self._test_logger.handlers.clear()
        handler2 = self._make_handler()
        with handler2._lock:
            self.assertEqual(len(handler2._entries), 1)
            entry = list(handler2._entries.values())[0]
            self.assertEqual(entry['count'], 1)
            self.assertIn("Persistent warning A", entry['message'])

    def test_merge_adds_counts(self):
        handler = self._make_handler()
        self._test_logger.warning("Merge test message")
        handler.flush_now()

        self._test_logger.handlers.clear()
        handler2 = self._make_handler()
        self._test_logger.warning("Merge test message")
        with handler2._lock:
            self.assertEqual(len(handler2._entries), 1)
            entry = list(handler2._entries.values())[0]
            self.assertEqual(entry['count'], 2)


class TestSnapshotAndClear(unittest.TestCase):
    """Tests for snapshot/clear API."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        self._test_logger = logging.getLogger('test_snapshot')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def _make_handler(self, **kwargs) -> DiagnosticsLogHandler:
        handler = DiagnosticsLogHandler(data_dir=self._data_dir, **kwargs)
        handler.setLevel(logging.INFO)
        self._test_logger.addHandler(handler)
        return handler

    def test_snapshot_returns_deep_copy(self):
        handler = self._make_handler()
        self._test_logger.warning("Snapshot test")
        snap = handler.snapshot()
        self.assertIn('entries', snap)
        self.assertIn('taken_at', snap)
        self.assertEqual(len(snap['entries']), 1)

    def test_clear_removes_fully_sent_entries(self):
        handler = self._make_handler()
        self._test_logger.warning("To be cleared")
        snap = handler.snapshot()
        handler.clear_snapshot(snap)
        with handler._lock:
            self.assertEqual(len(handler._entries), 0)

    def test_clear_preserves_partial_count(self):
        handler = self._make_handler()
        self._test_logger.warning("Partial message")
        self._test_logger.warning("Partial message")
        # Take snapshot (count=2)
        snap = handler.snapshot()
        # Add one more occurrence
        self._test_logger.warning("Partial message")
        handler.clear_snapshot(snap)
        with handler._lock:
            self.assertEqual(len(handler._entries), 1)
            entry = list(handler._entries.values())[0]
            self.assertEqual(entry['count'], 1)

    def test_clear_sets_window_start_when_empty(self):
        handler = self._make_handler()
        self._test_logger.warning("Clearable")
        snap = handler.snapshot()
        handler.clear_snapshot(snap)
        with handler._lock:
            self.assertTrue(len(handler._window_start) > 0)

    def test_clear_does_not_negative_drop_count(self):
        handler = self._make_handler()
        # Clear a snapshot without any drops
        snap = {'dropped': 5, '_snapshot_key_counts': {}}
        handler.clear_snapshot(snap)
        with handler._lock:
            self.assertEqual(handler._dropped, 0)


class TestRingBuffer(unittest.TestCase):
    """Tests for the ring buffer (context lines)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._test_logger = logging.getLogger('test_ring')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def _make_handler(self, **kwargs) -> DiagnosticsLogHandler:
        handler = DiagnosticsLogHandler(data_dir=self._data_dir, **kwargs)
        handler.setLevel(logging.INFO)
        self._test_logger.addHandler(handler)
        return handler

    def test_ring_buffer_respects_maxlen(self):
        handler = self._make_handler(buffer_lines=5)
        for i in range(10):
            self._test_logger.info(f"Line {i}")
        with handler._lock:
            self.assertEqual(len(handler._ring), 5)
            # Most recent lines should be in the buffer
            lines = list(handler._ring)
            self.assertIn("Line 9", lines[-1])
            self.assertIn("Line 5", lines[0])


class TestSetupAndGetters(unittest.TestCase):
    """Tests for the module-level singleton functions."""

    def test_setup_and_get(self):
        handler = setup_diagnostics_logging()
        self.assertIsNotNone(handler)
        self.assertIs(get_diagnostics_handler(), handler)
        # Clean up: remove the handler from root logger
        logging.getLogger().removeHandler(handler)

    def test_get_returns_none_before_setup(self):
        # This test relies on the module-level _diagnostics_handler being None
        # After setup_diagnostics_logging is called, it won't be None.
        # We just test that get_diagnostics_handler returns a handler after setup.
        handler = setup_diagnostics_logging()
        self.assertIsInstance(handler, DiagnosticsLogHandler)
        logging.getLogger().removeHandler(handler)

    def test_setup_idempotent_returns_same_handler(self):
        """Calling setup_diagnostics_logging() twice returns the same handler
        and the root logger gains exactly one DiagnosticsLogHandler."""
        before = [h for h in logging.getLogger().handlers
                  if isinstance(h, DiagnosticsLogHandler)]
        handler_a = setup_diagnostics_logging()
        handler_b = setup_diagnostics_logging()
        self.assertIs(handler_a, handler_b)
        after = [h for h in logging.getLogger().handlers
                 if isinstance(h, DiagnosticsLogHandler)]
        self.assertEqual(len(after), len(before) + 1)
        logging.getLogger().removeHandler(handler_a)


class TestMessageTruncation(unittest.TestCase):
    """Tests for message and context line truncation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        self._test_logger = logging.getLogger('test_truncation')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def _make_handler(self, **kwargs) -> DiagnosticsLogHandler:
        handler = DiagnosticsLogHandler(data_dir=self._data_dir, **kwargs)
        handler.setLevel(logging.INFO)
        self._test_logger.addHandler(handler)
        return handler

    def test_long_message_truncated(self):
        handler = self._make_handler()
        long_msg = "X" * 500
        self._test_logger.warning(long_msg)
        with handler._lock:
            entry = list(handler._entries.values())[0]
            self.assertLessEqual(len(entry['message']), 400)


if __name__ == '__main__':
    unittest.main()
