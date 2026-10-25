"""transcribe_from_smil marks each segment's bounds as measured, so they survive the sentence rebuild."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber


def test_smil_segments_are_marked_as_timed():
    with tempfile.TemporaryDirectory() as tmp:
        transcriber = AudioTranscriber(Path(tmp), MagicMock(), Polisher())
        segments = [{'start': 0.0, 'end': 10.0, 'text': "And then the"},
                    {'start': 10.0, 'end': 100.0, 'text': "government nationalized the mines."}]
        with patch.object(transcriber.smil_extractor, 'has_media_overlays', return_value=True), \
                patch.object(transcriber.smil_extractor, 'extract_transcript', return_value=segments):
            transcript = transcriber.transcribe_from_smil("book-1", Path(tmp) / "book.epub", [])

    assert [s.get('timed_bounds') for s in transcript] == [True, True]
