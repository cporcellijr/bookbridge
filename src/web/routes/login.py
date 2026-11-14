"""Login, first-run setup, and logout HTTP routes without endpoint renaming."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register authentication endpoints with their original names and methods."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/setup', 'setup', setup, ['GET', 'POST']),
        ('/login', 'login', login, ['GET', 'POST']),
        ('/logout', 'logout', logout, ['GET', 'POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def login():
    """Render/handle the login form."""
    web = _web_server()
    if web.database_service is not None:
        try:
            if web.database_service.count_users() == 0:
                return web.redirect(web.url_for('setup', next=web.request.args.get('next') or web.url_for('index')))
        except Exception:
            pass
    if web.current_user() is not None:
        return web.redirect(web.url_for('index'))

    remote_user = web._remote_auth_user()
    if remote_user is not None:
        return web._establish_session_and_redirect(remote_user)

    error = None
    if web.request.method == 'POST':
        username = (web.request.form.get('username') or '').strip()
        password = web.request.form.get('password') or ''
        failure_key = (username.lower(), web.request.remote_addr or "")
        now = web.time.time()
        with web._LOGIN_FAILURES_LOCK:
            failures = [t for t in web._LOGIN_FAILURES.get(failure_key, []) if now - t < web._LOGIN_FAILURE_WINDOW]
            if failures:
                web._LOGIN_FAILURES[failure_key] = failures
            else:
                web._LOGIN_FAILURES.pop(failure_key, None)
        if len(failures) >= web._LOGIN_FAILURE_LIMIT:
            return web.render_template('login.html', error="Too many login attempts. Try again shortly."), 429
        user = None
        if web.database_service is not None:
            user = web.database_service.verify_user_credentials(username, password)
        if user:
            with web._LOGIN_FAILURES_LOCK:
                web._LOGIN_FAILURES.pop(failure_key, None)
            return web._establish_session_and_redirect(user)
        with web._LOGIN_FAILURES_LOCK:
            web._LOGIN_FAILURES[failure_key] = failures + [now]
        error = "Invalid username or password"
        web.logger.warning("Failed login attempt for username '%s' from %s", username, web.request.remote_addr)

    return web.render_template('login.html', error=error), (401 if error else 200)


def setup():
    """First-run admin setup. Only available while no users exist."""
    web = _web_server()
    if web.database_service is None:
        return ("Database not initialized", 500)
    try:
        if web.database_service.count_users() != 0:
            return web.redirect(web.url_for('login'))
    except Exception as e:
        web.logger.error("Could not inspect users for setup: %s", e, exc_info=True)
        return ("Database unavailable", 500)

    error = None
    if web.request.method == 'POST':
        username = (web.request.form.get('username') or '').strip()
        password = web.request.form.get('password') or ''
        confirm_password = web.request.form.get('confirm_password') or ''

        if not username:
            error = "Username is required"
        elif not password:
            error = "Password is required"
        elif password != confirm_password:
            error = "Passwords do not match"
        else:
            try:
                from src.db.user_bootstrap import create_initial_admin_user
                user, _counts = create_initial_admin_user(web.database_service, username, password)
                web.session.clear()
                return web._establish_session_and_redirect(user)
            except ValueError:
                return web.redirect(web.url_for('login'))
            except Exception as e:
                web.logger.error("Initial admin setup failed: %s", e, exc_info=True)
                error = "Could not create admin account"

    return web.render_template('setup.html', error=error), (400 if error else 200)


def logout():
    web = _web_server()
    web.session.clear()
    return web.redirect(web.url_for('login'))
