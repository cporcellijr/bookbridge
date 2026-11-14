import os
import time
from collections import defaultdict
from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)
from typing import Optional
from zoneinfo import ZoneInfo
from ..models import (
    Book,
    KosyncDocument,
    KOReaderBookStat,
    KOReaderPageStat,
    UserBook,
)
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class KOReaderStatsMixin:
    """Database operations for koreader stats."""

    @staticmethod
    def _normalize_koreader_device_key(device: str = None, device_id: str = None) -> str:
        return str(device_id or device or "").strip()

    def upsert_koreader_book_stats(
        self,
        device: str,
        device_id: str,
        books: list[dict],
        user_id: int = None,
    ) -> int:
        """Upsert KOReader book metadata rows for one device."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return 0
        uid = self._resolve_uid(user_id)

        rows = []
        now = utcnow()
        for book in books or []:
            md5 = str(book.get("md5") or book.get("book_md5") or "").strip()
            if not md5:
                continue

            rows.append({
                "md5": md5,
                "user_id": uid,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "ko_book_id": book.get("ko_book_id"),
                "title": str(book.get("title") or "").strip() or None,
                "authors": str(book.get("authors") or "").strip() or None,
                "pages": book.get("pages"),
                "total_read_pages": book.get("total_read_pages"),
                "total_read_time": book.get("total_read_time"),
                "last_updated": now,
            })

        if not rows:
            return 0

        with self.get_session() as session:
            stmt = sqlite_insert(KOReaderBookStat).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["md5", "user_id", "device_key"],
                set_={
                    "device": stmt.excluded.device,
                    "device_id": stmt.excluded.device_id,
                    "ko_book_id": stmt.excluded.ko_book_id,
                    "title": stmt.excluded.title,
                    "authors": stmt.excluded.authors,
                    "pages": stmt.excluded.pages,
                    "total_read_pages": stmt.excluded.total_read_pages,
                    "total_read_time": stmt.excluded.total_read_time,
                    "last_updated": now,
                },
            )
            session.execute(stmt)
        return len(rows)

    def bulk_insert_koreader_page_stats(
        self,
        device: str,
        device_id: str,
        page_stats: list[dict],
        user_id: int = None,
    ) -> dict:
        """Bulk insert KOReader page stats with replay-safe dedupe and cross-device echo suppression."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return {"accepted": 0, "duplicates": 0, "echoes": 0}
        uid = self._resolve_uid(user_id)

        rows = []
        now = utcnow()
        for entry in page_stats or []:
            md5 = str(entry.get("md5") or entry.get("book_md5") or "").strip()
            if not md5:
                continue

            try:
                page = int(entry.get("page"))
                start_time = float(entry.get("start_time"))
                duration = max(float(entry.get("duration") or 0), 0.0)
            except (TypeError, ValueError):
                continue

            if page < 0 or start_time <= 0:
                continue

            try:
                total_pages = int(entry.get("total_pages")) if entry.get("total_pages") is not None else None
            except (TypeError, ValueError):
                total_pages = None
            if total_pages is not None and total_pages <= 0:
                total_pages = None

            rows.append({
                "md5": md5,
                "user_id": uid,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "page": page,
                "start_time": start_time,
                "duration": duration,
                "total_pages": total_pages,
                "uploaded_at": now,
            })

        if not rows:
            return {"accepted": 0, "duplicates": 0, "echoes": 0}

        # Echo suppression: an event whose (md5, start_time, duration) already exists
        # under another device_key is a merged copy injected into this device's
        # statistics.sqlite by the plugin, not new reading on this device.
        # Run this read in its own short session and release it before opening the
        # write session below, so the INSERT holds the write lock for as little
        # time as possible under contention (see issue #315).
        batch_md5s = {row["md5"] for row in rows}
        with self.get_session() as read_session:
            foreign_query = read_session.query(
                KOReaderPageStat.md5,
                KOReaderPageStat.start_time,
                KOReaderPageStat.duration,
            ).filter(
                KOReaderPageStat.md5.in_(batch_md5s),
                KOReaderPageStat.device_key != device_key,
            )
            foreign = self._scope_koreader_user(foreign_query, KOReaderPageStat, uid).all()
            foreign_fingerprints = {
                (item.md5, float(item.start_time), float(item.duration)) for item in foreign
            }
        fresh_rows = [
            row for row in rows
            if (row["md5"], row["start_time"], row["duration"]) not in foreign_fingerprints
        ]
        echoes = len(rows) - len(fresh_rows)

        inserted = 0
        if fresh_rows:
            with self.get_session() as session:
                stmt = sqlite_insert(KOReaderPageStat).values(fresh_rows)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["md5", "user_id", "device_key", "page", "start_time"]
                )
                result = session.execute(stmt)
                inserted = max(int(result.rowcount or 0), 0)

        return {
            "accepted": inserted,
            "duplicates": max(len(fresh_rows) - inserted, 0),
            "echoes": echoes,
        }

    def get_merged_koreader_page_stats(
        self,
        exclude_device_key: str,
        md5s: Optional[set[str]] = None,
        since: Optional[float] = None,
        user_id: int = None,
        limit: int = 10000,
    ) -> dict:
        """Page-stat events from all devices except the requesting one, for cross-device merging.

        ``since`` filters on the bridge-side ``uploaded_at`` timestamp (epoch seconds) so
        late uploads of old reading events are never missed; the returned ``watermark``
        is the max ``uploaded_at`` seen and should be passed back as the next ``since``.
        Rows missing ``total_pages`` fall back to the uploading device's book-stats page
        count; rows with no usable total are skipped (KOReader's rescaling view divides
        by total_pages).
        """
        exclude_device_key = str(exclude_device_key or "").strip()
        if not exclude_device_key:
            return {"page_stats": [], "watermark": since}
        uid = self._resolve_uid(user_id)
        limit = max(min(int(limit or 10000), 10000), 1)

        with self.get_session() as session:
            query = session.query(KOReaderPageStat).filter(
                KOReaderPageStat.device_key != exclude_device_key
            )
            query = self._scope_koreader_user(query, KOReaderPageStat, uid)
            if md5s:
                query = query.filter(KOReaderPageStat.md5.in_(md5s))
            if since is not None:
                query = query.filter(
                    KOReaderPageStat.uploaded_at >= datetime.utcfromtimestamp(float(since))
                )
            rows = query.order_by(KOReaderPageStat.uploaded_at.asc(), KOReaderPageStat.id.asc()).limit(limit + 1).all()
            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]
            if not rows:
                return {"page_stats": [], "watermark": since, "truncated": False}

            fallback_keys = {(row.md5, row.device_key) for row in rows if not row.total_pages}
            fallback_pages: dict[tuple[str, str], int] = {}
            if fallback_keys:
                meta_query = session.query(
                    KOReaderBookStat.md5,
                    KOReaderBookStat.device_key,
                    KOReaderBookStat.pages,
                ).filter(KOReaderBookStat.md5.in_({md5 for md5, _ in fallback_keys}))
                meta_rows = self._scope_koreader_user(meta_query, KOReaderBookStat, uid).all()
                for meta in meta_rows:
                    if meta.pages and int(meta.pages) > 0:
                        fallback_pages[(meta.md5, meta.device_key)] = int(meta.pages)

            watermark = since
            results = []
            for row in rows:
                if row.uploaded_at is not None:
                    uploaded_epoch = row.uploaded_at.replace(tzinfo=timezone.utc).timestamp()
                    if watermark is None or uploaded_epoch > watermark:
                        watermark = uploaded_epoch
                total_pages = int(row.total_pages) if row.total_pages else fallback_pages.get((row.md5, row.device_key))
                if not total_pages or total_pages <= 0:
                    continue
                results.append({
                    "md5": row.md5,
                    "page": int(row.page),
                    "start_time": float(row.start_time),
                    "duration": float(row.duration or 0),
                    "total_pages": total_pages,
                })
            return {"page_stats": results, "watermark": watermark, "truncated": truncated}

    def get_merged_koreader_book_meta(
        self,
        exclude_device_key: str,
        md5s: set[str],
        user_id: int = None,
    ) -> list[dict]:
        """Canonical book metadata (md5, title, authors, pages) for the given md5s.

        Drawn from other devices' uploaded book stats so a device that never opened a
        book can create its local ``book`` row before merging foreign page events. One row
        per md5, preferring the largest ``pages`` (then most recent ``last_updated``) so
        KOReader's page-stat rescaling stays sane. md5s with no usable metadata row are
        omitted — the plugin can't build a meaningful entry from them.
        """
        exclude_device_key = str(exclude_device_key or "").strip()
        md5s = {str(m).strip() for m in (md5s or set()) if str(m).strip()}
        if not md5s:
            return []
        uid = self._resolve_uid(user_id)

        with self.get_session() as session:
            query = session.query(KOReaderBookStat).filter(KOReaderBookStat.md5.in_(md5s))
            query = self._scope_koreader_user(query, KOReaderBookStat, uid)
            if exclude_device_key:
                query = query.filter(KOReaderBookStat.device_key != exclude_device_key)
            rows = query.all()

            best: dict[str, KOReaderBookStat] = {}
            for row in rows:
                current = best.get(row.md5)
                if current is None or self._koreader_book_meta_rank(row) > self._koreader_book_meta_rank(current):
                    best[row.md5] = row

            return [
                {
                    "md5": row.md5,
                    "title": row.title or "",
                    "authors": row.authors or "",
                    "pages": int(row.pages) if row.pages else 0,
                }
                for row in best.values()
            ]

    @staticmethod
    def _koreader_book_meta_rank(row) -> tuple[int, float]:
        """Sort key for picking the canonical book-stats row: most pages, then most recent."""
        pages = int(row.pages) if row.pages else 0
        last_updated = row.last_updated.timestamp() if row.last_updated else 0.0
        return (pages, last_updated)

    @staticmethod
    def _local_date_from_epoch(timestamp: float, tz_name: str) -> str:
        return datetime.fromtimestamp(float(timestamp), ZoneInfo(tz_name)).date().isoformat()

    @staticmethod
    def _date_range(start_date, end_date):
        days = []
        cursor = start_date
        while cursor <= end_date:
            days.append(cursor)
            cursor += timedelta(days=1)
        return days

    @staticmethod
    def _calculate_streak(activity_dates: set, reference_date) -> int:
        streak = 0
        cursor = reference_date
        while cursor in activity_dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def _get_koreader_book_links(self, session) -> dict:
        links = {}
        uid = self._resolve_uid(None)

        linked_docs_query = session.query(KosyncDocument).filter(KosyncDocument.linked_abs_id.isnot(None))
        if uid is not None:
            linked_docs_query = linked_docs_query.filter(
                (KosyncDocument.user_id == uid) | (KosyncDocument.user_id.is_(None))
            )
        linked_docs = linked_docs_query.all()
        if linked_docs:
            linked_abs_ids = {doc.linked_abs_id for doc in linked_docs if doc.linked_abs_id}
            books_query = session.query(Book).filter(Book.abs_id.in_(linked_abs_ids))
            if uid is not None:
                books_query = books_query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(UserBook.user_id == uid)
            books = books_query.all()
            books_by_id = {book.abs_id: book for book in books}
            for doc in linked_docs:
                book = books_by_id.get(doc.linked_abs_id)
                if book and doc.document_hash:
                    links.setdefault(str(doc.document_hash), book)

        direct_books_query = session.query(Book).filter(Book.kosync_doc_id.isnot(None))
        if uid is not None:
            direct_books_query = direct_books_query.join(UserBook, UserBook.abs_id == Book.abs_id).filter(UserBook.user_id == uid)
        direct_books = direct_books_query.all()
        for book in direct_books:
            if book.kosync_doc_id:
                links.setdefault(str(book.kosync_doc_id), book)

        return links

    def _get_all_koreader_active_md5s(self, session) -> set[str]:
        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.md5)
        rows = self._scope_koreader_user(query, KOReaderPageStat, uid).distinct().all()
        return {
            str(row[0]).strip()
            for row in rows
            if row and row[0] and str(row[0]).strip()
        }

    def _get_latest_koreader_book_metadata(self, session, md5s: set[str]) -> dict:
        if not md5s:
            return {}

        uid = self._resolve_uid(None)
        query = self._scope_koreader_user(
            session.query(KOReaderBookStat).filter(KOReaderBookStat.md5.in_(md5s)),
            KOReaderBookStat,
            uid,
        )
        rows = query.order_by(
            KOReaderBookStat.last_updated.desc(),
            KOReaderBookStat.id.desc(),
        ).all()
        metadata = {}
        for row in rows:
            metadata.setdefault(row.md5, row)
        return metadata

    def _build_koreader_book_contexts(self, session, md5s: set[str]) -> dict:
        if not md5s:
            return {}

        book_links = self._get_koreader_book_links(session)
        metadata_by_md5 = self._get_latest_koreader_book_metadata(session, md5s)
        contexts = {}

        for md5 in md5s:
            book = book_links.get(md5)
            meta = metadata_by_md5.get(md5)
            abs_id = getattr(book, "abs_id", None)
            is_linked = bool(abs_id)
            contexts[md5] = {
                "md5": md5,
                "absId": abs_id,
                "bookKey": f"abs:{abs_id}" if abs_id else f"koreader:{md5}",
                "isLinked": is_linked,
                "title": getattr(book, "abs_title", None) or getattr(meta, "title", None) or "Unknown book",
                "author": getattr(book, "abs_author", None) or getattr(meta, "authors", None),
            }

        return contexts

    def _build_koreader_daily_totals(
        self,
        session,
        md5s: set[str],
        tz_name: str,
        start_date=None,
        end_date=None,
    ) -> list[dict]:
        if not md5s:
            return []

        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
        query = self._scope_koreader_user(query, KOReaderPageStat, uid).filter(KOReaderPageStat.md5.in_(md5s))
        if start_date is not None:
            start_epoch = datetime.combine(
                start_date,
                datetime.min.time(),
                tzinfo=ZoneInfo(tz_name),
            ).timestamp()
            query = query.filter(KOReaderPageStat.start_time >= start_epoch)
        if end_date is not None:
            next_day = end_date + timedelta(days=1)
            end_epoch = datetime.combine(
                next_day,
                datetime.min.time(),
                tzinfo=ZoneInfo(tz_name),
            ).timestamp()
            query = query.filter(KOReaderPageStat.start_time < end_epoch)

        buckets = defaultdict(lambda: {"seconds": 0, "pages": 0})
        for row in query.all():
            date_key = self._local_date_from_epoch(row.start_time, tz_name)
            buckets[date_key]["seconds"] += int(max(row.duration or 0, 0))
            buckets[date_key]["pages"] += 1

        if start_date is None or end_date is None:
            return [
                {"date": date_key, "seconds": values["seconds"], "pages": values["pages"]}
                for date_key, values in sorted(buckets.items())
            ]

        return [
            {
                "date": day.isoformat(),
                "seconds": buckets[day.isoformat()]["seconds"],
                "pages": buckets[day.isoformat()]["pages"],
            }
            for day in self._date_range(start_date, end_date)
        ]

    def _get_koreader_activity_dates(self, session, md5s: set[str], tz_name: str) -> set:
        if not md5s:
            return set()

        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.start_time).filter(KOReaderPageStat.md5.in_(md5s))
        rows = self._scope_koreader_user(query, KOReaderPageStat, uid).all()
        return {
            datetime.fromisoformat(self._local_date_from_epoch(row.start_time, tz_name)).date()
            for row in rows
        }

    def get_koreader_dashboard_summary(self, tz_name: str) -> Optional[dict]:
        """Get high-level KOReader reading stats for the dashboard."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return None

            contexts = self._build_koreader_book_contexts(session, md5s)

            from sqlalchemy import func

            uid = self._resolve_uid(None)
            total_seconds = int(
                self._scope_koreader_user(
                    session.query(func.coalesce(func.sum(KOReaderPageStat.duration), 0)),
                    KOReaderPageStat,
                    uid,
                )
                .filter(KOReaderPageStat.md5.in_(md5s))
                .scalar()
                or 0
            )
            pages_read = self._koreader_pages_read(session, md5s)
            books_tracked = int(
                self._scope_koreader_user(session.query(KOReaderPageStat.md5), KOReaderPageStat, uid)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .distinct()
                .count()
            )

            now_local = datetime.now(ZoneInfo(tz_name)).date()
            week_start = now_local - timedelta(days=6)
            daily = self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=week_start,
                end_date=now_local,
            )
            activity_dates = self._get_koreader_activity_dates(session, md5s, tz_name)
            if not activity_dates:
                return None

            week_total = sum(day["seconds"] for day in daily)
            best_day = max(daily, key=lambda day: day["seconds"], default=None)
            linked_book_ids = sorted({
                context["absId"]
                for context in contexts.values()
                if context.get("absId")
            })
            tracked_book_keys = sorted({
                context["bookKey"]
                for context in contexts.values()
                if context.get("bookKey")
            })
            linked_books_tracked = sum(1 for context in contexts.values() if context.get("isLinked"))
            books_tracked = len(contexts)

            return {
                "booksTracked": books_tracked,
                "linkedBooksTracked": linked_books_tracked,
                "unlinkedBooksTracked": max(books_tracked - linked_books_tracked, 0),
                "daysRead": len(activity_dates),
                "totalSeconds": total_seconds,
                "pagesRead": pages_read,
                "pagesPerHour": round(pages_read / (total_seconds / 3600), 1) if total_seconds > 0 else 0,
                "secondsPerPage": int(total_seconds / pages_read) if pages_read > 0 else 0,
                "trackedBookIds": linked_book_ids,
                "trackedBookKeys": tracked_book_keys,
                "weekTotalSeconds": week_total,
                "dailyAverageSeconds": int(week_total / max(len(daily), 1)),
                "bestDay": best_day,
                "currentStreakDays": self._calculate_streak(activity_dates, now_local),
            }

    def get_koreader_daily_totals(self, days: int, tz_name: str) -> list[dict]:
        """Get recent KOReader daily totals."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            end_date = datetime.now(ZoneInfo(tz_name)).date()
            start_date = end_date - timedelta(days=max(int(days or 1) - 1, 0))
            return self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=start_date,
                end_date=end_date,
            )

    def get_koreader_activity_dates(self, tz_name: str) -> list[str]:
        """Get all KOReader activity dates in the configured timezone."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            dates = self._get_koreader_activity_dates(session, md5s, tz_name)
            return [day.isoformat() for day in sorted(dates)]

    def get_koreader_heatmap(self, start_date: date, end_date: date, tz_name: str) -> list[dict]:
        """Get KOReader daily totals for each date from start_date through end_date."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            return self._build_koreader_daily_totals(
                session,
                md5s,
                tz_name,
                start_date=start_date,
                end_date=end_date,
            )

    def get_koreader_books_for_date(self, date_str: str, tz_name: str) -> dict:
        """Get KOReader books with activity for one local date."""
        target_date = datetime.fromisoformat(str(date_str)).date()
        tz = ZoneInfo(tz_name)
        start_epoch = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).timestamp()
        end_epoch = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=tz).timestamp()

        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {
                    "date": target_date.isoformat(),
                    "totalSeconds": 0,
                    "totalPages": 0,
                    "totalBooks": 0,
                    "books": [],
                }

            contexts = self._build_koreader_book_contexts(session, md5s)
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

            if not rows:
                return {
                    "date": target_date.isoformat(),
                    "totalSeconds": 0,
                    "totalPages": 0,
                    "totalBooks": 0,
                    "books": [],
                }

            grouped = {}
            session_gap_seconds = self._session_gap_seconds()

            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue

                entry = grouped.setdefault(context["bookKey"], {
                    "bookKey": context["bookKey"],
                    "md5": row.md5,
                    "absId": context["absId"],
                    "isLinked": context["isLinked"],
                    "title": context["title"],
                    "author": context["author"],
                    "totalSeconds": 0,
                    "pagesRead": 0,
                    "sessionCount": 0,
                    "firstStartedAt": None,
                    "lastEndedAt": None,
                    "_session_state": {},
                })

                duration = int(max(row.duration or 0, 0))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                entry["totalSeconds"] += duration
                entry["pagesRead"] += 1
                entry["firstStartedAt"] = int(row.start_time) if entry["firstStartedAt"] is None else min(entry["firstStartedAt"], int(row.start_time))
                entry["lastEndedAt"] = int(event_end) if entry["lastEndedAt"] is None else max(entry["lastEndedAt"], int(event_end))

                previous_end = entry["_session_state"].get(row.device_key)
                if previous_end is None or (float(row.start_time) - float(previous_end)) > session_gap_seconds:
                    entry["sessionCount"] += 1
                entry["_session_state"][row.device_key] = event_end

            books = []
            for entry in grouped.values():
                entry.pop("_session_state", None)
                books.append(entry)

            books.sort(key=lambda item: (int(item.get("lastEndedAt") or 0), int(item.get("totalSeconds") or 0)), reverse=True)

            return {
                "date": target_date.isoformat(),
                "totalSeconds": sum(int(book.get("totalSeconds") or 0) for book in books),
                "totalPages": sum(int(book.get("pagesRead") or 0) for book in books),
                "totalBooks": len(books),
                "books": books,
            }

    def get_koreader_calendar_month(self, month_str: str, tz_name: str) -> dict:
        """Get KOReader book activity grouped by local day for one month."""
        month_start = datetime.fromisoformat(f"{str(month_str)}-01").date()
        if month_start.month == 12:
            next_month = datetime(month_start.year + 1, 1, 1).date()
        else:
            next_month = datetime(month_start.year, month_start.month + 1, 1).date()

        tz = ZoneInfo(tz_name)
        start_epoch = datetime.combine(month_start, datetime.min.time(), tzinfo=tz).timestamp()
        end_epoch = datetime.combine(next_month, datetime.min.time(), tzinfo=tz).timestamp()

        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {
                    "month": month_start.strftime("%Y-%m"),
                    "days": {},
                }

            contexts = self._build_koreader_book_contexts(session, md5s)
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

            day_buckets = {}
            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue

                local_date = self._local_date_from_epoch(row.start_time, tz_name)
                day_bucket = day_buckets.setdefault(local_date, {})
                entry = day_bucket.setdefault(context["bookKey"], {
                    "bookKey": context["bookKey"],
                    "md5": row.md5,
                    "absId": context["absId"],
                    "isLinked": context["isLinked"],
                    "title": context["title"],
                    "author": context["author"],
                    "totalSeconds": 0,
                    "pagesRead": 0,
                    "lastEndedAt": 0,
                })

                duration = int(max(row.duration or 0, 0))
                event_end = int(float(row.start_time + max(row.duration or 0, 0)))
                entry["totalSeconds"] += duration
                entry["pagesRead"] += 1
                entry["lastEndedAt"] = max(int(entry["lastEndedAt"] or 0), event_end)

            normalized_days = {}
            for date_key, books in day_buckets.items():
                ordered_books = sorted(
                    books.values(),
                    key=lambda item: (int(item.get("totalSeconds") or 0), int(item.get("lastEndedAt") or 0)),
                    reverse=True,
                )
                normalized_days[date_key] = ordered_books

            return {
                "month": month_start.strftime("%Y-%m"),
                "days": normalized_days,
            }

    def get_koreader_recent_sessions(self, limit: int, tz_name: str) -> list[dict]:
        """Derive recent reading sessions from KOReader page stats."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []

            contexts = self._build_koreader_book_contexts(session, md5s)
            sample_size = max(int(limit or 10) * 400, 4000)
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = (
                self._scope_koreader_user(rows_query, KOReaderPageStat, uid)
                .order_by(KOReaderPageStat.start_time.desc())
                .limit(sample_size)
                .all()
            )
            if not rows:
                return []

            rows = list(reversed(rows))
            grouped_rows = defaultdict(list)
            for row in rows:
                context = contexts.get(row.md5)
                if not context:
                    continue
                grouped_rows[(context["bookKey"], row.device_key)].append(row)

            sessions = []
            session_gap_seconds = self._session_gap_seconds()
            for (book_key, device_key), grouped in grouped_rows.items():
                current = None
                for row in grouped:
                    duration = int(max(row.duration or 0, 0))
                    event_end = float(row.start_time + max(row.duration or 0, 0))
                    if current is None:
                        current = {
                            "bookKey": book_key,
                            "md5": row.md5,
                            "deviceKey": device_key,
                            "startTime": float(row.start_time),
                            "endTime": event_end,
                            "durationSeconds": duration,
                            "pagesRead": 1,
                        }
                        continue

                    gap = float(row.start_time) - float(current["endTime"])
                    if gap > session_gap_seconds:
                        sessions.append(current)
                        current = {
                            "bookKey": book_key,
                            "md5": row.md5,
                            "deviceKey": device_key,
                            "startTime": float(row.start_time),
                            "endTime": event_end,
                            "durationSeconds": duration,
                            "pagesRead": 1,
                        }
                        continue

                    current["endTime"] = max(float(current["endTime"]), event_end)
                    current["durationSeconds"] += duration
                    current["pagesRead"] += 1

                if current is not None:
                    sessions.append(current)

            sessions.sort(key=lambda entry: entry["endTime"], reverse=True)

            normalized = []
            for entry in sessions[: max(int(limit or 10), 1)]:
                md5 = entry["md5"]
                context = contexts.get(md5) or {}
                book_key_safe = str(entry["bookKey"]).replace(":", "-")
                normalized.append({
                    "id": f"reading-{book_key_safe}-{int(entry['startTime'])}",
                    "activityType": "reading",
                    "bookKey": entry["bookKey"],
                    "absId": context.get("absId"),
                    "isLinked": bool(context.get("isLinked")),
                    "title": context.get("title") or "Unknown book",
                    "author": context.get("author"),
                    "durationSeconds": int(entry["durationSeconds"]),
                    "pagesRead": int(entry["pagesRead"]),
                    "startedAt": int(entry["startTime"]),
                    "endedAt": int(entry["endTime"]),
                    "deviceKey": entry["deviceKey"],
                })

            return normalized

    def _session_gap_seconds(self) -> int:
        """Idle gap (seconds) that splits KOReader page events into sessions."""
        try:
            minutes = float(os.environ.get("KOREADER_SESSION_GAP_MINUTES", "30"))
        except (TypeError, ValueError):
            minutes = 30.0
        return int(max(minutes, 1) * 60)

    @staticmethod
    def _reconstruct_sessions(rows, gap_seconds: int) -> list[dict]:
        """Cluster ordered page-stat rows into sessions, split per device on idle gaps.

        Returns sessions newest-first as {startTime, endTime, durationSeconds, pagesRead}.
        """
        by_device = defaultdict(list)
        for row in rows:
            by_device[row.device_key].append(row)

        sessions = []
        for grouped in by_device.values():
            grouped.sort(key=lambda r: r.start_time)
            current = None
            for row in grouped:
                duration = int(max(row.duration or 0, 0))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                if current is None:
                    current = {"startTime": float(row.start_time), "endTime": event_end,
                               "durationSeconds": duration, "pagesRead": 1}
                elif (float(row.start_time) - float(current["endTime"])) > gap_seconds:
                    sessions.append(current)
                    current = {"startTime": float(row.start_time), "endTime": event_end,
                               "durationSeconds": duration, "pagesRead": 1}
                else:
                    current["endTime"] = max(float(current["endTime"]), event_end)
                    current["durationSeconds"] += duration
                    current["pagesRead"] += 1
            if current is not None:
                sessions.append(current)

        sessions.sort(key=lambda s: s["endTime"], reverse=True)
        for s in sessions:
            s["startTime"] = int(s["startTime"])
            s["endTime"] = int(s["endTime"])
        return sessions

    def _distinct_pages_count(self, session, md5s, start_epoch=None, end_epoch=None) -> int:
        """Count distinct (md5, page) screen-pages in an optional time window."""
        if not md5s:
            return 0
        uid = self._resolve_uid(None)
        query = session.query(KOReaderPageStat.md5, KOReaderPageStat.page).filter(
            KOReaderPageStat.md5.in_(md5s)
        )
        query = self._scope_koreader_user(query, KOReaderPageStat, uid)
        if start_epoch is not None:
            query = query.filter(KOReaderPageStat.start_time >= start_epoch)
        if end_epoch is not None:
            query = query.filter(KOReaderPageStat.start_time < end_epoch)
        return query.distinct().count()

    def _koreader_pages_read(self, session, md5s, metadata: dict = None) -> int:
        """All-time 'pages read' matching KOReader's own number.

        Uses KOReader's per-book ``total_read_pages`` (which applies its read-time
        threshold, so it matches the device), falling back to distinct screen-pages
        for any md5 that has page stats but no uploaded book-stats row.
        """
        if not md5s:
            return 0
        if metadata is None:
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
        total = 0
        missing = set()
        for md5 in md5s:
            meta = metadata.get(md5)
            if meta and meta.total_read_pages:
                total += int(meta.total_read_pages)
            else:
                missing.add(md5)
        if missing:
            total += self._distinct_pages_count(session, missing)
        return total

    def _percent_complete_for_md5s(self, metadata: dict, md5s) -> Optional[float]:
        """Best-effort % complete from KOReader book metadata across a book's md5s."""
        best_read = 0
        total_pages = 0
        for md5 in md5s:
            meta = metadata.get(md5)
            if meta and meta.pages and (meta.total_read_pages or 0) >= best_read:
                best_read = meta.total_read_pages or 0
                total_pages = meta.pages or 0
        if total_pages > 0:
            return round(min(best_read / total_pages, 1.0) * 100, 1)
        return None

    def get_koreader_hour_histogram(self, tz_name: str) -> list[dict]:
        """Reading activity bucketed by local hour-of-day (0-23)."""
        buckets = [{"hour": h, "seconds": 0, "pages": 0} for h in range(24)]
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return buckets
            tz = ZoneInfo(tz_name)
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
            for row in rows:
                hour = datetime.fromtimestamp(float(row.start_time), tz).hour
                buckets[hour]["seconds"] += int(max(row.duration or 0, 0))
                buckets[hour]["pages"] += 1
        return buckets

    def get_koreader_book_list(self, tz_name: str) -> list[dict]:
        """Per-book reading rollup for the books list (newest activity first)."""
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return []
            contexts = self._build_koreader_book_contexts(session, md5s)
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
            keys_md5 = defaultdict(set)
            for md5, ctx in contexts.items():
                keys_md5[ctx["bookKey"]].add(md5)

            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat.md5, KOReaderPageStat.start_time, KOReaderPageStat.duration)
                .filter(KOReaderPageStat.md5.in_(md5s))
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
            agg = {}
            for row in rows:
                ctx = contexts.get(row.md5)
                if not ctx:
                    continue
                entry = agg.setdefault(ctx["bookKey"], {
                    "bookKey": ctx["bookKey"], "absId": ctx["absId"], "isLinked": ctx["isLinked"],
                    "title": ctx["title"], "author": ctx["author"],
                    "totalSeconds": 0, "lastReadAt": 0,
                })
                entry["totalSeconds"] += int(max(row.duration or 0, 0))
                entry["lastReadAt"] = max(entry["lastReadAt"], int(float(row.start_time + max(row.duration or 0, 0))))

            result = []
            for key, entry in agg.items():
                total = entry["totalSeconds"]
                pages = self._koreader_pages_read(session, keys_md5.get(key, set()), metadata)
                result.append({
                    **entry,
                    "pagesRead": pages,
                    "lastReadAt": entry["lastReadAt"] or None,
                    "pagesPerHour": round(pages / (total / 3600), 1) if total > 0 else 0,
                    "secondsPerPage": int(total / pages) if pages > 0 else 0,
                    "percentComplete": self._percent_complete_for_md5s(metadata, keys_md5.get(key, set())),
                })
            result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
            return result

    def get_koreader_book_detail(self, book_key: str, tz_name: str) -> Optional[dict]:
        """Full per-book reading detail: pace, sessions, daily heatmap, completion."""
        with self.get_session() as session:
            all_md5s = self._get_all_koreader_active_md5s(session)
            if not all_md5s:
                return None
            contexts = self._build_koreader_book_contexts(session, all_md5s)
            md5s = {md5 for md5, ctx in contexts.items() if ctx["bookKey"] == book_key}
            if not md5s:
                return None
            ctx = contexts[next(iter(md5s))]
            metadata = self._get_latest_koreader_book_metadata(session, md5s)

            uid = self._resolve_uid(None)
            rows_query = (
                session.query(KOReaderPageStat)
                .filter(KOReaderPageStat.md5.in_(md5s))
                .order_by(KOReaderPageStat.start_time.asc())
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()
            if not rows:
                return None

            sessions = self._reconstruct_sessions(rows, self._session_gap_seconds())
            total_seconds = sum(int(max(r.duration or 0, 0)) for r in rows)
            pages_read = self._koreader_pages_read(session, md5s, metadata)
            session_count = len(sessions)

            daily_seconds = defaultdict(int)
            daily_pages = defaultdict(set)
            for row in rows:
                date_key = self._local_date_from_epoch(row.start_time, tz_name)
                daily_seconds[date_key] += int(max(row.duration or 0, 0))
                daily_pages[date_key].add((row.md5, row.page))
            heatmap = [
                {"date": key, "seconds": daily_seconds[key], "pages": len(daily_pages[key])}
                for key in sorted(daily_seconds)
            ]

            return {
                "bookKey": book_key, "absId": ctx["absId"], "isLinked": ctx["isLinked"],
                "title": ctx["title"], "author": ctx["author"],
                "totalSeconds": total_seconds, "pagesRead": pages_read,
                "sessionCount": session_count,
                "avgSessionSeconds": int(total_seconds / session_count) if session_count else 0,
                "pagesPerHour": round(pages_read / (total_seconds / 3600), 1) if total_seconds > 0 else 0,
                "secondsPerPage": int(total_seconds / pages_read) if pages_read > 0 else 0,
                "firstReadAt": int(rows[0].start_time),
                "lastReadAt": int(float(rows[-1].start_time + max(rows[-1].duration or 0, 0))),
                "percentComplete": self._percent_complete_for_md5s(metadata, md5s),
                "heatmap": heatmap,
                "sessions": sessions[:50],
            }

    def _koreader_available_years(self, session, md5s, tz_name: str) -> list[int]:
        from sqlalchemy import func
        uid = self._resolve_uid(None)
        bounds_query = (
            session.query(func.min(KOReaderPageStat.start_time), func.max(KOReaderPageStat.start_time))
            .filter(KOReaderPageStat.md5.in_(md5s))
        )
        bounds = self._scope_koreader_user(bounds_query, KOReaderPageStat, uid).first()
        if not bounds or bounds[0] is None:
            return []
        tz = ZoneInfo(tz_name)
        first_year = datetime.fromtimestamp(float(bounds[0]), tz).year
        last_year = datetime.fromtimestamp(float(bounds[1]), tz).year
        return list(range(first_year, last_year + 1))

    def get_koreader_yearly_recap(self, year: int, tz_name: str) -> dict:
        """Year-in-review for reading: monthly totals + books finished that year."""
        empty_months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
        with self.get_session() as session:
            md5s = self._get_all_koreader_active_md5s(session)
            if not md5s:
                return {"year": year, "months": empty_months, "totalSeconds": 0,
                        "totalPages": 0, "booksFinished": 0, "finishedBooks": [], "availableYears": []}

            contexts = self._build_koreader_book_contexts(session, md5s)
            metadata = self._get_latest_koreader_book_metadata(session, md5s)
            keys_md5 = defaultdict(set)
            for md5, ctx in contexts.items():
                keys_md5[ctx["bookKey"]].add(md5)

            tz = ZoneInfo(tz_name)
            start_epoch = datetime(year, 1, 1, tzinfo=tz).timestamp()
            end_epoch = datetime(year + 1, 1, 1, tzinfo=tz).timestamp()
            uid = self._resolve_uid(None)
            rows_query = (
                session.query(
                    KOReaderPageStat.md5, KOReaderPageStat.page,
                    KOReaderPageStat.start_time, KOReaderPageStat.duration,
                )
                .filter(KOReaderPageStat.md5.in_(md5s))
                .filter(KOReaderPageStat.start_time >= start_epoch)
                .filter(KOReaderPageStat.start_time < end_epoch)
            )
            rows = self._scope_koreader_user(rows_query, KOReaderPageStat, uid).all()

            months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
            month_pages = [set() for _ in range(12)]
            year_pages = set()
            last_event_by_key = {}
            for row in rows:
                ctx = contexts.get(row.md5)
                if not ctx:
                    continue
                local_dt = datetime.fromtimestamp(float(row.start_time), tz)
                bucket = months[local_dt.month - 1]
                bucket["seconds"] += int(max(row.duration or 0, 0))
                month_pages[local_dt.month - 1].add((row.md5, row.page))
                year_pages.add((row.md5, row.page))
                event_end = float(row.start_time + max(row.duration or 0, 0))
                last_event_by_key[ctx["bookKey"]] = max(last_event_by_key.get(ctx["bookKey"], 0), event_end)

            for index, bucket in enumerate(months):
                bucket["pages"] = len(month_pages[index])

            # KOReader's true read-status lives in .sdr sidecars (not ingested), so we
            # approximate "finished" from pages read. KOReader's read-time threshold makes
            # total_read_pages undercount, so 95% is treated as effectively complete.
            finished_books = []
            for book_key, last_end in last_event_by_key.items():
                pct = self._percent_complete_for_md5s(metadata, keys_md5.get(book_key, set()))
                if pct is None or pct < 95:
                    continue
                ctx = contexts[next(iter(keys_md5[book_key]))]
                finished_dt = datetime.fromtimestamp(last_end, tz)
                finished_books.append({
                    "bookKey": book_key, "absId": ctx["absId"],
                    "title": ctx["title"], "author": ctx["author"],
                    "finishedAt": int(last_end),
                    "finishedDate": finished_dt.date().isoformat(), "month": finished_dt.month,
                })
                months[finished_dt.month - 1]["finished"] += 1
            finished_books.sort(key=lambda item: item["finishedAt"], reverse=True)

            return {
                "year": year,
                "months": months,
                "totalSeconds": sum(m["seconds"] for m in months),
                "totalPages": len(year_pages),
                "booksFinished": len(finished_books),
                "finishedBooks": finished_books,
                "availableYears": self._koreader_available_years(session, md5s, tz_name),
            }
