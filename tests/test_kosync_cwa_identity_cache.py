"""The CWA identity check on every KoSync PUT queries Calibre's metadata.db at most once per
(hash, book) until the recheck window passes, negative results included (pre-release review #4)."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.api import kosync_server
from src.utils.ebook_utils import LRUCache


class TestAdoptCwaIdentityCache(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (('_cwa_identity_checked', LRUCache(capacity=1024)),
                            ('_database_service', MagicMock())):
            patcher = patch.object(kosync_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.book = SimpleNamespace(abs_id='abs-1', abs_title='Title', ebook_source=None,
                                    original_ebook_filename=None, ebook_filename='Title - Author.epub')

    def test_negative_lookup_is_not_repeated_on_every_put(self) -> None:
        with patch('src.services.cwa_checksum_resolver.find_calibre_book_id', return_value=None) as lookup:
            for _ in range(3):
                kosync_server._adopt_cwa_identity('ab' * 16, self.book)
        lookup.assert_called_once_with('ab' * 16)

    def test_negative_lookup_is_retried_after_recheck_window(self) -> None:
        with (
            patch('src.services.cwa_checksum_resolver.find_calibre_book_id', return_value=None) as lookup,
            patch.object(kosync_server.time, 'monotonic', side_effect=[1000.0, 1001.0, 1000.0 + 3601, 1000.0 + 3601]),
        ):
            kosync_server._adopt_cwa_identity('ab' * 16, self.book)
            kosync_server._adopt_cwa_identity('ab' * 16, self.book)
            kosync_server._adopt_cwa_identity('ab' * 16, self.book)
        self.assertEqual(lookup.call_count, 2)


if __name__ == '__main__':
    unittest.main()
