"""KOReader/KoSync header utilities.

Centralises the MD5 key hashing and header construction used by both
the KoSync *client* (api_clients.KoSyncClient) and the KoSync
*server* (kosync_server.kosync_auth_required).
"""

import hashlib

KOSYNC_ACCEPT = "application/vnd.koreader.v1+json"


def hash_kosync_key(plain_key: str) -> str:
    """Return the MD5 hex-digest of a KoSync password/key."""
    return hashlib.md5(plain_key.encode("utf-8")).hexdigest()


def kosync_auth_headers(user: str, hashed_key: str) -> dict:
    """Build the standard header dict expected by KoSync-compatible servers.

    Includes auth credentials and the KOReader accept type on every
    request — some servers (e.g. Grimmory) require all three headers
    even on unauthenticated endpoints like /healthcheck.
    """
    return {
        "x-auth-user": user,
        "x-auth-key": hashed_key,
        "accept": KOSYNC_ACCEPT,
    }
