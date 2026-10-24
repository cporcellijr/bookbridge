"""get_kosync_content_id hashes a file's bytes in either hash method."""

import hashlib

from src.utils.ebook_utils import EbookParser


def _koreader_partial_md5(content: bytes) -> str:
    md5 = hashlib.md5()
    for i in range(-1, 11):
        offset = 0 if i == -1 else 1024 * (4 ** i)
        if offset >= len(content):
            break
        md5.update(content[offset:offset + 1024])
    return md5.hexdigest()


def test_content_id_ignores_filename_mode(tmp_path):
    content = bytes(range(256)) * 400
    path = tmp_path / "White Teeth - Zadie Smith.epub"
    path.write_bytes(content)
    parser = EbookParser(books_dir=str(tmp_path))

    parser.hash_method = "filename"
    assert parser.get_kosync_id(path) == hashlib.md5(path.name.encode()).hexdigest()
    assert parser.get_kosync_content_id(path) == _koreader_partial_md5(content)

    parser.hash_method = "content"
    assert parser.get_kosync_id(path) == _koreader_partial_md5(content)
