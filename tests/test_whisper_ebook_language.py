"""Regression tests for issue #617: Russian audio transcribed as an English translation.

With WHISPER_LANGUAGE blank, Whisper guessed the language from the first 30s of a
chunk. A music intro gave ``detected=en`` (0.62), Whisper then translated the
Russian narration into English, and the content guard refused the map at 0%
overlap. The ebook's own ``dc:language`` now picks the language when the setting
is blank; the setting still wins, and auto-detect remains the last resort.
"""

import logging
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.utils import transcriber as transcriber_module
from src.utils.transcriber import AudioTranscriber
from src.utils.transcription_providers import LocalWhisperProvider, ebook_language_hint
from tests.test_source_audio_safeguard import _job_setup

RUSSIAN = "Квартальное совещание правоохранительных инстанций в присутствии его сиятельства"
TRANSLATION = "The quarter meeting of the law enforcement agencies in the presence of its shine"


def _write_epub(path: Path, language: str | None) -> Path:
    lang = f"<dc:language>{language}</dc:language>" if language is not None else ""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>'))
        zf.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>T</dc:title>{lang}</metadata></package>'))
    return path


def _music_intro_model(supported=("en", "ru", "uk")):
    """Whisper as the reporter measured it: auto-detect hears the music intro as
    English and translates; told ``ru``, it transcribes the Russian."""
    model = MagicMock()
    model.supported_languages = list(supported)

    def transcribe(_path, language=None, **_kw):
        if language == "ru":
            return [SimpleNamespace(start=60, end=70, text=RUSSIAN, words=None)], \
                SimpleNamespace(language="ru", language_probability=1.0)
        return [SimpleNamespace(start=60, end=70, text=TRANSLATION, words=None)], \
            SimpleNamespace(language=language or "en", language_probability=0.62)

    model.transcribe.side_effect = transcribe
    return model


def _provider(model):
    provider = LocalWhisperProvider()
    provider._model = model
    return provider


@patch.dict(os.environ, {"WHISPER_LANGUAGE": ""})
def test_reported_music_intro_is_transcribed_in_the_ebooks_language(caplog):
    provider = _provider(_music_intro_model())

    with caplog.at_level(logging.INFO, logger="src.utils.transcription_providers"):
        unhinted = provider.transcribe(Path("part_000.wav"))
    assert unhinted[0]["text"] == TRANSLATION
    assert "Whisper language: requested=auto, detected=en, confidence=0.62" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.utils.transcription_providers"):
        result = provider.transcribe(Path("part_000.wav"), language="ru")
    assert result[0]["text"] == RUSSIAN
    assert provider._model.transcribe.call_args.kwargs["task"] == "transcribe"
    assert "Whisper language: requested=ru, detected=ru, confidence=1.0, VAD=False, source=ebook" in caplog.text


@patch.dict(os.environ, {"WHISPER_LANGUAGE": " UK "})
def test_the_setting_wins_over_the_ebooks_language(caplog):
    provider = _provider(_music_intro_model())
    with caplog.at_level(logging.INFO, logger="src.utils.transcription_providers"):
        provider.transcribe(Path("part_000.wav"), language="ru")
    assert provider._model.transcribe.call_args.kwargs["language"] == "uk"
    assert "source=setting" in caplog.text


@pytest.mark.parametrize("hint,supported", [("xx", ("en", "ru")), ("ru", ("en",)), (None, ("en", "ru"))])
@patch.dict(os.environ, {"WHISPER_LANGUAGE": ""})
def test_an_unusable_hint_leaves_auto_detect(hint, supported, caplog):
    """An unknown code, or a language an English-only model can't do, must not
    reach faster-whisper (it raises on unsupported codes)."""
    provider = _provider(_music_intro_model(supported))
    with caplog.at_level(logging.INFO, logger="src.utils.transcription_providers"):
        provider.transcribe(Path("part_000.wav"), language=hint)
    assert provider._model.transcribe.call_args.kwargs["language"] is None
    assert "requested=auto" in caplog.text and "source=auto" in caplog.text


@pytest.mark.parametrize("written,expected", [
    ("ru", "ru"), ("ru-RU", "ru"), ("rus", "ru"), ("Russian", "ru"), ("en-GB", "en"),
    ("und", None), ("", None), (None, None),
])
def test_ebook_language_hint_reads_dc_language(tmp_path, written, expected):
    assert ebook_language_hint(_write_epub(tmp_path / "book.epub", written)) == expected


def test_ebook_language_hint_ignores_missing_and_non_epub_files(tmp_path):
    assert ebook_language_hint(None) is None
    assert ebook_language_hint(tmp_path / "comic.cbz") is None
    broken = tmp_path / "broken.epub"
    broken.write_text("not a zip", encoding="utf-8")
    assert ebook_language_hint(broken) is None


def test_process_audio_passes_the_language_to_the_provider(tmp_path):
    transcriber = AudioTranscriber(tmp_path, MagicMock(), MagicMock())
    transcriber.get_audio_duration = MagicMock(return_value=100.0)
    provider = MagicMock()
    provider.supports_raw_audio = True
    provider.get_name.return_value = "raw-provider"
    provider.last_vad_filter = False
    provider.transcribe.return_value = [{"start": 0, "end": 100, "text": RUSSIAN}]
    with patch.object(transcriber_module, "get_transcription_provider", return_value=provider):
        transcriber.process_audio("book", [{"stream_url": "http://x/book.m4b", "ext": "m4b"}], language="ru")
    assert provider.transcribe.call_args.kwargs["language"] == "ru"


def test_whisper_job_passes_the_ebooks_language(tmp_path, monkeypatch):
    """The background job reads the EPUB it aligns against and hands its language to Whisper."""
    manager, _db, _client, transcriber, _align, book, _s, _o = _job_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("CTC_ENABLED", "false")
    _write_epub(tmp_path / "book.epub", "ru")
    transcriber.process_audio.return_value = [{"start": 0.0, "end": 1.0, "text": RUSSIAN}]

    manager._run_background_job(book)

    assert transcriber.process_audio.call_args.kwargs["language"] == "ru"
