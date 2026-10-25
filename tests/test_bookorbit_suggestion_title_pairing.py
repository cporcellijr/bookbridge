"""#549: a full Suggestions scan pairs BookOrbit's light candidates (clean title, no
filename) with files on the shared /books disk. Files named 'Author - Series 15 -
Title.epub' matched no filename key, so the BookOrbit candidate was dropped and the
same file was offered only as a Local File."""

import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.web_server as ws
from src.utils.local_ebook_metadata import clear_metadata_cache

REPORTED_FILENAME = "Ellis Peters - The Chronicles of Brother Cadfael 15 - The Confession of Brother Haluin.epub"


def _epub(path, title, author):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        zf.writestr("OEBPS/content.opf", (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
            '</metadata></package>'
        ))


def _clients(bookorbit_books):
    return SimpleNamespace(
        booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookorbit_client=MagicMock(is_configured=MagicMock(return_value=True),
                                   get_all_ebooks=MagicMock(return_value=bookorbit_books)),
        bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
        kavita_client=MagicMock(is_configured=MagicMock(return_value=False)),
        abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )


def test_full_scan_pairs_bookorbit_title_with_author_series_title_filename(tmp_path):
    clear_metadata_cache()
    folder = tmp_path / "Ellis Peters" / "The Confession of Brother Haluin"
    folder.mkdir(parents=True)
    _epub(folder / REPORTED_FILENAME, "The Confession of Brother Haluin", "Ellis Peters")
    bookorbit = [{"id": 4242, "title": "The Confession of Brother Haluin", "authors": "Ellis Peters",
                  "language": "", "fileName": None}]

    with patch.object(ws, "EBOOK_DIR", tmp_path, create=True), patch.object(ws, "uc", return_value=_clients(bookorbit)):
        results = ws.get_searchable_ebooks("")

    assert [(r.name, r.source, r.source_id) for r in results] == [(REPORTED_FILENAME, "BookOrbit", 4242)]


def test_filename_keys_still_pair_title_author_names(tmp_path):
    clear_metadata_cache()
    (tmp_path / "Dune - Frank Herbert (1965).epub").write_bytes(b"not a zip")

    with patch.object(ws, "EBOOK_DIR", tmp_path, create=True):
        index = ws._build_local_ebook_title_index()

    assert index["dune"] == "Dune - Frank Herbert (1965).epub"
