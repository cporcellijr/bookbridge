"""Local File candidates are built from the filename alone, so scoring compared the
audiobook title against the "Title - Author" stem with no candidate author, which capped
an exact title-and-author match near 50%. The title and authors now come from Calibre's
metadata.opf, then the EPUB's own OPF, then the filename.
"""
import os
import sys
import zipfile
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import src.web_server as web_server
from src.services.suggestions_service import SuggestionsService

EbookResult = web_server.EbookResult


def _service() -> SuggestionsService:
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: '',
        logger=MagicMock(),
    )


def _local(filename, folder="Author/Title (1)"):
    return EbookResult(name=filename, path=f"/books/{folder}/{filename}", source="Local File")


def _abs_item(title, author):
    return {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": author,
        "audio_path": "/audiobooks/x/x.m4b",
    }


def _top(title, author, *candidates):
    svc = _service()
    result = svc._scan_single_audiobook(_abs_item(title, author), svc._prepare_candidate_pool(list(candidates)))
    return result["matches"][0] if result else None


def test_calibre_filename_gives_an_exact_match_full_marks():
    top = _top("The Pillars of the Earth", "Ken Follett", _local("The Pillars of the Earth - Ken Follett.epub"))
    assert top["score"] == 100.0


def test_translator_credit_does_not_lower_the_author_score():
    top = _top(
        "The Savage Detectives",
        "Roberto Bolaño, Natasha Wimmer - translator",
        _local("The Savage Detectives - Roberto Bolaño.epub"),
    )
    assert top["score"] == 100.0


def test_title_containing_the_separator_keeps_its_parts():
    pool = _service()._prepare_candidate_pool([_local("Asian Saga 1 - Shogun - James Clavell.epub")])
    assert (pool[0]["title"], pool[0]["author"]) == ("Asian Saga 1 - Shogun", "James Clavell")


def test_filename_without_separator_is_unchanged():
    pool = _service()._prepare_candidate_pool([_local("Dune.epub")])
    assert (pool[0]["title"], pool[0]["author"]) == ("Dune", "")


def test_sources_with_metadata_are_not_split():
    grimmory = EbookResult(name="x.epub", title="Star Wars - Thrawn", authors="Timothy Zahn", source="Grimmory")
    pool = _service()._prepare_candidate_pool([grimmory])
    assert (pool[0]["title"], pool[0]["author"]) == ("Star Wars - Thrawn", "Timothy Zahn")


def test_a_different_author_is_never_an_exact_match():
    top = _top("Dune", "Brian Herbert", _local("Dune - Frank Herbert.epub"))
    assert top["score"] < 100.0


def test_candidate_with_several_authors_matches_one_of_them():
    top = _top("Good Omens", "Neil Gaiman", _local("Good Omens - Terry Pratchett & Neil Gaiman.epub"))
    assert top["score"] == 100.0


