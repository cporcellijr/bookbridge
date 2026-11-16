"""Replay-safe aggregate export for non-KOReader sources. No native session import."""

import hashlib
import json
import logging
import math
import time

from sqlalchemy.dialects.sqlite import insert

from src.db.models import BookOrbitSessionEvent
from src.utils.user_config import resolve_setting
from src.utils.user_context import get_current_user_id
from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)


class BookOrbitSessionService:
    def __init__(self, database, client):
        self.database = database
        self.client = client
        self.user_id = database._resolve_uid(get_current_user_id()) or 0
        self.server = str(client._get_base_url()).rstrip('/')
        api_key = (client._get_api_key() if hasattr(client, '_get_api_key') else
                   resolve_setting(getattr(client, '_creds', None), 'BOOKORBIT_API_KEY', ''))
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
        return dict(start_time=start_ms / 1000, end_time=end_ms / 1000,
                    duration_seconds=int(min(active, wall_seconds, 14400)),
                    start_progress=progress[0], end_progress=progress[1], book_type=book_type)

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
            if row.status == 'delivered':
                return True
            if row.origin == 'koreader':
                # Old aggregate receipts are retained, never replayed as web reading.
                row.status = 'superseded'
                return True
            payload = json.loads(row.payload_json)
            book_id = row.bookorbit_book_id
        if payload.get('book_file_id') is None:
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
        if not self.client.create_reading_session(book_id=book_id, **payload, session_id=event_id):
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
