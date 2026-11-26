from unittest.mock import Mock

import pytest

from src.services.companion_audio import CompanionAudioUnavailable, verify_companion_audio


def _provider_clients():
    abs_client, orbit_client = Mock(), Mock()
    abs_client.get_audio_files.return_value = [{'stream_url': 'https://abs.invalid/file'}]
    orbit_client.get_audiobook_info.return_value = {'tracks': [{'id': 42}]}
    return abs_client, orbit_client


@pytest.mark.parametrize('same', [True, False])
def test_compares_downloads_without_mounted_paths(same):
    abs_client, orbit_client = _provider_clients()
    staged = []

    def write(payload, path):
        staged.append(path)
        return bool(path.write_bytes(payload))

    abs_client.download_file.side_effect = lambda _, path: write(b'audio A', path)
    orbit_client.download_file_to_path.side_effect = lambda _, path: write(b'audio A' if same else b'audio B', path)

    assert verify_companion_audio(abs_client, orbit_client, 'abs-id', 'orbit-id') is same
    assert len(staged) == 2 and not any(path.exists() for path in staged)


def test_download_failure_is_distinct_from_audio_mismatch():
    abs_client, orbit_client = _provider_clients()
    abs_client.download_file.return_value = False
    with pytest.raises(CompanionAudioUnavailable, match='from ABS'):
        verify_companion_audio(abs_client, orbit_client, 'abs-id', 'orbit-id')
    orbit_client.download_file_to_path.assert_not_called()


def test_verified_mounted_file_never_needs_download(tmp_path, monkeypatch):
    monkeypatch.setenv('AUDIOBOOKS_DIR', str(tmp_path))
    path = tmp_path / 'same.m4b'
    size = path.write_bytes(b'original audio')
    abs_client, orbit_client = _provider_clients()
    abs_client.get_audio_files.return_value = [{'local_path': str(path)}]
    orbit_client.get_audiobook_info.return_value = {'tracks': [
        {'id': 42, 'absolute_path': str(path), 'filename': path.name, 'size_bytes': size},
    ]}
    assert verify_companion_audio(abs_client, orbit_client, 'abs-id', 'orbit-id')
    abs_client.download_file.assert_not_called()
    orbit_client.download_file_to_path.assert_not_called()
