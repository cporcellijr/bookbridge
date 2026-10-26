"""Cross-device KOReader reading-status sync (sidecar ``summary.status``).

KOReader keeps a book's reading status in its ``.sdr`` sidecar, which no existing
channel carries: KoSync moves position only and the statistics DB has no status
column. So a book finished on one device stayed "new" on another.

The arbitration rule here was derived from the real divergence between two live
devices (a Kindle and a Kobo sharing 101 books by partial md5, 30 of which
disagreed on status): order by the device's own ``summary.modified`` date, and
break a same-day tie by status precedence. Ordering by date alone resolved 28 of
the 30 with no counterexamples; both remaining ties are same-day and resolve via
precedence. The conflicts replayed in ``test_real_device_conflicts_all_resolve``
are taken verbatim from that scan.
"""

import hashlib
import os
import shutil
import unittest

import pytest

from src.db.database_service import DatabaseService
from src.db.models import KOReaderBookStatus


def _hex(seed: str) -> str:
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


@pytest.fixture
def database(tmp_path):
    service = DatabaseService(str(tmp_path / "status.db"))
    yield service
    service.db_manager.close()


def _upload(database, device, books, received_at=1000.0):
    return database.upsert_koreader_book_status(
        device=device, device_id=device, books=books, user_id=0, received_at=received_at
    )


def _winner(database, md5):
    for row in database.resolve_koreader_book_status(user_id=0):
        if row["md5"] == md5:
            return row
    return None


# ---------------------------------------------------------------------------
# The arbiter
# ---------------------------------------------------------------------------

def test_later_modified_date_wins(database):
    """The device whose status changed most recently decides."""
    md5 = _hex("altered-carbon")
    _upload(database, "kobo", [{"md5": md5, "status": "reading", "modified": "2026-04-29"}])
    _upload(database, "kindle", [{"md5": md5, "status": "complete", "modified": "2026-05-05"}])

    assert _winner(database, md5)["status"] == "complete"


def test_later_modified_date_wins_even_when_it_un_finishes(database):
    """Date leads precedence: a genuinely newer 'reading' beats an older 'complete'.

    This is the case that distinguishes the rule from "finished always wins" --
    a re-read must be able to reopen a finished book.
    """
    md5 = _hex("re-read")
    _upload(database, "kindle", [{"md5": md5, "status": "complete", "modified": "2026-01-01"}])
    _upload(database, "kobo", [{"md5": md5, "status": "reading", "modified": "2026-09-01"}])

    assert _winner(database, md5)["status"] == "reading"


def test_same_day_tie_breaks_toward_complete(database):
    """Two devices, same date: 'complete' wins so a reopen can't silently un-finish.

    The 'reading' row is received LATER on the bridge's own clock, so precedence
    is the only thing that can decide this: drop the tie-break and 'reading' wins.
    """
    md5 = _hex("otherlife-dreams")
    _upload(database, "kobo", [{"md5": md5, "status": "complete", "modified": "2026-04-19"}],
            received_at=1000.0)
    _upload(database, "kindle", [{"md5": md5, "status": "reading", "modified": "2026-04-19"}],
            received_at=2000.0)

    assert _winner(database, md5)["status"] == "complete"


def test_same_day_tie_orders_abandoned_above_reading(database):
    md5 = _hex("abandoned-tie")
    _upload(database, "kobo", [{"md5": md5, "status": "abandoned", "modified": "2026-06-01"}],
            received_at=1000.0)
    _upload(database, "kindle", [{"md5": md5, "status": "reading", "modified": "2026-06-01"}],
            received_at=2000.0)

    assert _winner(database, md5)["status"] == "abandoned"


