"""Kosync login mirror helpers; extracted without behavior changes."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

KOSYNC_MIRRORED_LOGIN_KEY = "KOSYNC_MIRRORED_LOGIN"


def _kosync_login_fingerprint(username, password):
    web = _web_server()
    return web.hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()


def _mirror_kosync_login(user, registry, holders):
    web = _web_server()
    client = registry.get_clients(user.id).booklore_client
    if not client.is_configured():
        return False
    login = client.get_koreader_sync_login()
    if not login:
        return False
    username, password = login
    conditions = web.get_persistent_condition_logger()
    reader = web.sanitize_log_data(user.username)

    collision_key = f"kosync_login_mirror_collision:{user.id}"
    holder = holders.get(username.lower())
    if holder is not None and holder.id != user.id:
        conditions.warn(
            web.logger, collision_key,
            "⚠️ KoSync login for '%s' not taken from Grimmory: the username '%s' already belongs to '%s'",
            reader, web.sanitize_log_data(username), web.sanitize_log_data(holder.username),
        )
        return False
    conditions.resolve(web.logger, collision_key, "KoSync username for '%s' no longer belongs to another user", reader)

    creds = web.database_service.get_user_credentials(user.id)
    stored_user = creds.get("KOSYNC_USER") or ""
    stored_key = creds.get("KOSYNC_KEY") or ""
    mirrored = creds.get(KOSYNC_MIRRORED_LOGIN_KEY)
    own_login_key = f"kosync_login_mirror_kept:{user.id}"
    if (
        stored_user and stored_key
        and (stored_user, stored_key) != (username, password)
        and mirrored != _kosync_login_fingerprint(stored_user, stored_key)
    ):
        conditions.warn(
            web.logger, own_login_key,
            "⚠️ KoSync login for '%s' was set in BookBridge and is kept; clear their KoSync username "
            "to take the login from Grimmory",
            reader,
        )
        return False
    conditions.resolve(web.logger, own_login_key, "KoSync login for '%s' now follows Grimmory", reader)

    changed = []
    if stored_user != username:
        changed.append("username")
    if stored_key != password:
        changed.append("password")
    fingerprint = _kosync_login_fingerprint(username, password)
    if changed or mirrored != fingerprint:
        web.database_service.set_user_credentials(user.id, {
            "KOSYNC_USER": username,
            "KOSYNC_KEY": password,
            KOSYNC_MIRRORED_LOGIN_KEY: fingerprint,
        })
    if not changed:
        return False
    registry.invalidate(user.id)
    holders[username.lower()] = user
    web.logger.info("🔑 KoSync login for '%s' taken from Grimmory (%s)", reader, ", ".join(changed))
    return True


def _mirror_kosync_logins_from_grimmory():
    web = _web_server()
    if not web.env_truthy('KOSYNC_CREDENTIALS_FROM_GRIMMORY') or web.database_service is None:
        return
    conditions = web.get_persistent_condition_logger()
    try:
        registry = web.container.user_client_registry()
        users = web.database_service.list_users()
        holders = {}
        for user in users:
            name = (web.database_service.get_user_credentials(user.id).get("KOSYNC_USER") or "").lower()
            if name:
                holders.setdefault(name, user)
        global_name = web.os.environ.get("KOSYNC_USER", "").lower()
        default_user_id = web.database_service._default_user_id()
        default_user = next((user for user in users if user.id == default_user_id), None)
        if global_name and default_user is not None:
            holders.setdefault(global_name, default_user)
    except Exception as e:
        conditions.warn(
            web.logger, "kosync_login_mirror", "KoSync login mirror: could not list users: %s", e, exc_info=True,
        )
        return
    conditions.resolve(web.logger, "kosync_login_mirror", "KoSync login mirror: users listed again")

    updated = 0
    for user in users:
        if not user.active:
            continue
        failure_key = f"kosync_login_mirror:{user.id}"
        try:
            if _mirror_kosync_login(user, registry, holders):
                updated += 1
        except Exception as e:
            conditions.warn(
                web.logger, failure_key, "KoSync login mirror failed for user %s: %s", user.id, e, exc_info=True,
            )
        else:
            conditions.resolve(web.logger, failure_key, "KoSync login mirror works again for user %s", user.id)
    return updated

