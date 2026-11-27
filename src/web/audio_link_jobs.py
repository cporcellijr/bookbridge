"""Drain persisted audio-link jobs without blocking the scheduler or web requests."""

import threading
import time

from flask_babel import gettext as _
from sqlalchemy.exc import IntegrityError

from src.services.companion_audio import AUDIO_PROVIDERS, CompanionAudioUnavailable
from src.utils.user_config import N_


_worker_lock = threading.Lock()
enqueue_lock = threading.Lock()


def _validate_target(db, job, inputs):
    uid = job.audio_link_user_id
    book = db.get_book(job.abs_id)
    if (not book or not db.is_user_linked(uid, job.abs_id) or
            book.sync_mode != 'audiobook' or not book.ebook_filename or
            (book.audio_source or 'ABS') != inputs['primary'] or
            db.primary_audio_item_id(uid, book) != inputs['primary_id']):
        raise CompanionAudioUnavailable(N_('The primary audiobook or book access changed. Submit the match again.'))
    existing = db.get_book_by_audio_source(inputs['provider'], inputs['item_id'])
    if inputs['provider'] == 'ABS' and not existing:
        existing = db.get_book(inputs['item_id'])
    if existing and existing.abs_id != job.abs_id:
        raise CompanionAudioUnavailable(N_('This audiobook already has its own mapping.'))
    linked = db.resolve_companion_audio(uid, book, inputs['provider'])
    if linked and linked != inputs['item_id']:
        raise CompanionAudioUnavailable(N_('Unlink the current audiobook for this service before replacing it.'))
    return linked == inputs['item_id']


def process_audio_link(web, job):
    db = web.database_service
    inputs = job.audio_link_inputs
    uid = job.audio_link_user_id
    error = None
    try:
        user = db.get_user(uid)
        if not user or not user.active:
            raise CompanionAudioUnavailable(N_('The requesting account is no longer active.'))
        if not _validate_target(db, job, inputs):
            clients = web.container.user_client_registry().get_clients(uid)
            if not web.verify_audio_pair(clients, (inputs['primary'], inputs['primary_id']),
                                         (inputs['provider'], inputs['item_id'])):
                raise CompanionAudioUnavailable(N_('The providers returned different audio bytes or track order.'))
            if not _validate_target(db, job, inputs):
                db.set_companion_audio(uid, job.abs_id, inputs['provider'], inputs['item_id'],
                                       inputs['primary_id'], primary_provider=inputs['primary'])
    except CompanionAudioUnavailable as exc:
        error = str(exc)
    except IntegrityError:
        error = N_('This audiobook is already linked to another book.')
    except Exception:
        web.logger.exception('Audio link job %s failed', job.id)
        error = N_('Could not verify the audio. Check the provider connections and try again.')
    db.update_job_by_id(job.id, stage='failed' if error else 'completed',
                        last_attempt=time.time(), last_error=error, progress=0.0 if error else 1.0)


def dispatch_audio_links(web):
    if not _worker_lock.acquire(blocking=False):
        return

    def drain():
        try:
            while job := web.database_service.next_audio_link_job():
                process_audio_link(web, job)
        except Exception:
            web.logger.exception('Audio-link queue worker stopped; the scheduler will retry')
        finally:
            _worker_lock.release()

    try:
        threading.Thread(target=drain, name='audio-link-queue', daemon=True).start()
    except Exception:
        _worker_lock.release()
        raise


def add_audio_link_queue_rows(snapshot, books, jobs):
    titles = {book.abs_id: book.abs_title or book.abs_id for book in books}
    for job in jobs:
        if job.abs_id not in titles:
            continue
        provider = (job.audio_link_inputs or {}).get('provider')
        section = {'queued': 'pending', 'running': 'running'}.get(job.stage, 'recent')
        snapshot[section].append(dict(
            title=titles[job.abs_id], operation=_('Link audio · %(provider)s', provider=AUDIO_PROVIDERS.get(provider, ('', ''))[1]),
            status=job.stage, timestamp=job.last_attempt, detail=_(job.last_error) if job.last_error else {
                'queued': _('Waiting for audio verification'), 'running': _('Verifying the audio'),
                'completed': _('Audiobook linked'),
            }.get(job.stage, ''), percent=100 if job.stage == 'completed' else None,
        ))
    snapshot['recent'].sort(key=lambda row: row['timestamp'] or 0, reverse=True)
    del snapshot['recent'][25:]
