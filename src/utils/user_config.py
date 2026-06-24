"""Per-user credential resolution (multi-user Phase 2).

API clients accept an optional per-user credentials dict. When a value is set
for the user it wins. Admin users may fall back to the global os.environ value
(the shared/admin config); regular users must provide their own account-level
values so blank fields do not accidentally sync the admin library.

In the shared-NAS model only auth/account values differ per user — server URLs,
library IDs and engine settings stay global, so they simply fall through to
os.environ when absent from the user's dict.
"""

import os

_ALLOW_GLOBAL_FALLBACK_KEY = "__allow_global_fallback__"

# Keys stored per-user (credentials/accounts + per-service enable toggles).
# Server URLs, library IDs, and engine/catalog settings stay global.
PER_USER_CREDENTIAL_KEYS = frozenset({
    # Audiobookshelf (server URL stays global; API token + library are per-user)
    "ABS_KEY", "ABS_LIBRARY_ID",
    # KOReader / KoSync (server URL global; account is per-user)
    "KOSYNC_USER", "KOSYNC_KEY", "KOSYNC_ENABLED",
    # Storyteller
    "STORYTELLER_USER", "STORYTELLER_PASSWORD", "STORYTELLER_ENABLED",
    # Calibre-Web (Automated)
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_ENABLED",
    "CWA_SYNC_TOKEN", "CWA_SYNC_ENABLED",
    # BookOrbit (account + the user's own destination collection)
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_ENABLED",
    "BOOKORBIT_SHELF_NAME",
    # Grimmory / BookLore (account + the user's own shelf/library)
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_ENABLED",
    "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID",
    # Trackers (write targets are per-user accounts)
    "HARDCOVER_TOKEN", "HARDCOVER_ENABLED",
    "STORYGRAPH_SESSION_COOKIE", "STORYGRAPH_REMEMBER_USER_TOKEN", "STORYGRAPH_ENABLED",
})


# UI grouping for the per-user credentials page. (group_label, [(key, label, type)])
# type: 'text' (blank clears), 'secret' (blank keeps existing), 'bool' (checkbox).
PER_USER_FIELD_GROUPS = [
    ("Audiobookshelf", [
        ("ABS_KEY", "API token", "secret"),
        ("ABS_LIBRARY_ID", "Library ID (optional, for a separate library)", "text"),
    ]),
    ("KOReader / KoSync", [
        ("KOSYNC_ENABLED", "Enabled", "bool"),
        ("KOSYNC_USER", "Sync username", "text"),
        ("KOSYNC_KEY", "Sync password", "secret"),
    ]),
    ("Storyteller", [
        ("STORYTELLER_ENABLED", "Enabled", "bool"),
        ("STORYTELLER_USER", "Username", "text"),
        ("STORYTELLER_PASSWORD", "Password", "secret"),
    ]),
    ("Calibre-Web (Automated)", [
        ("CWA_ENABLED", "Enabled", "bool"),
        ("CWA_USERNAME", "Username", "text"),
        ("CWA_PASSWORD", "Password", "secret"),
        ("CWA_SYNC_ENABLED", "Kobo sync enabled", "bool"),
        ("CWA_SYNC_TOKEN", "Kobo sync token", "secret"),
    ]),
    ("BookOrbit", [
        ("BOOKORBIT_ENABLED", "Enabled", "bool"),
        ("BOOKORBIT_USER", "Username", "text"),
        ("BOOKORBIT_PASSWORD", "Password", "secret"),
        ("BOOKORBIT_SHELF_NAME", "Collection name (synced books moved here)", "text"),
    ]),
    ("Grimmory / BookLore", [
        ("BOOKLORE_ENABLED", "Enabled", "bool"),
        ("BOOKLORE_USER", "Username", "text"),
        ("BOOKLORE_PASSWORD", "Password", "secret"),
        ("BOOKLORE_SHELF_NAME", "Shelf name (synced books moved here)", "text"),
        ("BOOKLORE_LIBRARY_ID", "Library ID (optional)", "text"),
    ]),
    ("Hardcover", [
        ("HARDCOVER_ENABLED", "Enabled", "bool"),
        ("HARDCOVER_TOKEN", "API token", "secret"),
    ]),
    ("StoryGraph", [
        ("STORYGRAPH_ENABLED", "Enabled", "bool"),
        ("STORYGRAPH_SESSION_COOKIE", "Session cookie", "secret"),
        ("STORYGRAPH_REMEMBER_USER_TOKEN", "Remember-user token", "secret"),
    ]),
]


def resolve_setting(credentials, key, default=None):
    """Resolve a config value for a (possibly per-user) client.

    Returns the user's value when present and non-empty. For recognized
    per-user account keys, regular user bundles do not fall back to the global
    admin environment unless their registry explicitly allows it.
    """
    if credentials:
        val = credentials.get(key)
        if val not in (None, ""):
            return val
        if key in PER_USER_CREDENTIAL_KEYS and credentials.get(_ALLOW_GLOBAL_FALLBACK_KEY) is False:
            return default
    return os.environ.get(key, default)


def user_setting(key, default=None):
    """Resolve a setting for the current request/cycle's user (ambient context),
    falling back to the global os.environ value. Use this for direct (non-client,
    non-cached) settings reads — library id, enable flags, search scope — that
    must honor the logged-in user instead of the global/admin config."""
    from src.utils.user_context import get_current_user_credentials
    return resolve_setting(get_current_user_credentials(), key, default)
