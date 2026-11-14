"""Dashboard sync warning helpers; extracted without behavior changes."""

from importlib import import_module
from src.utils.ebook_utils import LRUCache

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

def _storyteller_transcript_source(storyteller_uuid, storyteller_manifest):
    return "storyteller" if storyteller_uuid or storyteller_manifest else None


def _get_dashboard_sync_warning_clients(mapping, integrations):
    client_names = []

    # A book repointed away from ABS keeps its old ABS progress row so the
    # repoint stays undoable, but the sync engine never refreshes that row and
    # the dashboard hides its tile — counting it would flag permanent drift
    # nothing can clear.
    if (
        integrations.get('abs')
        and mapping.get('sync_mode') != 'ebook_only'
        and (mapping.get('audio_source') or 'ABS') == 'ABS'
    ):
        client_names.append('abs')

    if integrations.get('bookloreaudio') and mapping.get('audio_source') == 'BookLore':
        client_names.append('bookloreaudio')

    if integrations.get('bookorbitaudio') and mapping.get('audio_source') == 'BookOrbit':
        client_names.append('bookorbitaudio')

    if integrations.get('kosync'):
        client_names.append('kosync')

    if integrations.get('storyteller') and (
        mapping.get('storyteller_uuid')
        or mapping.get('storyteller_legacy_link')
        or 'storyteller' in mapping.get('states', {})
    ):
        client_names.append('storyteller')

    if integrations.get('booklore') and (
        mapping.get('booklore_id')
        or 'booklore' in mapping.get('states', {})
    ):
        client_names.append('booklore')

    if integrations.get('bookorbit') and (
        mapping.get('ebook_source') == 'BookOrbit'
        or 'bookorbit' in mapping.get('states', {})
    ):
        client_names.append('bookorbit')

    if integrations.get('kavita') and (
        mapping.get('ebook_source') == 'Kavita'
        or 'kavita' in mapping.get('states', {})
    ):
        client_names.append('kavita')

    return client_names


_AUDIO_AXIS_SYNC_CLIENTS = {'abs', 'bookloreaudio', 'bookorbitaudio'}


def _dashboard_text_axis_pct(client_name, state, mapping):
    """Return a client's progress as an ebook text-axis percentage (0-100).

    Ebook clients already report on the text axis. Audio clients report on the
    time axis; convert their timestamp to a text fraction via the book's
    alignment map so the two are comparable. Falls back to the raw percentage
    when no alignment map is available (e.g. audio-only books)."""
    web = _web_server()
    percentage = state.get('percentage')
    if percentage is None:
        return None

    if client_name not in _AUDIO_AXIS_SYNC_CLIENTS:
        return float(percentage)

    alignment_service = getattr(web.manager, "alignment_service", None) if web.manager else None
    if not alignment_service:
        return float(percentage)

    timestamp = state.get('timestamp') or 0
    if timestamp <= 0:
        duration = mapping.get('duration') or 0
        if duration > 0:
            timestamp = (float(percentage) / 100.0) * duration
    if timestamp <= 0:
        return float(percentage)

    try:
        text_fraction = alignment_service.get_progress_for_time(mapping.get('abs_id'), float(timestamp))
    except Exception:
        text_fraction = None

    if not isinstance(text_fraction, (int, float)) or isinstance(text_fraction, bool):
        return float(percentage)
    return text_fraction * 100.0


_DASHBOARD_SYNC_WARNING_CACHE = LRUCache(capacity=512)


def _dashboard_sync_warning_candidates(mapping, integrations):
    """The (client, state) pairs eligible for the drift comparison.

    Split out from the computation so callers can count the candidates — and bail
    — before paying for any audio-to-text axis conversion."""
    candidates = []
    states = mapping.get('states', {})

    for client_name in _get_dashboard_sync_warning_clients(mapping, integrations):
        state = states.get(client_name)
        if not state:
            continue
        raw = state.get('percentage')
        if raw is None or raw <= 0:
            continue
        candidates.append((client_name, state))

    return candidates


def _dashboard_sync_warning_cache_key(mapping, candidates):
    """Fingerprint the inputs the drift number is derived from.

    Anything that can change the answer belongs here: the book, the set of
    candidate clients, each one's reported position, and the duration used as the
    timestamp fallback. Identical fingerprint, identical result."""
    return (
        mapping.get('abs_id'),
        mapping.get('duration') or 0,
        tuple(
            (name, state.get('percentage'), state.get('timestamp'))
            for name, state in candidates
        ),
    )


def _compute_dashboard_sync_warning_pct(mapping, integrations):
    candidates = _dashboard_sync_warning_candidates(mapping, integrations)

    # One reporting client cannot drift from anything. Returning here keeps a
    # single-integration install off the alignment path entirely.
    if len(candidates) < 2:
        return 0.0

    cache_key = _dashboard_sync_warning_cache_key(mapping, candidates)
    cached = _DASHBOARD_SYNC_WARNING_CACHE.get(cache_key)
    if cached is not None:
        return cached

    progress_values = []
    for client_name, state in candidates:
        value = _dashboard_text_axis_pct(client_name, state, mapping)
        if value is None:
            continue
        progress_values.append(value)

    if len(progress_values) < 2:
        return 0.0

    warning_pct = round(max(progress_values) - min(progress_values), 1)
    _DASHBOARD_SYNC_WARNING_CACHE.put(cache_key, warning_pct)
    return warning_pct

