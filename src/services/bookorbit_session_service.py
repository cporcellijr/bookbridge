"""Session-only replication. Never reads/writes a progress State or locator.

Independent KOReader uploaders do not share activity ids. In native-owner mode
BookOrbit owns those measurements; BridgeSync telemetry is retained as a receipt,
not counted a second time. ABS sessions still use the durable export outbox.
"""

import hashlib
import json
import logging
import math
import time

from sqlalchemy.dialects.sqlite import insert

from src.db.models import BookOrbitSessionEvent, ReadingSession
from src.utils.user_config import resolve_setting, user_preference_enabled
from src.utils.user_context import get_current_user_id, get_current_user_credentials
from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)


def native_sessions_enabled(database):
    # The legacy default-user maintenance cycle carries no ambient user context.
    # Honor that reader's preference there too, not just in per-user HTTP/cycles.
    if database is not None and get_current_user_id() is None and get_current_user_credentials() is None:
        uid = database._resolve_uid(None)
        if isinstance(uid, int) and uid > 0:
            value = database.get_user_credential(uid, 'BOOKORBIT_NATIVE_READING_SESSIONS')
            if value not in (None, ''):
                return str(value).strip().lower() in {'true', '1', 'yes', 'on'}
    return user_preference_enabled('BOOKORBIT_NATIVE_READING_SESSIONS', 'false', database)


