"""Ambient 'current sync user' for the duration of a per-user operation.

A sync cycle runs for one user. Rather than thread user_id through every state
call, the cycle sets this contextvar and the data layer (DatabaseService) uses
it as the default owner for state reads/writes. It is per-thread/per-context, so
concurrent cycles in different threads don't interfere. When unset (e.g. the
default single-user cycle, or a Flask request), callers fall back to the default
(admin) user.
"""

import contextvars

_current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)
# The current user's per-user credential/setting overrides (a dict of
# PER_USER_CREDENTIAL_KEYS -> value). Lets non-cached settings reads (library
# id, enable flags, search scope) resolve per-user without a DB hit each time.
_current_user_credentials: contextvars.ContextVar = contextvars.ContextVar(
    "current_user_credentials", default=None
)


def get_current_user_id():
    return _current_user_id.get()


def set_current_user_id(user_id):
    """Set the ambient user; returns a token for reset()."""
    return _current_user_id.set(user_id)


def reset_current_user_id(token) -> None:
    try:
        _current_user_id.reset(token)
    except Exception:
        pass


def get_current_user_credentials():
    return _current_user_credentials.get()


def set_current_user_credentials(creds):
    """Set the ambient per-user credentials/settings; returns a reset token."""
    return _current_user_credentials.set(creds)


def reset_current_user_credentials(token) -> None:
    try:
        _current_user_credentials.reset(token)
    except Exception:
        pass
