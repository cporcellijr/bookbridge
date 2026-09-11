import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.api.cwa_client import CWAClient

class TestCWAClient(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict('os.environ', {
            'CWA_ENABLED': 'true',
            'CWA_SERVER': 'http://cwa:8083',
            'CWA_USERNAME': 'user',
            'CWA_PASSWORD': 'pass'
        })
        self.env_patcher.start()
        self.client = CWAClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch('requests.Session.get')
    def test_search_ebooks_parsing(self, mock_get):
        # Mock XML response (Atom)
        mock_response_content = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
            <entry>
                <title>Test Book</title>
                <author>
                    <name>Test Author</name>
                </author>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/download/123/epub" />
            </entry>
        </feed>
        """
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = mock_response_content
        
        results = self.client.search_ebooks("Test Book")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Test Book')
        self.assertEqual(results[0]['author'], 'Test Author')
        self.assertEqual(results[0]['download_url'], 'http://cwa:8083/download/123/epub')

    @patch('requests.Session.get')
    def test_download_ebook(self, mock_get):
        # The download is staged beside the destination and published on success,
        # so the assertion is on what lands on disk rather than on the open() call.
        payload = b"fake content" * 100
        mock_get.return_value.__enter__.return_value.status_code = 200
        mock_get.return_value.__enter__.return_value.headers = {}
        mock_get.return_value.__enter__.return_value.iter_content.return_value = [payload]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'test.epub'
            success = self.client.download_ebook('http://url', str(target))

            self.assertTrue(success)
            self.assertEqual(target.read_bytes(), payload)

    def test_get_book_by_id_rejects_missing_identifier(self):
        with patch.object(self.client.session, 'get') as mock_get:
            self.assertIsNone(self.client.get_book_by_id(None))
            self.assertIsNone(self.client.get_book_by_id('None'))

        mock_get.assert_not_called()

    # -- get_book_uuid (issue #427: series search must resolve the right book) --

    # A CWA series search: "The Butcher's Masquerade" is returned first, the
    # selected book "Dungeon Crawler Carl" is fourth. CWA uses urn:uuid atom ids
    # and /opds/download/<id>/<fmt>/ acquisition links.
    _SERIES_FEED = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>The Butcher's Masquerade</title>
            <id>urn:uuid:6eae08f0-1622-4287-a767-359f84f15834</id>
            <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                  href="/opds/download/509/epub/" />
        </entry>
        <entry>
            <title>Dungeon Crawler Carl</title>
            <id>urn:uuid:d02f40b4-873a-4d04-8c56-ffcf3033979d</id>
            <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                  href="/opds/download/505/epub/" />
        </entry>
    </feed>
    """

    def _mock_search(self, mock_get, feed):
        # Skip endpoint discovery so the mock only serves the search response.
        self.client.search_template = 'http://cwa:8083/opds/search/{searchTerms}'
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = feed

    @patch('requests.Session.get')
    def test_get_book_uuid_selects_matching_series_entry(self, mock_get):
        # Stored id is the title-derived slug; must resolve to Dungeon Crawler
        # Carl's UUID, not the first (The Butcher's Masquerade) entry.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        self.assertEqual(uuid, 'd02f40b4-873a-4d04-8c56-ffcf3033979d')

    @patch('requests.Session.get')
    def test_get_book_uuid_ambiguous_returns_none(self, mock_get):
        # A multi-result search with no entry matching the stored id must not
        # guess — it returns None so the sync is skipped rather than corrupting
        # another book's progress.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('Some_Other_Book')
        self.assertIsNone(uuid)

    @patch('requests.Session.get')
    def test_get_book_uuid_single_result_is_unambiguous(self, mock_get):
        single_feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Solo Book</title>
                <id>urn:uuid:11111111-2222-3333-4444-555555555555</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/700/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, single_feed)
        uuid = self.client.get_book_uuid('Solo_Book')
        self.assertEqual(uuid, '11111111-2222-3333-4444-555555555555')

    @patch('requests.Session.get')
    def test_get_book_uuid_matches_numeric_download_id(self, mock_get):
        # When the stored id is the numeric Calibre id, match it against the
        # download link even though the entry is not first in the feed.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('505')
        self.assertEqual(uuid, 'd02f40b4-873a-4d04-8c56-ffcf3033979d')

    # -- _parse_opds id extraction (issue #427 defect 1: regex asymmetry) --

    def test_parse_opds_extracts_numeric_id_from_download_link(self):
        # CWA's acquisition link is /opds/download/<id>/epub/, not /book/<id>
        # or /books/<id>. _parse_opds must recognize it too (get_book_uuid
        # already did) so a numeric Calibre id is stored instead of falling
        # back to a truncated title slug.
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Only A Download Link</title>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/505/epub/" />
            </entry>
        </feed>
        """
        results = self.client._parse_opds(feed)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], '505')

    def test_parse_opds_falls_back_to_title_slug_without_numeric_id(self):
        # No link and no atom:id carries a numeric id here, so the fallback
        # chain (atom:id, then title slug) must still produce the slug.
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>No Numeric Anywhere</title>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/abc/epub/" />
            </entry>
        </feed>
        """
        results = self.client._parse_opds(feed)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'No_Numeric_Anywhere')

    # -- get_book_uuid single-result corroboration (defect 2) --

    @patch('requests.Session.get')
    def test_get_book_uuid_single_result_without_corroboration_returns_none(self, mock_get):
        # A lone search result whose title slug has nothing to do with the
        # stored key must not be accepted blindly — that is the exact #427
        # failure (a renamed book + a loose fuzzy match).
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Completely Different Book</title>
                <id>urn:uuid:99999999-8888-7777-6666-555555555555</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/999/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, feed)
        uuid = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        self.assertIsNone(uuid)

    @patch('requests.Session.get')
    def test_get_book_uuid_refuses_a_series_sibling_whose_slug_is_a_prefix(self, mock_get):
        # The reason a lone result is never accepted on its own. This stored key
        # is the 30-char truncation of "Dungeon Crawler Carl: The Butcher's
        # Masquerade", and the only search hit is the series opener "Dungeon
        # Crawler Carl" — a DIFFERENT book whose slug happens to prefix the key.
        # Binding them is precisely the wrong-book corruption #427 reported, so
        # any prefix/fuzzy relaxation of the slug test must stay out of here.
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Dungeon Crawler Carl</title>
                <id>urn:uuid:22222222-3333-4444-5555-666666666666</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/321/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, feed)
        truncated_key = "Dungeon_Crawler_Carl__The_Butc"  # 30 chars, as _parse_opds would have stored it
        self.assertEqual(len(truncated_key), 30)
        self.assertIsNone(self.client.get_book_uuid(truncated_key))

    @patch('requests.Session.get')
    def test_get_book_uuid_slug_match_is_case_insensitive(self, mock_get):
        # The slug is derived from the title, so a capitalisation edit in Calibre
        # must not orphan an existing mapping.
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>DUNGEON CRAWLER CARL</title>
                <id>urn:uuid:22222222-3333-4444-5555-666666666666</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/321/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, feed)
        uuid = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        self.assertEqual(uuid, '22222222-3333-4444-5555-666666666666')

    # -- get_book_uuid negative-result caching (defect 3) --

    @patch('requests.Session.get')
    def test_get_book_uuid_failed_resolution_is_not_cached(self, mock_get):
        # This client is a DI Singleton, so caching a None would wedge CWA
        # sync for the book until the process restarts. A failure must be
        # retried on the next call, not served from cache.
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Completely Different Book</title>
                <id>urn:uuid:99999999-8888-7777-6666-555555555555</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/999/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, feed)
        self.assertIsNone(self.client.get_book_uuid('Dungeon_Crawler_Carl'))
        self.assertIsNone(self.client.get_book_uuid('Dungeon_Crawler_Carl'))
        self.assertEqual(mock_get.call_count, 2)

    @patch('requests.Session.get')
    def test_get_book_uuid_successful_resolution_is_cached(self, mock_get):
        # A resolved UUID is still worth caching; only negative results must
        # bypass the cache.
        self._mock_search(mock_get, self._SERIES_FEED)
        first = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        second = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        self.assertEqual(first, 'd02f40b4-873a-4d04-8c56-ffcf3033979d')
        self.assertEqual(second, first)
        self.assertEqual(mock_get.call_count, 1)


