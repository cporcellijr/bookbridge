"""BookOrbit supplies the author the dashboard could not otherwise derive.

``display_author`` is derived, not stored. ``_build_dashboard_mapping`` always
passes ``base_author=''``, so before this change the only sources were the
Grimmory cache (``_get_cached_ebook_display_metadata`` reads
``booklore_books`` and nothing else), Storyteller metadata, and
``_parse_dashboard_filename_fallback``, which needs a literal ``Title - Author``
stem.

On an install where Grimmory is off and the library comes from BookOrbit, none
of those fire: BookOrbit's filenames look like
``03. Other Worlds Than These (2026).epub``. Measured on the live library at the
time of the fix, **58% of ebook-only books (36/62) had no author**, against 6%
of audiobook-mode books — and 35 of those 36 were ``ebook_source='BookOrbit'``.

BookOrbit knows the author and ``BookOrbitClient`` already caches it (light info
behind a TTL and a non-blocking refresh lock), so the dashboard prefetches it in
bulk rather than adding a table or a per-book call.

Ranking matters: the prefetched author fills a gap only. It must never displace
an author the Grimmory cache or Storyteller already resolved.
"""

import unittest
from unittest.mock import patch

from src.web_server import (
    _bookorbit_author_for_book,
    _bookorbit_source_ids,
    _prefetch_bookorbit_authors,
    _resolve_dashboard_display_metadata,
)


class _Book:
    """Minimal stand-in for the Book ORM row the resolver reads."""

    def __init__(self, abs_id="b1", sync_mode="ebook_only", ebook_filename=None,
                 original_ebook_filename=None, ebook_source=None, ebook_source_id=None,
                 audio_source=None, audio_source_id=None):
        self.abs_id = abs_id
        self.sync_mode = sync_mode
        self.ebook_filename = ebook_filename
        self.original_ebook_filename = original_ebook_filename
        self.ebook_source = ebook_source
        self.ebook_source_id = ebook_source_id
        self.audio_source = audio_source
        self.audio_source_id = audio_source_id


class TestBookOrbitSourceIds(unittest.TestCase):

    def test_collects_ids_from_both_sides(self):
        books = [
            _Book(ebook_source="BookOrbit", ebook_source_id="11"),
            _Book(audio_source="BookOrbit", audio_source_id="22"),
        ]
        self.assertEqual(_bookorbit_source_ids(books), {"11", "22"})

    def test_ignores_other_libraries(self):
        books = [
            _Book(ebook_source="Kavita", ebook_source_id="99"),
            _Book(ebook_source="BookLore", ebook_source_id="98"),
        ]
        self.assertEqual(_bookorbit_source_ids(books), set())

    def test_ids_are_stringified(self):
        """BookOrbit ids arrive as ints from the API and as text from the DB."""
        self.assertEqual(_bookorbit_source_ids([
            _Book(ebook_source="BookOrbit", ebook_source_id=11),
        ]), {"11"})


class TestPrefetchBookOrbitAuthors(unittest.TestCase):

    def _client(self, books):
        class _Client:
            def is_configured(self):
                return True

            def get_all_books(self):
                return books
        return _Client()

    def test_returns_authors_for_referenced_books_only(self):
        books = [_Book(ebook_source="BookOrbit", ebook_source_id="11")]
        client = self._client([
            {"id": 11, "title": "Wanted", "authors": "Ann Author"},
            {"id": 12, "title": "Not in our library", "authors": "Bea Writer"},
        ])
        with patch("src.web_server.uc", return_value=type("B", (), {"bookorbit_client": client})()):
            got = _prefetch_bookorbit_authors(books, {"bookorbit": True})
        self.assertEqual(got, {"11": "Ann Author"})

    def test_skips_entirely_when_bookorbit_is_off(self):
        """Same gate the dashboard tile uses; no client call at all."""
        books = [_Book(ebook_source="BookOrbit", ebook_source_id="11")]
        with patch("src.web_server.uc", side_effect=AssertionError("must not be called")):
            self.assertEqual(_prefetch_bookorbit_authors(books, {"bookorbit": False}), {})

    def test_skips_when_no_book_came_from_bookorbit(self):
        books = [_Book(ebook_source="Kavita", ebook_source_id="9")]
        with patch("src.web_server.uc", side_effect=AssertionError("must not be called")):
            self.assertEqual(_prefetch_bookorbit_authors(books, {"bookorbit": True}), {})

    def test_a_client_failure_degrades_to_no_authors(self):
        """A dashboard render must not fail because a library is unreachable."""
        books = [_Book(ebook_source="BookOrbit", ebook_source_id="11")]
        with patch("src.web_server.uc", side_effect=RuntimeError("boom")):
            self.assertEqual(_prefetch_bookorbit_authors(books, {"bookorbit": True}), {})

    def test_blank_authors_are_not_recorded(self):
        books = [_Book(ebook_source="BookOrbit", ebook_source_id="11")]
        client = self._client([{"id": 11, "title": "T", "authors": "   "}])
        with patch("src.web_server.uc", return_value=type("B", (), {"bookorbit_client": client})()):
            self.assertEqual(_prefetch_bookorbit_authors(books, {"bookorbit": True}), {})


