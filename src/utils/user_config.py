"""Per-user credential resolution (multi-user Phase 2).

API clients accept an optional per-user credentials dict. When a value is set
for the user it wins. Only the primary admin may fall back to the global
os.environ value (the shared/admin config, which is that account mirrored
outward); every other user — including a second admin — must provide their own
account-level values so blank fields do not accidentally sync someone else's
library. See global_fallback_allowed().

In the shared-NAS model only auth/account values differ per user — server URLs,
library IDs and engine settings stay global, so they simply fall through to
os.environ when absent from the user's dict.
"""

import logging
import os

logger = logging.getLogger(__name__)

_ALLOW_GLOBAL_FALLBACK_KEY = "__allow_global_fallback__"

# Keys stored per-user (credentials/accounts + per-service enable toggles).
# Server URLs, library IDs, and engine/catalog settings stay global.
PER_USER_CREDENTIAL_KEYS = frozenset({
    # Audiobookshelf (server URL stays global; enable flag + API token + library
    # + collection are per-user). ABS_ENABLED is deliberately NOT in
    # ENGINE_MIRROR_KEYS: the primary admin switching their own ABS off must not
    # take the global singletons down for everyone else.
    "ABS_ENABLED", "ABS_KEY", "ABS_LIBRARY_ID", "ABS_COLLECTION_NAME",
    # KOReader / KoSync (server URL global; account is per-user)
    "KOSYNC_USER", "KOSYNC_KEY", "KOSYNC_ENABLED", "KOSYNC_AUTH_METHOD",
    "DEVICE_SYNC_COLLECTION_SOURCE", "DEVICE_SYNC_COLLECTIONS",
    "DEVICE_SYNC_EXCLUDED_SHELVES", "DEVICE_SYNC_SHELF_FILTER", "DEVICE_SYNC_HARDCOVER_LISTS",
    "DEVICE_SYNC_HARDCOVER_LIST_NAMES",
    # Storyteller
    "STORYTELLER_USER", "STORYTELLER_PASSWORD", "STORYTELLER_ENABLED", "STORYTELLER_AUTO_LINK",
    # Calibre-Web (Automated)
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_ENABLED",
    "CWA_SYNC_TOKEN", "CWA_SYNC_ENABLED",
    # BookOrbit (account + the user's own destination collection)
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_ENABLED",
    "BOOKORBIT_SHELF_NAME",
    # BookOrbit KOReader-sync account (annotation hub spoke; kosync-style creds)
    "BOOKORBIT_KOSYNC_USER", "BOOKORBIT_KOSYNC_KEY", "BOOKORBIT_KOSYNC_OWNER",
    # Kavita (auth key identifies the Kavita user; collection/library are user choices)
    "KAVITA_ENABLED", "KAVITA_API_KEY", "KAVITA_LIBRARY_ID", "KAVITA_COLLECTION_NAME",
    # Grimmory / BookLore (account + the user's own shelf/library)
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_ENABLED",
    "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID", "BOOKLORE_ANNOTATION_SYNC",
    # Readest (Supabase cloud sync; the account is per-user and the rotating
    # access/refresh tokens are cached per-user — the user never pastes a JWT)
    "READEST_ENABLED", "READEST_ANNOTATION_SYNC", "READEST_EMAIL", "READEST_PASSWORD",
    "READEST_ACCESS_TOKEN", "READEST_REFRESH_TOKEN", "READEST_TOKEN_EXPIRES_AT",
    "READEST_UPLOAD_ON_MATCH", "READEST_GROUP_NAME",
    "READEST_UPLOAD_READING",
    # BookFusion
    "BOOKFUSION_ENABLED", "BOOKFUSION_ACCESS_TOKEN", "BOOKFUSION_API_KEY",
    "BOOKFUSION_ANNOTATION_SYNC",
    # Trackers (write targets are per-user accounts)
    "HARDCOVER_TOKEN", "HARDCOVER_ENABLED",
    "HARDCOVER_GRIMMORY_LIST_SYNC", "HARDCOVER_GRIMMORY_LIST_PREFIX",
    "HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES",
    "STORYGRAPH_SESSION_COOKIE", "STORYGRAPH_REMEMBER_USER_TOKEN", "STORYGRAPH_ENABLED",
})


