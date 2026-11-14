"""Diagnostics helpers helpers; extracted without behavior changes."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

def _run_diagnostics_send(
    force: bool = False,
    manual: bool = False,
    user_message: str = "",
) -> dict:
    """Build service flags and delegate to the diagnostics sender.

    Every flag is computed in its own try/except so a missing or broken
    provider never prevents the heartbeat from reaching the collector.
    """
    web = _web_server()
    def _flag(fn):
        try:
            return bool(fn())
        except Exception:
            return False

    flags = {
        'abs': _flag(lambda: web.container.abs_client().is_configured()),
        'kosync': web.env_truthy('KOSYNC_ENABLED'),
        'storyteller': _flag(lambda: web.container.storyteller_client().is_configured()),
        'booklore': _flag(lambda: web.container.booklore_client().is_configured()),
        'bookfusion': _flag(lambda: web.container.bookfusion_client().is_configured()),
        'book_orbit': _flag(lambda: web.container.bookorbit_client().is_configured()),
        'kavita': _flag(lambda: web.container.kavita_client().is_configured()),
        'cwa': _flag(lambda: web.container.cwa_client().is_configured()),
        'hardcover': _flag(lambda: web.container.hardcover_client().is_configured()),
        'storygraph': _flag(lambda: web.container.storygraph_client().is_configured()),
        'goodreads': _flag(lambda: web.container.goodreads_client().is_configured()),
        'slash_books': web.os.path.isdir(web.os.environ.get('EBOOK_IMPORT_DIR', '/books')),
    }
    try:
        total_books = len(web.database_service.get_books_by_status('active'))
    except Exception:
        total_books = None

    from src.services import diagnostics
    return diagnostics.maybe_send_diagnostics(
        web.database_service,
        service_flags=flags,
        total_books=total_books,
        force=force,
        manual=manual,
        user_message=user_message,
    )


def _diagnostics_receiver_context():
    """Validate settings and derive the receiver base URL for proxied requests.

    Returns ``(base_url, token, error_message)``.  ``base_url`` is
    ``scheme://netloc`` parsed from ``DIAGNOSTICS_ENDPOINT_URL`` (no
    attacker-controlled path component).  ``token`` is the raw ingest
    token.  When either setting is missing/empty *error_message* is a
    human-readable string and both URL and token are empty.
    """
    web = _web_server()
    endpoint = web.os.environ.get('DIAGNOSTICS_ENDPOINT_URL', '').strip()
    token = web.os.environ.get('DIAGNOSTICS_INGEST_TOKEN', '').strip()
    if not endpoint:
        return '', '', 'Diagnostics endpoint is not configured.'
    if not token:
        return '', '', 'Diagnostics ingest token is not configured.'
    parsed = web.urlparse(endpoint)
    base = (
        f'{parsed.scheme}://{parsed.netloc}'
        if parsed.scheme in ('http', 'https') and parsed.netloc else ''
    )
    if not base:
        return '', '', 'Diagnostics endpoint URL is invalid.'
    return base, token, ''

