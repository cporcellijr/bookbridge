"""Bulk Calibre identifier lookups stay local and distinguish failed reads."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.services import calibre_identifier_resolver as resolver_module
from src.services.calibre_identifier_resolver import CalibreIdentifierResolver


class TestCalibreIdentifierBulk(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "metadata.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT)"
            )
            conn.executemany(
                "INSERT INTO identifiers (book, type, val) VALUES (?, ?, ?)",
                [
                    (0, "audiobookshelf_id", "abs-zero"),
                    (42, "audiobookshelf_id", "abs-42"),
                    (43, "AUDIOBOOKSHELF_ID", "abs-43"),
                    (44, "isbn", "not-abs"),
                ],
            )
        self.saved_env = {
            key: os.environ.get(key)
            for key in ("CALIBRE_USE_ABS_IDENTIFIER", "CALIBRE_LIBRARY_PATH")
        }
        os.environ["CALIBRE_USE_ABS_IDENTIFIER"] = "true"
        os.environ["CALIBRE_LIBRARY_PATH"] = self.tmp_dir

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_bulk_reads_once_maps_every_id_and_never_calls_cwa(self):
        client = MagicMock()
        resolver = CalibreIdentifierResolver(cwa_client=client)
        real_connect = sqlite3.connect
        connections = []
        statements = []

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(resolver_module.sqlite3, "connect", side_effect=connect):
            result = resolver.get_abs_ids([0, "42", 43, 44, 999])

        self.assertEqual(result, {
            "0": "abs-zero",
            "42": "abs-42",
            "43": "abs-43",
            "44": None,
            "999": None,
        })
        self.assertEqual(len(connections), 1)
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)
        client.session.get.assert_not_called()
        client.is_configured.assert_not_called()
        self.assertEqual(resolver.get_abs_id(42), "abs-42")

    def test_batch_rereads_edits_and_updates_scalar_cache(self):
        resolver = CalibreIdentifierResolver()
        self.assertEqual(resolver.get_abs_id(42), "abs-42")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE identifiers SET val = 'changed' WHERE book = 42 "
                "AND LOWER(type) = 'audiobookshelf_id'"
            )
        self.assertEqual(resolver.get_abs_ids([42]), {"42": "changed"})
        self.assertEqual(resolver.get_abs_id(42), "changed")

    def test_unavailable_or_corrupt_db_returns_none(self):
        resolver = CalibreIdentifierResolver(cwa_client=MagicMock())
        os.environ["CALIBRE_LIBRARY_PATH"] = str(Path(self.tmp_dir) / "missing")
        self.assertIsNone(resolver.get_abs_ids([42]))

        corrupt = Path(self.tmp_dir) / "corrupt.db"
        corrupt.write_text("not a sqlite database", encoding="utf-8")
        os.environ["CALIBRE_LIBRARY_PATH"] = str(corrupt)
        self.assertIsNone(resolver.get_abs_ids([42]))
        resolver._cwa_client.session.get.assert_not_called()

    def test_disabled_and_empty_inputs_return_empty_mapping(self):
        resolver = CalibreIdentifierResolver()
        os.environ["CALIBRE_USE_ABS_IDENTIFIER"] = "false"
        self.assertEqual(resolver.get_abs_ids([42]), {})
        os.environ["CALIBRE_USE_ABS_IDENTIFIER"] = "true"
        self.assertEqual(resolver.get_abs_ids([]), {})
        self.assertEqual(resolver.get_abs_ids(None), {})

    def test_old_scalar_miss_does_not_hide_later_authoritative_batch_hit(self):
        os.environ["CALIBRE_LIBRARY_PATH"] = ""
        resolver = CalibreIdentifierResolver()
        self.assertIsNone(resolver.get_abs_id(42))
        os.environ["CALIBRE_LIBRARY_PATH"] = self.tmp_dir
        self.assertEqual(resolver.get_abs_ids([42]), {"42": "abs-42"})
        self.assertEqual(resolver.get_abs_id(42), "abs-42")


if __name__ == "__main__":
    unittest.main()
