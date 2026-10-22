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
    def add(title, index, series="My Struggle", author=AUTHOR):
        folder = tmp_path / author / f"{title} ({index})"
        folder.mkdir(parents=True)
        epub = folder / f"{title} - {author}.epub"
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


def test_distinctive_title_survives_conflicting_series_and_title_numbers(library):
    candidate = library("The Daughters' War, Book 1", 0, series="Blacktongue", author="Christopher Buehlman")
    scores = _scan("The Daughters' War, Book 2", "Blacktongue #2", candidate, author="Christopher Buehlman")
    assert "The Daughters' War, Book 1" in scores


def test_a_series_match_never_auto_matches(library):
    scores = _scan("My Struggle, Book 3", "My Struggle #3", library("Boyhood Island", 3))
    assert scores["Boyhood Island"] < 95


def test_another_volume_of_the_same_series_is_not_suggested(library):
    assert _scan("My Struggle, Book 5", "My Struggle #5", library("My Struggle", 4)) == {}


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