class TestBookOrbitAuthorForBook(unittest.TestCase):

    def test_reads_the_ebook_side(self):
        book = _Book(ebook_source="BookOrbit", ebook_source_id="11")
        self.assertEqual(_bookorbit_author_for_book(book, {"11": "Ann Author"}), "Ann Author")

    def test_reads_the_audio_side(self):
        book = _Book(sync_mode="audiobook_only", audio_source="BookOrbit", audio_source_id="22")
        self.assertEqual(_bookorbit_author_for_book(book, {"22": "Bea Writer"}), "Bea Writer")

    def test_absent_book_yields_empty(self):
        book = _Book(ebook_source="BookOrbit", ebook_source_id="11")
        self.assertEqual(_bookorbit_author_for_book(book, {"99": "Someone"}), "")

    def test_no_prefetch_yields_empty(self):
        book = _Book(ebook_source="BookOrbit", ebook_source_id="11")
        self.assertEqual(_bookorbit_author_for_book(book, None), "")


class TestResolverRanking(unittest.TestCase):
    """Where BookOrbit's author sits among the other sources."""

    def _resolve(self, book, **kwargs):
        # The dashboard always hands the resolver a prefetched Grimmory dict; an
        # empty one keeps the lookup off the module-global database_service.
        kwargs.setdefault("cached_booklore_by_filename", {})
        return _resolve_dashboard_display_metadata(
            book, "Some Title", "", "", **kwargs
        )

    def test_fills_the_gap_a_bare_filename_leaves(self):
        """The reported symptom: no ' - ' in the stem, so no author at all."""
        book = _Book(ebook_filename="03. Other Worlds Than These (2026).epub")
        self.assertEqual(self._resolve(book)["display_author"], "")
        self.assertEqual(
            self._resolve(book, bookorbit_author="Ann Author")["display_author"],
            "Ann Author",
        )

    def test_does_not_displace_the_storyteller_author(self):
        """Ranked below the caches: it may only ever fill a gap."""
        book = _Book(ebook_filename="Anything.epub")
        got = self._resolve(
            book,
            storyteller_meta={"author": "Cached Author"},
            bookorbit_author="BookOrbit Author",
        )
        self.assertEqual(got["display_author"], "Cached Author")

    def test_outranks_the_filename_guess(self):
        """Real library metadata beats a stem split on ' - '."""
        book = _Book(ebook_filename="Some Title - Guessed Author.epub")
        self.assertEqual(
            self._resolve(book, bookorbit_author="Ann Author")["display_author"],
            "Ann Author",
        )

    def test_filename_still_wins_when_bookorbit_has_nothing(self):
        book = _Book(ebook_filename="Some Title - Guessed Author.epub")
        self.assertEqual(
            self._resolve(book, bookorbit_author="")["display_author"],
            "Guessed Author",
        )

    def test_author_is_whitespace_normalized(self):
        book = _Book(ebook_filename="Anything.epub")
        self.assertEqual(
            self._resolve(book, bookorbit_author="  Ann   Author ")["display_author"],
            "Ann Author",
        )

    def test_title_is_left_alone(self):
        """Deliberately author-only; BookOrbit titles are not adopted here."""
        book = _Book(ebook_filename="03. Other Worlds Than These (2026).epub")
        before = self._resolve(book)["display_title"]
        after = self._resolve(book, bookorbit_author="Ann Author")["display_title"]
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
