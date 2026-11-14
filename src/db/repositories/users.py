from typing import List, Optional
from ..models import (
    Book,
    State,
    KosyncDocument,
    ReadingSession,
    KOReaderBookStat,
    KOReaderPageStat,
    KoreaderAnnotation,
    KoreaderAnnotationDeviceState,
    User,
    UserBook,
    Base,
)
from src.utils.time_utils import utcnow
from ._common import _username_key
import logging

logger = logging.getLogger('src.db.database_service')

class UsersMixin:
    """Database operations for users."""

    def get_user(self, user_id: int) -> Optional[User]:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                session.expunge(user)
            return user

    @staticmethod
    def _find_user_by_name(session, username: str, exclude_id: Optional[int] = None) -> Optional[User]:
        """Match a username by NFC + casefold; the users table is tiny so this runs in Python."""
        key = _username_key(username)
        if not key:
            return None
        for user in session.query(User).order_by(User.id):
            if user.id != exclude_id and _username_key(user.username) == key:
                return user
        return None

    def get_user_by_username(self, username: str) -> Optional[User]:
        if not username:
            return None
        with self.get_session() as session:
            user = self._find_user_by_name(session, username)
            if user:
                session.expunge(user)
            return user

    def list_users(self) -> List[User]:
        with self.get_session() as session:
            users = session.query(User).order_by(User.id).all()
            for user in users:
                session.expunge(user)
            return users

    def count_users(self) -> int:
        with self.get_session() as session:
            return session.query(User).count()

    def create_user(self, username: str, password: str = None, role: str = 'user',
                    active: int = 1) -> User:
        """Create a user with an optional plaintext password (hashed here).

        Enforces Unicode case-insensitive username uniqueness (the DB unique
        index is case-sensitive). The duplicate check and insert share one
        BEGIN IMMEDIATE transaction so concurrent creators cannot both win.
        Raises ValueError on a clash.
        """
        from sqlalchemy import text
        from werkzeug.security import generate_password_hash
        username = (username or "").strip()
        password_hash = generate_password_hash(password) if password else None
        clash = False
        with self.get_session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if self._find_user_by_name(session, username) is not None:
                clash = True
            else:
                user = User(username=username, password_hash=password_hash,
                            role=role, active=active)
                session.add(user)
                session.flush()
                session.refresh(user)
                session.expunge(user)
        if clash:
            raise ValueError(f"Username '{username}' already exists")
        return user

    def set_user_password(self, user_id: int, password: str) -> bool:
        from werkzeug.security import generate_password_hash
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            user.password_hash = generate_password_hash(password) if password else None
            return True

    def set_user_language(self, user_id: int, language: Optional[str]) -> bool:
        """Set the account's UI language. Pass None/'' to clear the choice (the
        locale selector then falls back to Accept-Language, then English).
        Returns False when the account no longer exists."""
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            user.language = (language or "").strip() or None
            return True

    def update_user_account(self, user_id: int, *, password: Optional[str] = None,
                            role: Optional[str] = None, active: Optional[bool] = None) -> Optional[User]:
        """Update account fields atomically while retaining the primary and last admin."""
        from sqlalchemy import text
        from werkzeug.security import generate_password_hash

        if role is not None:
            role = role.strip().lower()
            if role not in ('admin', 'user'):
                raise ValueError(f"Invalid role: {role}")
        policy_error = None
        with self.get_session() as session:
            # Reserve the SQLite writer before reading policy state across processes.
            session.execute(text("BEGIN IMMEDIATE"))
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return None
            role_changed = role is not None and role != user.role
            if role_changed and role == 'user':
                primary = session.query(User).filter(User.role == 'admin').order_by(User.id).first()
                if primary and primary.id == user_id:
                    policy_error = "Can't demote the primary admin — the global service settings belong to that account"
            if role_changed and role == 'admin':
                primary = session.query(User).filter(User.role == 'admin').order_by(User.id).first()
                if primary and user_id < primary.id:
                    policy_error = "Can't promote this account to admin — it would become the primary admin and take over the global service settings"
            if not policy_error and user.role == 'admin' and user.active and (role == 'user' or active is False):
                if session.query(User).filter(User.role == 'admin', User.active == 1).count() <= 1:
                    policy_error = "Can't demote the last active admin" if role == 'user' else "Can't disable the last active admin"
            if not policy_error:
                if password is not None:
                    user.password_hash = generate_password_hash(password) if password else None
                if role is not None:
                    user.role = role
                if active is not None:
                    user.active = int(active)
                session.flush()
            session.expunge(user)
        if policy_error:
            raise ValueError(policy_error)
        if role_changed:
            self._default_uid = None
        return user

    def set_username(self, user_id: int, new_username: str) -> tuple:
        """Rename a user. Returns (ok, error_message). Enforces uniqueness
        (Unicode case-insensitive) and a non-empty name."""
        from sqlalchemy import text
        new_username = (new_username or "").strip()
        if not new_username:
            return False, "Username cannot be empty"
        with self.get_session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if self._find_user_by_name(session, new_username, exclude_id=user_id) is not None:
                return False, "That username is already taken"
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False, "User not found"
            user.username = new_username
            return True, None

    def delete_user(self, user_id: int) -> bool:
        """Delete scoped user data while retaining shared catalog, the primary admin and an active admin."""
        from sqlalchemy import text

        with self.get_session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            primary = session.query(User).filter(User.role == 'admin').order_by(User.id).first()
            blocked = None
            if primary and primary.id == user_id:
                blocked = "Can't delete the primary admin — the global service settings belong to that account"
            elif user.role == 'admin' and user.active and session.query(User).filter(
                User.role == 'admin', User.active == 1
            ).count() <= 1:
                blocked = "Can't delete the last active admin"
            if not blocked:
                # SQLite may not enforce foreign keys; remove scoped data explicitly.
                annotation_ids = session.query(KoreaderAnnotation.id).filter(KoreaderAnnotation.user_id == user_id)
                session.query(KoreaderAnnotationDeviceState).filter(
                    KoreaderAnnotationDeviceState.annotation_id.in_(annotation_ids)
                ).delete(synchronize_session=False)
                # Shared catalog rows keep the retired id: ids are never reissued,
                # while NULL would hand them to the default admin.
                shared = (Book.__tablename__, KosyncDocument.__tablename__)
                for table in reversed(Base.metadata.sorted_tables):
                    if 'user_id' in table.c and table.name not in shared:
                        session.execute(table.delete().where(table.c.user_id == user_id))
                session.query(KosyncDocument).filter(KosyncDocument.user_id == user_id).update({
                    KosyncDocument.percentage: 0, KosyncDocument.progress: None,
                    KosyncDocument.timestamp: None, KosyncDocument.device: None,
                    KosyncDocument.device_id: None,
                }, synchronize_session=False)
                session.delete(user)
        if blocked:
            raise ValueError(blocked)
        self._default_uid = None
        return True

    def touch_user_login(self, user_id: int) -> None:
        with self.get_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                user.last_login = utcnow()

    def verify_user_credentials(self, username: str, password: str) -> Optional[User]:
        """Return the active User if username+password match, else None."""
        from werkzeug.security import check_password_hash
        user = self.get_user_by_username(username)
        if not user or not user.active or not user.password_hash:
            return None
        if check_password_hash(user.password_hash, password or ""):
            return user
        return None

    def assign_orphan_rows_to_user(self, user_id: int) -> dict:
        """Backfill NULL user_id rows on per-user tables to `user_id`.

        Used once at multi-user bootstrap to hand the pre-existing single user's
        progress/stats to the default admin. Returns per-table update counts."""
        counts = {}
        with self.get_session() as session:
            for model in (Book, State, KosyncDocument, ReadingSession, KOReaderBookStat, KOReaderPageStat):
                updated = session.query(model).filter(
                    model.user_id.is_(None)
                ).update({model.user_id: user_id}, synchronize_session=False)
                counts[model.__tablename__] = updated

            # Visibility (dashboard/sync/manifest) keys off user_books links, not
            # Book.user_id. The d7f0a2c4e6b8 migration creates those links at schema
            # time, but on a fresh upgrade the admin is created AFTER migrations run,
            # so the books just assigned above would have NO link and the admin's
            # dashboard would be empty. Only seed links when this user has none yet
            # (the broken state) so established multi-user installs are untouched.
            session.flush()
            if session.query(UserBook).filter(UserBook.user_id == user_id).count() == 0:
                owned_ids = [r[0] for r in session.query(Book.abs_id).filter(Book.user_id == user_id).all()]
                for abs_id in owned_ids:
                    session.add(UserBook(user_id=user_id, abs_id=abs_id))
                counts['user_books'] = len(owned_ids)
        return counts