class TestCWADownloadPublication(unittest.TestCase):
    """A failed ebook download must never replace a good file in the cache."""

    def setUp(self):
        self.env_patcher = patch.dict('os.environ', {
            'CWA_ENABLED': 'true',
            'CWA_SERVER': 'http://cwa:8083',
            'CWA_USERNAME': 'user',
            'CWA_PASSWORD': 'pass',
        })
        self.env_patcher.start()
        self.client = CWAClient()
        self.client.session = MagicMock()

    def tearDown(self):
        self.env_patcher.stop()

    def _respond(self, chunks, headers):
        response = MagicMock()
        response.status_code = 200
        response.headers = headers
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks
        self.client.session.get.return_value = response

    def test_truncated_download_preserves_existing_ebook(self):
        self._respond([b'x' * 500], {'Content-Length': '4096'})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'book.epub'
            target.write_bytes(b'a previously downloaded epub')

            self.assertFalse(self.client.download_ebook('http://cwa:8083/dl/1', str(target)))
            self.assertEqual(target.read_bytes(), b'a previously downloaded epub')
            self.assertEqual(list(Path(tmp).glob('*.part')), [])

    def test_error_page_is_rejected_as_too_small(self):
        self._respond([b'<html>Not found</html>'], {})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'book.epub'

            self.assertFalse(self.client.download_ebook('http://cwa:8083/dl/1', str(target)))
            self.assertFalse(target.exists())

    def test_complete_download_is_published(self):
        self._respond([b'y' * 4096], {'Content-Length': '4096'})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'book.epub'

            self.assertTrue(self.client.download_ebook('http://cwa:8083/dl/1', str(target)))
            self.assertEqual(target.stat().st_size, 4096)

    def test_transparently_decoded_body_is_accepted(self):
        self._respond([b'z' * 4096], {'Content-Length': '900', 'Content-Encoding': 'gzip'})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'book.epub'

            self.assertTrue(self.client.download_ebook('http://cwa:8083/dl/1', str(target)))
            self.assertEqual(target.stat().st_size, 4096)