# Library-lookup credentials the primary admin's account also lends to the
# engine's global singletons (shelf-watch, scans, suggestions, ABS socket,
# manifest). web_server mirrors these to the global settings when the primary
# admin saves; user_bootstrap warns at boot when the two stores diverge.
ENGINE_MIRROR_KEYS = (
    "ABS_KEY", "ABS_LIBRARY_ID",
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID",
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_SHELF_NAME",
    "KAVITA_API_KEY", "KAVITA_LIBRARY_ID", "KAVITA_COLLECTION_NAME",
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_SYNC_TOKEN",
)


# Reading preferences: how each reader wants their own books handled. The admin's
# Settings value is the server default; a reader's own On/Off on My Integrations
# overrides it, and a blank choice keeps following the default. Deliberately NOT
# in PER_USER_CREDENTIAL_KEYS, so every reader, regular users included, falls back
# to the global value.
PER_USER_PREFERENCE_KEYS = frozenset({
    "KOREADER_ANNOTATION_SYNC",
    "KOREADER_STATUS_SYNC_ENABLED",
    "KOREADER_SYNC_READ_HISTORY",
    "KOSYNC_FURTHEST_WINS",
    "SYNC_COMPLETION_PROPAGATION",
    "ABS_READING_CHECKPOINTS",
    "STORYTELLER_LISTENING_SESSIONS",
    "SERIES_SHOW_CURRENT_BOOK_DETAIL",
})


# Display text in PER_USER_FIELD_GROUPS is marked with N_() so Babel extracts it
# (see babel.cfg); it is translated only where it is shown, `{{ _(label) }}`.
# N_() is a no-op: setting keys, stored values and group comparisons such as
# `group == 'KOReader Collections'` keep the English originals.
def N_(text: str) -> str:
    return text


def _select(*options) -> str:
    """Build a 'select:value=Label|...' field type from (value, label) pairs.

    Each label is its own N_() call, so a translator gets one msgid per option
    and the template can translate the label it splits back out of the spec.
    The spec string itself is unchanged, so its parsers do not need to change.
    """
    return "select:" + "|".join(f"{value}={label}" for value, label in options)


