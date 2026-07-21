"""KOReader/KoSync header utilities.

Centralises the MD5 key hashing and header construction used by both
the KoSync *client* (api_clients.KoSyncClient) and the KoSync
*server* (kosync_server.kosync_auth_required).
"""

import hashlib

KOSYNC_ACCEPT = "application/vnd.koreader.v1+json"


def hash_kosync_key(plain_key: str) -> str:
    """Return the MD5 hex-digest of a KoSync password/key."""
    # KOReader/KoSync protocol clients send MD5(password) in x-auth-key.
    return hashlib.md5(plain_key.encode("utf-8")).hexdigest()  # nosec B324


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


def kosync_request_kwargs(
    user: str,
    plain_key: str,
    auth_method: str = "kosync",
) -> dict:
    """Build requests-compatible authentication arguments for a KoSync server."""
    if str(auth_method).strip().lower() == "basic":
        return {
            "headers": {"accept": KOSYNC_ACCEPT},
            "auth": (user, plain_key),
        }

    hashed_key = hash_kosync_key(plain_key) if plain_key else ""
    return {"headers": kosync_auth_headers(user, hashed_key)}
