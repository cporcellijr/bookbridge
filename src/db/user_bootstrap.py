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


def _repair_bookorbit_links(database_service, admin) -> dict:
    """Repair legacy BookOrbit ownership links after assigning orphan rows."""
    try:
        counts = database_service.repair_missing_bookorbit_user_links()
    except Exception as exc:
        logger.warning(
            "Multi-user bootstrap: BookOrbit ownership repair failed for admin '%s': %s",
            admin.username,
            exc,
        )
        return {}
    if counts.get("created"):
        logger.info(
            "Multi-user bootstrap: repaired %d BookOrbit ownership links for admin '%s'",
            counts["created"],
            admin.username,
        )
    return counts


def _prefill_admin_integrations_from_global(database_service, admin) -> int:
    """Seed the admin's per-user integration credentials from the existing global
    settings so an install upgrading to multi-user doesn't have to re-enter
    everything. Fills blank fields only, and considers each per-user key exactly
    once (tracked in `admin_integrations_prefilled_keys`) — so newly-promoted keys
    (e.g. ABS_COLLECTION_NAME) seed on the next startup without re-seeding or
    clobbering anything the admin already set or intentionally cleared. Returns the
    number of values copied."""
    try:
        settings = database_service.get_all_settings()
    except Exception:
        return 0

    from src.utils.user_config import PER_USER_CREDENTIAL_KEYS

    raw = settings.get("admin_integrations_prefilled_keys") or ""
    already = {k for k in raw.split(",") if k}
    # Back-compat: the old one-time boolean meant every per-user key known at that
    # time had been seeded. Seed `already` from the admin's current credentials so
    # we don't re-seed keys they already have (or set), while still picking up keys
    # newly promoted to per-user since then.
    if not already and settings.get("admin_integrations_prefilled") == "true":
        for key in PER_USER_CREDENTIAL_KEYS:
            try:
                if database_service.get_user_credential(admin.id, key):
                    already.add(key)
            except Exception:
                continue

    copied = 0
    for key in PER_USER_CREDENTIAL_KEYS:
        if key.startswith("__") or key in already:
            continue
        gval = (settings.get(key) or "").strip()
        if gval:
            try:
                if not database_service.get_user_credential(admin.id, key):
                    database_service.set_user_credential(admin.id, key, gval)
                    copied += 1
            except Exception:
                continue
        already.add(key)  # considered once — don't reconsider on future startups

    try:
        database_service.set_setting("admin_integrations_prefilled_keys", ",".join(sorted(already)))
        database_service.set_setting("admin_integrations_prefilled", "true")
    except Exception:
        pass
    if copied:
        logger.info(
            "Multi-user bootstrap: pre-filled %d global integration values into admin '%s' account",
            copied, admin.username,
        )
    return copied


def _warn_on_credential_divergence(database_service, admin) -> list:
    """Warn when an engine-mirrored credential's global settings copy differs
    from the admin's per-user account value. Background singletons (shelf
    watch, scans, ABS socket, manifest) use the global copy while syncs use
    the account copy, so silent divergence produces 'tests pass but sync or
    background features fail' reports (#328). Returns the divergent keys."""
    from src.utils.user_config import ENGINE_MIRROR_KEYS
    try:
        settings = database_service.get_all_settings()
    except Exception:
        return []
    divergent = []
    for key in ENGINE_MIRROR_KEYS:
        gval = (settings.get(key) or "").strip()
        try:
            uval = (database_service.get_user_credential(admin.id, key) or "").strip()
        except Exception:
            continue
        if uval and gval != uval:
            divergent.append(key)
            logger.warning(
                "\u26a0\ufe0f Credential divergence: global %s differs from admin '%s' account value \u2014 "
                "background services use the global copy while syncs use the account copy. "
                "Re-save Account \u2192 Integrations to reconcile.",
                key,
                admin.username,
            )
    return divergent


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
        _repair_bookorbit_links(database_service, admin)
        _prefill_admin_integrations_from_global(database_service, admin)
        _warn_on_credential_divergence(database_service, admin)
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
    _repair_bookorbit_links(database_service, user)
    _prefill_admin_integrations_from_global(database_service, user)
    return user, counts
