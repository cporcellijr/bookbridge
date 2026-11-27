"""C09 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from src.web.audio_link_jobs import dispatch_audio_links

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def sync_daemon():
    """Background sync daemon running in a separate thread."""
    web = _web_server()
    try:
        # Setup schedule for sync operations
        # Use the global SYNC_PERIOD_MINS which is validated
        web.schedule.every(int(web.SYNC_PERIOD_MINS)).minutes.do(web.manager.run_sync_for_all_users)
        web.schedule.every(int(web.SYNC_PERIOD_MINS)).minutes.do(web._mirror_kosync_logins_from_grimmory)
        web.schedule.every(1).minutes.do(web.manager.check_pending_jobs)
        web.schedule.every(30).seconds.do(dispatch_audio_links, web)
        web.schedule.every(1).minutes.do(web._suggestions_auto_scan_tick)
        web.schedule.every(1).minutes.do(web.manager.flush_reading_sessions_for_all_users)
        web.schedule.every(int(web.SYNC_PERIOD_MINS)).minutes.do(web._reconcile_aligned_shelf)
        web.schedule.every(1).hours.do(web._run_diagnostics_send)
        web.schedule.every(1).hours.do(web._run_storyteller_library_hourly)
        web.schedule.every(int(web.SYNC_PERIOD_MINS)).minutes.do(_reconcile_abs_collection)

        web.logger.info(f"🔄 Sync daemon started (period: {web.SYNC_PERIOD_MINS} minutes)")

        web._mirror_kosync_logins_from_grimmory()

        # Run initial sync cycle (per user)
        try:
            web.manager.run_sync_for_all_users()
        except Exception as e:
            web.logger.error(f"❌ Initial sync cycle failed: {e}", exc_info=True)

        web._reconcile_aligned_shelf()

        # Catch-up diagnostics send on startup (24h guard inside is a no-op when not due)
        try:
            web._run_diagnostics_send()
        except Exception:
            pass

        _reconcile_abs_collection()

        # Main daemon loop
        while True:
            try:
                # logger.debug("Running pending schedule jobs...")
                web.schedule.run_pending()
                web.time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                web.logger.error(f"❌ Sync daemon error: {e}", exc_info=True)
                web.time.sleep(60)  # Wait longer on error

    except Exception as e:
        web.logger.error(f"❌ Sync daemon crashed: {e}", exc_info=True)


def _is_abs_audio_book(book):
    if (book.sync_mode or '') == 'ebook_only':
        return False
    if (book.audio_source or 'ABS').strip().lower() != 'abs':
        return False
    return not str(book.abs_id).startswith(('booklore:', 'bookorbit:', 'ebook-'))


def _reconcile_abs_collection():
    web = _web_server()
    if not web.env_truthy('ABS_COLLECTION_RECONCILE') or web.database_service is None:
        return
    conditions = web.get_persistent_condition_logger()
    try:
        default_owner = web.database_service._default_user_id()
        by_owner = {}
        for book in web.database_service.get_books_by_status('active'):
            if _is_abs_audio_book(book):
                by_owner.setdefault(book.user_id or default_owner, []).append(book.abs_id)
        registry = web.container.user_client_registry()
    except Exception as e:
        conditions.warn(
            web.logger, "abs_collection_reconcile", "ABS collection reconcile failed: %s", e, exc_info=True,
        )
        return
    conditions.resolve(web.logger, "abs_collection_reconcile", "ABS collection reconcile resumed")
    added = 0
    for owner, item_ids in by_owner.items():
        if owner is None:
            continue
        try:
            bundle = registry.get_clients(owner)
            if not bundle.abs_client.is_configured():
                continue
            collection = web.resolve_setting(bundle.credentials, "ABS_COLLECTION_NAME", "Synced with KOReader")
            count, refused = bundle.abs_client.add_missing_to_collection(item_ids, collection)
        except Exception as e:
            conditions.warn(
                web.logger, f"abs_collection_reconcile:{owner}",
                "ABS collection reconcile failed for user %s: %s", owner, e, exc_info=True,
            )
            continue
        conditions.resolve(
            web.logger, f"abs_collection_reconcile:{owner}", "ABS collection reconcile resumed for user %s", owner,
        )
        added += count
        refused_key = f"abs_collection_refused:{owner}:{collection}"
        if refused:
            conditions.warn(
                web.logger, refused_key,
                "ABS collection '%s' of user %s is missing %d matched audiobook(s) that could not be added: %s",
                collection, owner, len(refused), refused[0],
            )
        else:
            conditions.resolve(
                web.logger, refused_key, "ABS collection '%s' of user %s is complete again", collection, owner,
            )
    if added:
        web.logger.info("🏷️ ABS collection reconcile added %d matched audiobook(s)", added)
    return added
