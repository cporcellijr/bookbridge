"""Replay-safe aggregate export for non-KOReader sources. No native session import."""

import hashlib
import json
import logging
import math
import time
from typing import Any

from src.utils.user_config import resolve_setting
from src.utils.user_context import get_current_user_id
from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)


class BookOrbitSessionService:
    """Queue and deliver aggregate reading-session exports for BookOrbit."""

    def __init__(self, database: Any, client: Any) -> None:
        """Create a service scoped to the current user and BookOrbit identity."""
        self.database = database
        self.client = client
        self.user_id = database._resolve_uid(get_current_user_id()) or 0
        self.server = str(client._get_base_url()).rstrip('/')
        api_key = (client._get_api_key() if hasattr(client, '_get_api_key') else
                   resolve_setting(getattr(client, '_creds', None), 'BOOKORBIT_API_KEY', ''))
        identity = [self.server, client._get_username(), api_key]
        self.scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def destination_matches(self) -> bool:
        """Whether the client's current URL, username, API key and user match this instance."""
        current = type(self)(self.database, self.client)
        return (current.scope, current.user_id) == (self.scope, self.user_id)

    def _key(self, direction: str, book_id: int, identity: Any) -> str:
        value = json.dumps([self.scope, self.user_id, direction, int(book_id), identity],
                           sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def _store(
        self, book: Any, book_id: int, direction: str, identity: Any, payload: dict,
        status: str = 'pending', origin: str = 'bridge',
    ) -> str:
        key = self._key(direction, book_id, identity)
        self.database.store_bookorbit_session_event(
            key, self.scope, self.user_id, book.abs_id, int(book_id), direction, origin, status,
            json.dumps(payload, sort_keys=True, allow_nan=False), time.time(),
        )
        return key

    @staticmethod
    def payload(
        start_time: float, end_time: float, duration_seconds: float,
        start_progress: float | None, end_progress: float | None, book_type: str,
    ) -> dict:
        """Validate and format one aggregate session payload."""
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

    def enqueue(
        self, book: Any, book_id: int, payload: dict, identity: Any = None,
        origin: str = 'bridge',
    ) -> str:
        """Persist an idempotent pending export for a BookOrbit book."""
        payload = self._pin_ebook(book, book_id, payload)
        return self._store(book, book_id, 'export', identity or payload, payload, origin=origin)

    @staticmethod
    def _pin_ebook(book: Any, book_id: int, payload: dict) -> dict:
        file_id = getattr(book, 'ebook_source_file_id', None)
        if (file_id is not None and str(getattr(book, 'ebook_source_id', '')) == str(book_id)
                and str(payload['book_type']).lower() not in {'audio', 'audiobook'}):
            return dict(payload, book_file_id=int(file_id))
        return payload

    def exists(self, event_id: str) -> bool:
        """Whether the event ID has already been persisted."""
        return self.database.bookorbit_session_event_exists(event_id)

    def deliver(self, event_id: str) -> bool:
        """Deliver one pending aggregate session without crossing account scopes."""
        if not self.destination_matches():
            return False
        row = self.database.get_bookorbit_session_event(event_id, self.scope, self.user_id)
        if row['status'] == 'delivered':
            return True
        if row['origin'] == 'koreader':
            self.database.mark_bookorbit_session_event_status(
                event_id, self.scope, self.user_id, 'superseded',
            )
            # Old aggregate receipts are retained, never replayed as web reading.
            return True
        payload = json.loads(row['payload_json'])
        book_id = row['bookorbit_book_id']
        if payload.get('book_file_id') is None:
            kind = 'audiobook' if str(payload['book_type']).lower() in {'audio', 'audiobook'} else 'ebook'
            file_id = self.client._resolve_primary_file_id(book_id, kind)
            if file_id is None:
                file_id = self.client._resolve_primary_file_id(book_id, 'ebook')
            if file_id is None:
                return False
            if not self.destination_matches():
                return False
            if payload.get('book_file_id') is None:
                payload = json.loads(self.database.update_bookorbit_session_event_payload(
                    event_id, self.scope, self.user_id, file_id,
                ))
        if not self.destination_matches():
            return False
        if not self.client.create_reading_session(book_id=book_id, **payload, session_id=event_id):
            return False
        self.database.mark_bookorbit_session_event_status(
            event_id, self.scope, self.user_id, 'delivered',
        )
        return True

    def flush(self) -> None:
        """Deliver up to 100 pending exports for the current destination."""
        if not self.destination_matches():
            return
        keys = self.database.get_pending_bookorbit_session_event_ids(self.scope, self.user_id)
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
