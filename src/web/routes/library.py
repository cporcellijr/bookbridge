"""Library HTTP routes registered without endpoint namespaces."""

from typing import Any

from src.services.job_queue_view import acquisition_job_needs_choice


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/', 'index', index, None),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def index():
    """Dashboard - loads books and progress from database service"""
    web = _web_server()
    user = web.current_user()
    mappings, overall_progress, integrations = web._dashboard_mappings_for_user(user)

    suggestions = [s for s in web.database_service.get_all_pending_suggestions(user_id=web.get_current_user_id()) if len(s.matches) > 0]
    acquisition_choice_jobs = [
        job for job in web.database_service.get_acquisition_jobs(user.id)
        if acquisition_job_needs_choice(job)
    ] if user else []
    grouped_mappings = web._group_dashboard_mappings_by_series(mappings)
    library_template = web.ui_template('index.html')
    hero_mapping = None
    wallpaper_mapping = None
    if library_template == 'next/index.html':
        display = web._display_settings_for(user)
        if display["UI_SHOW_HERO"] == "true":
            hero_mapping = web._pick_hero_mapping(mappings)
            if hero_mapping:
                hero_mapping["is_hero"] = True
                leader = hero_mapping.get("last_sync_leader")
                hero_mapping["hero_leader_service"] = web._hero_leader_service(leader)
                hero_mapping["hero_leader_progress"] = web._hero_leader_percent(hero_mapping, leader)
        if display["UI_LIBRARY_BACKGROUND"] == "cover":
            wallpaper_mapping = hero_mapping or web._pick_hero_mapping(mappings)

    latest_version, update_available = web.get_update_status()
    whats_new_ctx = web._build_whats_new_context(user)

    show_diagnostics_modal = (
        not web.env_truthy('DIAGNOSTICS_PROMPTED')
        and bool(web.os.environ.get('DIAGNOSTICS_ENDPOINT_URL', '').strip())
        and (web.current_app.config.get('LOGIN_DISABLED') or (user and getattr(user, 'is_admin', False)))
    )

    return web.render_template(
        library_template,
        hero_mapping=hero_mapping,
        wallpaper_mapping=wallpaper_mapping,
        mappings=mappings,
        grouped_mappings=grouped_mappings,
        integrations=integrations,
        progress=overall_progress,
        suggestions=suggestions,
        acquisition_choice_jobs=acquisition_choice_jobs,
        app_version=web.APP_VERSION,
        update_available=update_available,
        latest_version=latest_version,
        show_diagnostics_modal=show_diagnostics_modal,
        whats_new=whats_new_ctx
    )
