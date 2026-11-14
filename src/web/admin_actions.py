"""Admin actions helpers; extracted without behavior changes."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

def _apply_user_admin_action(form):
    """Handle a single user-management action (create/reset/toggle/delete).

    Shared by the legacy /admin/users page and the Settings → Users tab.
    Returns (message, error)."""
    web = _web_server()
    message = None
    error = None

    action = form.get('action')
    try:
        if action == 'create':
            username = (form.get('username') or '').strip()
            password = form.get('password') or ''
            role = form.get('role') or 'user'
            if not username or not password:
                error = "Username and password are required"
            elif web.database_service.get_user_by_username(username):
                error = "That username already exists"
            else:
                new_user = web.database_service.create_user(username, password, role=role)
                message = f"Created user '{username}'"
                linked = web._share_library_with_new_user(new_user)
                if linked:
                    message = f"Created user '{username}' and shared {linked} book(s)"
        elif action == 'reset_password':
            uid = int(form.get('user_id'))
            new_pw = form.get('password') or ''
            if not new_pw:
                error = "Password cannot be empty"
            else:
                web.database_service.set_user_password(uid, new_pw)
                message = "Password reset"
        elif action == 'toggle_active':
            uid = int(form.get('user_id'))
            target = web.database_service.get_user(uid)
            if target:
                disabling = bool(target.active)
                try:
                    web.database_service.update_user_account(uid, active=not disabling)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    web._after_user_account_changed(uid)
                    message = f"{'Disabled' if disabling else 'Enabled'} '{target.username}'"
                    if not disabling:
                        linked = web._share_library_with_returning_user(target)
                        if linked:
                            message = f"Enabled '{target.username}' and shared {linked} book(s)"
        elif action == 'set_role':
            uid = int(form.get('user_id'))
            new_role = (form.get('role') or '').strip().lower()
            target = web.database_service.get_user(uid)
            demoting = new_role == 'user'
            if not target:
                error = "User not found"
            elif new_role not in ('admin', 'user'):
                error = "Invalid role"
            elif target.role == new_role:
                error = f"'{target.username}' is already {new_role}"
            else:
                try:
                    web.database_service.update_user_account(uid, role=new_role)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    # The cached client bundle carries the global-fallback flag, which
                    # this change flips.
                    web._after_user_account_changed(uid)
                    web.logger.info(
                        "👤 Role change: '%s' is now '%s'",
                        web.sanitize_log_data(target.username), new_role,
                    )
                    if demoting:
                        message = f"'{target.username}' is now a regular user"
                    else:
                        message = (
                            f"'{target.username}' is now an admin. They still use their own "
                            f"service logins — set them under Integrations."
                        )
        elif action == 'delete':
            uid = int(form.get('user_id'))
            target = web.database_service.get_user(uid)
            if not target:
                error = "User not found"
            elif uid == web.current_user().id:
                error = "You can't delete your own account"
            else:
                try:
                    deleted = web.database_service.delete_user(uid)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    if deleted:
                        web._clear_match_queue_for_user_id(uid)
                        web._invalidate_user_clients(uid)
                        if web.abs_socket_manager is not None:
                            web.abs_socket_manager.stop_user(uid)
                        message = f"Deleted '{target.username}'"
                    else:
                        error = "User not found"
        elif action == 'share_library':
            if not web.env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
                error = "Enable Features → Shared Library first, then share the existing catalog."
            else:
                result = web.database_service.share_all_books_with_active_users()
                links = result.get('links', 0)
                users = result.get('users', 0)
                web.logger.info(
                    "🔗 Shared %d existing book link(s) across %d active user(s) (share-all-books reconcile)",
                    links, users,
                )
                if links:
                    message = f"Shared {links} book link(s) across {users} user(s)"
                else:
                    message = f"All {users} user(s) already see the full library"
    except Exception as e:
        error = f"Action failed: {e}"
    return message, error

