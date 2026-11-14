import json
from collections import defaultdict
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo
from ..models import Book, ReadingSession, ReadingSessionBuffer
import logging

logger = logging.getLogger('src.db.database_service')

_LISTENING_FINISHED_PROGRESS = 0.99

_READING_SESSION_DESTINATIONS = {
    "grimmory": "grimmory_status",
    "bookorbit": "bookorbit_status",
}

class ReadingSessionsMixin:
    """Database operations for reading sessions."""

    def has_matching_reading_session(
        self,
        abs_id: str,
        session_type: str,
        start_time: float,
        end_time: float,
        duration_seconds: int,
        start_progress: float = None,
        end_progress: float = None,
        leader_client: str = None,
        user_id: int = None,
    ) -> bool:
        """Return whether an identical user-scoped reading session exists."""
        uid = self._resolve_uid(user_id)
        duration_seconds = min(int(duration_seconds), 14400)
        with self.get_session() as session:
            return session.query(ReadingSession.id).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.session_type == session_type,
                ReadingSession.start_time == float(start_time),
                ReadingSession.end_time == float(end_time),
                ReadingSession.duration_seconds == duration_seconds,
                ReadingSession.start_progress == start_progress,
                ReadingSession.end_progress == end_progress,
                ReadingSession.leader_client == leader_client,
                ReadingSession.user_id == uid,
            ).first() is not None

    def has_reading_history(self, abs_id: str, user_id: int = None) -> bool:
        """Whether the (ambient) user has any recorded session for abs_id."""
        uid = self._resolve_uid(user_id)
        with self.get_session() as session:
            return session.query(ReadingSession.id).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.user_id == uid,
            ).first() is not None

    @staticmethod
    def _close_reading_session(session, row: ReadingSessionBuffer, now: float) -> None:
        """Close the buffer and insert local history in the same transaction."""
        from sqlalchemy import func
        from src.services.reading_session_aggregator import MAX_SESSION_SECONDS

        duration = int(min(row.accumulated_seconds,
                           max(0, row.last_event_at - row.started_at), MAX_SESSION_SECONDS))
        row.closed_at = now
        if duration <= 0:
            row.grimmory_status = "disabled"
            row.bookorbit_status = "disabled"
            return
        book_title = session.query(
            func.coalesce(func.nullif(Book.abs_title, ''), func.nullif(Book.audio_title, ''))
        ).filter(Book.abs_id == row.abs_id).scalar()
        session.add(ReadingSession(
            abs_id=row.abs_id, session_type=row.session_type,
            start_time=row.last_event_at - duration, end_time=row.last_event_at,
            duration_seconds=duration, start_progress=row.start_progress,
            end_progress=row.end_progress, leader_client=row.leader_client,
            user_id=row.user_id or None, book_title=book_title,
        ))
        logger.info("Reading session closed: user=%s book='%s' type=%s estimated_seconds=%s",
                    row.user_id, row.abs_id, row.session_type, duration)

    def extend_reading_session(self, abs_id: str, session_type: str, leader_client: str,
                               now: float, previous_at: float | None, position_delta: float,
                               start_progress: float, end_progress: float, gap_seconds: float,
                               grimmory_book_id: int | None = None,
                               bookorbit_book_id: int | None = None,
                               bookorbit_candidate_ids: list | None = None,
                               end_location: str | None = None,
                               complete: bool = False, user_id: int | None = None) -> None:
        """Accumulate one observation; callers serialize writes with the sync lock."""
        from src.services.reading_session_aggregator import (
            MAX_SESSION_SECONDS, movement_seconds, unexplained_idle_seconds,
        )

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            row = session.query(ReadingSessionBuffer).filter_by(
                user_id=uid, abs_id=abs_id, session_type=session_type, closed_at=None,
            ).first()
            if row is not None and now <= row.last_event_at:
                return
            if row is not None:
                # A destination id only means "a different book" when both sides are
                # known. It is re-resolved on every observation and legitimately comes
                # back None while that client's book cache is cold, and reading None as
                # a change would split the session and strand its remainder undelivered.
                destination_changed = any(
                    stored is not None and incoming is not None and stored != incoming
                    for stored, incoming in (
                        (row.grimmory_book_id, grimmory_book_id),
                        (row.bookorbit_book_id, bookorbit_book_id),
                    )
                )
                idle = unexplained_idle_seconds(now - row.last_event_at, position_delta)
                if (
                    idle > gap_seconds
                    or now - row.started_at >= MAX_SESSION_SECONDS
                    or row.leader_client != leader_client
                    or destination_changed
                ):
                    previous_at = max(previous_at or row.last_event_at, row.last_event_at)
                    self._close_reading_session(session, row, now)
                    session.flush()  # release the partial unique index before inserting its successor
                    row = None
            if row is None:
                # Local history survives buffer cleanup and bounds the first interval
                # after completion, force-close, a restart, or an idle gap.
                previous = session.query(ReadingSession.end_time).filter_by(
                    abs_id=abs_id, session_type=session_type, user_id=uid or None,
                ).order_by(ReadingSession.end_time.desc()).first()
                if previous:
                    previous_at = max(previous_at or previous[0], previous[0])
                contribution = movement_seconds(position_delta, now, previous_at, gap_seconds)
                if contribution <= 0:
                    return
                row = ReadingSessionBuffer(
                    user_id=uid, abs_id=abs_id, session_type=session_type,
                    leader_client=leader_client, started_at=now - contribution,
                    last_event_at=now, accumulated_seconds=contribution,
                    start_progress=start_progress, end_progress=end_progress,
                    end_location=end_location,
                    grimmory_book_id=grimmory_book_id,
                    grimmory_status="pending" if grimmory_book_id is not None else "disabled",
                    bookorbit_book_id=bookorbit_book_id,
                    bookorbit_status="pending" if bookorbit_book_id is not None else "disabled",
                )
                if bookorbit_candidate_ids:
                    row.bookorbit_candidate_ids = json.dumps(bookorbit_candidate_ids)
                session.add(row)
            else:
                baseline = max(previous_at or row.last_event_at, row.last_event_at)
                row.accumulated_seconds += movement_seconds(position_delta, now, baseline, gap_seconds)
                row.last_event_at = now
                row.end_progress = end_progress
                row.end_location = end_location
                if grimmory_book_id is not None and row.grimmory_book_id is None:
                    row.grimmory_book_id = grimmory_book_id
                    if row.grimmory_status == "disabled":
                        row.grimmory_status = "pending"
                if bookorbit_book_id is not None and row.bookorbit_book_id is None:
                    row.bookorbit_book_id = bookorbit_book_id
                    if row.bookorbit_status == "disabled":
                        row.bookorbit_status = "pending"
                if bookorbit_candidate_ids:
                    row.bookorbit_candidate_ids = json.dumps(bookorbit_candidate_ids)
            if complete:
                self._close_reading_session(session, row, now)

    def close_reading_sessions(self, now: float, gap_seconds: float,
                               user_id: int | None = None, all_users: bool = False) -> None:
        """Close idle or over-age buffers, including users no longer eligible for sync.

        The idle timer is deliberately more patient than the gap the next
        observation is judged against, so a service that flushes progress
        infrequently gets the chance to prove the sitting never ended.
        """
        from src.services.reading_session_aggregator import (
            IDLE_CLOSE_PATIENCE, MAX_SESSION_SECONDS,
        )

        idle_limit = gap_seconds * IDLE_CLOSE_PATIENCE
        uid = None if all_users else (self._resolve_uid(user_id) or 0)
        with self.get_session() as session:
            query = session.query(ReadingSessionBuffer).filter(ReadingSessionBuffer.closed_at.is_(None))
            if not all_users:
                query = query.filter(ReadingSessionBuffer.user_id == uid)
            for row in query.all():
                if now - row.last_event_at > idle_limit or now - row.started_at >= MAX_SESSION_SECONDS:
                    self._close_reading_session(session, row, now)

    def get_pending_reading_sessions(self, user_id: int | None = None) -> list[ReadingSessionBuffer]:
        """Return at most 200 closed buffers with at least one destination still pending."""
        from sqlalchemy import or_

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            rows = session.query(ReadingSessionBuffer).filter(
                ReadingSessionBuffer.user_id == uid,
                ReadingSessionBuffer.closed_at.isnot(None),
                or_(
                    ReadingSessionBuffer.grimmory_status == "pending",
                    ReadingSessionBuffer.bookorbit_status == "pending",
                ),
            ).order_by(ReadingSessionBuffer.id).limit(200).all()
            for row in rows:
                session.expunge(row)
            return rows

    def mark_reading_session_delivered(self, session_id: int, destination: str, status: str,
                                       user_id: int | None = None) -> None:
        """Acknowledge success or an explicitly disabled destination within its owner scope."""
        if destination not in _READING_SESSION_DESTINATIONS:
            raise ValueError("Invalid reading session delivery destination")
        if status not in {"delivered", "disabled", "failed"}:
            raise ValueError("Invalid reading session delivery status")
        column = _READING_SESSION_DESTINATIONS[destination]
        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            session.query(ReadingSessionBuffer).filter_by(
                id=session_id, user_id=uid, **{column: "pending"},
            ).update({column: status}, synchronize_session=False)

    def record_reading_session_delivery_failure(self, session_id: int, now: float,
                                                user_id: int | None = None) -> bool:
        """Count one failed delivery pass; abandon the row once retries run out.

        Returns whether the remaining destinations were given up on, so a
        decommissioned service cannot pin the queue or grow the buffer forever.
        """
        from src.services.reading_session_aggregator import DELIVERY_RETRY_WINDOW_SECONDS

        uid = self._resolve_uid(user_id) or 0
        with self.get_session() as session:
            row = session.query(ReadingSessionBuffer).filter_by(
                id=session_id, user_id=uid,
            ).first()
            if row is None:
                return False
            row.delivery_attempts = (row.delivery_attempts or 0) + 1
            if (row.closed_at or now) > now - DELIVERY_RETRY_WINDOW_SECONDS:
                return False
            abandoned = [
                name for name, column in _READING_SESSION_DESTINATIONS.items()
                if getattr(row, column) == "pending"
            ]
            if not abandoned:
                return False
            for column in (_READING_SESSION_DESTINATIONS[name] for name in abandoned):
                setattr(row, column, "failed")
            logger.warning(
                "⚠️ Giving up on reading session delivery for '%s' after %s attempts: %s",
                row.abs_id, row.delivery_attempts, ", ".join(sorted(abandoned)),
            )
            return True

    def purge_delivered_reading_sessions(self, now: float) -> None:
        """Remove buffers after 30 days once every destination is terminal."""
        terminal = ("delivered", "disabled", "failed")
        with self.get_session() as session:
            session.query(ReadingSessionBuffer).filter(
                ReadingSessionBuffer.closed_at < now - 30 * 86400,
                ReadingSessionBuffer.grimmory_status.in_(terminal),
                ReadingSessionBuffer.bookorbit_status.in_(terminal),
            ).delete(synchronize_session=False)

    def record_reading_session(self, abs_id: str, session_type: str, start_time: float,
                               end_time: float, duration_seconds: int,
                               start_progress: float = None, end_progress: float = None,
                               leader_client: str = None, user_id: int = None) -> None:
        """Record a local reading session for dashboard stats.

        Callers must pre-compute duration_seconds (exact telemetry or heuristic).
        This method only persists and applies a safety cap.
        """
        from sqlalchemy import func

        try:
            if duration_seconds <= 0:
                return
            # Safety cap at 4 hours
            duration_seconds = min(duration_seconds, 14400)
            uid = self._resolve_uid(user_id)

            with self.get_session() as db_session:
                book_title = db_session.query(
                    func.coalesce(func.nullif(Book.abs_title, ''), func.nullif(Book.audio_title, ''))
                ).filter(Book.abs_id == abs_id).scalar()
                if not book_title:
                    # A removed book keeps the name its earlier sessions recorded.
                    book_title = db_session.query(ReadingSession.book_title).filter(
                        ReadingSession.abs_id == abs_id, func.nullif(ReadingSession.book_title, '').isnot(None),
                    ).order_by(ReadingSession.end_time.desc()).limit(1).scalar()
            session = ReadingSession(
                abs_id=abs_id,
                session_type=session_type,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration_seconds,
                start_progress=start_progress,
                end_progress=end_progress,
                leader_client=leader_client,
                user_id=uid,
                book_title=book_title,
            )
            with self.get_session() as db_session:
                db_session.add(session)
        except Exception as e:
            logger.debug(f"Failed to record reading session for '{abs_id}': {e}")

    def get_reading_stats(self, abs_id: str, user_id: int = None) -> Optional[dict]:
        """Get aggregated reading stats for one book."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            query = session.query(
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('listen_seconds'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type != 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('read_seconds'),
                func.count(ReadingSession.id).label('session_count'),
                func.coalesce(func.sum(ReadingSession.duration_seconds), 0).label('total_duration'),
                func.max(ReadingSession.end_time).label('last_session_time'),
                # See get_all_reading_stats for why bare leader column matches max(end_time) row
                ReadingSession.leader_client.label('last_leader'),
                ReadingSession.book_title.label('book_title'),
            ).filter(ReadingSession.abs_id == abs_id)
            uid = self._resolve_uid(user_id)
            if uid is not None:
                query = query.filter(ReadingSession.user_id == uid)
            row = query.first()

            if not row or row.session_count == 0:
                return None

            return {
                'listen_seconds': int(row.listen_seconds),
                'read_seconds': int(row.read_seconds),
                'session_count': int(row.session_count),
                'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                'last_session_time': row.last_session_time,
                'last_leader': row.last_leader,
                'book_title': row.book_title,
            }

    def get_all_reading_stats(self, user_id: int = None) -> dict:
        """Bulk fetch reading stats for all books. Returns dict[abs_id, stats_dict]."""
        from sqlalchemy import func, case

        with self.get_session() as session:
            query = session.query(
                ReadingSession.abs_id,
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('listen_seconds'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type == 'AUDIOBOOK', 1),
                    else_=0
                )), 0).label('listen_session_count'),
                func.coalesce(func.sum(case(
                    (ReadingSession.session_type != 'AUDIOBOOK', ReadingSession.duration_seconds),
                    else_=0
                )), 0).label('read_seconds'),
                func.count(ReadingSession.id).label('session_count'),
                func.coalesce(func.sum(ReadingSession.duration_seconds), 0).label('total_duration'),
                func.max(ReadingSession.end_time).label('last_session_time'),
                # last_leader = the leader of the MOST RECENT session (max end_time).
                # SQLite guarantees that with exactly one max()/min() aggregate and no
                # GROUP-BY ambiguity, bare (non-aggregated) columns take their value from
                # the same input row that produced that max — here, the max(end_time) row.
                # This gives us the latest leader per book with no extra query. Do not
                # "clean this up" into a plain column; it is load-bearing.
                ReadingSession.leader_client.label('last_leader'),
                ReadingSession.book_title.label('book_title'),
            )
            uid = self._resolve_uid(user_id)
            if uid is not None:
                query = query.filter(ReadingSession.user_id == uid)
            rows = query.group_by(ReadingSession.abs_id).all()

            result = {}
            for row in rows:
                if row.session_count == 0:
                    continue
                result[row.abs_id] = {
                    'listen_seconds': int(row.listen_seconds),
                    'listen_session_count': int(row.listen_session_count),
                    'read_seconds': int(row.read_seconds),
                    'session_count': int(row.session_count),
                    'avg_session_seconds': int(row.total_duration) // int(row.session_count),
                    'last_session_time': row.last_session_time,
                    'last_leader': row.last_leader,
                    'book_title': row.book_title,
                }
            return result

    def get_listening_days(self, tz_name: str) -> dict[str, int]:
        """Seconds of the ambient user's AUDIOBOOK sessions per local date (YYYY-MM-DD)."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession.start_time, ReadingSession.duration_seconds).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).all()
        days: dict[str, int] = defaultdict(int)
        for start_time, duration in rows:
            days[self._local_date_from_epoch(start_time, tz_name)] += int(max(duration or 0, 0))
        return dict(days)

    def get_listening_finished_books(self, tz_name: str) -> list[dict]:
        """Audiobooks the ambient user's own sessions reached the end of, newest first,
        each at its first finish, in the stats-page finished-book shape."""
        from sqlalchemy import func
        uid = self._resolve_uid(None)
        tz = ZoneInfo(tz_name)
        with self.get_session() as session:
            rows = session.query(
                ReadingSession.abs_id, func.min(ReadingSession.end_time),
                func.max(Book.abs_title), func.max(Book.audio_title), func.max(ReadingSession.book_title),
            ).outerjoin(Book, Book.abs_id == ReadingSession.abs_id).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
                ReadingSession.end_progress >= _LISTENING_FINISHED_PROGRESS,
            ).group_by(ReadingSession.abs_id).all()
        books = []
        for abs_id, finished_at, abs_title, audio_title, book_title in rows:
            finished_dt = datetime.fromtimestamp(float(finished_at), tz)
            books.append({
                "bookKey": f"abs:{abs_id}", "absId": abs_id,
                "title": abs_title or audio_title or book_title or "Unknown book", "author": None,
                "finishedAt": int(finished_at),
                "finishedDate": finished_dt.date().isoformat(), "month": finished_dt.month,
            })
        books.sort(key=lambda book: book["finishedAt"], reverse=True)
        return books

    def get_recent_listening_sessions(self, limit: int = 10) -> list[dict]:
        """Newest-first AUDIOBOOK sessions of the ambient user, in the stats-page session shape."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession, Book.abs_title, Book.audio_title).outerjoin(
                Book, Book.abs_id == ReadingSession.abs_id,
            ).filter(
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).order_by(ReadingSession.end_time.desc()).limit(limit).all()
            return [{
                "id": f"local-{rs.id}",
                "activityType": "listening",
                "absId": rs.abs_id,
                "title": abs_title or audio_title or rs.book_title or "Unknown title",
                "subtitle": None,
                "author": None,
                "durationSeconds": int(rs.duration_seconds or 0),
                "startedAt": int(rs.start_time),
                "endedAt": int(rs.end_time),
                "coverPath": None,
            } for rs, abs_title, audio_title in rows]

    def get_book_listening_detail(self, abs_id: str, session_limit: int = 40) -> Optional[dict]:
        """One book's AUDIOBOOK totals and newest-first sessions for the ambient user."""
        uid = self._resolve_uid(None)
        with self.get_session() as session:
            rows = session.query(ReadingSession).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.session_type == 'AUDIOBOOK', ReadingSession.user_id == uid,
            ).order_by(ReadingSession.end_time.desc()).all()
            if not rows:
                return None
            total_seconds = sum(int(rs.duration_seconds or 0) for rs in rows)
            latest_progress = next((rs.end_progress for rs in rows if rs.end_progress is not None), None)
            return {
                "totalSeconds": total_seconds,
                "sessionCount": len(rows),
                "avgSessionSeconds": total_seconds // len(rows),
                "progressPercent": round(min(latest_progress, 1.0) * 100, 1) if latest_progress is not None else None,
                "lastReadAt": int(rows[0].end_time),
                "bookTitle": rows[0].book_title,
                "sessions": [{
                    "startTime": int(rs.start_time),
                    "endTime": int(rs.end_time),
                    "durationSeconds": int(rs.duration_seconds or 0),
                    "startProgress": rs.start_progress,
                    "endProgress": rs.end_progress,
                } for rs in rows[:session_limit]],
            }

    def delete_recent_estimated_kosync_session(
        self,
        abs_id: str,
        start_time: float,
        end_time: float,
        start_progress: float = None,
        end_progress: float = None,
        time_window_seconds: int = 600,
        progress_tolerance: float = 0.02,
    ) -> bool:
        """Delete the closest overlapping estimated KoSync session for a book."""
        with self.get_session() as session:
            candidates = session.query(ReadingSession).filter(
                ReadingSession.abs_id == abs_id,
                ReadingSession.leader_client.like('KoSync:%'),
                ReadingSession.start_time >= (start_time - time_window_seconds),
                ReadingSession.start_time <= (start_time + time_window_seconds),
                ReadingSession.end_time >= (end_time - time_window_seconds),
                ReadingSession.end_time <= (end_time + time_window_seconds),
            ).all()

            best = None
            best_score = None
            for candidate in candidates:
                if start_progress is not None and candidate.start_progress is not None:
                    if abs(float(candidate.start_progress) - float(start_progress)) > progress_tolerance:
                        continue
                if end_progress is not None and candidate.end_progress is not None:
                    if abs(float(candidate.end_progress) - float(end_progress)) > progress_tolerance:
                        continue

                score = abs(float(candidate.start_time) - float(start_time)) + abs(float(candidate.end_time) - float(end_time))
                if best is None or score < best_score:
                    best = candidate
                    best_score = score

            if not best:
                return False

            session.delete(best)
            return True
