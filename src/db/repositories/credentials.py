from typing import Optional
from ..models import Setting, User, UserCredential
from src.utils import secret_store
import logging

logger = logging.getLogger('src.db.database_service')

class CredentialsMixin:
    """Database operations for credentials."""

    def get_user_credential(self, user_id: int, key: str, default: str = None) -> Optional[str]:
        with self.get_session() as session:
            cred = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            return self._read_value(key, cred.value) if cred else default

    def get_user_credentials(self, user_id: int) -> dict:
        with self.get_session() as session:
            creds = session.query(UserCredential).filter(
                UserCredential.user_id == user_id
            ).all()
            return {c.key: self._read_value(c.key, c.value) for c in creds}

    def _write_user_credentials(self, user_id: int, values: dict,
                                expected: Optional[dict] = None) -> bool:
        """The one credential writer: atomic, and a no-op once the account is gone.

        ``expected`` makes the write a compare-and-set: it is dropped unless every
        listed key currently holds the given value (missing counts as ''). A change
        of Readest identity clears that account's cached tokens in the same
        transaction. Returns False when nothing was written.
        """
        from sqlalchemy import exists, literal, select, text
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        updates = dict(values)
        written = 0
        with self.get_session() as session:
            if expected is not None or any(key in updates for key in self._READEST_IDENTITY_KEYS):
                # Reads that decide the write: hold the writer lock from read to write.
                session.execute(text("BEGIN IMMEDIATE"))
                watched = set(self._READEST_IDENTITY_KEYS) | set(expected or {})
                current = {
                    row.key: self._read_value(row.key, row.value)
                    for row in session.query(UserCredential).filter(
                        UserCredential.user_id == user_id, UserCredential.key.in_(watched)
                    )
                }
                if expected is not None and any(
                    (current.get(key) or '').strip() != (want or '').strip() for key, want in expected.items()
                ):
                    logger.info("🔐 Dropped credential write for user %s: identity changed meanwhile", user_id)
                    return False
                if any(updates[key] != current.get(key) for key in self._READEST_IDENTITY_KEYS if key in updates):
                    updates.update({key: '' for key in self._READEST_TOKEN_KEYS})
            for key, value in updates.items():
                stored = self._store_value(key, value)
                # One statement that writes only while the account exists: atomic against a
                # concurrent delete_user without serializing every credential writer.
                row = select(literal(user_id), literal(key), literal(stored)).where(
                    exists().where(User.id == user_id)
                )
                written += session.execute(
                    sqlite_insert(UserCredential).from_select(['user_id', 'key', 'value'], row)
                    .on_conflict_do_update(
                        index_elements=[UserCredential.user_id, UserCredential.key],
                        set_={"value": stored},
                    )
                ).rowcount
        if updates and not written:
            # An in-flight token refresh must not outlive its deleted account.
            logger.info("🔐 Ignored credential write for missing user %s", user_id)
            return False
        return True

    def set_user_credential(self, user_id: int, key: str, value: str) -> Optional[UserCredential]:
        """Store a per-user credential. Secret keys are encrypted at rest; the
        returned (detached) row carries the plaintext the caller passed in.
        Returns None when the account no longer exists."""
        plain = str(value) if value is not None else None
        if not self._write_user_credentials(user_id, {key: value}):
            return None
        with self.get_session() as session:
            cred = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            if cred is None:
                return None
            session.expunge(cred)
            cred.value = plain
            return cred

    def set_user_credentials(self, user_id: int, values: dict[str, str]) -> bool:
        """Save related credentials atomically, encrypting secret values at rest.

        Returns False (writing nothing) when the account no longer exists."""
        return self._write_user_credentials(user_id, values)

    def persist_readest_tokens(self, user_id: int, tokens: dict[str, str],
                               email: str, password: str) -> bool:
        """Store refreshed Readest tokens only if the account's Readest identity is
        still the one that obtained them; otherwise drop them."""
        return self._write_user_credentials(
            user_id, tokens, expected={'READEST_EMAIL': email, 'READEST_PASSWORD': password}
        )

    def encrypt_plaintext_secrets(self) -> int:
        """Wrap any secret still sitting in plaintext, in both credential stores.

        Idempotent and additive: already-wrapped values and non-secret keys are
        left alone, so this is safe to run on every boot. Returns the number of
        rows rewritten. No-op when encryption is unavailable.
        """
        if not secret_store.available():
            return 0

        rewritten = 0
        secret_keys = secret_store.secret_keys()
        with self.get_session() as session:
            for model in (Setting, UserCredential):
                rows = session.query(model).filter(model.key.in_(secret_keys)).all()
                for row in rows:
                    if not row.value or secret_store.is_encrypted(row.value):
                        continue
                    row.value = secret_store.encrypt(row.value)
                    rewritten += 1
        if rewritten:
            logger.info(f"🔐 Encrypted {rewritten} plaintext credential(s) at rest")
        return rewritten

    def delete_user_credential(self, user_id: int, key: str) -> bool:
        with self.get_session() as session:
            cred = session.query(UserCredential).filter(
                UserCredential.user_id == user_id, UserCredential.key == key
            ).first()
            if cred:
                session.delete(cred)
                return True
            return False