class BookOrbitSessionService:
    def __init__(self, database, client):
        self.database = database
        self.client = client
        self.user_id = database._resolve_uid(get_current_user_id()) or 0
        self.server = str(client._get_base_url()).rstrip('/')
        api_key = (client._get_api_key() if hasattr(client, '_get_api_key') else
                   resolve_setting(getattr(client, '_creds', None), 'BOOKORBIT_API_KEY', ''))
        # Password rotation does not change the account. Never persist a password
        # fingerprint that could become an offline password-guessing oracle.
        identity = [self.server, client._get_username(), api_key]
        self.scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _key(self, direction, book_id, identity):
        value = json.dumps([self.scope, self.user_id, direction, int(book_id), identity],
                           sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def _store(self, book, book_id, direction, identity, payload, status='pending', origin='bridge'):
        key = self._key(direction, book_id, identity)
        with self.database.get_session() as session:
            session.execute(insert(BookOrbitSessionEvent).values(
                event_id=key, account_scope=self.scope, user_id=self.user_id, abs_id=book.abs_id,
                bookorbit_book_id=int(book_id), direction=direction, origin=origin, status=status,
                payload_json=json.dumps(payload, sort_keys=True, allow_nan=False), created_at=time.time(),
            ).on_conflict_do_nothing(index_elements=['event_id']))
        return key

    @staticmethod
    def payload(start_time, end_time, duration_seconds, start_progress, end_progress, book_type):
        start, end, active = float(start_time), float(end_time), float(duration_seconds)
        if not all(math.isfinite(number) for number in (start, end, active)) or end <= start or active <= 0:
            raise ValueError('Invalid reading session interval')
        start_ms, end_ms = math.floor(start * 1000), math.floor(end * 1000)
        wall_seconds = (end_ms - start_ms) // 1000
        progress = []
        for value in (start_progress, end_progress):
            number = float(value) if value is not None else None
            if number is not None and (not math.isfinite(number) or not 0 <= number <= 1):
                raise ValueError('Invalid reading session progress')
            progress.append(number)
        if int(min(active, wall_seconds, 14400)) <= 0:
            raise ValueError('Empty reading session')
        # BookOrbit's JS Date/JSON API has millisecond precision. Persist the
        # actual wire interval so its returned copy has an exact fingerprint.
        return dict(start_time=start_ms / 1000, end_time=end_ms / 1000,
                    duration_seconds=int(min(active, wall_seconds, 14400)),
                    start_progress=progress[0], end_progress=progress[1], book_type=book_type)

    def store_telemetry(self, book, book_id, payload):
        """Retain duplicate-channel telemetry without inflating measured totals."""
        return self._store(book, book_id, 'telemetry', payload, payload, 'native_owned')

    def record_device(self, book, book_id, payload, leader_client, native_owned):
        """Commit receipt, optional local measurement and export in ONE transaction."""
        payload = self._pin_ebook(book, book_id, payload)
        direction = 'telemetry' if native_owned else 'export'
        key = self._key(direction, book_id, payload)
        with self.database.get_session() as session:
            session.execute(insert(BookOrbitSessionEvent).values(
                event_id=key, account_scope=self.scope, user_id=self.user_id, abs_id=book.abs_id,
                bookorbit_book_id=int(book_id), direction=direction, origin='koreader',
                status='native_owned' if native_owned else 'pending',
                payload_json=json.dumps(payload, sort_keys=True, allow_nan=False), created_at=time.time(),
            ).on_conflict_do_nothing(index_elements=['event_id']))
            receipt = session.get(BookOrbitSessionEvent, key)
            if not native_owned and receipt.local_session_id is None:
                local = session.query(ReadingSession).filter_by(
                    abs_id=book.abs_id, session_type=payload['book_type'],
                    start_time=payload['start_time'], end_time=payload['end_time'],
                    duration_seconds=payload['duration_seconds'], start_progress=payload['start_progress'],
                    end_progress=payload['end_progress'], leader_client=leader_client,
                    user_id=self.user_id or None,
                ).first()
                if local is None:
                    local = ReadingSession(
                        abs_id=book.abs_id, session_type=payload['book_type'],
                        start_time=payload['start_time'], end_time=payload['end_time'],
                        duration_seconds=payload['duration_seconds'], start_progress=payload['start_progress'],
                        end_progress=payload['end_progress'], leader_client=leader_client,
                        user_id=self.user_id or None,
                        book_title=getattr(book, 'abs_title', None) or getattr(book, 'audio_title', None),
                    )
                    session.add(local)
                    session.flush()
                receipt.local_session_id = local.id
        return key

    def enqueue(self, book, book_id, payload, identity=None, origin='bridge'):
        payload = self._pin_ebook(book, book_id, payload)
        return self._store(book, book_id, 'export', identity or payload, payload, origin=origin)

    @staticmethod
    def _pin_ebook(book, book_id, payload):
        file_id = getattr(book, 'ebook_source_file_id', None)
        if (file_id is not None and str(getattr(book, 'ebook_source_id', '')) == str(book_id)
                and str(payload['book_type']).lower() not in {'audio', 'audiobook'}):
            return dict(payload, book_file_id=int(file_id))
        return payload

    def exists(self, event_id):
        with self.database.get_session() as session:
            return session.get(BookOrbitSessionEvent, event_id) is not None

    def deliver(self, event_id):
        with self.database.get_session() as session:
            row = session.query(BookOrbitSessionEvent).filter_by(
                event_id=event_id, account_scope=self.scope, user_id=self.user_id, direction='export').one()
            if row.status in {'delivered', 'native_owned'}:
                return True
            if row.origin == 'koreader' and native_sessions_enabled(self.database):
                row.status = 'native_owned'
                return True
            payload = json.loads(row.payload_json)
            book_id = row.bookorbit_book_id
        if payload.get('book_file_id') is None:
            # No network while holding a database transaction. Freeze the target
            # before the POST; concurrent/uncertain retries reuse that decision.
            kind = 'audiobook' if str(payload['book_type']).lower() in {'audio', 'audiobook'} else 'ebook'
            file_id = self.client._resolve_primary_file_id(book_id, kind)
            if file_id is None:
                file_id = self.client._resolve_primary_file_id(book_id, 'ebook')
            if file_id is None:
                return False
            with self.database.get_session() as session:
                row = session.get(BookOrbitSessionEvent, event_id)
                payload = json.loads(row.payload_json)
                if payload.get('book_file_id') is None:
                    payload['book_file_id'] = file_id
                    row.payload_json = json.dumps(payload, sort_keys=True)
        if not self.client.create_reading_session(
            book_id=book_id, **payload, session_id=event_id,
        ):
            return False
        with self.database.get_session() as session:
            session.query(BookOrbitSessionEvent).filter_by(
                event_id=event_id, user_id=self.user_id).update({'status': 'delivered'})
        return True

    def flush(self):
        with self.database.get_session() as session:
            keys = [key for key, in session.query(BookOrbitSessionEvent.event_id).filter_by(
                account_scope=self.scope, user_id=self.user_id, direction='export', status='pending').limit(100).all()]
        for key in keys:
            try:
                if self.deliver(key):
                    get_persistent_condition_logger().resolve(
                        logger, f'bookorbit-session:{self.user_id}:{key}', 'BookOrbit session delivery resolved')
                else:
                    get_persistent_condition_logger().warn(
                        logger, f'bookorbit-session:{self.user_id}:{key}',
                        'BookOrbit session delivery pending for user %s; will retry', self.user_id)
            except Exception:
                logger.warning('BookOrbit session remains pending: %s', key, exc_info=True)

    def import_book(self, book, book_id):
        """Import measured native sessions prospectively. No estimates or echo exports.

        Poll the complete post-activation window again so checkpoint extensions
        are upserted by remote id. Never touch pre-activation local history.
        """
        marker = self._store(book, book_id, 'cursor', 'activation', {}, 'active')
        with self.database.get_session() as session:
            since = session.get(BookOrbitSessionEvent, marker).created_at
            cursor = session.get(BookOrbitSessionEvent, marker)
            last_polled = json.loads(cursor.payload_json).get('last_polled', 0)
            if 0 <= time.time() - last_polled < 60:
                return
            cursor.payload_json = json.dumps({'last_polled': time.time()})
        rows = self.client.list_reading_sessions(book_id, since)
        with self.database.get_session() as session:
            session.get(BookOrbitSessionEvent, marker).status = 'active' if rows is not None else 'unavailable'
        if rows is None:
            logger.warning('BookOrbit native sessions unavailable for book %s; retaining receipts for retry', book_id)
            return
        exports = {}
        with self.database.get_session() as session:
            receipts = session.query(BookOrbitSessionEvent).filter_by(
                account_scope=self.scope, user_id=self.user_id, bookorbit_book_id=int(book_id),
                direction='export').filter(BookOrbitSessionEvent.status != 'native_owned').all()
            for receipt in receipts:
                sent = json.loads(receipt.payload_json)
                fingerprint = (sent.get('book_file_id'), round(sent['start_time'] * 1000),
                               round(sent['end_time'] * 1000), sent['duration_seconds'])
                exports.setdefault(fingerprint, []).append(sent)
        for remote in rows:
            try:
                self._import_session(book, book_id, remote, since, exports)
            except (TypeError, ValueError, KeyError):
                logger.warning('Ignoring invalid BookOrbit session for %s', book.abs_id, exc_info=True)

    def _import_session(self, book, book_id, remote, since, exports):
        if not isinstance(remote, dict):
            return
        start = self.client._parse_session_timestamp(remote.get('startedAt'))
        end = self.client._parse_session_timestamp(remote.get('endedAt'))
        if start is None or end is None or start < since or remote.get('id') is None:
            return
        payload = self.payload(start, end, remote['durationSeconds'],
                               None if remote.get('endProgress') is None or remote.get('progressDelta') is None
                               else (float(remote['endProgress']) - float(remote['progressDelta'])) / 100,
                               None if remote.get('endProgress') is None else float(remote['endProgress']) / 100,
                               'AUDIOBOOK' if remote.get('sessionType') == 'listen' else 'EBOOK')
        payload['duration_seconds'] = int(min(float(remote['durationSeconds']),
                                             (round(end * 1000) - round(start * 1000)) // 1000))
        payload.update(start_time=start, end_time=end)
        if isinstance(remote['id'], bool) or int(remote['id']) <= 0:
            raise ValueError('Invalid native session id')
        key = self._key('import', book_id, int(remote['id']))
        # BookOrbit's list currently omits the client sessionId. Match only our
        # exact stored POST, never a broad time/progress intersection.
        fingerprint = (remote.get('bookFileId'), round(start * 1000), round(end * 1000), payload['duration_seconds'])
        if remote.get('source') == 'web' and any(
                self._same_export(sent, payload) for sent in exports.get(fingerprint, ())):
            return
        with self.database.get_session() as session:
            session.execute(insert(BookOrbitSessionEvent).values(
                event_id=key, account_scope=self.scope, user_id=self.user_id, abs_id=book.abs_id,
                bookorbit_book_id=int(book_id), direction='import', origin='bookorbit', status='imported',
                payload_json=json.dumps(remote), created_at=time.time(),
            ).on_conflict_do_nothing(index_elements=['event_id']))
            receipt = session.get(BookOrbitSessionEvent, key)
            if receipt.abs_id != book.abs_id:
                return  # One provider activity must not be counted in two mappings.
            if receipt.status in {'local_removed', 'local_changed'}:
                return
            local = session.get(ReadingSession, receipt.local_session_id) if receipt.local_session_id else None
            if receipt.local_session_id is not None:
                previous = json.loads(receipt.payload_json)
                if local is None:
                    receipt.status = 'local_removed'
                    return  # Do not resurrect an explicitly removed local copy.
                if (local.abs_id != receipt.abs_id or local.user_id != (self.user_id or None)
                        or local.start_time != self.client._parse_session_timestamp(previous.get('startedAt'))
                        or local.end_time != self.client._parse_session_timestamp(previous.get('endedAt'))
                        or local.duration_seconds != int(previous['durationSeconds'])):
                    receipt.status = 'local_changed'
                    return  # Never overwrite a manually edited or reused SQLite row id.
            # Updates only affect this import's own row, never existing history.
            values = dict(abs_id=book.abs_id, session_type=payload['book_type'],
                          start_time=start, end_time=end, duration_seconds=payload['duration_seconds'],
                          start_progress=payload['start_progress'], end_progress=payload['end_progress'],
                          leader_client=('KoSync:BookOrbit' if remote.get('source') == 'koreader' else
                                         'BookOrbitAudio' if payload['book_type'] == 'AUDIOBOOK' else 'BookOrbit'),
                          user_id=self.user_id or None,
                          book_title=getattr(book, 'abs_title', None) or getattr(book, 'audio_title', None))
            if local is None:
                local = ReadingSession(**values)
                session.add(local)
                session.flush()
                receipt.local_session_id = local.id
            else:
                for field, value in values.items():
                    setattr(local, field, value)
            receipt.payload_json = json.dumps(remote)

    @staticmethod
    def _same_export(export, native):
        if ((str(export.get('book_type')).lower() in {'audio', 'audiobook'})
                != (native.get('book_type') == 'AUDIOBOOK')):
            return False
        if export['duration_seconds'] != native['duration_seconds']:
            return False
        if any(round(export[key] * 1000) != round(native[key] * 1000) for key in ('start_time', 'end_time')):
            return False
        for key in ('start_progress', 'end_progress'):
            left, right = export.get(key), native.get(key)
            if left is None or right is None:
                if left != right:
                    return False
            elif abs(float(left) - float(right)) > 0.000101:
                return False
        return True
