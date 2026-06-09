"""Semantic position rescue in EbookParser.find_text_location (OLLAMA_EBOOK_TEXT_FALLBACK)."""

import os
import unittest
from unittest.mock import patch

from src.utils.ebook_utils import EbookParser


class _KeywordOllama:
    """Embeds by topic keyword; records texts so window capping can be asserted."""

    def __init__(self, configured=True):
        self._configured = configured
        self.embedded_texts = []

    def is_configured(self):
        return self._configured

    def embed(self, texts):
        self.embedded_texts.extend(texts)
        out = []
        for t in texts:
            low = (t or "").lower()
            if "ocean" in low:
                out.append([1.0, 0.0])
            elif "mountain" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.4, 0.4])
        return out


_ENV = {
    "OLLAMA_ENABLED": "true",
    "OLLAMA_EBOOK_TEXT_FALLBACK": "true",
    "OLLAMA_ALIGN_SIM_THRESHOLD": "0.72",
}


def _book_text():
    # First half is about mountains, second half about the ocean.
    return ("mountain ridge summit " * 300).strip() + " " + ("ocean tide wave " * 300).strip()


class TestSemanticTextFallback(unittest.TestCase):
    def setUp(self):
        self.stub = _KeywordOllama()
        self.parser = EbookParser(books_dir=".", ollama_client=self.stub)

    def test_finds_position_in_matching_topic_region(self):
        text = _book_text()
        with patch.dict(os.environ, _ENV):
            idx = self.parser._semantic_text_fallback("the rolling ocean swallowed them", text, None)
        self.assertGreaterEqual(idx, 0)
        # The ocean half starts midway through the text.
        self.assertGreater(idx, len(text) * 0.4)

    def test_hint_window_limits_search_region(self):
        text = _book_text()
        with patch.dict(os.environ, _ENV):
            idx = self.parser._semantic_text_fallback("the rolling ocean swallowed them", text, 0.9)
        self.assertGreater(idx, len(text) * 0.7)

    def test_below_threshold_returns_minus_one(self):
        # Query embeds to [0,1]; plain windows embed to [0.4,0.4] → cosine ≈ 0.71 < 0.72.
        with patch.dict(os.environ, _ENV):
            idx = self.parser._semantic_text_fallback("mountain", "plain text " * 500, None)
        self.assertEqual(idx, -1)

    def test_disabled_setting_returns_minus_one(self):
        with patch.dict(os.environ, {**_ENV, "OLLAMA_EBOOK_TEXT_FALLBACK": "false"}):
            idx = self.parser._semantic_text_fallback("the rolling ocean", _book_text(), None)
        self.assertEqual(idx, -1)
        self.assertEqual(self.stub.embedded_texts, [])

    def test_unconfigured_client_returns_minus_one(self):
        parser = EbookParser(books_dir=".", ollama_client=_KeywordOllama(configured=False))
        with patch.dict(os.environ, _ENV):
            self.assertEqual(parser._semantic_text_fallback("ocean", _book_text(), None), -1)

    def test_no_client_returns_minus_one(self):
        parser = EbookParser(books_dir=".")
        with patch.dict(os.environ, _ENV):
            self.assertEqual(parser._semantic_text_fallback("ocean", _book_text(), None), -1)

    def test_embedded_windows_are_capped(self):
        # A very long book must not push windows beyond the embed char cap.
        long_text = ("mountain " * 40_000) + ("ocean " * 40_000)
        with patch.dict(os.environ, _ENV):
            idx = self.parser._semantic_text_fallback("ocean tide", long_text, None)
        self.assertGreaterEqual(idx, 0)
        cap = EbookParser._SEMANTIC_EMBED_MAX_CHARS
        # First embedded text is the query; the rest are windows.
        self.assertTrue(all(len(t) <= cap for t in self.stub.embedded_texts[1:]))


if __name__ == "__main__":
    unittest.main()
