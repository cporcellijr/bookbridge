import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.services.calibre_identifier_resolver import CalibreIdentifierResolver


def _build_calibre_db(path: Path, rows):
    """Create a minimal Calibre-like metadata.db with an identifiers table."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE books ("
            "id INTEGER PRIMARY KEY, title TEXT)"
        )
        conn.execute(
            "CREATE TABLE identifiers ("
            "id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT)"
        )
        for book_id, ident_type, val in rows:
            conn.execute(
                "INSERT INTO books (id, title) VALUES (?, ?)",
                (book_id, f"book {book_id}"),
            )
            conn.execute(
                "INSERT INTO identifiers (book, type, val) VALUES (?, ?, ?)",
                (book_id, ident_type, val),
            )
        conn.commit()
    finally:
        conn.close()


class TestCalibreIdentifierResolver(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "metadata.db"
        _build_calibre_db(
            self.db_path,
            [
                (42, "audiobookshelf_id", "abs-uuid-42"),
                (43, "AUDIOBOOKSHELF_ID", "abs-uuid-43-upper"),
                (44, "isbn", "9780000000000"),
            ],
        )

        self._saved_env = {
            k: os.environ.get(k)
            for k in ("CALIBRE_USE_ABS_IDENTIFIER", "CALIBRE_LIBRARY_PATH")
        }
        os.environ["CALIBRE_USE_ABS_IDENTIFIER"] = "true"
        os.environ["CALIBRE_LIBRARY_PATH"] = self.tmp_dir

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_disabled_returns_none(self):
        os.environ["CALIBRE_USE_ABS_IDENTIFIER"] = "false"
        resolver = CalibreIdentifierResolver()
        self.assertIsNone(resolver.get_abs_id(42))

    def test_sqlite_hit(self):
        resolver = CalibreIdentifierResolver()
        self.assertEqual(resolver.get_abs_id(42), "abs-uuid-42")

    def test_sqlite_case_insensitive_type(self):
        resolver = CalibreIdentifierResolver()
        self.assertEqual(resolver.get_abs_id(43), "abs-uuid-43-upper")

    def test_sqlite_miss_returns_none(self):
        resolver = CalibreIdentifierResolver()
        self.assertIsNone(resolver.get_abs_id(44))
        self.assertIsNone(resolver.get_abs_id(9999))

    def test_string_book_id_accepted(self):
        resolver = CalibreIdentifierResolver()
        self.assertEqual(resolver.get_abs_id("42"), "abs-uuid-42")

    def test_caches_result(self):
        resolver = CalibreIdentifierResolver()
        self.assertEqual(resolver.get_abs_id(42), "abs-uuid-42")
        # Mutate DB to verify cache returns prior result
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE identifiers SET val = 'changed' WHERE book = 42")
        conn.commit()
        conn.close()
        self.assertEqual(resolver.get_abs_id(42), "abs-uuid-42")
        resolver.refresh()
        self.assertEqual(resolver.get_abs_id(42), "changed")

    def test_missing_path_falls_through(self):
        os.environ["CALIBRE_LIBRARY_PATH"] = "/nonexistent/path/that/does/not/exist"
        resolver = CalibreIdentifierResolver()
        self.assertIsNone(resolver.get_abs_id(42))

    def test_cwa_fallback_used_when_sqlite_unavailable(self):
        os.environ["CALIBRE_LIBRARY_PATH"] = ""
        cwa_client = MagicMock()
        cwa_client.is_configured.return_value = True
        cwa_client.base_url = "http://cwa.test"
        cwa_client.timeout = 5

        response = MagicMock()
        response.status_code = 200
        response.text = '{"identifiers": {"audiobookshelf_id": "from-cwa-99"}}'
        response.json.return_value = {
            "identifiers": {"audiobookshelf_id": "from-cwa-99"}
        }
        cwa_client.session.get.return_value = response

        resolver = CalibreIdentifierResolver(cwa_client=cwa_client)
        self.assertEqual(resolver.get_abs_id(99), "from-cwa-99")
        cwa_client.session.get.assert_called_once()
        called_url = cwa_client.session.get.call_args[0][0]
        self.assertEqual(called_url, "http://cwa.test/ajax/book/99")

    def test_cwa_fallback_html_redirect_returns_none(self):
        os.environ["CALIBRE_LIBRARY_PATH"] = ""
        cwa_client = MagicMock()
        cwa_client.is_configured.return_value = True
        cwa_client.base_url = "http://cwa.test"
        cwa_client.timeout = 5
        response = MagicMock()
        response.status_code = 200
        response.text = "<!DOCTYPE html><html>login</html>"
        cwa_client.session.get.return_value = response

        resolver = CalibreIdentifierResolver(cwa_client=cwa_client)
        self.assertIsNone(resolver.get_abs_id(7))

    def test_cwa_fallback_skipped_when_sqlite_succeeds(self):
        cwa_client = MagicMock()
        cwa_client.is_configured.return_value = True
        resolver = CalibreIdentifierResolver(cwa_client=cwa_client)
        self.assertEqual(resolver.get_abs_id(42), "abs-uuid-42")
        cwa_client.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