# UI grouping for the per-user credentials page. (group_label, [(key, label, type)])
# type: 'text' (blank clears), 'secret' (blank keeps existing), 'bool' (checkbox),
# 'pref' (Server default / On / Off; blank follows the server default).
PER_USER_FIELD_GROUPS = [
    (N_("Reading preferences"), [
        ("KOREADER_ANNOTATION_SYNC", N_("Copy my highlights and notes between my KOReader devices"), "pref"),
        ("KOREADER_STATUS_SYNC_ENABLED", N_("Show a book as reading or finished on all my KOReader devices"), "pref"),
        ("KOREADER_SYNC_READ_HISTORY", N_("Show books I read on one KOReader device in the History of my others"), "pref"),
        ("KOSYNC_FURTHEST_WINS", N_("Don't let an out-of-date device move my place backwards"), "pref"),
        ("SYNC_COMPLETION_PROPAGATION", N_("When I finish a book in one app, mark it finished in all of them"), "pref"),
        ("ABS_READING_CHECKPOINTS", N_("Save my ebook reading as entries in my Audiobookshelf listening history"), "pref"),
        ("STORYTELLER_LISTENING_SESSIONS", N_("Count reading along in Storyteller as Audiobookshelf listening time"), "pref"),
        ("SERIES_SHOW_CURRENT_BOOK_DETAIL", N_("Show each series on my Library as the book I'm on"), "pref"),
    ]),
    (N_("Audiobookshelf"), [
        ("ABS_ENABLED", N_("Enabled"), "bool"),
        ("ABS_KEY", N_("API token"), "secret"),
        ("ABS_LIBRARY_ID", N_("Library ID (optional, for a separate library)"), "text"),
        ("ABS_COLLECTION_NAME", N_("Collection name (synced books moved here)"), "text"),
    ]),
    (N_("KOReader / KoSync"), [
        ("KOSYNC_ENABLED", N_("Enabled"), "bool"),
        (
            "KOSYNC_AUTH_METHOD",
            N_("Authentication"),
            _select(
                ("kosync", N_("KoSync headers (default)")),
                ("basic", N_("HTTP Basic (Calibre-Web Automated)")),
            ),
        ),
        ("KOSYNC_USER", N_("Sync username"), "text"),
        ("KOSYNC_KEY", N_("Sync password"), "secret"),
        (
            "DEVICE_SYNC_SHELF_FILTER",
            N_("Bridge Sync: only download books on these Grimmory shelves (comma-separated, blank = all)"),
            "text",
        ),
    ]),
    (N_("KOReader Collections"), [
        (
            "DEVICE_SYNC_COLLECTION_SOURCE",
            N_("Collection source"),
            _select(
                ("off", N_("Off / Disabled")),
                ("grimmory", N_("Grimmory Shelves")),
                ("hardcover", N_("Hardcover Lists")),
            ),
        ),
        (
            "DEVICE_SYNC_COLLECTIONS",
            N_("Grimmory shelf mode"),
            _select(
                ("off", N_("Off / Disabled")),
                ("all", N_("All Shelves")),
                ("magic", N_("Magic Shelves Only")),
                ("shelf", N_("Regular Shelves Only")),
            ),
        ),
        ("DEVICE_SYNC_EXCLUDED_SHELVES", N_("Grimmory shelves to exclude"), "text"),
        (
            "DEVICE_SYNC_HARDCOVER_LISTS",
            N_("Hardcover list mode"),
            _select(
                ("all", N_("All Lists")),
                ("selected", N_("Selected Lists Only")),
            ),
        ),
        ("DEVICE_SYNC_HARDCOVER_LIST_NAMES", N_("Hardcover list names"), "text"),
    ]),
    (N_("Storyteller"), [
        ("STORYTELLER_ENABLED", N_("Enabled"), "bool"),
        ("STORYTELLER_USER", N_("Username"), "text"),
        ("STORYTELLER_PASSWORD", N_("Password"), "secret"),
        ("STORYTELLER_AUTO_LINK", N_("Automatically link aligned Storyteller books"), "bool"),
    ]),
    (N_("Grimmory"), [
        ("BOOKLORE_ENABLED", N_("Enabled"), "bool"),
        ("BOOKLORE_USER", N_("Username"), "text"),
        ("BOOKLORE_PASSWORD", N_("Password"), "secret"),
        ("BOOKLORE_SHELF_NAME", N_("Shelf name (synced books moved here)"), "text"),
        ("BOOKLORE_LIBRARY_ID", N_("Library ID (optional; blank uses all libraries)"), "text"),
        ("BOOKLORE_ANNOTATION_SYNC", N_("Highlight sync"), "bool"),
    ]),
    (N_("BookFusion"), [
        ("BOOKFUSION_ENABLED", N_("Enabled"), "bool"),
        ("BOOKFUSION_ACCESS_TOKEN", N_("Access token"), "secret"),
        ("BOOKFUSION_API_KEY", N_("Calibre API key (for uploads)"), "secret"),
        ("BOOKFUSION_ANNOTATION_SYNC", N_("Highlight sync"), "bool"),
    ]),
    (N_("BookOrbit"), [
        ("BOOKORBIT_ENABLED", N_("Enabled"), "bool"),
        ("BOOKORBIT_USER", N_("Username"), "text"),
        ("BOOKORBIT_PASSWORD", N_("Password"), "secret"),
        ("BOOKORBIT_SHELF_NAME", N_("Collection name (synced books moved here)"), "text"),
        ("BOOKORBIT_KOSYNC_USER", N_("KOReader sync username (highlight sync)"), "text"),
        ("BOOKORBIT_KOSYNC_KEY", N_("KOReader sync password (highlight sync)"), "secret"),
        ("BOOKORBIT_KOSYNC_OWNER", N_("KOReader sync owner (must match BookOrbit username)"), "text"),
    ]),
    (N_("Kavita"), [
        ("KAVITA_ENABLED", N_("Enabled"), "bool"),
        ("KAVITA_API_KEY", N_("Authentication key"), "secret"),
        ("KAVITA_LIBRARY_ID", N_("Library ID (optional; blank uses all libraries)"), "text"),
        ("KAVITA_COLLECTION_NAME", N_("Collection name (synced books moved here)"), "text"),
    ]),
    (N_("Readest"), [
        ("READEST_ENABLED", N_("Enabled"), "bool"),
        ("READEST_ANNOTATION_SYNC", N_("Highlight sync"), "bool"),
        ("READEST_EMAIL", N_("Account email"), "text"),
        ("READEST_PASSWORD", N_("Account password"), "secret"),
        ("READEST_UPLOAD_ON_MATCH", N_("Upload matched books to Readest"), "bool"),
        ("READEST_UPLOAD_READING", N_("Upload books you are currently reading"), "bool"),
        ("READEST_GROUP_NAME", N_("Group name for uploaded books"), "text"),
    ]),
    (N_("Calibre-Web Automated"), [
        ("CWA_ENABLED", N_("Enabled"), "bool"),
        ("CWA_USERNAME", N_("Username"), "text"),
        ("CWA_PASSWORD", N_("Password"), "secret"),
        ("CWA_SYNC_ENABLED", N_("Kobo sync enabled"), "bool"),
        ("CWA_SYNC_TOKEN", N_("Kobo sync token"), "secret"),
    ]),
    (N_("Hardcover"), [
        ("HARDCOVER_ENABLED", N_("Enabled"), "bool"),
        ("HARDCOVER_TOKEN", N_("API token"), "secret"),
        (
            "HARDCOVER_GRIMMORY_LIST_SYNC",
            N_("Grimmory shelves to Hardcover lists"),
            _select(
                ("off", N_("Off / Disabled")),
                ("all", N_("All Shelves")),
                ("magic", N_("Magic Shelves Only")),
                ("shelf", N_("Regular Shelves Only")),
            ),
        ),
        ("HARDCOVER_GRIMMORY_LIST_PREFIX", N_("Hardcover list name prefix"), "text"),
        ("HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES", N_("Grimmory shelves to exclude"), "text"),
    ]),
    (N_("StoryGraph"), [
        ("STORYGRAPH_ENABLED", N_("Enabled"), "bool"),
        ("STORYGRAPH_SESSION_COOKIE", N_("Session cookie"), "secret"),
        ("STORYGRAPH_REMEMBER_USER_TOKEN", N_("Remember-user token"), "secret"),
    ]),
]


