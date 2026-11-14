from typing import List, Optional
from ..models import State
import logging

logger = logging.getLogger('src.db.database_service')

class StateMixin:
    """Database operations for state."""

    def get_state(self, abs_id: str, client_name: str, user_id: int = None) -> Optional[State]:
        """Get a specific state by book + client (+ user)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(
                State.abs_id == abs_id,
                State.client_name == client_name,
            )
            if uid is not None:
                query = query.filter(State.user_id == uid)
            state = query.first()
            if state:
                session.expunge(state)
            return state

    def get_states_for_book(self, abs_id: str, user_id: int = None) -> List[State]:
        """Get all states for a book (scoped to a user)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(State.abs_id == abs_id)
            if uid is not None:
                query = query.filter(State.user_id == uid)
            states = query.all()
            for state in states:
                session.expunge(state)
            return states

    def get_all_states(self, user_id: int = None, abs_ids=None) -> List[State]:
        """Get all states. When user_id is given, scope to that user; otherwise
        return every row (dashboard, until per-user scoping in the UI).
        ``abs_ids`` restricts the read to those books."""
        if user_id is None:
            logger.debug(
                "get_all_states called with user_id=None — returning unfiltered "
                "bulk data. Future callers should pass an explicit user_id."
            )
        wanted = None if abs_ids is None else list(abs_ids)
        with self.get_session() as session:
            query = session.query(State)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            if wanted is None:
                states = query.all()
            else:
                states = []
                for i in range(0, len(wanted), 500):  # stay under SQLite's variable limit
                    states.extend(query.filter(State.abs_id.in_(wanted[i:i + 500])).all())
            for state in states:
                session.expunge(state)
            return states

    def save_state(self, state: State) -> State:
        """Save or update a state model, keyed by (abs_id, client_name, user_id)."""
        if state.user_id is None:
            state.user_id = self._resolve_uid(None)
        with self.get_session() as session:
            existing = session.query(State).filter(
                State.abs_id == state.abs_id,
                State.client_name == state.client_name,
                State.user_id == state.user_id,
            ).first()
            self._mark_progress_written(session, state.abs_id)

            if existing:
                # Update existing state
                for attr in ['last_updated', 'percentage', 'timestamp', 'xpath', 'cfi',
                             'service_updated_at', 'status', 'locator_source', 'locator_json']:
                    if hasattr(state, attr):
                        setattr(existing, attr, getattr(state, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                # Create new state
                session.add(state)
                session.flush()
                session.refresh(state)
                session.expunge(state)
                return state

    def delete_states_for_book(self, abs_id: str, user_id: int = None) -> int:
        """Delete states for a book. Scoped to a user when user_id is given."""
        with self.get_session() as session:
            self._mark_progress_written(session, abs_id)
            query = session.query(State).filter(State.abs_id == abs_id)
            if user_id is not None:
                query = query.filter(State.user_id == user_id)
            count = query.count()
            query.delete()
            return count

    def delete_state(self, abs_id: str, client_name: str, user_id: int = None) -> bool:
        """Delete one book+client state (scoped to a user like get_state)."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            query = session.query(State).filter(
                State.abs_id == abs_id,
                State.client_name == client_name,
            )
            if uid is not None:
                query = query.filter(State.user_id == uid)
            return query.delete() > 0
