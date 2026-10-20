import os
import unittest
from unittest.mock import MagicMock, patch

from src.web_server import (
    _auto_match_threshold,
    _is_same_folder_match,
    _auto_match_suggestions,
)


def _make_match(
    score: float,
    match_reason: str | None = None,
    ebook_filename: str = "book.epub",
    display_name: str = "Book Title",
    source: str = "KOReader",
    source_id: str = "123",
) -> dict:
    """Build a minimal match dict for testing."""
    m = {
        "ebook_filename": ebook_filename,
        "display_name": display_name,
        "source": source,
        "source_id": source_id,
        "score": score,
    }
    if match_reason is not None:
        m["match_reason"] = match_reason
    return m


def _make_suggestion(
    abs_id: str = "abs-1",
    abs_title: str = "Test Audiobook",
    audio_source: str = "ABS",
    audio_source_id: str = "abs-1",
    audio_title: str = "Test Audiobook",
    matches: list[dict] | None = None,
) -> dict:
    """Build a minimal suggestion dict for testing."""
    return {
        "abs_id": abs_id,
        "abs_title": abs_title,
        "audio_source": audio_source,
        "audio_source_id": audio_source_id,
        "audio_title": audio_title,
        "matches": matches or [],
    }


class TestIsSameFolderMatch(unittest.TestCase):
    """Tests for _is_same_folder_match."""

    def test_same_folder_reason_is_same_folder(self):
        """A match with match_reason 'same_folder' is reported as same-folder."""
        match = _make_match(100.0, match_reason="same_folder")
        self.assertTrue(_is_same_folder_match(match))

    def test_same_folder_ambiguous_reason_is_same_folder(self):
        """A match with match_reason 'same_folder_ambiguous' is reported as same-folder."""
        match = _make_match(94.0, match_reason="same_folder_ambiguous")
        self.assertTrue(_is_same_folder_match(match))

    def test_no_match_reason_is_not_same_folder(self):
        """A match with no match_reason key is reported as NOT same-folder."""
        match = _make_match(100.0)  # no match_reason key
        self.assertFalse(_is_same_folder_match(match))

    def test_none_match_reason_is_not_same_folder(self):
        """A match with match_reason set to None is reported as NOT same-folder."""
        match = _make_match(100.0, match_reason=None)
        self.assertFalse(_is_same_folder_match(match))

    def test_future_same_folder_tier_is_same_folder(self):
        """A hypothetical future tier 'same_folder_something_new' is also same-folder (prefix rule)."""
        match = _make_match(100.0, match_reason="same_folder_something_new")
        self.assertTrue(_is_same_folder_match(match))