# Service availability gates. A user may pick their own account for each
# integration, but they may not switch on a service the admin has turned off
# install-wide: the global value for these keys is authoritative and only ever
# takes capability away.
#
# A key belongs here only if an admin can actually switch its global on. The
# `*_ANNOTATION_SYNC` flags for Grimmory and BookFusion have no toggle anywhere in
# Settings, so their global sits at the seeded 'false' for good — gating those would
# not enforce a decision, it would silently end highlight sync that users have turned
# on for themselves.
SERVICE_ENABLE_KEYS = frozenset({
    "ABS_ENABLED",
    "KOSYNC_ENABLED",
    "READEST_ENABLED",
    # CWA's Kobo sync half has its own global toggle in Settings, so it can be
    # gated like a service; the annotation-sync flags cannot — see below.
    "CWA_SYNC_ENABLED",
    "STORYTELLER_ENABLED",
    "BOOKLORE_ENABLED",
    "BOOKORBIT_ENABLED",
    "KAVITA_ENABLED",
    "BOOKFUSION_ENABLED",
    "CWA_ENABLED",
    "HARDCOVER_ENABLED",
    "STORYGRAPH_ENABLED",
})

_FALSEY_SETTING_VALUES = frozenset({"false", "0", "no", "off"})
_TRUTHY_SETTING_VALUES = frozenset({"true", "1", "yes", "on"})


def global_service_disabled(key: str) -> bool:
    """Whether a service gate is switched off install-wide.

    Only an explicit falsey global counts. An unset key is not a decision, and
    must never be read as one — several gates ship with no seeded value.
    """
    if key not in SERVICE_ENABLE_KEYS:
        return False
    return str(os.environ.get(key, "")).strip().lower() in _FALSEY_SETTING_VALUES


