"""Application-level encryption at rest for stored credentials.

BookBridge must replay most of its credentials verbatim to third-party services
(ABS API tokens, Storyteller/CWA/BookOrbit/Grimmory passwords, KoSync sync
passwords, tracker tokens), so they cannot be one-way hashed the way a login
password is. Instead the values in the ``settings`` and ``user_credentials``
tables are wrapped with Fernet (AES-128-CBC + HMAC-SHA256) so a leaked
``database.db``, a copied backup, or a casual ``SELECT * FROM user_credentials``
does not hand over every account the bridge touches.

Threat model, stated plainly: by default the key lives beside the database in
``DATA_DIR``. That protects the database file in isolation — it does **not**
protect against an attacker who already has the whole data volume or the host.
Set ``BOOKBRIDGE_SECRET_KEY`` (or ``BOOKBRIDGE_SECRET_KEY_FILE`` pointing
outside the volume) to separate the key from the ciphertext.

``BOOKBRIDGE_SECRET_KEY`` is deliberately **not** a registered setting: it is
read from the real process environment only, never from the database, because a
key stored next to the ciphertext it protects would be pointless.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Marks a wrapped value. Legacy plaintext rows carry no prefix and are read
# through untouched, then re-wrapped on the next write.
SECRET_PREFIX = "enc:v1:"

_KEY_ENV = "BOOKBRIDGE_SECRET_KEY"
_KEY_FILE_ENV = "BOOKBRIDGE_SECRET_KEY_FILE"
_KDF_SALT = b"bookbridge-secret-store-v1"

_lock = threading.Lock()
_fernet = None
_init_done = False
_unavailable_reason: Optional[str] = None


def _extra_secret_keys() -> frozenset:
    """Secret settings that have no per-user field-group entry."""
    return frozenset({
        "LLM_API_KEY",
        "DEEPGRAM_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "DIAGNOSTICS_INGEST_TOKEN",
        # Flask session signing key — leaking it allows session forgery.
        "WEB_SECRET_KEY",
        # Rotating Readest session tokens, cached by the client, never typed.
        "READEST_ACCESS_TOKEN",
        "READEST_REFRESH_TOKEN",
    })


def secret_keys() -> frozenset:
    """Every config key whose value is encrypted at rest.

    Derived from the ``'secret'`` field types in ``PER_USER_FIELD_GROUPS`` so a
    new credential added to the per-user UI is covered automatically, plus the
    global-only secrets that never appear on that page.
    """
    from src.utils.user_config import PER_USER_FIELD_GROUPS

    keys = {
        key
        for _group, fields in PER_USER_FIELD_GROUPS
        for key, _label, ftype in fields
        if ftype == "secret"
    }
    return frozenset(keys | _extra_secret_keys())


def is_secret_key(key: str) -> bool:
    """True when values for ``key`` are encrypted at rest."""
    return key in secret_keys()


def _key_file_path() -> Path:
    override = os.environ.get(_KEY_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("DATA_DIR", "/data")) / "secret.key"


def _coerce_fernet_key(raw: str) -> bytes:
    """Accept a real Fernet key verbatim; derive one from any other passphrase."""
    candidate = raw.strip()
    try:
        if len(base64.urlsafe_b64decode(candidate.encode("utf-8"))) == 32:
            return candidate.encode("utf-8")
    except Exception:
        pass
    derived = hashlib.scrypt(
        candidate.encode("utf-8"), salt=_KDF_SALT, n=2**14, r=8, p=1, dklen=32
    )
    return base64.urlsafe_b64encode(derived)


def _load_or_create_key_file() -> bytes:
    from cryptography.fernet import Fernet

    path = _key_file_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return _coerce_fernet_key(existing)

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.decode("utf-8"), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows bind mounts and some network filesystems ignore chmod.
        pass
    logger.info(f"🔐 Generated credential encryption key at {path}")
    return key


def _get_fernet():
    global _fernet, _init_done, _unavailable_reason
    if _init_done:
        return _fernet
    with _lock:
        if _init_done:
            return _fernet
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            _unavailable_reason = "the 'cryptography' package is not installed"
            logger.error(
                "🔓 Credential encryption UNAVAILABLE: the 'cryptography' package "
                "is not installed. Secrets are being stored in PLAINTEXT. Rebuild "
                "the container (docker compose up -d --build) to fix this."
            )
            _init_done = True
            return None
        try:
            env_key = os.environ.get(_KEY_ENV, "").strip()
            raw = _coerce_fernet_key(env_key) if env_key else _load_or_create_key_file()
            _fernet = Fernet(raw)
        except Exception as e:
            _unavailable_reason = str(e)
            logger.error(
                f"🔓 Credential encryption UNAVAILABLE ({e}). Secrets are being "
                f"stored in PLAINTEXT."
            )
            _fernet = None
        _init_done = True
        return _fernet


def available() -> bool:
    """True when values can actually be encrypted."""
    return _get_fernet() is not None


def reset_cache() -> None:
    """Drop the cached key so the next call re-resolves it. For tests and for
    re-reading a key file that was replaced at runtime."""
    global _fernet, _init_done, _unavailable_reason
    with _lock:
        _fernet = None
        _init_done = False
        _unavailable_reason = None


def is_encrypted(value: Optional[str]) -> bool:
    """True when ``value`` is a wrapped ciphertext rather than legacy plaintext."""
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def encrypt(value: Optional[str]) -> Optional[str]:
    """Wrap a plaintext value for storage.

    Passes through ``None``, empty strings, and already-wrapped values. Returns
    the input unchanged when encryption is unavailable, so the bridge keeps
    working (loudly, in plaintext) rather than losing access to its accounts.
    """
    if value is None or value == "" or is_encrypted(value):
        return value
    fernet = _get_fernet()
    if fernet is None:
        return value
    try:
        token = fernet.encrypt(str(value).encode("utf-8")).decode("utf-8")
        return f"{SECRET_PREFIX}{token}"
    except Exception as e:
        logger.error(f"🔓 Failed to encrypt credential value: {e}")
        return value


def decrypt(value: Optional[str], label: str = "credential") -> Optional[str]:
    """Unwrap a stored value.

    Legacy plaintext (no prefix) is returned as-is. A ciphertext that will not
    decrypt — wrong key, restored database without its ``secret.key`` — yields
    an empty string rather than the raw token, so the unreadable credential
    reads as "not configured" instead of being replayed to a service as a
    password.
    """
    if not is_encrypted(value):
        return value
    fernet = _get_fernet()
    if fernet is None:
        logger.error(
            f"🔓 Cannot decrypt {label}: encryption is unavailable "
            f"({_unavailable_reason})."
        )
        return ""
    try:
        return fernet.decrypt(value[len(SECRET_PREFIX):].encode("utf-8")).decode("utf-8")
    except Exception:
        logger.error(
            f"🔐 Could not decrypt {label} — the encryption key does not match "
            f"the stored value. If you restored a backup, restore "
            f"{_key_file_path()} alongside it or re-enter this credential."
        )
        return ""