def test_real_device_conflicts_all_resolve(database):
    """Replay of the measured Kindle/Kobo conflicts, including both same-day ties.

    Each tuple is (kobo_status, kobo_modified, kindle_status, kindle_modified,
    expected). Taken verbatim from the 2026-09-18 scan of both devices.
    """
    cases = [
        ("reading", "2026-06-21", "complete", "2026-06-22", "complete"),
        ("reading", "2026-04-29", "complete", "2026-05-05", "complete"),
        ("complete", "2026-05-17", "reading", "2026-05-15", "complete"),
        ("complete", "2026-04-15", "reading", "2026-04-10", "complete"),
        ("reading", "2026-07-09", "complete", "2026-07-21", "complete"),
        ("complete", "2026-09-17", "reading", "2026-09-15", "complete"),
        ("complete", "2026-08-07", "reading", "2026-08-06", "complete"),
        # The two same-day ties -- date cannot separate these, precedence must.
        ("complete", "2026-04-19", "reading", "2026-04-19", "complete"),
        ("complete", "2026-04-11", "reading", "2026-04-10", "complete"),
    ]
    for index, (ko_s, ko_m, ki_s, ki_m, expected) in enumerate(cases):
        md5 = _hex(f"conflict-{index}")
        # The losing device always uploads LAST, so nothing can be resolved by
        # arrival order standing in for the rule under test.
        first, second = ("kobo", "kindle") if expected == ko_s else ("kindle", "kobo")
        rows = {
            "kobo": {"md5": md5, "status": ko_s, "modified": ko_m},
            "kindle": {"md5": md5, "status": ki_s, "modified": ki_m},
        }
        _upload(database, first, [rows[first]], received_at=1000.0)
        _upload(database, second, [rows[second]], received_at=2000.0)
        assert _winner(database, md5)["status"] == expected, f"case {index} ({ko_s}/{ki_s})"


def test_winner_reports_its_source_device(database):
    md5 = _hex("source-device")
    _upload(database, "kobo", [{"md5": md5, "status": "complete", "modified": "2026-05-05"}])
    _upload(database, "kindle", [{"md5": md5, "status": "reading", "modified": "2026-04-01"}])

    assert _winner(database, md5)["source_device_key"] == "kobo"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_empty_status_is_not_stored(database):
    """KOReader writes `status = ""` in the wild; it carries no decision and must
    never outrank another device's real status."""
    md5 = _hex("empty-status")
    accepted = _upload(database, "kobo", [{"md5": md5, "status": "", "modified": "2026-09-18"}])

    assert accepted == 0
    assert _winner(database, md5) is None


def test_empty_status_cannot_override_a_real_one(database):
    md5 = _hex("empty-vs-real")
    _upload(database, "kindle", [{"md5": md5, "status": "complete", "modified": "2026-01-01"}])
    _upload(database, "kobo", [{"md5": md5, "status": "", "modified": "2026-09-18"}])

    assert _winner(database, md5)["status"] == "complete"


def test_reupload_updates_in_place_rather_than_duplicating(database):
    """One row per (md5, user, device) -- a device re-reporting must not accumulate."""
    md5 = _hex("reupload")
    _upload(database, "kobo", [{"md5": md5, "status": "reading", "modified": "2026-05-01"}])
    _upload(database, "kobo", [{"md5": md5, "status": "complete", "modified": "2026-05-09"}])

    with database.get_session() as session:
        rows = session.query(KOReaderBookStatus).filter(KOReaderBookStatus.md5 == md5).all()
        assert len(rows) == 1
    assert _winner(database, md5)["status"] == "complete"


def test_status_is_scoped_per_user(database):
    """Two users reading the same EPUB must not see each other's status."""
    md5 = _hex("shared-epub")
    database.upsert_koreader_book_status(
        device="kobo", device_id="kobo", user_id=0,
        books=[{"md5": md5, "status": "complete", "modified": "2026-05-01"}],
    )
    database.upsert_koreader_book_status(
        device="kindle", device_id="kindle", user_id=7,
        books=[{"md5": md5, "status": "reading", "modified": "2026-09-01"}],
    )

    assert _winner(database, md5)["status"] == "complete"
    other = [r for r in database.resolve_koreader_book_status(user_id=7) if r["md5"] == md5]
    assert other and other[0]["status"] == "reading"


def test_missing_md5_rows_are_skipped(database):
    accepted = _upload(database, "kobo", [{"status": "reading", "modified": "2026-09-18"}])
    assert accepted == 0


def test_upload_without_device_identity_is_rejected(database):
    accepted = database.upsert_koreader_book_status(
        device="", device_id="", user_id=0,
        books=[{"md5": _hex("no-device"), "status": "reading", "modified": "2026-09-18"}],
    )
    assert accepted == 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

TEST_DIR = '/tmp/test_koreader_book_status_sync'
_LEAKY_ENV = ('DATA_DIR', 'KOSYNC_USER', 'KOSYNC_KEY')


