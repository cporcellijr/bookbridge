"""Tests for the diagnostics warning collector (Phase 1 core)."""
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_koreader_xpath_not_scrubbed(self):
        """A KOReader xpointer survives scrubbing verbatim — it is the payload."""
        text = (
            "Could not resolve XPath in Some Book (2017).epub: "
            "/body/DocFragment[5]/body/div/p[42]/text().0"
        )
        result = scrub_diagnostic_text(text)
        self.assertIn("/body/DocFragment[5]/body/div/p[42]/text().0", result)
        self.assertNotIn("path:", result)

    def test_quoted_xpath_survives_the_quoted_span_rule(self):
        """The exemption is worthless if the quoted-span rule hashes it back.

        Every emitting site quotes the xpointer — e.g. ebook_utils' "Error resolving
        XPath '<xpath>'" — so rule 5 would turn the exempted token into 't:<hash>'
        and leave the finding just as unactionable as before.
        """
        text = (
            "❌ Error resolving XPath "
            "'/body/DocFragment[5]/body/div/p[42]/text().0': list index out of range"
        )
        result = scrub_diagnostic_text(text)
        self.assertIn("/body/DocFragment[5]/body/div/p[42]/text().0", result)
        self.assertNotIn("t:", result)

    def test_quoted_non_xpath_is_still_hashed(self):
        """The exemption must not become a hole for ordinary quoted text."""
        result = scrub_diagnostic_text("Failed for 'Some Readers Private Title'")
        self.assertNotIn("Some Readers Private Title", result)
        self.assertIn("t:", result)

    def test_xpath_with_id_predicate_not_scrubbed(self):
        """An @id= xpointer is exempt from the filesystem-path rule."""
        text = "resolve //*[@id='chapter-3']/p[2] failed"
        result = scrub_diagnostic_text(text)
        self.assertIn("//*[@id='chapter-3']/p[2]", result)
        self.assertNotIn("path:", result)

    def test_posix_path_with_index_bracket_still_scrubbed(self):
        """A real path with a [N] segment is NOT mistaken for an xpointer."""
        text = "Failed to open /data/books/file[1].epub"
        result = scrub_diagnostic_text(text)
        self.assertIn("path:", result)
        self.assertIn(".epub", result)
        self.assertNotIn("/data/books", result)

    def test_xpath_like_token_without_marker_still_scrubbed(self):
        """The detector is conservative: no text()/DocFragment/@id= means hash it."""
        text = "walk /body/div/p failed"
        result = scrub_diagnostic_text(text)
        self.assertIn("path:", result)
        self.assertNotIn("/body/div/p", result)

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

    def test_adjacent_short_quoted_spans_are_not_mispaired(self):
        text = "Hash 'abc' resolved to book 'one'"
        self.assertEqual(scrub_diagnostic_text(text), text)

    def test_plain_text_untouched(self):
        text = "Sync completed successfully"
        result = scrub_diagnostic_text(text)
        self.assertEqual(result, text)

    def test_empty_string(self):
        self.assertEqual(scrub_diagnostic_text(""), "")

    # -- email scrubbing -------------------------------------------------

    def test_email_replaced_with_stable_token(self):
        text = "Contact admin@example.com for help"
        result = scrub_diagnostic_text(text)
        self.assertIn("email:", result)
        self.assertNotIn("admin@example.com", result)

    def test_email_token_is_deterministic(self):
        text = "Reported by reader.one@my-library.io"
        r1 = scrub_diagnostic_text(text)
        r2 = scrub_diagnostic_text(text)
        self.assertEqual(r1, r2)
        expected = f"email:{_sha1_prefix('reader.one@my-library.io')}"
        self.assertIn(expected, r1)

    # -- bare IPv4 scrubbing -----------------------------------------------

    def test_bare_ipv4_with_port_hashes_address_only(self):
        text = "KoSync backend at 192.168.1.50:5758 timed out"
        result = scrub_diagnostic_text(text)
        expected = f"ip:{_sha1_prefix('192.168.1.50')}:5758"
        self.assertIn(expected, result)
        self.assertNotIn("192.168.1.50", result)

    def test_bare_ipv4_without_port_hashes_address(self):
        text = "Ping to 10.0.0.1 failed"
        result = scrub_diagnostic_text(text)
        expected = f"ip:{_sha1_prefix('10.0.0.1')}"
        self.assertIn(expected, result)
        self.assertNotIn("10.0.0.1", result)

    def test_three_octet_version_string_not_treated_as_ip(self):
        text = "Upgraded to version 7.3.4 successfully"
        result = scrub_diagnostic_text(text)
        self.assertIn("7.3.4", result)
        self.assertNotIn("ip:", result)

    def test_ip_inside_url_handled_by_url_rule_not_ip_rule(self):
        text = "See http://192.168.1.50:8080/status for details"
        result = scrub_diagnostic_text(text)
        self.assertIn("url:", result)
        self.assertNotIn("192.168.1.50", result)
        self.assertNotIn("ip:", result)


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

    def test_short_quoted_values_share_templates(self):
        """Short dynamic values must not consume one template each."""
        self.assertEqual(
            _make_template("KOSync: hash 'abc' resolved to book 'one'"),
            _make_template("KOSync: hash 'xyz' resolved to book 'two'"),
        )
        self.assertEqual(
            _make_template('Failed to inspect "alpha.epub" at XPath "/a/b"'),
            _make_template('Failed to inspect "omega.epub" at XPath "/x/y"'),
        )

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

    def test_xpath_positions_collapse_to_one_template(self):
        """Preserved xpointers still collapse per document shape, not per position.

        This is what keeps the XPath scrub exemption from re-inflating template
        cardinality: ``_make_template``'s digit collapse turns ``p[42]/text().0``
        into ``p[#]/text().#``, so two positions in the same document share one
        template.
        """
        msg1 = "Could not resolve XPath: /body/DocFragment[5]/body/div/p[42]/text().0"
        msg2 = "Could not resolve XPath: /body/DocFragment[5]/body/div/p[97]/text().12"

        tpl1 = _make_template(scrub_diagnostic_text(msg1))
        tpl2 = _make_template(scrub_diagnostic_text(msg2))

        self.assertEqual(tpl1, tpl2)
        self.assertIn("/body/DocFragment[#]/body/div/p[#]/text().#", tpl1)

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

    def test_kosync_bare_hash_warnings_collapse_when_quoted(self):
        """The three KoSync WARNING log shapes collapse to one template each when the hash is quoted.

        This guards the fix for diagnostics log-cardinality overflow (fleet findings #940/#713/#714).
        The quotes around the 32-hex hash make scrub_diagnostic_text tokenize it as a quoted span
        (t:<hash>), which _make_template then collapses to t:#. A bare (unquoted) hex hash is now
        ALSO collapsed, by _make_template's hex-run collapse (>= 6 hex chars containing a digit) --
        this generalizes the original quote-only fix so unquoted ids can no longer blow the
        500-template cap either. See the companion assertions below.
        """
        from src.services.diagnostics import scrub_diagnostic_text, _make_template
        
        # Two distinct 32-hex document hashes (32 chars each)
        hash1 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        hash2 = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        
        # Shape A: "KOSync: Document not found: '<HASH>' (GET from 10.0.0.1). trailing"
        msg_a1 = f"KOSync: Document not found: '{hash1}' (GET from 10.0.0.1). Document not found on server"
        msg_a2 = f"KOSync: Document not found: '{hash2}' (GET from 10.0.0.1). Document not found on server"
        
        scrubbed_a1 = scrub_diagnostic_text(msg_a1)
        scrubbed_a2 = scrub_diagnostic_text(msg_a2)
        tpl_a1 = _make_template(scrubbed_a1)
        tpl_a2 = _make_template(scrubbed_a2)
        
        self.assertEqual(tpl_a1, tpl_a2)
        self.assertIn("t:#", tpl_a1)
        self.assertIn("KOSync: Document not found:", tpl_a1)
        # The bare IP is now scrubbed to ip:<hash> by scrub_diagnostic_text
        # and collapsed to ip:# by _make_template's scrub-token collapse.
        self.assertIn("(GET from ip:#).", tpl_a1)
        
        # Shape B: "KOSync: GET-discovery for '<HASH>' dropped — discovery queue full (5/5)"
        msg_b1 = f"KOSync: GET-discovery for '{hash1}' dropped \u2014 discovery queue full (5/5)"
        msg_b2 = f"KOSync: GET-discovery for '{hash2}' dropped \u2014 discovery queue full (5/5)"
        
        scrubbed_b1 = scrub_diagnostic_text(msg_b1)
        scrubbed_b2 = scrub_diagnostic_text(msg_b2)
        tpl_b1 = _make_template(scrubbed_b1)
        tpl_b2 = _make_template(scrubbed_b2)
        
        self.assertEqual(tpl_b1, tpl_b2)
        self.assertIn("t:#", tpl_b1)
        self.assertIn("KOSync: GET-discovery for", tpl_b1)
        self.assertIn("dropped \u2014 discovery queue full (#/#)", tpl_b1)
        
        # Shape C: "KOSync: Suppressing response for '<HASH>' - percentage 12.34% but no locator available. Returning 404 to prevent page-0 reset."
        msg_c1 = f"KOSync: Suppressing response for '{hash1}' - percentage 12.34% but no locator available. Returning 404 to prevent page-0 reset."
        msg_c2 = f"KOSync: Suppressing response for '{hash2}' - percentage 12.34% but no locator available. Returning 404 to prevent page-0 reset."
        
        scrubbed_c1 = scrub_diagnostic_text(msg_c1)
        scrubbed_c2 = scrub_diagnostic_text(msg_c2)
        tpl_c1 = _make_template(scrubbed_c1)
        tpl_c2 = _make_template(scrubbed_c2)
        
        self.assertEqual(tpl_c1, tpl_c2)
        self.assertIn("t:#", tpl_c1)
        self.assertIn("KOSync: Suppressing response for", tpl_c1)
        self.assertIn("percentage #.#% but no locator available. Returning # to prevent page-# reset.", tpl_c1)
        
        # Companion assertion: the SAME message shapes but with BARE (unquoted) hashes
        # ALSO collapse to the same template per hash pair -- the hex-run collapse in
        # _make_template (change: template canonicalization) catches unquoted hex ids
        # directly, so quoting is no longer required for a hex hash to collapse.
        bare_a1 = f"KOSync: Document not found: {hash1} (GET from 10.0.0.1). Document not found on server"
        bare_a2 = f"KOSync: Document not found: {hash2} (GET from 10.0.0.1). Document not found on server"
        bare_b1 = f"KOSync: GET-discovery for {hash1} dropped \u2014 discovery queue full (5/5)"
        bare_b2 = f"KOSync: GET-discovery for {hash2} dropped \u2014 discovery queue full (5/5)"
        bare_c1 = f"KOSync: Suppressing response for {hash1} - percentage 12.34% but no locator available. Returning 404 to prevent page-0 reset."
        bare_c2 = f"KOSync: Suppressing response for {hash2} - percentage 12.34% but no locator available. Returning 404 to prevent page-0 reset."
        
        bare_scrubbed_a1 = scrub_diagnostic_text(bare_a1)
        bare_scrubbed_a2 = scrub_diagnostic_text(bare_a2)
        bare_tpl_a1 = _make_template(bare_scrubbed_a1)
        bare_tpl_a2 = _make_template(bare_scrubbed_a2)
        
        bare_scrubbed_b1 = scrub_diagnostic_text(bare_b1)
        bare_scrubbed_b2 = scrub_diagnostic_text(bare_b2)
        bare_tpl_b1 = _make_template(bare_scrubbed_b1)
        bare_tpl_b2 = _make_template(bare_scrubbed_b2)
        
        bare_scrubbed_c1 = scrub_diagnostic_text(bare_c1)
        bare_scrubbed_c2 = scrub_diagnostic_text(bare_c2)
        bare_tpl_c1 = _make_template(bare_scrubbed_c1)
        bare_tpl_c2 = _make_template(bare_scrubbed_c2)
        
        # Bare hex hashes now collapse too, via the hex-run rule in _make_template --
        # quoting is a sufficient but no longer necessary condition for collapse.
        self.assertEqual(bare_tpl_a1, bare_tpl_a2, "Bare hex hashes in shape A should collapse (hex-run rule)")
        self.assertEqual(bare_tpl_b1, bare_tpl_b2, "Bare hex hashes in shape B should collapse (hex-run rule)")
        self.assertEqual(bare_tpl_c1, bare_tpl_c2, "Bare hex hashes in shape C should collapse (hex-run rule)")
        self.assertIn('#', bare_tpl_a1)
        self.assertIn('#', bare_tpl_b1)
        self.assertIn('#', bare_tpl_c1)

    def test_schedule_auto_discovery_warning_collapses_across_hashes(self):
        """Production warning from _schedule_auto_discovery collapses to same template across hashes.
        
        This guards the load-bearing quote in the production log format string:
        "KOSync: %s-discovery for '%s' dropped \u2014 discovery queue full (%d/%d)"
        
        The '%s' quote around the doc_id makes scrub_diagnostic_text tokenize it as a
        quoted span (t:<hash>), which _make_template then collapses to t:#. If the quote
        is removed from the format string, the bare 32-hex hash survives digit-collapse
        and each distinct hash yields a distinct template — blowing the template cardinality cap.
        """
        from src.api import kosync_server
        from src.services.diagnostics import scrub_diagnostic_text, _make_template
        import os

        # Save original module globals and env var for restoration
        original_queued_count = kosync_server._queued_discovery_count
        original_auto_create = os.environ.get('AUTO_CREATE_EBOOK_MAPPING')

        # Two distinct 32-hex hashes that won't be in _active_scans
        hash1 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        hash2 = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"

        try:
            # Force queue-full condition and enable auto-create
            kosync_server._queued_discovery_count = kosync_server._MAX_QUEUED_DISCOVERY
            os.environ['AUTO_CREATE_EBOOK_MAPPING'] = 'true'

            # Capture first warning
            with self.assertLogs('src.api.kosync_server', level='WARNING') as cm1:
                kosync_server._schedule_auto_discovery(hash1, source='get')
            msg1 = cm1.records[0].getMessage()
            tpl1 = _make_template(scrub_diagnostic_text(msg1))

            # Capture second warning
            with self.assertLogs('src.api.kosync_server', level='WARNING') as cm2:
                kosync_server._schedule_auto_discovery(hash2, source='get')
            msg2 = cm2.records[0].getMessage()
            tpl2 = _make_template(scrub_diagnostic_text(msg2))

            # Templates must collapse to the same value
            self.assertEqual(tpl1, tpl2)
            # And the collapsed template must contain the t:# token
            self.assertIn("t:#", tpl1)
            # Verify the rest of the message shape is preserved
            self.assertIn("KOSync: get-discovery for", tpl1)
            self.assertIn("dropped \u2014 discovery queue full (#/#)", tpl1)

        finally:
            # Restore module globals and env var
            kosync_server._queued_discovery_count = original_queued_count
            if original_auto_create is None:
                os.environ.pop('AUTO_CREATE_EBOOK_MAPPING', None)
            else:
                os.environ['AUTO_CREATE_EBOOK_MAPPING'] = original_auto_create

    # -- per-value id collapse (UUIDs, hex ids, long alnum ids) -----------

    def test_uuids_collapse_to_same_template(self):
        first = _make_template(
            "Sync failed for book_id 4f9c2a10-8b3e-4d1a-9c7f-6e2b1a0d5c33"
        )
        second = _make_template(
            "Sync failed for book_id a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        self.assertEqual(first, second)
        self.assertIn('#', first)
        self.assertIn("Sync failed for book_id #", first)

    def test_hex_ids_collapse_to_same_template(self):
        first = _make_template("Resolved hash 3f9ab42c1d to book")
        second = _make_template("Resolved hash 9c1d2e3f4a to book")
        self.assertEqual(first, second)
        self.assertIn('#', first)

    def test_json_request_ids_collapse_after_quoted_scrub_tokens(self):
        """Fleet finding 7289: a quoted t:# token must not shift quote pairing."""
        first = _make_template(
            'BookOrbit koreader request path:3e75edb9 returned 401: {"statusCode":401,'
            '"message":"t:e417846e","path":"t:1b04e7ce","timestamp":"t:d34b6533","requestId":"req-93t"}'
        )
        second = _make_template(
            'BookOrbit koreader request path:3e75edb9 returned 401: {"statusCode":401,'
            '"message":"t:e417846e","path":"t:1b04e7ce","timestamp":"t:59583a44","requestId":"req-95i"}'
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            'BookOrbit koreader request path:# returned 401: {"<value>":#,"<value>":"t:#",'
            '"<value>":"t:#","<value>":"t:#","<value>":"<value>"}',
        )

    def test_object_addresses_collapse_to_same_template(self):
        """Fleet finding 7289: repr addresses like 'at 0x7f4cf94090' are per-process."""
        message = (
            "❌ Storyteller API request failed ('GET' 't:12e4a39a'): HTTPConnectionPool("
            "host='ip:e595c452', port=8001): Max retries exceeded with url: path:5f44182b "
            "(Caused by ConnectTimeoutError(<HTTPConnection(host='ip:e595c452', port=8001) "
            "at {}>, 't:0fa3be7a'))"
        )
        first = _make_template(message.format("0x7f4cf94090"))
        self.assertEqual(first, _make_template(message.format("0x7f842cee10")))
        self.assertIn("at #>", first)

    def test_long_alnum_ids_collapse_to_same_template(self):
        first = _make_template("Fetching li_4nrp2qhewiqe3xdi2q from library")
        second = _make_template("Fetching li_9xz8qhewiqe3aa1b from library")
        self.assertEqual(first, second)
        self.assertIn('#', first)
        self.assertIn("Fetching # from library", first)

    def test_hex_shaped_english_words_are_not_collapsed(self):
        self.assertEqual(
            _make_template("decade retention policy"),
            "decade retention policy",
        )
        self.assertEqual(
            _make_template("efface old cache entries"),
            "efface old cache entries",
        )

    def test_http_status_preserved_alongside_id_collapse(self):
        tpl = _make_template("Request to service abc123defabc returned 502")
        self.assertIn("502", tpl)
        self.assertIn('#', tpl)


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

    def test_short_quoted_values_do_not_exhaust_template_cap(self):
        """Regression for diagnostics findings #940, #1034, and #1289."""
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler(max_templates=2)

        for value in ('abc', 'def', 'ghi'):
            self._test_logger.warning(
                "KOSync: hash '%s' resolved to book '%s'", value, value,
            )
            self._test_logger.warning(
                'Failed to inspect "%s.epub" at XPath "/%s"', value, value,
            )

        with handler._lock:
            self.assertEqual(len(handler._entries), 2)
            self.assertEqual(handler._dropped, 0)
            self.assertEqual(
                sorted(entry['count'] for entry in handler._entries.values()),
                [3, 3],
            )
            self.assertTrue(any(
                "'abc'" in entry['message']
                for entry in handler._entries.values()
            ))

    # -- traceback capture ----------------------------------------------

    def test_traceback_captured_on_exc_info(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        try:
            1 / 0
        except ZeroDivisionError:
            self._test_logger.warning("Division failed", exc_info=True)
        with handler._lock:
            entry = list(handler._entries.values())[0]
            self.assertIn('traceback', entry)
            self.assertIn('ZeroDivisionError', entry['traceback'])
            self.assertLessEqual(len(entry['traceback']), 1200)
            self.assertLessEqual(len(entry['traceback'].split('\n')), 12)

    def test_no_traceback_key_without_exc_info(self):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.warning("Plain warning, no exception here")
        with handler._lock:
            entry = list(handler._entries.values())[0]
            self.assertNotIn('traceback', entry)

    def test_traceback_not_touched_on_repeat_occurrence(self):
        """Incrementing an existing entry must not add/alter 'traceback'."""
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        handler = self._make_handler()
        self._test_logger.warning("Repeatable warning")
        self._test_logger.warning("Repeatable warning")
        with handler._lock:
            entry = list(handler._entries.values())[0]
            self.assertEqual(entry['count'], 2)
            self.assertNotIn('traceback', entry)


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

    def test_flush_now_writes_compact_json_readable_by_json_load(self):
        """The buffer file is now compact (no indent) but still valid JSON."""
        handler = self._make_handler()
        self._test_logger.warning("Compact flush test")
        handler.flush_now()

        buf_path = Path(self._data_dir) / 'diagnostics_buffer.json'
        with open(buf_path, 'r', encoding='utf-8') as f:
            raw = f.read()
        data = json.loads(raw)
        self.assertEqual(len(data['entries']), 1)
        self.assertIn('Compact flush test', data['entries'][0]['message'])
        # Compact separators (',', ':') and no indent -> single line.
        self.assertNotIn('\n', raw)


class TestTmpFileSweep(unittest.TestCase):
    """Tests for the orphaned mkstemp .tmp-file sweep on handler construction."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)

    def tearDown(self):
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def test_stale_tmp_swept_fresh_kept_buffer_untouched(self):
        stale = Path(self._data_dir) / 'diagnostics_aaaaaaaa.tmp'
        fresh = Path(self._data_dir) / 'diagnostics_bbbbbbbb.tmp'
        buffer_file = Path(self._data_dir) / 'diagnostics_buffer.json'

        stale.write_text('stale-orphan', encoding='utf-8')
        fresh.write_text('fresh-inflight', encoding='utf-8')
        buffer_contents = (
            '{"entries": [], "dropped": 0, '
            '"window_start": "2026-01-01T00:00:00+00:00"}'
        )
        buffer_file.write_text(buffer_contents, encoding='utf-8')

        old_time = time.time() - 7200  # 2 hours old, well past the 3600s cutoff
        os.utime(stale, (old_time, old_time))

        DiagnosticsLogHandler(data_dir=self._data_dir)

        self.assertFalse(stale.exists(), "Stale .tmp file should be swept")
        self.assertTrue(fresh.exists(), "Fresh .tmp file should survive")
        self.assertEqual(
            buffer_file.read_text(encoding='utf-8'), buffer_contents,
            "Buffer file content must be untouched by the sweep",
        )


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


class TestShutdownFlushRegistration(unittest.TestCase):
    """Tests for the atexit-registered shutdown flush (fires once, on first
    handler creation only)."""

    def setUp(self):
        import src.services.diagnostics as _mod
        self._saved_handler = _mod._diagnostics_handler
        _mod._diagnostics_handler = None

    def tearDown(self):
        import src.services.diagnostics as _mod
        if _mod._diagnostics_handler is not None and _mod._diagnostics_handler is not self._saved_handler:
            logging.getLogger().removeHandler(_mod._diagnostics_handler)
        _mod._diagnostics_handler = self._saved_handler

    @patch('src.services.diagnostics.atexit.register')
    def test_first_setup_registers_flush_now_once(self, mock_register):
        handler = setup_diagnostics_logging()
        mock_register.assert_called_once_with(handler.flush_now)

    @patch('src.services.diagnostics.atexit.register')
    def test_second_setup_does_not_register_again(self, mock_register):
        handler_a = setup_diagnostics_logging()
        mock_register.reset_mock()
        handler_b = setup_diagnostics_logging()
        self.assertIs(handler_a, handler_b)
        mock_register.assert_not_called()


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
