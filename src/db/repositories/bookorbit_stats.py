"""Database operations for BookOrbit KOReader page-stat exports."""

import hashlib
import json
import logging
import math
import time

from sqlalchemy import tuple_
from sqlalchemy.dialects.sqlite import insert

from ..models import BookOrbitPageStatExport, BookOrbitSessionEvent, KOReaderPageStat

logger = logging.getLogger(__name__)


class BookOrbitStatsMixin:
    """Persist and retrieve BookOrbit page-stat relay events."""

    @staticmethod
    def _bookorbit_page_event(row: KOReaderPageStat) -> dict | None:
        if (not row.device_id or len(row.device_id) > 100 or len(row.md5) != 32
                or any(c not in '0123456789abcdefABCDEF' for c in row.md5)):
            return None
        values = (row.page, row.start_time, row.duration, row.total_pages)
        if any(v is None or not math.isfinite(float(v)) or int(v) != float(v) for v in values):
            return None
        page, start, duration, total = map(int, values)
        if page < 0 or start <= 0 or not 0 < duration <= 86400 or total <= 0:
            return None
        return dict(page=page, startTime=start, durationSeconds=duration, totalPages=total)

    def enqueue_bookorbit_page_stat_exports(
        self, user_id: int | None, scope: str, device_id: str, keys: list[tuple[str, int, float]],
    ) -> None:
        """Queue exports only for canonical page-stat rows, in batches of 100 keys."""
        for offset in range(0, len(keys), 100):
            with self.get_session() as session:
                rows = session.query(KOReaderPageStat).filter(
                    KOReaderPageStat.user_id == (user_id or None),
                    KOReaderPageStat.device_id == device_id,
                    tuple_(KOReaderPageStat.md5, KOReaderPageStat.page, KOReaderPageStat.start_time).in_(keys[offset:offset + 100]),
                ).all()
                for row in rows:
                    event = self._bookorbit_page_event(row)
                    if event is None:
                        logger.warning('KOReader page row %s cannot be relayed losslessly; local data retained', row.id)
                        continue
                    identity = [scope, user_id, row.device_id, row.md5.lower(), row.page, int(row.start_time)]
                    event_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
                    payload = dict(deviceId=row.device_id, deviceModel=row.device or 'KOReader',
                                   hash=row.md5.lower(), event=event)
                    session.execute(insert(BookOrbitPageStatExport).values(
                        event_id=event_id, account_scope=scope, user_id=user_id,
                        page_stat_id=row.id, status='pending', payload_json=json.dumps(payload), created_at=time.time(),
                    ).on_conflict_do_nothing(index_elements=['event_id']))

    def get_pending_bookorbit_page_stat_exports(
        self, scope: str, user_id: int | None,
    ) -> list[tuple[str, dict]]:
        """Return the oldest 500 pending exports for one account scope."""
        with self.get_session() as session:
            rows = session.query(BookOrbitPageStatExport).filter_by(
                account_scope=scope, user_id=user_id, status='pending',
            ).order_by(BookOrbitPageStatExport.last_attempt_at, BookOrbitPageStatExport.created_at,
                       BookOrbitPageStatExport.event_id).limit(500).all()
            return [(row.event_id, json.loads(row.payload_json)) for row in rows]

    def mark_bookorbit_page_stat_exports_attempted(
        self, scope: str, user_id: int | None, event_ids: list[str], attempted_at: float,
    ) -> None:
        """Record an attempt for selected pending export events."""
        with self.get_session() as session:
            session.query(BookOrbitPageStatExport).filter(
                BookOrbitPageStatExport.account_scope == scope,
                BookOrbitPageStatExport.user_id == user_id,
                BookOrbitPageStatExport.event_id.in_(event_ids),
            ).update({'last_attempt_at': attempted_at}, synchronize_session=False)

    def acknowledge_bookorbit_page_stat_exports(
        self, scope: str, user_id: int | None, event_ids: list[str],
    ) -> None:
        """Mark acknowledged export events as delivered."""
        with self.get_session() as session:
            session.query(BookOrbitPageStatExport).filter(
                BookOrbitPageStatExport.account_scope == scope,
                BookOrbitPageStatExport.user_id == user_id,
                BookOrbitPageStatExport.event_id.in_(event_ids),
            ).update({'status': 'delivered'}, synchronize_session=False)

    def store_bookorbit_session_event(
        self, event_id: str, scope: str, user_id: int, abs_id: str,
        bookorbit_book_id: int, direction: str, origin: str, status: str, payload_json: str,
        created_at: float,
    ) -> None:
        """Insert one idempotent aggregate session event."""
        with self.get_session() as session:
            session.execute(insert(BookOrbitSessionEvent).values(
                event_id=event_id, account_scope=scope, user_id=user_id, abs_id=abs_id,
                bookorbit_book_id=bookorbit_book_id, direction=direction, origin=origin,
                status=status, payload_json=payload_json, created_at=created_at,
            ).on_conflict_do_nothing(index_elements=['event_id']))

    def bookorbit_session_event_exists(self, event_id: str) -> bool:
        """Whether an aggregate session event with this id exists."""
        with self.get_session() as session:
            return session.get(BookOrbitSessionEvent, event_id) is not None

    def get_bookorbit_session_event(
        self, event_id: str, scope: str, user_id: int,
    ) -> dict:
        """Load one export event for its owning account."""
        with self.get_session() as session:
            row = session.query(BookOrbitSessionEvent).filter_by(
                event_id=event_id, account_scope=scope, user_id=user_id, direction='export',
            ).one()
            return dict(status=row.status, origin=row.origin,
                        payload_json=row.payload_json, bookorbit_book_id=row.bookorbit_book_id)

    def update_bookorbit_session_event_payload(
        self, event_id: str, scope: str, user_id: int, file_id: int,
    ) -> str:
        """Pin a resolved file id once and return the stored payload."""
        with self.get_session() as session:
            row = session.query(BookOrbitSessionEvent).filter_by(
                event_id=event_id, account_scope=scope, user_id=user_id, direction='export',
            ).one()
            payload = json.loads(row.payload_json)
            if payload.get('book_file_id') is None:
                payload['book_file_id'] = file_id
                row.payload_json = json.dumps(payload, sort_keys=True)
            return row.payload_json

    def mark_bookorbit_session_event_status(
        self, event_id: str, scope: str, user_id: int, status: str,
    ) -> None:
        """Set the status of one aggregate export for its owning account."""
        with self.get_session() as session:
            session.query(BookOrbitSessionEvent).filter_by(
                event_id=event_id, account_scope=scope, user_id=user_id, direction='export',
            ).update({'status': status})

    def get_pending_bookorbit_session_event_ids(self, scope: str, user_id: int) -> list[str]:
        """Return pending aggregate export IDs for one account."""
        with self.get_session() as session:
            return [key for key, in session.query(BookOrbitSessionEvent.event_id).filter_by(
                account_scope=scope, user_id=user_id, direction='export', status='pending',
            ).limit(100).all()]
