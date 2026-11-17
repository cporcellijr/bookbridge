"""Relay persisted KOReader page events without changing their device identity."""

import logging
import time
from typing import Any

from src.services.bookorbit_session_service import BookOrbitSessionService
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class BookOrbitPageStatService:
    """Relay canonical KOReader page events to BookOrbit."""

    def __init__(self, database: Any, client: Any) -> None:
        """Create a relay service scoped to the current BookOrbit account."""
        self.database = database
        self.client = client
        identity = BookOrbitSessionService(database, client)
        self.user_id = identity.user_id
        self.scope = identity.scope

    def enqueue_upload(self, device_id: str, page_stats: list[dict]) -> None:
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
        self.database.enqueue_bookorbit_page_stat_exports(
            self.user_id, self.scope, device_id, keys,
        )

    def flush(self) -> None:
        """Send pending events in native request batches and acknowledge receipts."""
        identity = BookOrbitSessionService(self.database, self.client)
        if (identity.user_id, identity.scope) != (self.user_id, self.scope):
            logger.warning('BookOrbit page relay destination changed; pending events retained for user %s', self.user_id)
            return
        pending = self.database.get_pending_bookorbit_page_stat_exports(self.scope, self.user_id)
        if not pending:
            return
        creds = getattr(self.client, '_creds', None)
        username = str(resolve_setting(creds, 'BOOKORBIT_KOSYNC_USER', '') or '').strip()
        key = self.client.normalize_kosync_key(resolve_setting(creds, 'BOOKORBIT_KOSYNC_KEY', ''))
        owner = str(resolve_setting(creds, 'BOOKORBIT_KOSYNC_OWNER', '') or username).strip()
        account = str(self.client._get_username() or '').strip()
        conditions = get_persistent_condition_logger()
        condition_key = f'bookorbit-page-relay-credentials:{self.user_id}'
        if not username or not key or not account or owner.casefold() != account.casefold():
            conditions.warn(
                logger, condition_key,
                'BookOrbit page relay pending: KOReader credentials and matching account owner required for user %s',
                self.user_id,
            )
            return
        conditions.resolve(
            logger, condition_key,
            'BookOrbit page relay credentials recovered for user %s', self.user_id,
        )
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
            self.database.mark_bookorbit_page_stat_exports_attempted(
                self.scope, self.user_id, selected, time.time(),
            )
            identity = BookOrbitSessionService(self.database, self.client)
            if (identity.user_id, identity.scope) != (self.user_id, self.scope):
                logger.warning('BookOrbit page relay destination changed; pending events retained for user %s', self.user_id)
                return
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
                self.database.acknowledge_bookorbit_page_stat_exports(
                    self.scope, self.user_id, acknowledged,
                )
