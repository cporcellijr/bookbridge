"""Multi-user bootstrap.

First-run admin creation is handled by the web setup screen. Startup only
backfills pre-existing single-user data to an admin once one exists.
"""

import logging

logger = logging.getLogger(__name__)


def _backfill_orphans_to_admin(database_service, admin) -> dict:
    counts = database_service.assign_orphan_rows_to_user(admin.id)
    moved = sum(counts.values())
    if moved:
        logger.info(
            "Multi-user bootstrap: assigned %d pre-existing rows to admin '%s' (%s)",
            moved,
            admin.username,
            ", ".join(f"{k}={v}" for k, v in counts.items() if v),
        )
    return counts


def _prefill_admin_integrations_from_global(database_service, admin) -> int:
    """One-time: seed the admin's per-user integration credentials from the
    existing global settings so an install upgrading to multi-user doesn't have
    to re-enter everything. Fills blank fields only; gated by a flag so it runs
    once. Returns the number of values copied."""
    try:
        settings = database_service.get_all_settings()
    except Exception:
        return 0
    if settings.get("admin_integrations_prefilled") == "true":
        return 0

    from src.utils.user_config import PER_USER_CREDENTIAL_KEYS

    copied = 0
    for key in PER_USER_CREDENTIAL_KEYS:
        if key.startswith("__"):
            continue
        gval = (settings.get(key) or "").strip()
        if not gval:
            continue
        try:
            if not database_service.get_user_credential(admin.id, key):
                database_service.set_user_credential(admin.id, key, gval)
                copied += 1
        except Exception:
            continue
    try:
        database_service.set_setting("admin_integrations_prefilled", "true")
    except Exception:
        pass
    if copied:
        logger.info(
            "Multi-user bootstrap: pre-filled %d global integration values into admin '%s' account",
            copied, admin.username,
        )
    return copied


def bootstrap_admin_user(database_service) -> None:
    """Backfill orphan per-user rows to the first admin, if one exists.

    Idempotent and safe to run on every startup. If no users exist yet, the
    first-run setup page will create the admin and run the same backfill.
    """
    try:
        if database_service.count_users() == 0:
            logger.info("Multi-user bootstrap: no users found; first-run setup is required")
            return

        admin = next((u for u in database_service.list_users() if u.role == "admin"), None)
        if not admin:
            logger.warning("Multi-user bootstrap: no admin user found; skipping orphan backfill")
            return

        _backfill_orphans_to_admin(database_service, admin)
        _prefill_admin_integrations_from_global(database_service, admin)
    except Exception as e:
        logger.error("Multi-user bootstrap failed: %s", e)


def create_initial_admin_user(database_service, username: str, password: str):
    """Create the first admin user and claim pre-existing single-user rows.

    Returns (user, counts). Raises ValueError when setup is no longer allowed.
    """
    if database_service.count_users() != 0:
        raise ValueError("Initial admin already exists")
    user = database_service.create_user(username, password, role="admin")
    counts = _backfill_orphans_to_admin(database_service, user)
    _prefill_admin_integrations_from_global(database_service, user)
    return user, counts