# ---- metadata from metadata.opf and the EPUB's own OPF ----

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        {creators}
        <dc:contributor opf:file-as="calibre" opf:role="bkp">calibre (9.11.0) [https://calibre-ebook.com]</dc:contributor>
    </metadata>
</package>
"""


def _opf(title, *creators):
    return _OPF.format(
        title=title,
        creators="\n        ".join(
            f'<dc:creator opf:role="{role}">{name}</dc:creator>' if role else f"<dc:creator>{name}</dc:creator>"
            for name, role in creators
        ),
    )


def _write_epub(path, opf_xml):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr("OEBPS/content.opf", opf_xml)


def _calibre_book(root, filename, metadata_opf=None, embedded_opf=None):
    folder = root / "Author" / "Title (1)"
    folder.mkdir(parents=True)
    epub = folder / filename
    _write_epub(epub, embedded_opf or _opf("Embedded Title", ("Embedded Author", "aut")))
    if metadata_opf is not None:
        (folder / "metadata.opf").write_text(metadata_opf, encoding="utf-8")
    return EbookResult(name=filename, path=epub, source="Local File")


def _pool_entry(candidate):
    return _service()._prepare_candidate_pool([candidate])[0]


def test_metadata_opf_gives_the_full_title_and_every_author(tmp_path):
    book = _calibre_book(
        tmp_path,
        "This Is How You Lose the Time War - Amal El-Mohtar.epub",
        metadata_opf=_opf("This Is How You Lose the Time War", ("Amal El-Mohtar", "aut"), ("Max Gladstone", "aut")),
    )
    entry = _pool_entry(book)
    assert entry["title"] == "This Is How You Lose the Time War"
    assert entry["author"] == "Amal El-Mohtar & Max Gladstone"


def test_metadata_opf_title_keeps_characters_the_filename_lost(tmp_path):
    book = _calibre_book(
        tmp_path, "Upgrade_ A Novel - Blake Crouch.epub",
        metadata_opf=_opf("Upgrade: A Novel", ("Blake Crouch", "aut")),
    )
    assert _pool_entry(book)["title"] == "Upgrade: A Novel"


def test_non_author_roles_are_left_out(tmp_path):
    book = _calibre_book(
        tmp_path, "Kafka on the Shore - Haruki Murakami.epub",
        metadata_opf=_opf("Kafka on the Shore", ("Haruki Murakami", "aut"), ("Philip Gabriel", "trl")),
    )
    assert _pool_entry(book)["author"] == "Haruki Murakami"


def test_embedded_opf_is_used_without_metadata_opf(tmp_path):
    book = _calibre_book(
        tmp_path, "Roadside Picnic - Arkady Strugatsky.epub",
        embedded_opf=_opf("Roadside Picnic", ("Arkady Strugatsky", None), ("Boris Strugatsky", None)),
    )
    entry = _pool_entry(book)
    assert (entry["title"], entry["author"]) == ("Roadside Picnic", "Arkady Strugatsky & Boris Strugatsky")


def test_audiobook_by_the_second_author_matches_exactly(tmp_path):
    book = _calibre_book(
        tmp_path, "This Is How You Lose the Time War - Amal El-Mohtar.epub",
        metadata_opf=_opf("This Is How You Lose the Time War", ("Amal El-Mohtar", "aut"), ("Max Gladstone", "aut")),
    )
    assert _top("This Is How You Lose the Time War", "Max Gladstone", book)["score"] == 100.0


def test_unreadable_metadata_falls_back_to_the_filename(tmp_path):
    folder = tmp_path / "Author" / "Title (1)"
    folder.mkdir(parents=True)
    epub = folder / "Dune - Frank Herbert.epub"
    epub.write_bytes(b"not a zip")
    (folder / "metadata.opf").write_text("<not xml", encoding="utf-8")
    entry = _pool_entry(EbookResult(name=epub.name, path=epub, source="Local File"))
    assert (entry["title"], entry["author"]) == ("Dune", "Frank Herbert")


def test_author_case_differences_do_not_lower_the_score():
    assert _top("White Noise", "Don DeLillo", _local("White Noise - Don Delillo.epub"))["score"] == 100.0
    assert _top("A Naked Singularity", "Sergio De La Pava", _local("A Naked Singularity - Sergio de La Pava.epub"))["score"] == 100.0


def test_sort_form_authors_do_not_match_on_a_shared_surname():
    candidate = EbookResult(name='Collected Stories.epub', title='Collected Stories', authors='King, Owen',
                            source='Grimmory', path='/books/owen/stories.epub')
    assert _top('Collected Stories', 'King, Stephen', candidate)['score'] < 100.0
    reverse = _service()._scan_single_ebook(
        {'title': 'Collected Stories', 'authors': 'King, Stephen', 'path': '/books/stephen/stories.epub'},
        [_abs_item('Collected Stories', 'King, Owen')],
    )
    assert reverse['matches'][0]['score'] < 100.0


def test_translator_does_not_match_as_the_writer():
    assert _top('Collected Poems', 'Jorge Luis Borges, Jane Doe - translator',
                _local('Collected Poems - Jane Doe.epub'))['score'] < 100.0
    assert SuggestionsService._author_score('Jane Doe - narrator', 'Jane Doe') == 0.0


def test_sort_form_author_keeps_both_name_parts_beside_role_credits():
    assert _top('Collected Stories', 'King, Stephen, Jane Doe - narrator',
                _local('Collected Stories - Stephen King.epub'))['score'] == 100.0


def test_epub3_role_refinements_filter_non_authors(tmp_path):
    opf = '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Collected Poems</dc:title>
        <dc:creator id="writer">Jorge Luis Borges</dc:creator>
        <meta refines="#writer" property="role" scheme="marc:relators">aut</meta>
        <meta refines="#writer" property="role" scheme="marc:relators">ill</meta>
        <dc:creator id="translator">Jane Doe</dc:creator>
        <meta refines="#translator" property="role" scheme="marc:relators">trl</meta>
      </metadata></package>'''
    book = _calibre_book(tmp_path, 'Collected Poems.epub', embedded_opf=opf)
    assert _pool_entry(book)['author'] == 'Jorge Luis Borges'
