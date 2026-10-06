"""Library scans check the extension before stat'ing, so audio files on a slow
shared /books mount never cost a stat per file."""
from pathlib import Path
from unittest.mock import patch

import src.web_server as web_server


def test_title_index_never_stats_non_ebook_files(tmp_path, monkeypatch):
    (tmp_path / "Dune - Frank Herbert.epub").write_bytes(b"x")
    audio = tmp_path / "Dune"
    audio.mkdir()
    for i in range(3):
        (audio / f"part{i}.mp3").write_bytes(b"x")
    monkeypatch.setattr(web_server, "EBOOK_DIR", tmp_path, raising=False)

    stat_targets = []
    real_is_file = Path.is_file

    def is_file(self):
        stat_targets.append(self.name)
        return real_is_file(self)

    with patch.object(Path, "is_file", is_file):
        index = web_server._build_local_ebook_title_index()

    assert index["dune"] == "Dune - Frank Herbert.epub"
    assert stat_targets == ["Dune - Frank Herbert.epub"]
