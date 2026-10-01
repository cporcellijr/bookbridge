"""Regression tests for comic (CBZ) linking from library providers.

Covers the maintainer-requested behaviors from PR #459:

1. get_searchable_ebooks() surfaces a Grimmory '.cbz' result (previously the
   '.epub'-only filter hid every comic), while non-CBX comic containers like
   '.cbr' stay excluded — the fixed-page progress path only understands CBZ.
2. A comic selected alongside an audiobook cannot become a paired mapping:
   the Add Book queue forces the item to ebook-only, and the direct /match
   route rejects the pairing outright (the audio path would see empty ebook
   text and could queue a pointless Whisper transcription).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_webserver import MockContainer

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class LinkableFilenameHelperTest(unittest.TestCase):
    def test_cbz_and_epub_are_linkable(self):
        from src.utils.ebook_utils import is_linkable_ebook_filename

        self.assertTrue(is_linkable_ebook_filename(
            "Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz"))
        self.assertTrue(is_linkable_ebook_filename("Book.epub"))
        self.assertTrue(is_linkable_ebook_filename("BOOK.CBZ"))
        self.assertFalse(is_linkable_ebook_filename("book.cbr"))
        self.assertFalse(is_linkable_ebook_filename("book.pdf"))
        self.assertFalse(is_linkable_ebook_filename(None))
        self.assertFalse(is_linkable_ebook_filename(""))

    def test_comic_helper_is_cbz_only(self):
        from src.utils.ebook_utils import is_comic_ebook_filename

        self.assertTrue(is_comic_ebook_filename("comic.cbz"))
        self.assertFalse(is_comic_ebook_filename("comic.cbr"))
        self.assertFalse(is_comic_ebook_filename("book.epub"))
        self.assertFalse(is_comic_ebook_filename(None))


class GrimmoryCbzSearchTest(unittest.TestCase):
    """get_searchable_ebooks must surface Grimmory comic rows, not just EPUBs."""

    def _clients(self, booklore_results):
        booklore = MagicMock()
        booklore.is_configured.return_value = True
        booklore.search_books.return_value = booklore_results
        return SimpleNamespace(
            booklore_client=booklore,
            bookorbit_client=MagicMock(is_configured=MagicMock(return_value=False)),
            bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
            abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
            library_service=None,
        )

    def test_get_searchable_ebooks_includes_grimmory_cbz(self):
        import src.web_server as ws

        clients = self._clients([
            {"id": 51, "fileName": "Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz",
             "title": "Fullmetal Alchemist, Vol. 1", "authors": "Hiromu Arakawa"},
        ])
        with patch.object(ws, "uc", return_value=clients), patch.object(
            ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True
        ):
            results = ws.get_searchable_ebooks("Fullmetal Alchemist")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "Grimmory")
        self.assertEqual(results[0].name,
                         "Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz")

    def test_get_searchable_ebooks_still_skips_unknown_comic_containers(self):
        import src.web_server as ws

        clients = self._clients([
            {"id": 52, "fileName": "Some Comic.cbr", "title": "Some Comic"},
        ])
        with patch.object(ws, "uc", return_value=clients), patch.object(
            ws, "EBOOK_DIR", Path("__missing_books_dir__"), create=True
        ):
            results = ws.get_searchable_ebooks("Some Comic")

        self.assertEqual(results, [])


class ComicAudiobookPairingTest(unittest.TestCase):
    """A comic must never be paired with an audiobook mapping."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        from src.db.database_service import DatabaseService

        self.svc = DatabaseService(os.path.join(self.tmp, "comics.db"))
        self.admin = self.svc.create_user("admin-comic", "adminpw", role="admin")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        resp = self.client.post('/login', data={
            'username': 'admin-comic', 'password': 'adminpw',
        })
        self.assertEqual(resp.status_code, 302, "login failed for admin-comic")

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_match_route_rejects_comic_with_audiobook(self):
        resp = self.client.post('/match', data={
            'audio_source': 'Grimmory',
            'audio_source_id': '51',
            'ebook_filename': 'Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz',
            'ebook_source': 'Booklore',
            'ebook_source_id': '51',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"ebook-only", resp.data)

    def test_queue_item_forces_ebook_only_for_comic(self):
        import src.web_server as ws

        clients = SimpleNamespace(
            booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
            storyteller_client=MagicMock(is_configured=MagicMock(return_value=False)),
        )
        form = {
            'audio_source': 'Grimmory',
            'audio_source_id': '51',
            'ebook_filename': 'Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz',
            'ebook_display_name': 'Fullmetal Alchemist, Vol. 1',
            'ebook_source': 'Booklore',
            'ebook_source_id': '51',
        }
        with patch.object(ws, "uc", return_value=clients):
            with self.app.test_request_context('/add-book', method='POST', data=form):
                item = ws._queue_item_from_match_form(clients)

        self.assertIsNotNone(item)
        self.assertIsNone(item['audio_source'])
        self.assertIsNone(item['audio_source_id'])
        self.assertEqual(item['ebook_filename'],
                         'Fullmetal Alchemist, Vol. 1 - Hiromu Arakawa (2014).cbz')


if __name__ == '__main__':
    unittest.main()
