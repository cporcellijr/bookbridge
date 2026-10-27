"""A shared series and volume number identifies a book whose titles share nothing."""

import zipfile
from unittest.mock import MagicMock, patch

import pytest

# The service module only imports cleanly after the app has loaded its services.
import src.web_server as web_server
from src.services.audio_source_adapters import AudioResult
from src.services.suggestions_service import SuggestionsService
from src.utils import local_ebook_metadata

AUTHOR = "Karl Ove Knausgaard"

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        <dc:creator opf:role="aut">{author}</dc:creator>
        <meta name="calibre:series" content="{series}"/>
        <meta name="calibre:series_index" content="{index}"/>
    </metadata>
</package>
"""


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


def _write_epub(path, opf_xml):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr("OEBPS/content.opf", opf_xml)


@pytest.fixture(autouse=True)
def _cold_cache():
    local_ebook_metadata.clear_metadata_cache()
    yield
    local_ebook_metadata.clear_metadata_cache()


@pytest.fixture
def library(tmp_path):
    def add(title, index, series="My Struggle", author=AUTHOR, file_title=None):
        file_title = file_title or title
        folder = tmp_path / author / f"{file_title} ({index})"
        folder.mkdir(parents=True)
        epub = folder / f"{file_title} - {author}.epub"
        opf = _OPF.format(title=title, author=author, series=series, index=index)
        _write_epub(epub, opf)
        (folder / "metadata.opf").write_text(opf, encoding="utf-8")
        return web_server.EbookResult(name=epub.name, path=epub, source="Local File")
    return add


def _scan(title, series, *candidates, author=AUTHOR):
    svc = _service()
    ab = {
        "id": "abs-1",
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": title,
        "audio_author": author,
        "audio_series": series,
        "audio_path": "/audiobooks/x/x.m4b",
    }
    result = svc._scan_single_audiobook(ab, svc._prepare_candidate_pool(list(candidates)))
    return {m["display_name"].split(" - ")[0]: m["score"] for m in (result or {}).get("matches", [])}


def test_the_same_volume_of_the_same_series_is_suggested(library):
    scores = _scan("My Struggle, Book 3", "My Struggle #3", library("Boyhood Island", 3))
    assert scores == {"Boyhood Island": SuggestionsService._SERIES_MATCH_SCORE}


def test_a_book_in_two_series_matches_by_its_numbered_series(library):
    # ABS lists every series a book is in, such as an umbrella series after its own.
    scores = _scan("My Struggle, Book 3", "My Struggle #3, Knausgaard Collection", library("Boyhood Island", 3))
    assert scores == {"Boyhood Island": SuggestionsService._SERIES_MATCH_SCORE}


def test_distinctive_title_survives_conflicting_series_and_title_numbers(library):
    candidate = library("The Daughters' War, Book 1", 0, series="Blacktongue", author="Christopher Buehlman")
    scores = _scan("The Daughters' War, Book 2", "Blacktongue #2", candidate, author="Christopher Buehlman")
    assert "The Daughters' War, Book 1" in scores


def test_a_series_match_never_auto_matches(library):
    scores = _scan("My Struggle, Book 3", "My Struggle #3", library("Boyhood Island", 3))
    assert scores["Boyhood Island"] < 95


def test_another_volume_of_the_same_series_is_not_suggested(library):
    assert _scan("My Struggle, Book 5", "My Struggle #5", library("My Struggle", 4)) == {}


def _calibre_volume(library, n):
    # Some editions are titled "Series: Book N", and Calibre writes the colon as "_" in file names.
    return library(f"My Struggle: Book {n}", n, file_title=f"My Struggle_ Book {n}")


def test_another_volume_titled_series_and_book_number_is_not_suggested(library):
    assert _scan("My Struggle, Book 5", "My Struggle #5", _calibre_volume(library, 4)) == {}


def test_only_the_same_volume_titled_series_and_book_number_is_suggested(library):
    scores = _scan(
        "My Struggle, Book 5", "My Struggle #5",
        _calibre_volume(library, 1), _calibre_volume(library, 4), _calibre_volume(library, 5),
    )
    assert list(scores) == ["My Struggle_ Book 5"]


@pytest.mark.parametrize("title", ["My Struggle: Book 1", "My Struggle_ Book 1", "My Struggle - Book 1", "My Struggle, Book 1", "My Struggle. Book 1"])
def test_normalizing_drops_a_book_number_after_any_separator(title):
    assert SuggestionsService._normalize_title_for_match(title) == "My Struggle"


@pytest.mark.parametrize("title", ["My Struggle: Book One", "My Struggle_ Book Two", "My Struggle, Book III"])
def test_normalizing_drops_a_book_number_written_as_a_word_or_numeral(title):
    assert SuggestionsService._normalize_title_for_match(title) == "My Struggle"


@pytest.mark.parametrize("title", ["My Struggle, Volume 2", "My Struggle, Vol. 2", "My Struggle, Vol.2", "My Struggle Volume Two",
                                   "My Struggle: Book 2.5"])
def test_normalizing_drops_a_volume_number(title):
    assert SuggestionsService._normalize_title_for_match(title) == "My Struggle"


def test_another_volume_titled_after_a_period_is_not_suggested(library):
    candidate = library("My Struggle. Book 4", 4, file_title="My Struggle. Book 4")
    assert _scan("My Struggle, Book 5", "My Struggle #5", candidate) == {}


@pytest.mark.parametrize("title", ["The Jungle Book II", "My Notebook Two", "My Notebook 2", "Dune Part 2", "Book 2", "Vol. 3"])
def test_normalizing_keeps_a_number_that_is_part_of_the_title(title):
    assert SuggestionsService._normalize_title_for_match(title) == title


def test_another_volume_numbered_in_words_is_not_suggested(library):
    candidate = library("My Struggle: Book One", 1, file_title="My Struggle_ Book One")
    assert _scan("My Struggle, Book 5", "My Struggle #5", candidate) == {}


def test_a_series_match_with_a_number_in_one_title_is_full(library):
    assert _scan("Boyhood Island, Book 3", "My Struggle #3", library("Boyhood Island", 3)) == {"Boyhood Island": 100.0}


def test_a_series_number_against_another_volume_in_the_title_is_capped(library):
    candidate = library("The Way of Kings, Book 2", 2, series="The Stormlight Archive")
    scores = _scan("The Way of Kings", "The Stormlight Archive #1", candidate)
    assert scores == {"The Way of Kings, Book 2": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_the_ebook_series_number_confirms_a_numbered_audiobook_title(library):
    assert _scan("My Struggle, Book 5", "", library("My Struggle", 5)) == {"My Struggle": 100.0}


def test_the_ebook_series_number_rules_out_another_volume(library):
    assert _scan("My Struggle, Book 5", "", library("My Struggle", 1)) == {}


def test_an_ebook_series_named_differently_leaves_another_volume_for_review(library):
    scores = _scan("My Struggle, Book 5", "", library("My Struggle", 1, series="Min kamp"))
    assert scores == {"My Struggle": SuggestionsService._SUBTITLE_STRIPPED_SCORE_CAP}


def test_a_title_match_survives_sources_numbering_the_series_differently(library):
    candidate = library("The Daughters' War", 0, series="Blacktongue", author="Christopher Buehlman")
    scores = _scan("The Daughters' War", "Blacktongue #2", candidate, author="Christopher Buehlman")
    assert scores == {"The Daughters' War": 100.0}


def test_a_series_match_needs_the_same_author(library):
    candidate = library("Boyhood Island", 3, author="Someone Else")
    assert "Boyhood Island" not in _scan("My Struggle, Book 3", "My Struggle #3", candidate)


def test_scan_records_carry_the_audiobook_series():
    item = AudioResult(source="ABS", source_id="abs-1", title="My Struggle, Book 3",
                       series_label="My Struggle #3", authors=AUTHOR)
    with patch.object(web_server, "get_searchable_audiobooks", return_value=[item]), \
            patch.object(web_server, "_browser_cover_url", return_value=""):
        records = web_server.get_suggestion_audiobooks()
    assert records[0]["audio_series"] == "My Struggle #3"


def _rerank_service():
    svc = _service()
    svc.ollama_client = MagicMock()
    svc.ollama_client.is_configured.return_value = True
    # The audiobook's text and every candidate's text are unrelated to each other.
    svc._embed_texts = lambda texts: {t: [1.0, 0.0] if t.startswith("My Struggle") else [0.0, 1.0] for t in texts}
    svc._run_judge = MagicMock(return_value=(None, 0))
    return svc


def _suggestion():
    return {
        "bridge_key": "abs-1",
        "audio_title": "My Struggle, Book 3",
        "audio_author": AUTHOR,
        "matches": [
            {"display_name": "Boyhood Island", "author": AUTHOR, "score": 90.0, "match_reason": "same_series"},
            {"display_name": "Some Other Book", "author": AUTHOR, "score": 70.0},
        ],
    }


_RERANK_ENV = {
    "OLLAMA_RERANK_SUGGESTIONS": "true", "OLLAMA_JUDGE_SUGGESTIONS": "true",
    "OLLAMA_SUGGEST_JUDGE_GATE": "true", "OLLAMA_SUGGEST_AUTOKEEP_SCORE": "90",
    "OLLAMA_RERANK_BAND_MIN": "60",
}


def test_reranking_keeps_a_series_match_score():
    svc, suggestion = _rerank_service(), _suggestion()
    with patch.dict("os.environ", _RERANK_ENV):
        matches = svc._ollama_rerank_band(suggestion["audio_title"], AUTHOR, suggestion["matches"])
    assert matches[0]["display_name"] == "Boyhood Island"
    assert matches[0]["score"] == SuggestionsService._SERIES_MATCH_SCORE


def test_the_judge_does_not_suppress_a_series_match():
    svc, suggestion = _rerank_service(), _suggestion()
    with patch.dict("os.environ", _RERANK_ENV):
        suppressed = svc._apply_ollama_reranking_batch([suggestion])
    assert "abs-1" not in suppressed
    assert svc._run_judge.call_count == 0


def test_the_suggestion_card_marks_a_series_match():
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / "templates" / "suggestions.html").read_text(encoding="utf-8")
    assert "matchReason === 'same_series'" in template
    assert "'Same series'" in template
    assert ".source-badge.sameseries {" in template
