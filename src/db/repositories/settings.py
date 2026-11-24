import json
from typing import Optional
from ..models import Setting
from src.utils import secret_store
import logging

logger = logging.getLogger('src.db.database_service')

_PENDING_SHELF_ADDS_KEY = "BOOKLORE_SHELF_PENDING_ADDS"

_SHELF_ASSIGNMENTS_KEY = "BOOKLORE_SHELF_ASSIGNMENTS"

class SettingsMixin:
    """Database operations for settings."""

    @staticmethod
    def _store_value(key: str, value) -> Optional[str]:
        """Coerce a value for storage, encrypting it when ``key`` is a secret."""
        stored = str(value) if value is not None else None
        if stored is not None and secret_store.is_secret_key(key):
            return secret_store.encrypt(stored)
        return stored

    @staticmethod
    def _read_value(key: str, value) -> Optional[str]:
        """Reverse of :meth:`_store_value`. Legacy plaintext passes through."""
        if secret_store.is_encrypted(value):
            return secret_store.decrypt(value, label=key)
        return value

    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get a setting value by key."""
        with self.get_session() as session:
            setting = session.query(Setting).filter(Setting.key == key).first()
            if setting:
                return self._read_value(key, setting.value)
            return default

    def set_setting(self, key: str, value: str) -> Setting:
        """Set a setting value. Secret keys are encrypted at rest; the returned
        (detached) row carries the plaintext the caller passed in."""
        plain = str(value) if value is not None else None
        stored_value = self._store_value(key, value)
        with self.get_session() as session:
            existing = session.query(Setting).filter(Setting.key == key).first()
            if existing:
                existing.value = stored_value
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                existing.value = plain
                return existing
            else:
                new_setting = Setting(key=key, value=stored_value)
                session.add(new_setting)
                session.flush()
                session.refresh(new_setting)
                session.expunge(new_setting)
                new_setting.value = plain
                return new_setting

    def get_all_settings(self) -> dict:
        """Get all settings as a dictionary."""
        with self.get_session() as session:
            settings = session.query(Setting).all()
            return {s.key: self._read_value(s.key, s.value) for s in settings}

    def get_json_setting(self, key: str, default=None):
        """Get a JSON setting value, returning default on missing or invalid JSON."""
        raw = self.get_setting(key)
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid JSON setting for '%s'", key, exc_info=True)
            return default

    def set_json_setting(self, key: str, value) -> Setting:
        """Persist a JSON-serializable setting value."""
        return self.set_setting(key, json.dumps(value))

    def delete_setting(self, key: str) -> bool:
        """Delete a setting by key."""
        with self.get_session() as session:
            setting = session.query(Setting).filter(Setting.key == key).first()
            if setting:
                session.delete(setting)
                return True
            return False

    def get_pending_shelf_adds(self) -> list[tuple[str, Optional[int]]]:
        """Grimmory shelf adds deferred until alignment, as (abs_id, user_id) pairs.

        ``user_id`` is the reader whose login and shelf the add belongs to; None
        is the global login.
        """
        return [
            (abs_id, user_id)
            for abs_id, user_id in self.get_json_setting(_PENDING_SHELF_ADDS_KEY, default=[])
        ]

    def add_pending_shelf_add(self, abs_id: str, user_id: Optional[int]) -> None:
        """Defer a book's Grimmory shelf add for ``user_id`` until it is aligned."""
        with self._pending_shelf_adds_lock:
            pending = self.get_pending_shelf_adds()
            if (abs_id, user_id) not in pending:
                self.set_json_setting(_PENDING_SHELF_ADDS_KEY, pending + [(abs_id, user_id)])

    def remove_pending_shelf_adds(
        self, abs_id: str, user_id: Optional[int] = None, all_users: bool = False
    ) -> None:
        """Forget a book's deferred Grimmory shelf add for one user, or for every user."""
        with self._pending_shelf_adds_lock:
            pending = self.get_pending_shelf_adds()
            kept = [
                entry for entry in pending
                if entry[0] != abs_id or not (all_users or entry[1] == user_id)
            ]
            if len(kept) != len(pending):
                self.set_json_setting(_PENDING_SHELF_ADDS_KEY, kept)

    def get_shelf_assignments(self, abs_id: Optional[str] = None) -> list[dict]:
        """Return recorded successful Grimmory shelf targets."""
        assignments = self.get_json_setting(_SHELF_ASSIGNMENTS_KEY, default=[])
        if not isinstance(assignments, list):
            return []
        valid = [entry for entry in assignments if isinstance(entry, dict)
                 and isinstance(entry.get("abs_id"), str)
                 and isinstance(entry.get("shelf_name"), str)
                 and (entry.get("reader_id") is None or isinstance(entry.get("reader_id"), int))
                 and (entry.get("target_user_id") is None or isinstance(entry.get("target_user_id"), int))]
        return [entry for entry in valid if abs_id is None or entry["abs_id"] == abs_id]

    def record_shelf_assignment(
        self, abs_id: str, reader_id: Optional[int], target_user_id: Optional[int],
        shelf_name: str, source_id: Optional[str] = None,
    ) -> None:
        """Remember where a deferred Grimmory shelf add actually succeeded."""
        entry = {
            "abs_id": abs_id, "reader_id": reader_id,
            "target_user_id": target_user_id, "shelf_name": shelf_name,
            "source_id": source_id,
        }
        with self._pending_shelf_adds_lock:
            assignments = self.get_shelf_assignments()
            if entry not in assignments:
                self.set_json_setting(_SHELF_ASSIGNMENTS_KEY, assignments + [entry])

    def remove_shelf_assignments(self, abs_id: str) -> None:
        """Forget recorded Grimmory targets after mapping cleanup succeeds."""
        with self._pending_shelf_adds_lock:
            assignments = self.get_shelf_assignments()
            kept = [entry for entry in assignments if entry["abs_id"] != abs_id]
            if len(kept) != len(assignments):
                self.set_json_setting(_SHELF_ASSIGNMENTS_KEY, kept)