class TestKoreaderStatusEndpoints(unittest.TestCase):
    """The device-facing wire contract."""

    @classmethod
    def setUpClass(cls):
        # DATA_DIR decides which database create_app() binds, so it must be
        # restored: the next file to build an app would otherwise get this
        # class's database and see the wrong rows (failure mode #17).
        cls._prior_env = {key: os.environ.get(key) for key in _LEAKY_ENV}
        os.environ['DATA_DIR'] = TEST_DIR
        os.environ['KOSYNC_USER'] = 'testuser'
        os.environ['KOSYNC_KEY'] = 'testpass'
        if os.path.exists(TEST_DIR):
            shutil.rmtree(TEST_DIR)
        os.makedirs(TEST_DIR, exist_ok=True)

        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app
        for key, value in cls._prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        from src import web_server
        db = web_server.database_service
        with db.get_session() as session:
            session.query(KOReaderBookStatus).delete()
        if db.count_users() == 0:
            db.create_user("admin", "secret", role="admin")

        self._prior_gate = os.environ.get('KOREADER_STATUS_SYNC_ENABLED')
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'true'
        self.headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }

    def tearDown(self):
        if self._prior_gate is None:
            os.environ.pop('KOREADER_STATUS_SYNC_ENABLED', None)
        else:
            os.environ['KOREADER_STATUS_SYNC_ENABLED'] = self._prior_gate

    def test_upload_then_merged_round_trip(self):
        md5 = _hex("endpoint-round-trip")
        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo",
            "books": [{"md5": md5, "status": "complete", "modified": "2026-09-06"}],
        })
        self.assertEqual(post.status_code, 200, post.get_data(as_text=True))
        self.assertEqual(post.get_json()["accepted"], 1)

        merged = self.client.get('/koreader/device-sync/status/merged', headers=self.headers)
        self.assertEqual(merged.status_code, 200)
        body = merged.get_json()
        self.assertTrue(body["enabled"])
        entry = next(b for b in body["books"] if b["md5"] == md5)
        self.assertEqual(entry["status"], "complete")
        self.assertEqual(entry["modified"], "2026-09-06")

    def test_merged_returns_books_the_asking_device_never_reported(self):
        """A device that has never opened a book must still learn its status --
        that is the whole 'delivered but never opened' case."""
        md5 = _hex("never-opened-here")
        self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo",
            "books": [{"md5": md5, "status": "reading", "modified": "2026-09-17"}],
        })

        merged = self.client.get(
            '/koreader/device-sync/status/merged?device=kindle&device_id=kindle',
            headers=self.headers,
        )
        self.assertIn(md5, {b["md5"] for b in merged.get_json()["books"]})

    def test_unknown_status_value_is_rejected(self):
        """Values are written straight back into a device's sidecar, so a typo
        would become a status the reader can never clear."""
        md5 = _hex("bogus-status")
        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo",
            "books": [{"md5": md5, "status": "finished-ish", "modified": "2026-09-18"}],
        })
        self.assertEqual(post.status_code, 200)
        self.assertEqual(post.get_json()["accepted"], 0)
        self.assertEqual(post.get_json()["rejected"], 1)

    def test_gate_off_short_circuits_both_endpoints(self):
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'false'
        md5 = _hex("gated-off")

        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo",
            "books": [{"md5": md5, "status": "complete", "modified": "2026-09-18"}],
        })
        self.assertEqual(post.status_code, 200)
        self.assertFalse(post.get_json()["enabled"])

        merged = self.client.get('/koreader/device-sync/status/merged', headers=self.headers)
        self.assertFalse(merged.get_json()["enabled"])
        self.assertEqual(merged.get_json()["books"], [])

    def test_gate_accepts_checkbox_spelling_on(self):
        """Settings checkboxes POST 'on', not 'true' -- failure mode #1."""
        os.environ['KOREADER_STATUS_SYNC_ENABLED'] = 'on'
        merged = self.client.get('/koreader/device-sync/status/merged', headers=self.headers)
        self.assertTrue(merged.get_json()["enabled"])

    def test_missing_device_identity_is_a_400(self):
        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "books": [{"md5": _hex("x"), "status": "reading", "modified": "2026-09-18"}],
        })
        self.assertEqual(post.status_code, 400)

    def test_books_must_be_an_array(self):
        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo", "books": {"md5": "x"},
        })
        self.assertEqual(post.status_code, 400)

    def test_oversized_upload_is_rejected(self):
        from src.api import kosync_server
        books = [
            {"md5": _hex(str(i)), "status": "reading", "modified": "2026-09-18"}
            for i in range(kosync_server._KOREADER_STATUS_MAX_BOOKS + 1)
        ]
        post = self.client.post('/koreader/device-sync/status', headers=self.headers, json={
            "device": "kobo", "device_id": "kobo", "books": books,
        })
        self.assertEqual(post.status_code, 413)

    def test_unauthenticated_request_is_rejected(self):
        post = self.client.post('/koreader/device-sync/status', json={
            "device": "kobo", "device_id": "kobo", "books": [],
        })
        self.assertIn(post.status_code, (401, 403))



    # --- merged-statistics history flag -------------------------------------
    # History is per-device by design ("books I opened here"), so widening it to
    # "books I read anywhere" is opt-in and off by default.

    def _merged_body(self):
        return self.client.get(
            '/koreader/device-sync/statistics/merged?device=kindle&device_id=kindle',
            headers=self.headers,
        ).get_json()

    def test_merge_history_flag_off_by_default(self):
        prior = os.environ.pop('KOREADER_SYNC_READ_HISTORY', None)
        try:
            self.assertFalse(self._merged_body()["merge_history"])
        finally:
            if prior is not None:
                os.environ['KOREADER_SYNC_READ_HISTORY'] = prior

    def test_merge_history_flag_enabled_when_set(self):
        prior = os.environ.get('KOREADER_SYNC_READ_HISTORY')
        os.environ['KOREADER_SYNC_READ_HISTORY'] = 'true'
        try:
            self.assertTrue(self._merged_body()["merge_history"])
        finally:
            if prior is None:
                os.environ.pop('KOREADER_SYNC_READ_HISTORY', None)
            else:
                os.environ['KOREADER_SYNC_READ_HISTORY'] = prior

    def test_merge_history_flag_accepts_checkbox_spelling(self):
        prior = os.environ.get('KOREADER_SYNC_READ_HISTORY')
        os.environ['KOREADER_SYNC_READ_HISTORY'] = 'on'
        try:
            self.assertTrue(self._merged_body()["merge_history"])
        finally:
            if prior is None:
                os.environ.pop('KOREADER_SYNC_READ_HISTORY', None)
            else:
                os.environ['KOREADER_SYNC_READ_HISTORY'] = prior