class TestAutoMatchThreshold(unittest.TestCase):
    """Tests for _auto_match_threshold."""

    def _set_env(self, key: str, value: str | None):
        if value is None:
            if key in os.environ:
                del os.environ[key]
        else:
            os.environ[key] = value

    def _restore_env(self, key: str, old_value: str | None):
        if old_value is None:
            if key in os.environ:
                del os.environ[key]
        else:
            os.environ[key] = old_value

    def test_default_threshold_when_absent(self):
        """Returns 100.0 when SUGGESTIONS_AUTO_MATCH_THRESHOLD is absent."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", None)
            self.assertEqual(_auto_match_threshold(), 100.0)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old)

    def test_returns_parsed_value_when_set(self):
        """Returns the parsed float value when the variable is set."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", "95.5")
            self.assertEqual(_auto_match_threshold(), 95.5)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old)

    def test_fallback_to_default_on_unparseable(self):
        """Falls back to 100.0 when the value is unparseable."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", "abc")
            self.assertEqual(_auto_match_threshold(), 100.0)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old)

    def test_clamps_negative_to_zero(self):
        """Clamps a negative value to 0.0."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", "-10")
            self.assertEqual(_auto_match_threshold(), 0.0)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old)

    def test_clamps_above_100_to_100(self):
        """Clamps a value above 100 to 100.0."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", "150")
            self.assertEqual(_auto_match_threshold(), 100.0)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old)


class TestAutoMatchSuggestions(unittest.TestCase):
    """Tests for _auto_match_suggestions."""

    def _set_env(self, key: str, value: str | None):
        if value is None:
            if key in os.environ:
                del os.environ[key]
        else:
            os.environ[key] = value

    def _restore_env(self, key: str, old_value: str | None):
        if old_value is None:
            if key in os.environ:
                del os.environ[key]
        else:
            os.environ[key] = old_value

    @patch("src.web_server.container")
    def test_disabled_by_default(self, mock_container):
        """With SUGGESTIONS_AUTO_MATCH_ENABLED absent, a perfect 100 match is returned untouched."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", None)

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_not_called()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_enabled_with_ordinary_100_match(self, mock_container):
        """Enabled with an ordinary 100-scoring match: mapping created once, suggestion removed, auto_matched=1."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 0)
            mock_mapping.create_audio_mapping_from_match.assert_called_once()
            self.assertEqual(out["stats"].get("auto_matched"), 1)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server._shelve_saved_ebook")
    @patch("src.web_server._record_readalong_intent_for_match")
    @patch("src.web_server.container")
    def test_readalong_requested_only_for_bookorbit_matches_when_enabled(self, mock_container, mock_record, _shelve):
        """SUGGESTIONS_AUTO_MATCH_READALONG ('true' or 'on') requests a read-along for a BookOrbit-audio match only."""
        keys = ("SUGGESTIONS_AUTO_MATCH_ENABLED", "SUGGESTIONS_AUTO_MATCH_READALONG")
        old = {key: os.environ.get(key) for key in keys}
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")
            for readalong, audio_source, expected in (
                ("true", "BookOrbit", True),
                ("on", "BookOrbit", True),
                ("false", "BookOrbit", False),
                (None, "BookOrbit", False),
                ("true", "ABS", False),
            ):
                with self.subTest(readalong=readalong, audio_source=audio_source):
                    mock_record.reset_mock()
                    self._set_env("SUGGESTIONS_AUTO_MATCH_READALONG", readalong)
                    mock_mapping = MagicMock()
                    mock_mapping.create_audio_mapping_from_match.return_value = MagicMock(
                        abs_id="book-1", audio_source=audio_source)
                    mock_container.book_mapping_service.return_value = mock_mapping
                    suggestion = _make_suggestion(audio_source=audio_source, matches=[_make_match(100.0)])

                    out = _auto_match_suggestions({"suggestions": [suggestion], "stats": {}}, user_id=1)

                    self.assertEqual(out["stats"].get("auto_matched"), 1)
                    if expected:
                        mock_record.assert_called_once_with("book-1")
                    else:
                        mock_record.assert_not_called()
        finally:
            for key, value in old.items():
                self._restore_env(key, value)

    @patch("src.web_server._shelve_saved_ebook")
    @patch("src.web_server._record_readalong_intent_for_match", side_effect=RuntimeError("db locked"))
    @patch("src.web_server.container")
    def test_readalong_request_failure_keeps_the_auto_match(self, mock_container, _record, _shelve):
        keys = ("SUGGESTIONS_AUTO_MATCH_ENABLED", "SUGGESTIONS_AUTO_MATCH_READALONG")
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                self._set_env(key, "true")
            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = MagicMock(
                abs_id="book-1", audio_source="BookOrbit")
            mock_container.book_mapping_service.return_value = mock_mapping
            suggestion = _make_suggestion(audio_source="BookOrbit", matches=[_make_match(100.0)])

            with self.assertLogs("src.web_server", level="WARNING"):
                out = _auto_match_suggestions({"suggestions": [suggestion], "stats": {}}, user_id=1)

            self.assertEqual(out["suggestions"], [])
            self.assertEqual(out["stats"].get("auto_matched"), 1)
        finally:
            for key, value in old.items():
                self._restore_env(key, value)

    @patch("src.web_server.container")
    def test_same_folder_100_not_auto_linked(self, mock_container):
        """
        HEADLINE REGRESSION: a same-folder 100 is NOT auto-linked.

        _same_folder_tier in src/services/suggestions_service.py awards 100.0 merely for sharing
        a folder when the folder holds one candidate and the titles agree by a fuzzy ratio of
        only 45. Those matches are meant to be reviewed behind a 'Same folder?' badge —
        auto-linking one syncs a reader's position into the wrong book.
        """
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0, match_reason="same_folder")])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_not_called()
            self.assertNotIn("auto_matched", out.get("stats", {}))
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_same_folder_ambiguous_not_auto_linked_even_at_lower_threshold(self, mock_container):
        """A same_folder_ambiguous match is not auto-linked even when threshold is lowered to 90 (below its 94)."""
        old_enabled = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        old_threshold = os.environ.get("SUGGESTIONS_AUTO_MATCH_THRESHOLD")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")
            self._set_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", "90")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(94.0, match_reason="same_folder_ambiguous")])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_not_called()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old_enabled)
            self._restore_env("SUGGESTIONS_AUTO_MATCH_THRESHOLD", old_threshold)

    @patch("src.web_server.container")
    def test_mixed_candidates_ordinary_match_selected(self, mock_container):
        """When top match is same-folder 100 but an ordinary match also scores 100, the ordinary one IS auto-linked."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(
                matches=[
                    _make_match(100.0, match_reason="same_folder", ebook_filename="same_folder.epub"),
                    _make_match(100.0, match_reason=None, ebook_filename="ordinary.epub"),
                ]
            )
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 0)
            mock_mapping.create_audio_mapping_from_match.assert_called_once()
            # Verify the call received the ordinary match's ebook_filename
            call_kwargs = mock_mapping.create_audio_mapping_from_match.call_args.kwargs
            self.assertEqual(call_kwargs["ebook_filename"], "ordinary.epub")
            self.assertEqual(out["stats"].get("auto_matched"), 1)
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_below_threshold_not_auto_matched(self, mock_container):
        """An ordinary match scoring 99.0 against default threshold 100 is left in the list."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(99.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_not_called()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_exception_from_mapping_service_logs_warning(self, mock_container):
        """A raised exception from create_audio_mapping_from_match leaves the suggestion and logs a warning."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.side_effect = RuntimeError("DB error")
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            with self.assertLogs("src.web_server", level="WARNING") as cm:
                out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_called_once()
            self.assertTrue(any("Auto-match failed" in msg for msg in cm.output))
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_falsy_return_from_mapping_service_leaves_suggestion(self, mock_container):
        """A falsy return (service reports it did not save) leaves the suggestion and does not count toward auto_matched."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = False
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            self.assertNotIn("auto_matched", out.get("stats", {}))
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_empty_matches_list_passed_through(self, mock_container):
        """A suggestion with an empty matches list is passed through untouched without error."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 1)
            self.assertEqual(out["suggestions"][0], suggestion)
            mock_mapping.create_audio_mapping_from_match.assert_not_called()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_enabled_true_spelling(self, mock_container):
        """SUGGESTIONS_AUTO_MATCH_ENABLED='true' enables auto-match."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 0)
            mock_mapping.create_audio_mapping_from_match.assert_called_once()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)

    @patch("src.web_server.container")
    def test_enabled_on_spelling(self, mock_container):
        """SUGGESTIONS_AUTO_MATCH_ENABLED='on' enables auto-match (checkboxes POST 'on')."""
        old = os.environ.get("SUGGESTIONS_AUTO_MATCH_ENABLED")
        try:
            self._set_env("SUGGESTIONS_AUTO_MATCH_ENABLED", "on")

            mock_mapping = MagicMock()
            mock_mapping.create_audio_mapping_from_match.return_value = True
            mock_container.book_mapping_service.return_value = mock_mapping

            suggestion = _make_suggestion(matches=[_make_match(100.0)])
            results = {"suggestions": [suggestion], "stats": {}}

            out = _auto_match_suggestions(results, user_id=1)

            self.assertEqual(len(out["suggestions"]), 0)
            mock_mapping.create_audio_mapping_from_match.assert_called_once()
        finally:
            self._restore_env("SUGGESTIONS_AUTO_MATCH_ENABLED", old)


if __name__ == "__main__":
    unittest.main()