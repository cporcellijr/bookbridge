"""Per-reader picture uploads (shelf trinkets): what is accepted, limits, and where files live."""

import tempfile
import unittest
from pathlib import Path

from src.utils import user_uploads
from src.utils.user_uploads import UploadRejected

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'


class TestImageKind(unittest.TestCase):
    def test_accepts_svg_png_webp_by_content(self):
        self.assertEqual(user_uploads.image_kind(PNG), "png")
        self.assertEqual(user_uploads.image_kind(WEBP), "webp")
        self.assertEqual(user_uploads.image_kind(SVG), "svg")
        self.assertEqual(user_uploads.image_kind(b'<?xml version="1.0"?>\n<svg><rect/></svg>'), "svg")

    def test_rejects_everything_else(self):
        for data in (b"GIF89a....", b"<html><body>hi</body></html>", b'<?xml version="1.0"?><note/>',
                     b"<svg", b"not a picture", b"\xff\xd8\xff\xe0 jpeg"):
            with self.subTest(data=data[:12]):
                self.assertIsNone(user_uploads.image_kind(data))

    def test_svg_entities_are_never_expanded(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY x SYSTEM "file:///etc/passwd">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"><text>&b;&x;</text></svg>')
        # Parsing only proves it is an SVG; the file itself is stored byte for byte.
        self.assertEqual(user_uploads.image_kind(bomb), "svg")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, data, user=1, max_bytes=1000, max_count=3):
        return user_uploads.save_upload(self.data, user, "trinkets", data, max_bytes, max_count)

    def test_saves_under_the_readers_own_folder_with_a_random_name(self):
        name = self.save(SVG)
        self.assertRegex(name, r"^[0-9a-f]{12}\.svg$")
        path = self.data / "uploads" / "1" / "trinkets" / name
        self.assertEqual(path.read_bytes(), SVG)
        self.assertEqual(user_uploads.list_uploads(self.data, 1, "trinkets"), [name])
        self.assertEqual(user_uploads.list_uploads(self.data, 2, "trinkets"), [])
        self.assertEqual(user_uploads.upload_file(self.data, 1, "trinkets", name), path)
        self.assertIsNone(user_uploads.upload_file(self.data, 2, "trinkets", name), "another reader cannot reach it")

    def test_limits_and_reasons(self):
        cases = [(b"", "empty"), (PNG + b"\x00" * 1000, "too-big"), (b"<html/>", "not-an-image")]
        for data, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(UploadRejected) as caught:
                self.save(data)
            self.assertEqual(caught.exception.reason, reason)
        for data in (SVG, PNG, WEBP):
            self.save(data)
        with self.assertRaises(UploadRejected) as caught:
            self.save(SVG)
        self.assertEqual(caught.exception.reason, "full")
        self.assertEqual(len(user_uploads.list_uploads(self.data, 1, "trinkets")), 3)

    def test_names_outside_the_pattern_never_reach_the_disk(self):
        (self.data / "secret.svg").write_bytes(SVG)
        for name in ("../../secret.svg", "..", "", "ABCDEF012345.svg", "0123456789ab.svg.exe", "0123456789ab.gif"):
            with self.subTest(name=name):
                self.assertIsNone(user_uploads.upload_file(self.data, 1, "trinkets", name))
                self.assertFalse(user_uploads.delete_upload(self.data, 1, "trinkets", name))
        self.assertTrue((self.data / "secret.svg").exists())

    def test_delete_removes_only_that_file(self):
        keep, drop = self.save(SVG), self.save(PNG)
        self.assertTrue(user_uploads.delete_upload(self.data, 1, "trinkets", drop))
        self.assertFalse(user_uploads.delete_upload(self.data, 1, "trinkets", drop))
        self.assertEqual(user_uploads.list_uploads(self.data, 1, "trinkets"), [keep])


if __name__ == "__main__":
    unittest.main()
