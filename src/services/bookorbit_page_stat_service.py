"""Relay persisted KOReader page events without changing their device identity."""

import hashlib
import json
import logging
import math
import time

from sqlalchemy import tuple_
from sqlalchemy.dialects.sqlite import insert

from src.db.models import BookOrbitPageStatExport, KOReaderPageStat
from src.services.bookorbit_session_service import BookOrbitSessionService
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class BookOrbitPageStatService:
    def __init__(self, database, client):
        self.database = database
        self.client = client
        identity = BookOrbitSessionService(database, client)
        self.user_id = identity.user_id
        self.scope = identity.scope

    @staticmethod
    def _event(row):
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

    def enqueue_upload(self, device_id, page_stats):
        """Queue only canonical stored rows, including a lost-ACK upload's retry.

        Never forward the request blindly: its foreign echoes may have been rejected
        by the statistics repository. No historic table scan or backfill is done.
        """
        if not device_id:
            logger.warning('BookOrbit page relay needs the original KOReader device_id; local statistics retained')
            return
        keys = set()
        for entry in page_stats:
            try:
                keys.add((str(entry.get('md5') or entry.get('book_md5') or '').strip(),
                          int(entry['page']), float(entry['start_time'])))
            except (KeyError, ValueError, TypeError):
                continue
        keys = list(keys)
        for offset in range(0, len(keys), 100):
            with self.database.get_session() as session:
                rows = session.query(KOReaderPageStat).filter(
                    KOReaderPageStat.user_id == (self.user_id or None),
                    KOReaderPageStat.device_id == device_id,
                    tuple_(KOReaderPageStat.md5, KOReaderPageStat.page, KOReaderPageStat.start_time).in_(keys[offset:offset + 100]),
                ).all()
                for row in rows:
                    event = self._event(row)
                    if event is None:
                        logger.warning('KOReader page row %s cannot be relayed losslessly; local data retained', row.id)
                        continue
                    identity = [self.scope, self.user_id, row.device_id, row.md5.lower(), row.page, int(row.start_time)]
                    event_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
                    payload = dict(deviceId=row.device_id, deviceModel=row.device or 'KOReader',
                                   hash=row.md5.lower(), event=event)
                    session.execute(insert(BookOrbitPageStatExport).values(
                        event_id=event_id, account_scope=self.scope, user_id=self.user_id,
                        page_stat_id=row.id, status='pending', payload_json=json.dumps(payload), created_at=time.time(),
                    ).on_conflict_do_nothing(index_elements=['event_id']))

    def flush(self):
        with self.database.get_session() as session:
            rows = session.query(BookOrbitPageStatExport).filter_by(
                account_scope=self.scope, user_id=self.user_id, status='pending',
            ).order_by(BookOrbitPageStatExport.last_attempt_at, BookOrbitPageStatExport.created_at,
                       BookOrbitPageStatExport.event_id).limit(500).all()
            pending = [(row.event_id, json.loads(row.payload_json)) for row in rows]
        if not pending:
            return
        creds = getattr(self.client, '_creds', None)
        username = str(resolve_setting(creds, 'BOOKORBIT_KOSYNC_USER', '') or '').strip()
        key = self.client.normalize_kosync_key(resolve_setting(creds, 'BOOKORBIT_KOSYNC_KEY', ''))
        owner = str(resolve_setting(creds, 'BOOKORBIT_KOSYNC_OWNER', '') or username).strip()
        account = str(self.client._get_username() or '').strip()
        if not username or not key or not account or owner.casefold() != account.casefold():
            logger.warning('BookOrbit page relay pending: KOReader credentials and matching account owner required for user %s', self.user_id)
            return
        # A native request has one device identity and at most 50 books / 500 events.
        devices = {}
        for event_id, payload in pending:
            devices.setdefault(payload['deviceId'], []).append((event_id, payload))
        for device_id, entries in devices.items():
            books, receipts = {}, {}
            for event_id, payload in entries:
                md5 = payload['hash']
                if md5 not in books and len(books) >= 50:
                    continue
                books.setdefault(md5, dict(hash=md5, events=[]))['events'].append(payload['event'])
                receipts.setdefault(md5, []).append(event_id)
            selected = [event_id for ids in receipts.values() for event_id in ids]
            with self.database.get_session() as session:
                session.query(BookOrbitPageStatExport).filter(
                    BookOrbitPageStatExport.account_scope == self.scope,
                    BookOrbitPageStatExport.user_id == self.user_id,
                    BookOrbitPageStatExport.event_id.in_(selected),
                ).update({'last_attempt_at': time.time()}, synchronize_session=False)
            response = self.client.koreader_upload_page_stats(
                username, key, device_id, entries[0][1]['deviceModel'], list(books.values()))
            if not isinstance(response, dict) or not isinstance(response.get('results'), list):
                continue
            unmatched = response.get('unmatched')
            if not isinstance(unmatched, list):
                continue
            acknowledged = []
            seen = set()
            for result in response['results']:
                if not isinstance(result, dict):
                    continue
                md5 = result.get('hash')
                if md5 not in receipts or md5 in unmatched or md5 in seen:
                    continue
                seen.add(md5)
                accepted, duplicates = result.get('accepted'), result.get('duplicates')
                if (type(accepted) is int and type(duplicates) is int and accepted >= 0 and duplicates >= 0
                        and accepted + duplicates == len(receipts[md5])):
                    acknowledged.extend(receipts[md5])
            if acknowledged:
                with self.database.get_session() as session:
                    session.query(BookOrbitPageStatExport).filter(
                        BookOrbitPageStatExport.account_scope == self.scope,
                        BookOrbitPageStatExport.user_id == self.user_id,
                        BookOrbitPageStatExport.event_id.in_(acknowledged),
                    ).update({'status': 'delivered'}, synchronize_session=False)
