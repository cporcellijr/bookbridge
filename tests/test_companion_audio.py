import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.services.companion_audio import CompanionAudioUnavailable, verify_audio_pair
from src.utils.file_transfers import IncompleteTransferError, fingerprint_response


def _provider_clients():
    abs_client, orbit_client = Mock(), Mock()
    abs_client.get_audio_files.return_value = [{'stream_url': 'https://abs.invalid/file'}]
    orbit_client.get_audiobook_info.return_value = {'tracks': [{'id': 42}]}
    return SimpleNamespace(abs_client=abs_client, bookorbit_client=orbit_client)


def _verify(clients):
    return verify_audio_pair(clients, ('ABS', 'abs-id'), ('BookOrbit', 'orbit-id'))


def _response(chunks, size, status=200):
    response = Mock(status_code=status, headers={'Content-Length': str(size)})
    response.iter_content.return_value = iter(chunks)
    return response


@pytest.mark.parametrize('same', [True, False])
def test_compares_downloads_without_mounted_paths(same):
    clients = _provider_clients()
    clients.abs_client.fingerprint_file.return_value = fingerprint_response(_response([b'audio ', b'A'], 7))
    clients.bookorbit_client.fingerprint_file.return_value = fingerprint_response(
        _response([b'audio A' if same else b'audio B'], 7),
    )
    assert _verify(clients) is same
    clients.abs_client.download_file.assert_not_called()
    clients.bookorbit_client.download_file_to_path.assert_not_called()


def test_download_failure_is_distinct_from_audio_mismatch():
    clients = _provider_clients()
    clients.abs_client.fingerprint_file.return_value = None
    with pytest.raises(CompanionAudioUnavailable, match='from ABS'):
        _verify(clients)
    clients.bookorbit_client.fingerprint_file.assert_not_called()


def test_verified_mounted_file_never_needs_download(tmp_path, monkeypatch):
    monkeypatch.setenv('AUDIOBOOKS_DIR', str(tmp_path))
    path = tmp_path / 'same.m4b'
    size = path.write_bytes(b'original audio')
    clients = _provider_clients()
    clients.abs_client.get_audio_files.return_value = [{'local_path': str(path)}]
    clients.bookorbit_client.get_audiobook_info.return_value = {'tracks': [
        {'id': 42, 'absolute_path': str(path), 'filename': path.name, 'size_bytes': size},
    ]}
    assert _verify(clients)
    clients.abs_client.fingerprint_file.assert_not_called()
    clients.bookorbit_client.fingerprint_file.assert_not_called()


def test_fingerprint_streams_in_bounded_chunks_and_matches_local(tmp_path):
    path = tmp_path / 'track.m4b'
    path.write_bytes(b'audio abc')
    response = _response([b'audio ', b'', b'abc'], 9)
    clients = _provider_clients()
    clients.abs_client.get_audio_files.return_value = [{'local_path': str(path)}]
    clients.bookorbit_client.fingerprint_file.return_value = fingerprint_response(response)
    assert clients.bookorbit_client.fingerprint_file.return_value == (9, hashlib.sha256(b'audio abc').digest())
    response.iter_content.assert_called_once_with(chunk_size=1024 * 1024)
    assert _verify(clients)


@pytest.mark.parametrize('response', [_response([b'ab'], 3), _response([b'ab'], 2, 206), _response([], 0)])
def test_fingerprint_rejects_incomplete_bodies(response):
    with pytest.raises(IncompleteTransferError):
        fingerprint_response(response)
