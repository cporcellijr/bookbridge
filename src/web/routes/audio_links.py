"""Routes for adding an audiobook to an existing mapping."""

import re
from importlib import import_module

from flask import jsonify, request
from flask_babel import gettext as _

from src.services.companion_audio import AUDIO_PROVIDERS
from src.web.audio_link_jobs import enqueue_lock

_WEB_MODULE = None


def _web_server():
    return _WEB_MODULE if _WEB_MODULE is not None else import_module("src.web_server")


def register(app, web_module):
    global _WEB_MODULE
    _WEB_MODULE = web_module
    app.add_url_rule('/api/companion-audio/<abs_id>', view_func=companion_audio,
                     methods=['GET', 'POST', 'DELETE'])
    app.add_url_rule('/api/existing-audio-match-targets', view_func=existing_audio_match_targets)


def companion_audio(abs_id):
    web = _web_server()
    source = request.args.get('provider')
    if request.method == 'POST':
        payload = request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}
        return match_existing_audio(web, abs_id, payload.get('provider'), payload.get('item_id'))
    return match_existing_audio(web, abs_id, source)


def match_existing_audio(web, abs_id, provider, item_id=None):
    book = web.database_service.get_book(abs_id)
    if not book:
        return jsonify(error=_("Book not found")), 404
    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else web.database_service._default_user_id()
    if user_id is None:
        return jsonify(error=_("No active BookBridge user")), 409
    primary = book.audio_source or 'ABS'
    if primary not in AUDIO_PROVIDERS or book.sync_mode != 'audiobook' or not book.ebook_filename:
        return jsonify(error=_("Link an EPUB and a primary audiobook first.")), 409
    secondary = provider or {'ABS': 'BookOrbit', 'BookOrbit': 'ABS'}.get(primary)
    if not isinstance(secondary, str) or secondary not in AUDIO_PROVIDERS or primary == secondary:
        return jsonify(error=_("Select another audio service.")), 400
    if request.method == 'DELETE':
        web.database_service.delete_companion_audio(user_id, abs_id, secondary)
        return jsonify(success=True)
    if not web._build_dashboard_integrations().get(AUDIO_PROVIDERS[secondary][0]):
        return jsonify(error=_("The audio service is not enabled for this account.")), 409
    if request.method == 'GET':
        query = request.args.get('q', '').strip() or book.abs_title
        linked = web.database_service.resolve_companion_audio(user_id, book, secondary)
        candidates = [dict(id=row.source_id, title=row.title, authors=row.authors)
                      for row in web.get_searchable_audiobooks(query) if row.source == secondary][:20]
        return jsonify(provider=secondary, provider_label=AUDIO_PROVIDERS[secondary][1], linked=linked, candidates=candidates)

    if not isinstance(item_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,255}', item_id):
        return jsonify(error=_("Select a valid audiobook.")), 400
    primary_id = web.database_service.primary_audio_item_id(user_id, book)
    if not primary_id:
        return jsonify(error=_("The primary audio source is not linked for this user.")), 409
    existing = web.database_service.get_book_by_audio_source(secondary, item_id)
    if secondary == 'ABS' and not existing:
        existing = web.database_service.get_book(item_id)
    if existing and existing.abs_id != abs_id:
        return jsonify(error=_("This audiobook already has its own mapping. Neither mapping was changed.")), 409
    linked = web.database_service.resolve_companion_audio(user_id, book, secondary)
    if linked == item_id:
        return jsonify(success=True, provider=secondary)
    if linked:
        return jsonify(error=_("Unlink the current audiobook for this service before replacing it.")), 409
    with enqueue_lock:
        job_id = web.database_service.enqueue_audio_link(user_id, abs_id, dict(
            provider=secondary, item_id=item_id, primary=primary, primary_id=primary_id,
        ))
    if job_id is None:
        return jsonify(error=_("Another audiobook link for this service is already queued.")), 409
    return jsonify(success=True, status='queued', job_id=job_id, provider=secondary), 202


def existing_audio_match_targets():
    web = _web_server()
    user = web.current_user()
    if user is None:
        return jsonify(error=_("Authentication required")), 401
    provider = request.args.get('provider')
    if provider not in AUDIO_PROVIDERS:
        return jsonify(error=_("Select an audio service")), 400
    query = request.args.get('q', '').strip().casefold()
    books = web.database_service.get_all_books(user_id=user.id)
    matches = [book for book in books if book.sync_mode == 'audiobook' and book.ebook_filename
               and (book.audio_source or 'ABS') in AUDIO_PROVIDERS.keys() - {provider}
               and query in (book.abs_title or '').casefold()]
    matches.sort(key=lambda book: (book.abs_title or '').casefold())
    return jsonify(candidates=[dict(id=book.abs_id, title=book.abs_title or book.ebook_filename,
                                    authors=book.ebook_filename) for book in matches[:40]])