if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------------------
# The clear sentinel and the BookOrbit mapping
# ---------------------------------------------------------------------------


def test_record_status_for_book_covers_every_sibling_hash(database):
    """A book can have several document hashes; each device knows only its own."""
    from src.db.models import Book, KosyncDocument

    database.save_book(Book(abs_id="b1", abs_title="Sibling hashes", status="active"))
    for seed in ("copy-a", "copy-b"):
        database.save_kosync_document(KosyncDocument(
            document_hash=_hex(seed), linked_abs_id="b1", user_id=0,
        ))

    written = database.record_koreader_status_for_book(
        "b1", status="complete", device_key="bridge", user_id=0, modified="2026-09-14",
    )
    assert written == 2
    for seed in ("copy-a", "copy-b"):
        assert _winner(database, _hex(seed))["status"] == "complete"


def test_record_status_for_unlinked_book_writes_nothing(database):
    from src.db.models import Book

    database.save_book(Book(abs_id="b2", abs_title="No hashes", status="active"))
    assert database.record_koreader_status_for_book(
        "b2", status="complete", device_key="bridge", user_id=0) == 0


def test_only_if_absent_fills_a_gap(database):
    """A book with no status anywhere gets the bridge's inferred one."""
    from src.db.models import Book, KosyncDocument

    database.save_book(Book(abs_id="g1", abs_title="Gap", status="active"))
    database.save_kosync_document(KosyncDocument(
        document_hash=_hex("gap"), linked_abs_id="g1", user_id=0))

    written = database.record_koreader_status_for_book(
        "g1", status="reading", device_key="bridge", user_id=0, only_if_absent=True)
    assert written == 1
    assert _winner(database, _hex("gap"))["status"] == "reading"


