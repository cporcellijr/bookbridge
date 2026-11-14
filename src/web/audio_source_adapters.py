"""Audio source labels, bridge keys and forge text-item normalizers, moved from web_server."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


# Map internal audio source key -> (UI badge label, CSS class).
# These are the FULL product names used on UI chips, deliberately distinct from
# _audio_source_display_name(), which stays short ("ABS") because it feeds
# generated book titles.
_AUDIO_SOURCE_BADGE_LABELS = {
    "ABS": ("Audiobookshelf", "abs"),
    "BookLore": ("Grimmory", "grimmory"),
    "BookOrbit": ("BookOrbit", "bookorbit"),
}


def _audio_source_display_name(source):
    """'BookLore' is the internal key; users know it as Grimmory."""
    return "Grimmory" if source == "BookLore" else str(source or "")


def _audio_source_from_bridge_key(bridge_key):
    """Infer the audio source from a bridge key's prefix ('booklore:'/'bookorbit:').

    Un-prefixed non-empty keys are ABS item ids; an empty key yields ''."""
    web = _web_server()
    key = str(bridge_key or "")
    for src in web._LIBRARY_AUDIO_SOURCES:
        if key.lower().startswith(f"{src.lower()}:"):
            return src
    return "ABS" if key else ""


def suggestion_source_badge(audio_source: str | None, bridge_key: str | None = None) -> tuple[str, str]:
    """Resolve a suggestion's audio provider to a (label, css_class) badge pair."""
    source = (audio_source or _audio_source_from_bridge_key(bridge_key) or "ABS").strip()
    return _AUDIO_SOURCE_BADGE_LABELS.get(source, (source, "unknown"))


def _build_bridge_key(audio_source, audio_source_id):
    web = _web_server()
    if audio_source_id is None:
        return None
    source_id = str(audio_source_id).strip()
    if not source_id:
        return None

    head = source_id.lower().split(":", 1)[0]
    if ":" in source_id and head in web._AUDIO_BRIDGE_PREFIXES:
        return f"{head}:{source_id.split(':', 1)[1].strip()}"

    source_name = str(audio_source or "").strip().lower()
    if source_name in web._AUDIO_BRIDGE_PREFIXES:
        return f"{web._AUDIO_BRIDGE_PREFIXES[source_name]}:{source_id}"
    return source_id


def _normalize_text_source_type(raw_source):
    web = _web_server()
    return web.normalize_ebook_source(raw_source)


def _safe_local_source_path(raw_path) -> str:
    """Return a local ebook path only when it resolves inside a library root.

    The value arrives in a request payload, so it is confined to BOOKS_DIR /
    EXTRA_EBOOK_DIRS / the epub cache before any staging, parsing, hashing, or
    upload reads it. Returns '' for a missing or out-of-tree path, which the
    forge callers already treat as "local file path unavailable".
    """
    web = _web_server()
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    safe_path = web.safe_library_path(raw)
    if safe_path is None:
        web.logger.warning(
            "Refused local ebook source outside the configured library roots: %s",
            web.sanitize_log_data(raw),
        )
        return ""
    return str(safe_path)


def _build_forge_text_item(source_type, source_id, source_path, original_filename):
    normalized_source = _normalize_text_source_type(source_type)
    normalized_source_id = str(source_id or "").strip()
    normalized_source_path = str(source_path or "").strip()

    text_item = {
        "source": normalized_source,
        "path": normalized_source_path,
        "booklore_id": normalized_source_id,
        "bookorbit_id": normalized_source_id,
        "kavita_id": normalized_source_id,
        "bookfusion_id": normalized_source_id,
        "cwa_id": normalized_source_id,
        "abs_id": normalized_source_id,
        "source_id": normalized_source_id,
        "filename": original_filename,
    }

    if normalized_source == "ABS":
        text_item["abs_id"] = normalized_source_id
    if normalized_source == "Booklore":
        text_item["booklore_id"] = normalized_source_id
    if normalized_source == "BookOrbit":
        text_item["bookorbit_id"] = normalized_source_id
    if normalized_source == "Kavita":
        text_item["kavita_id"] = normalized_source_id
    if normalized_source == "BookFusion":
        text_item["bookfusion_id"] = normalized_source_id
    if normalized_source == "CWA":
        text_item["cwa_id"] = normalized_source_id
        if normalized_source_path:
            text_item["download_url"] = normalized_source_path
    if normalized_source == "Local File":
        text_item["path"] = _safe_local_source_path(normalized_source_path)

    return text_item


def _parse_audio_duration(raw_value):
    try:
        if raw_value is None or raw_value == "":
            return None
        return float(raw_value)
    except (TypeError, ValueError):
        return None
