import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient


class TestABSEbookItemLookup(unittest.TestCase):
    """Tests for ABSClient.find_ebook_item_for_file."""

    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"},
            clear=False,
        )
        self.env_patch.start()
        self.client = ABSClient()
        self.client.session = MagicMock()
        self.client.timeout = 30

    def tearDown(self):
        self.env_patch.stop()

    def _make_libraries_response(self, libs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"libraries": libs}
        return resp

    def _make_items_response(self, items):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": items}
        return resp

    def _make_item_details(self, ebook_file_meta):
        return {
            "media": {
                "ebookFile": {
                    "metadata": ebook_file_meta
                }
            }
        }

    def test_matches_across_different_mount_roots(self):
        """Matches file even when ABS path uses different mount root."""
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 1234)
            size = os.path.getsize(local_path)

            lib_id = "lib1"
            item_id = "item-123"
            # Library list
            libraries = [{"id": lib_id}]
            # Items listing
            items = [
                {
                    "id": item_id,
                    "path": "/dad audiobooks/Warlock",
                    "media": {"ebookFormat": "epub"},
                }
            ]
            # Mock session.get responses
            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib_id}/items" in url:
                    return self._make_items_response(items)
                if f"/api/items/{item_id}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {
                                "filename": "Warlock.epub",
                                "size": size,
                                "path": "/dad audiobooks/Warlock/Warlock.epub",
                            }
                        ),
                    )
                raise AssertionError(f"Unexpected URL {url}")

            self.client.session.get.side_effect = session_get

            result = self.client.find_ebook_item_for_file(local_path)
            self.assertEqual(result, item_id)

    def test_size_mismatch_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"x" * 10)

            lib_id = "lib1"
            item_id = "item-123"
            libraries = [{"id": lib_id}]
            items = [{"id": item_id, "path": "/a/Warlock", "media": {"ebookFormat": "epub"}}]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib_id}/items" in url:
                    return self._make_items_response(items)
                if f"/api/items/{item_id}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": 9999, "path": "/a/Warlock/Warlock.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))

    def test_supplementary_file_not_matched(self):
        """Item whose primary ebookFile is another filename should not match."""
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 42)
            size = os.path.getsize(local_path)

            lib_id = "lib1"
            item_id = "item-123"
            libraries = [{"id": lib_id}]
            items = [{"id": item_id, "path": "/a/Warlock", "media": {"ebookFormat": "epub"}}]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib_id}/items" in url:
                    return self._make_items_response(items)
                if f"/api/items/{item_id}" in url:
                    # primary ebook is different filename
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Other.epub", "size": size, "path": "/a/Warlock/Other.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))

    def test_two_libraries_same_depth_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 100)
            size = os.path.getsize(local_path)

            lib1 = "lib1"
            lib2 = "lib2"
            item1 = "item1"
            item2 = "item2"
            libraries = [{"id": lib1}, {"id": lib2}]
            items = [
                {"id": item1, "path": "/x/Warlock", "media": {"ebookFormat": "epub"}},
                {"id": item2, "path": "/y/Warlock", "media": {"ebookFormat": "epub"}},
            ]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib1}/items" in url:
                    return self._make_items_response([items[0]])
                if f"/api/libraries/{lib2}/items" in url:
                    return self._make_items_response([items[1]])
                if f"/api/items/{item1}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/x/Warlock/Warlock.epub"}
                        ),
                    )
                if f"/api/items/{item2}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/y/Warlock/Warlock.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))

    def test_deeper_matching_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            # local path: .../Author/Warlock/Warlock.epub
            local_path = os.path.join(tmp, "Author", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 55)
            size = os.path.getsize(local_path)

            lib_id = "lib1"
            item_a = "itemA"
            item_b = "itemB"
            libraries = [{"id": lib_id}]
            items = [
                {"id": item_b, "path": "/y/Other/Warlock", "media": {"ebookFormat": "epub"}},
                {"id": item_a, "path": "/x/Author/Warlock", "media": {"ebookFormat": "epub"}},
            ]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib_id}/items" in url:
                    return self._make_items_response(items)
                if f"/api/items/{item_a}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/x/Author/Warlock/Warlock.epub"}
                        ),
                    )
                if f"/api/items/{item_b}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/y/Other/Warlock/Warlock.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            result = self.client.find_ebook_item_for_file(local_path)
            self.assertEqual(result, item_a)

    def test_item_with_different_folder_name_not_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 10)

            lib_id = "lib1"
            item_id = "item-123"
            libraries = [{"id": lib_id}]
            # Folder name doesn't match parent or filename
            items = [{"id": item_id, "path": "/a/OtherFolder", "media": {"ebookFormat": "epub"}}]

            calls = []

            def session_get(url, timeout=30, params=None):
                calls.append(url)
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib_id}/items" in url:
                    return self._make_items_response(items)
                # Should not be called for item details
                raise AssertionError(f"Should not fetch details for {url}")

            self.client.session.get.side_effect = session_get
            result = self.client.find_ebook_item_for_file(local_path)
            self.assertIsNone(result)
            # Ensure get_item_details not called
            details_urls = [u for u in calls if "/api/items/" in u and "/file/" not in u]
            self.assertEqual(len(details_urls), 0)

    def test_library_list_non_200_returns_none(self):
        """Library listing non-200 should fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 100)
            size = os.path.getsize(local_path)

            lib1 = "lib1"
            lib2 = "lib2"
            item1 = "item1"
            item2 = "item2"
            libraries = [{"id": lib1}, {"id": lib2}]
            items_lib1 = [{"id": item1, "path": "/x/Warlock", "media": {"ebookFormat": "epub"}}]
            items_lib2 = [{"id": item2, "path": "/y/Warlock", "media": {"ebookFormat": "epub"}}]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib1}/items" in url:
                    return self._make_items_response(items_lib1)
                if f"/api/libraries/{lib2}/items" in url:
                    resp = MagicMock()
                    resp.status_code = 503
                    resp.json.return_value = {}
                    return resp
                if f"/api/items/{item1}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/x/Warlock/Warlock.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))

    def test_item_details_non_200_returns_none(self):
        """Item details returning falsy should fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 100)
            size = os.path.getsize(local_path)

            lib1 = "lib1"
            lib2 = "lib2"
            item1 = "item1"
            item2 = "item2"
            libraries = [{"id": lib1}, {"id": lib2}]
            items_lib1 = [{"id": item1, "path": "/x/Warlock", "media": {"ebookFormat": "epub"}}]
            items_lib2 = [{"id": item2, "path": "/y/Warlock", "media": {"ebookFormat": "epub"}}]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib1}/items" in url:
                    return self._make_items_response(items_lib1)
                if f"/api/libraries/{lib2}/items" in url:
                    return self._make_items_response(items_lib2)
                if f"/api/items/{item1}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/x/Warlock/Warlock.epub"}
                        ),
                    )
                if f"/api/items/{item2}" in url:
                    resp = MagicMock()
                    resp.status_code = 500
                    resp.json.return_value = {}
                    return resp
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))

    def test_library_list_exception_returns_none(self):
        """Library listing exception should fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            local_path = os.path.join(tmp, "Library", "Warlock", "Warlock.epub")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(b"a" * 100)
            size = os.path.getsize(local_path)

            lib1 = "lib1"
            lib2 = "lib2"
            item1 = "item1"
            item2 = "item2"
            libraries = [{"id": lib1}, {"id": lib2}]
            items_lib1 = [{"id": item1, "path": "/x/Warlock", "media": {"ebookFormat": "epub"}}]
            items_lib2 = [{"id": item2, "path": "/y/Warlock", "media": {"ebookFormat": "epub"}}]

            def session_get(url, timeout=30, params=None):
                if url.endswith("/api/libraries"):
                    return self._make_libraries_response(libraries)
                if f"/api/libraries/{lib1}/items" in url:
                    return self._make_items_response(items_lib1)
                if f"/api/libraries/{lib2}/items" in url:
                    raise requests.exceptions.ConnectionError("boom")
                if f"/api/items/{item1}" in url:
                    return MagicMock(
                        status_code=200,
                        json=lambda: self._make_item_details(
                            {"filename": "Warlock.epub", "size": size, "path": "/x/Warlock/Warlock.epub"}
                        ),
                    )
                raise AssertionError(url)

            self.client.session.get.side_effect = session_get
            self.assertIsNone(self.client.find_ebook_item_for_file(local_path))


if __name__ == "__main__":
    unittest.main()