def services_off_server_wide(credentials) -> list[str]:
    """Service gates switched on in ``credentials`` that the admin has switched off
    install-wide, sorted.

    The gate in :func:`resolve_setting` is silent by design: the user's own value is
    stored untouched and simply answers 'false' for as long as the global is off.
    The integrations pages show that as "Off server-wide"; this is the same answer
    for code that reports a user's integrations back (the admin API), so a
    provisioning script does not take a saved 'true' for a working one.
    ``credentials`` may be a stored credential dict or the values of a single
    write; only explicit truthy values count, a blank is not a switch.
    """
    if not credentials:
        return []
    return sorted(
        key for key in SERVICE_ENABLE_KEYS
        if str(credentials.get(key, "")).strip().lower() in _TRUTHY_SETTING_VALUES
        and global_service_disabled(key)
    )


def resolve_setting(credentials, key, default=None, *, enforce_global_gate: bool = True):
    """Resolve a config value for a (possibly per-user) client.

    Returns the user's value when present and non-empty. For recognized
    per-user account keys, regular user bundles do not fall back to the global
    admin environment unless their registry explicitly allows it.

    A service gate the admin has switched off install-wide resolves to 'false'
    for everyone, whatever the user stored. Enforcing it here rather than in each
    client means no code path can miss it — every client reads its enable flag
    through this function. The user's own value is left untouched in the database,
    so turning the global back on restores what each person chose.

    ``enforce_global_gate=False`` skips that check for callers that are not asking
    "should this run?" — an explicit *Test connection* answers "are these credentials
    good?", which does not depend on whether the service is currently switched on.
    """
    if enforce_global_gate and global_service_disabled(key):
        return "false"
    if credentials:
        val = credentials.get(key)
        if val not in (None, ""):
            return val
        if key in PER_USER_CREDENTIAL_KEYS and credentials.get(_ALLOW_GLOBAL_FALLBACK_KEY) is False:
            return default
    return os.environ.get(key, default)


def global_fallback_allowed(database_service, user) -> bool:
    """Whether this user's BLANK per-user credentials may fall back to the global config.

    The single source of truth for the ``_ALLOW_GLOBAL_FALLBACK_KEY`` flag that
    callers put in the credentials dict ``resolve_setting`` consumes. Only the
    PRIMARY admin may inherit: the global settings are that admin's own account
    mirrored outward (see ENGINE_MIRROR_KEYS), so letting a second admin inherit
    them would silently sync their books against the primary admin's
    Audiobookshelf/Grimmory/BookOrbit/CWA accounts.

    Fails closed — a database hiccup must never widen credential access.
    """
    if user is None or not getattr(user, "is_admin", False) or database_service is None:
        return False
    try:
        return bool(database_service.is_primary_admin(getattr(user, "id", None)))
    except Exception as e:
        logger.warning(
            "Primary-admin lookup failed for user %s; denying global credential fallback: %s",
            getattr(user, "id", "?"), e, exc_info=True,
        )
        return False


def user_preference(key: str, default: str = "", database_service=None) -> str:
    """A reading preference for the ambient user: their own choice, else the
    server-wide value from Settings, else ``default``.

    Sync cycles carry the user's credentials in context. KoSync requests carry
    only the user id, so pass ``database_service`` there to read the stored choice.
    """
    from src.utils.user_context import get_current_user_credentials, get_current_user_id
    credentials = get_current_user_credentials()
    own = None
    if credentials is not None:
        own = credentials.get(key)
    elif database_service is not None and get_current_user_id() is not None:
        user_id = get_current_user_id()
        try:
            own = database_service.get_user_credential(user_id, key)
        except Exception as e:
            logger.warning(
                "Reading preference %s for user %s could not be read; using the server default: %s",
                key, user_id, e, exc_info=True,
            )
    if own not in (None, ""):
        return str(own)
    return os.environ.get(key, default)


def user_preference_enabled(key: str, default: str = "false", database_service=None) -> bool:
    """Whether a boolean reading preference is on for the ambient user."""
    return user_preference(key, default, database_service).strip().lower() in _TRUTHY_SETTING_VALUES


def user_setting(key, default=None):
    """Resolve a setting for the current request/cycle's user (ambient context),
    falling back to the global os.environ value. Use this for direct (non-client,
    non-cached) settings reads — library id, enable flags, search scope — that
    must honor the logged-in user instead of the global/admin config."""
    from src.utils.user_context import get_current_user_credentials
    return resolve_setting(get_current_user_credentials(), key, default)
