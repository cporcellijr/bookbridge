"""C04 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _tracker_automatch_worker():
    web = _web_server()
    while True:
        sync_clients, book = web._TRACKER_AUTOMATCH_QUEUE.get()
        try:
            abs_id = getattr(book, "abs_id", "?")
            hardcover = sync_clients.get("Hardcover")
            if hardcover and hardcover.is_configured():
                try:
                    hardcover._automatch_hardcover(book)
                except Exception as e:
                    web.logger.warning("Deferred Hardcover automatch failed for '%s': %s", abs_id, e, exc_info=True)
            storygraph = sync_clients.get("StoryGraph")
            if storygraph and storygraph.is_configured():
                try:
                    storygraph._automatch_storygraph(book)
                except Exception as e:
                    web.logger.warning("Deferred StoryGraph automatch failed for '%s': %s", abs_id, e, exc_info=True)
            goodreads = sync_clients.get("Goodreads")
            if goodreads and goodreads.is_configured():
                try:
                    goodreads._automatch(book)
                except Exception as e:
                    web.logger.warning("Deferred Goodreads automatch failed for '%s': %s", abs_id, e, exc_info=True)
        except Exception as e:
            web.logger.error("Deferred tracker automatch worker error: %s", e, exc_info=True)
        finally:
            web._TRACKER_AUTOMATCH_QUEUE.task_done()


def _visible_batch_items(items, token):
    """Expose which submitted batch item is being handed off to a worker."""
    web = _web_server()
    for idx, item in enumerate(items):
        with web._VISIBLE_BATCH_LOCK:
            if token in web._VISIBLE_BATCHES:
                web._VISIBLE_BATCHES[token]['current'] = idx
        yield item


def _spawn_user_background(fn, *args, label="background"):
    """Run `fn(*args)` on a daemon thread with the request's user context re-bound.

    Batch-match processing (artifact downloads, hash, transcript ingest, save,
    collection/shelf) is slow and N×, but none of it needs the response. We capture
    the active bundle + ambient user id/credentials in the request and re-bind them in
    the worker so `uc()` / `user_setting` / DB ownership resolve for the same user
    off-thread (a worker thread has no Flask request context). Errors are logged.

    In test mode (`create_app(test_container=...)`) it runs inline so integration
    tests observe the side effects right after the request — the request context is
    still active there, so `uc()` resolves normally without the re-bind.
    """
    web = _web_server()
    batch_token = None
    if label in web._VISIBLE_BATCH_LABELS and args and isinstance(args[0], list) and args[0]:
        owner = web.current_user()
        batch_token = web.uuid.uuid4().hex
        # Only retain display metadata, never file paths or service credentials.
        safe_items = [
            {key: item.get(key) for key in (
                'abs_id', 'abs_title', 'audio_title', 'title', 'readalong_epub_requested'
            )} for item in args[0]
        ]
        with web._VISIBLE_BATCH_LOCK:
            web._VISIBLE_BATCHES[batch_token] = {
                'user_id': owner.id if owner else None,
                'label': web._VISIBLE_BATCH_LABELS[label],
                'items': safe_items,
                'current': -1,
            }
        args = (_visible_batch_items(args[0], batch_token), *args[1:])

    def finish_visible_batch():
        if batch_token is not None:
            with web._VISIBLE_BATCH_LOCK:
                web._VISIBLE_BATCHES.pop(batch_token, None)

    if web._BACKGROUND_TASKS_SYNCHRONOUS:
        try:
            fn(*args)
        except Exception as e:
            web.logger.error("%s failed: %s", label, e, exc_info=True)
        finally:
            finish_visible_batch()
        return

    bundle = web.uc()
    try:
        user = web.current_user()
        user_id = user.id if user is not None else None
    except Exception:
        user_id = None
    if user_id is None:
        user_id = web.get_current_user_id()
    creds = web.get_current_user_credentials()

    def runner():
        tok_bundle = web._active_bundle.set(bundle)
        tok_uid = web.set_current_user_id(user_id)
        tok_creds = web.set_current_user_credentials(creds)
        try:
            fn(*args)
        except Exception as e:
            web.logger.error("%s failed: %s", label, e, exc_info=True)
        finally:
            web.reset_current_user_credentials(tok_creds)
            web.reset_current_user_id(tok_uid)
            web._active_bundle.reset(tok_bundle)
            finish_visible_batch()

    try:
        web.threading.Thread(target=runner, daemon=True, name=label).start()
    except Exception:
        finish_visible_batch()
        raise
