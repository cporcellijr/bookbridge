"""Compare provider audio without requiring a shared filesystem namespace."""

from pathlib import Path
from tempfile import TemporaryDirectory
from functools import partial
from types import SimpleNamespace

from src.services.audio_source_adapters import BookOrbitAudioSourceAdapter
from src.utils.audio_track_identity import ordered_audio_matches


class CompanionAudioUnavailable(Exception):
    """A provider did not expose all the bytes needed for verification."""


AUDIO_PROVIDERS = {'ABS': ('abs', 'Audiobookshelf'), 'BookOrbit': ('bookorbitaudio', 'BookOrbit Audio'),
                   'BookLore': ('bookloreaudio', 'Grimmory Audio')}


def _tracks(clients, provider, item_id):
    if provider == 'ABS':
        client = clients.abs_client
        return [(row.get('local_path'), partial(client.download_file, row['stream_url']) if row.get('stream_url') else None)
                for row in client.get_audio_files(item_id) or []]
    if provider == 'BookOrbit':
        client = clients.bookorbit_client
        rows = (client.get_audiobook_info(item_id) or {}).get('tracks') or []
        return [(BookOrbitAudioSourceAdapter._verified_mounted_track(row),
                 partial(client.download_file_to_path, row['id']) if str(row.get('id')).isdigit() else None)
                for row in rows]
    if provider == 'BookLore':
        client = clients.booklore_client
        info = client.get_audiobook_info(item_id) or {}
        rows = info.get('tracks') or ([{'index': 0}] if info.get('chapters') else [])
        return [(None, partial(client.download_audiobook_track, item_id,
                               row['index'] if isinstance(row.get('index'), int) else idx))
                for idx, row in enumerate(rows)]
    return []


def verify_audio_pair(clients, left, right):
    """Verify provider items have the same complete tracks in playback order."""
    sides = [_tracks(clients, *item) for item in (left, right)]
    for side, (provider, _) in zip(sides, (left, right)):
        if not side:
            raise CompanionAudioUnavailable(f'{provider} did not return any audio tracks.')
    if len(sides[0]) != len(sides[1]):
        return False
    for pair in zip(*sides):
        if all(path for path, _ in pair):
            if not ordered_audio_matches([pair[0][0]], [pair[1][0]]):
                return False
            continue
        with TemporaryDirectory(prefix='bookbridge-audio-verify-') as scratch:
            paths = []
            for idx, (path, download) in enumerate(pair):
                if not path:
                    path = Path(scratch) / str(idx)
                    if not download or not download(path):
                        provider = (left, right)[idx][0]
                        raise CompanionAudioUnavailable(f'Could not read the audio from {provider}.')
                paths.append(path)
            if not ordered_audio_matches(paths[:1], paths[1:]):
                return False
    return True


def verify_companion_audio(abs_client, orbit_client, abs_id, orbit_id):
    return verify_audio_pair(SimpleNamespace(abs_client=abs_client, bookorbit_client=orbit_client),
                             ('ABS', abs_id), ('BookOrbit', orbit_id))
