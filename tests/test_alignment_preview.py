"""preview_alignment_map builds the lexical map align_and_store would store, without storing it."""

from unittest.mock import MagicMock

from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher


def test_preview_builds_the_map_and_stores_nothing():
    words = [f"word{n}" for n in range(400)]
    text = " ".join(words)
    segments = [{"start": float(i), "end": float(i + 1), "text": " ".join(words[i * 20:(i + 1) * 20])}
                for i in range(20)]
    db = MagicMock()
    service = AlignmentService(database_service=db, polisher=Polisher())

    alignment_map = service.preview_alignment_map(segments, text)

    assert alignment_map
    assert any("t_idx" in point for point in alignment_map)
    assert max(point["ts"] for point in alignment_map) <= 20.0
    db.get_session.assert_not_called()