def test_only_if_absent_never_overrules_a_real_decision(database):
    """Position says a book was opened. A reader marking it abandoned half way
    through is a stronger statement, and must not be dragged back to 'reading'
    on the next cycle -- and then every cycle after it."""
    from src.db.models import Book, KosyncDocument

    database.save_book(Book(abs_id="g2", abs_title="Abandoned", status="active"))
    database.save_kosync_document(KosyncDocument(
        document_hash=_hex("abandoned-book"), linked_abs_id="g2", user_id=0))
    _upload(database, "kobo",
            [{"md5": _hex("abandoned-book"), "status": "abandoned", "modified": "2026-01-01"}])

    written = database.record_koreader_status_for_book(
        "g2", status="reading", device_key="bridge", user_id=0, only_if_absent=True)

    assert written == 0
    assert _winner(database, _hex("abandoned-book"))["status"] == "abandoned"


def test_only_if_absent_is_per_hash(database):
    """A book with sibling hashes fills only the ones that have no status."""
    from src.db.models import Book, KosyncDocument

    database.save_book(Book(abs_id="g3", abs_title="Siblings", status="active"))
    for seed in ("sib-known", "sib-blank"):
        database.save_kosync_document(KosyncDocument(
            document_hash=_hex(seed), linked_abs_id="g3", user_id=0))
    _upload(database, "kindle",
            [{"md5": _hex("sib-known"), "status": "complete", "modified": "2026-02-02"}])

    written = database.record_koreader_status_for_book(
        "g3", status="reading", device_key="bridge", user_id=0, only_if_absent=True)

    assert written == 1
    assert _winner(database, _hex("sib-known"))["status"] == "complete"
    assert _winner(database, _hex("sib-blank"))["status"] == "reading"


def test_without_only_if_absent_the_write_still_wins(database):
    """Clear Progress and the completion edge are explicit, and still override."""
    from src.db.models import Book, KosyncDocument

    database.save_book(Book(abs_id="g4", abs_title="Explicit", status="active"))
    database.save_kosync_document(KosyncDocument(
        document_hash=_hex("explicit"), linked_abs_id="g4", user_id=0))
    _upload(database, "kobo",
            [{"md5": _hex("explicit"), "status": "reading", "modified": "2026-01-01"}])

    written = database.record_koreader_status_for_book(
        "g4", status="complete", device_key="bridge", user_id=0)
    assert written == 1
    assert _winner(database, _hex("explicit"))["status"] == "complete"


# ---------------------------------------------------------------------------
# Copies of one book
# ---------------------------------------------------------------------------

def _book_with_copies(database, abs_id, *md5s):
    from src.db.models import Book
    database.save_book(Book(abs_id=abs_id, abs_title=abs_id, status="active"))
    for md5 in md5s:
        database.ensure_linked_kosync_document(md5, abs_id)


def test_a_copy_keeps_its_own_status(database):
    """A sibling's newer status never overrules a decision made on this copy."""
    own, managed = _hex("own-copy"), _hex("managed-copy")
    _book_with_copies(database, "copies", own, managed)
    _upload(database, "kobo", [{"md5": own, "status": "reading", "modified": "2026-10-20"}])
    _upload(database, "kobo", [{"md5": managed, "status": "complete", "modified": "2026-10-24"}])

    assert _winner(database, own)["status"] == "reading"
    assert _winner(database, managed)["status"] == "complete"


def test_linked_copy_without_a_status_gets_the_books_status(database):
    reported, silent = _hex("reported-copy"), _hex("silent-copy")
    _book_with_copies(database, "silent", reported, silent)
    _upload(database, "kobo", [{"md5": reported, "status": "abandoned", "modified": "2026-10-22"}])

    assert _winner(database, silent)["status"] == "abandoned"


def test_unlinked_md5s_are_unchanged(database):
    first, second = _hex("loose-one"), _hex("loose-two")
    _upload(database, "kobo", [{"md5": first, "status": "reading", "modified": "2026-10-20"}])
    _upload(database, "kobo", [{"md5": second, "status": "complete", "modified": "2026-10-24"}])

    assert _winner(database, first)["status"] == "reading"
    assert _winner(database, second)["status"] == "complete"
