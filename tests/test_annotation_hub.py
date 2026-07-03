"""
Annotation hub tests: DB exchange semantics (two devices + tombstones),
the BookOrbit spoke service, and key/normalization helpers.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

DOC = "a" * 32


def _entry(datetime="2026-07-01 10:00:00", pos0="/body/DocFragment[7]/p[3]/text().0",
           pos1="/body/DocFragment[7]/p[3]/text().42", **kw):
    entry = {
        "datetime": datetime,
        "drawer": "lighten",
        "posFormat": "xpointer",
        "pos0": pos0,
        "pos1": pos1,
        "text": "highlighted words",
    }
    entry.update(kw)
    return entry


def _book(changes=None, keys=None, keys_complete=True):
    return {
        "hash": DOC,
        "keys": keys or [],
        "keysComplete": keys_complete,
        "changes": changes or [],
    }


def _keys_for(db, changes):
    return [
        {"k": db.compute_annotation_key(c["datetime"], c["pos0"]), "dt": c["datetime"]}
        for c in changes
    ]


class AnnotationHubBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from src.db.database_service import DatabaseService
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test.db'))

    def tearDown(self):
        if hasattr(self.db, 'db_manager'):
            self.db.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestDeviceExchange(AnnotationHubBase):
    def test_upload_then_second_device_receives_add(self):
        change = _entry()
        result = self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        # Uploader gets nothing back — it already has its own highlight.
        self.assertEqual(result["books"][0]["toApply"], {"add": [], "edit": [], "delete": []})

        result_b = self.db.exchange_koreader_annotations(
            user_id=None, device_key="kindle", books=[_book()],
        )
        adds = result_b["books"][0]["toApply"]["add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["pos0"], change["pos0"])
        self.assertEqual(adds[0]["text"], "highlighted words")
        self.assertEqual(adds[0]["version"], 1)

    def test_ack_stops_redelivery(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        result_b = self.db.exchange_koreader_annotations(
            user_id=None, device_key="kindle", books=[_book()],
        )
        add = result_b["books"][0]["toApply"]["add"][0]
        self.db.ack_koreader_annotations(
            user_id=None, device_key="kindle",
            books=[{"hash": DOC, "applied": [{"serverId": add["serverId"], "version": add["version"], "status": "applied"}], "deleted": []}],
        )
        again = self.db.exchange_koreader_annotations(
            user_id=None, device_key="kindle", books=[_book()],
        )
        self.assertEqual(again["books"][0]["toApply"], {"add": [], "edit": [], "delete": []})

    def test_edit_bumps_version_and_flows_as_edit(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        # kindle applies + acks v1
        result_b = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book()])
        add = result_b["books"][0]["toApply"]["add"][0]
        self.db.ack_koreader_annotations(
            user_id=None, device_key="kindle",
            books=[{"hash": DOC, "applied": [{"serverId": add["serverId"], "version": 1, "status": "applied"}], "deleted": []}],
        )
        # kobo edits the note
        edited = _entry(note="a new note", datetimeUpdated="2026-07-01 11:00:00")
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[edited], keys=_keys_for(self.db, [edited]))],
        )
        again = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book(
            keys=_keys_for(self.db, [edited]),
        )])
        to_apply = again["books"][0]["toApply"]
        self.assertEqual(len(to_apply["edit"]), 1)
        self.assertEqual(to_apply["edit"][0]["note"], "a new note")
        self.assertEqual(to_apply["edit"][0]["version"], 2)
        self.assertEqual(to_apply["add"], [])

    def test_stale_edit_does_not_overwrite_newer(self):
        newer = _entry(note="new", datetimeUpdated="2026-07-01 12:00:00")
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[newer], keys=_keys_for(self.db, [newer]))],
        )
        stale = _entry(note="old", datetimeUpdated="2026-07-01 11:00:00")
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kindle",
            books=[_book(changes=[stale], keys=_keys_for(self.db, [stale]))],
        )
        rows = self.db.get_user_annotations_for_book(None, DOC)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].note, "new")

    def test_deletion_propagates_as_tombstone(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        # kindle receives + acks
        result_b = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book()])
        add = result_b["books"][0]["toApply"]["add"][0]
        self.db.ack_koreader_annotations(
            user_id=None, device_key="kindle",
            books=[{"hash": DOC, "applied": [{"serverId": add["serverId"], "version": 1, "status": "applied"}], "deleted": []}],
        )
        # kobo deletes: complete key list no longer contains the key
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo", books=[_book(keys=[], keys_complete=True)],
        )
        rows = self.db.get_user_annotations_for_book(None, DOC, include_deleted=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].deleted)
        # kindle is told to delete
        again = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book(
            keys=_keys_for(self.db, [change]),
        )])
        deletes = again["books"][0]["toApply"]["delete"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["datetime"], change["datetime"])

    def test_unknown_device_key_omission_does_not_delete(self):
        """A device that never had an annotation can't delete it by omission."""
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        # A brand-new device syncs with an empty (complete) key list.
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="fresh-device", books=[_book(keys=[], keys_complete=True)],
        )
        rows = self.db.get_user_annotations_for_book(None, DOC)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].deleted)

    def test_incomplete_key_list_skips_deletion_detection(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo", books=[_book(keys=[], keys_complete=False)],
        )
        rows = self.db.get_user_annotations_for_book(None, DOC)
        self.assertFalse(rows[0].deleted)

    def test_users_are_isolated(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=1, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        other = self.db.exchange_koreader_annotations(user_id=2, device_key="kindle", books=[_book()])
        self.assertEqual(other["books"][0]["toApply"]["add"], [])


class TestBookOrbitSpokeDb(AnnotationHubBase):
    def test_apply_spoke_add_flows_to_devices(self):
        acks = self.db.apply_spoke_annotations(
            None, DOC, "@bookorbit",
            adds=[dict(_entry(), serverId=77, version=3)], edits=[], deletes=[],
        )
        self.assertEqual(acks["applied"], [{"serverId": 77, "version": 3, "status": "applied"}])
        result = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book()])
        adds = result["books"][0]["toApply"]["add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["text"], "highlighted words")

    def test_device_annotation_uploads_to_spoke_once(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        state = self.db.get_annotation_spoke_state(None, DOC, "@bookorbit")
        self.assertEqual(len(state["changes"]), 1)
        ann_id = state["changes"][0]["_id"]
        self.db.mark_spoke_annotations_uploaded(None, "@bookorbit", [ann_id])
        state2 = self.db.get_annotation_spoke_state(None, DOC, "@bookorbit")
        self.assertEqual(state2["changes"], [])
        self.assertEqual(len(state2["keys"]), 1)

    def test_spoke_delete_tombstones_and_reaches_devices(self):
        acks = self.db.apply_spoke_annotations(
            None, DOC, "@bookorbit",
            adds=[dict(_entry(), serverId=77, version=1)], edits=[], deletes=[],
        )
        # device receives + acks
        result = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book()])
        add = result["books"][0]["toApply"]["add"][0]
        self.db.ack_koreader_annotations(
            user_id=None, device_key="kindle",
            books=[{"hash": DOC, "applied": [{"serverId": add["serverId"], "version": add["version"], "status": "applied"}], "deleted": []}],
        )
        # BookOrbit deletes it
        acks = self.db.apply_spoke_annotations(None, DOC, "@bookorbit", adds=[], edits=[], deletes=[{"serverId": 77}])
        self.assertEqual(acks["deleted"], [{"serverId": 77, "status": "applied"}])
        again = self.db.exchange_koreader_annotations(user_id=None, device_key="kindle", books=[_book(keys=_keys_for(self.db, [_entry()]))])
        self.assertEqual(len(again["books"][0]["toApply"]["delete"]), 1)

    def test_local_tombstone_omitted_from_spoke_keys(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        # kobo deletes it
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo", books=[_book(keys=[], keys_complete=True)],
        )
        state = self.db.get_annotation_spoke_state(None, DOC, "@bookorbit")
        self.assertEqual(state["keys"], [])
        self.assertEqual(len(state["pending_delete_acks"]), 1)


class TestAnnotationSyncService(unittest.TestCase):
    def _service_with_db(self, db):
        from src.services.annotation_sync_service import AnnotationSyncService
        return AnnotationSyncService(db)

    def test_sync_user_exchanges_and_applies(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = {
            "keys": [{"k": "k1", "dt": "2026-07-01 10:00:00"}],
            "changes": [dict(_entry(), _id=5, serverId=None, version=None)],
            "pending_delete_acks": [],
        }
        db.apply_spoke_annotations.return_value = {
            "applied": [{"serverId": 9, "version": 1, "status": "applied"}],
            "deleted": [],
        }
        client = MagicMock()
        client.koreader_exchange_annotations.return_value = {
            "results": [{"hash": DOC, "toApply": {"add": [dict(_entry(datetime="2026-07-01 09:00:00"), serverId=9, version=1)], "edit": [], "delete": []}, "more": False}],
            "unmatched": [],
        }

        service = self._service_with_db(db)
        service._candidate_md5s = lambda user_id: [DOC]
        service.sync_user(1, client, "carl", "deadbeef" * 4)

        sent_books = client.koreader_exchange_annotations.call_args[0][2]
        self.assertEqual(sent_books[0]["hash"], DOC)
        self.assertTrue(sent_books[0]["keysComplete"])
        # internal _id + null fields stripped from the wire payload
        self.assertNotIn("_id", sent_books[0]["changes"][0])
        self.assertNotIn("serverId", sent_books[0]["changes"][0])
        db.apply_spoke_annotations.assert_called_once()
        db.mark_spoke_annotations_uploaded.assert_called_once()
        client.koreader_exchange_annotations_ack.assert_called_once()

    def test_unmatched_hash_is_skipped_afterwards(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = {"keys": [], "changes": [], "pending_delete_acks": []}
        client = MagicMock()
        client.koreader_exchange_annotations.return_value = {"results": [], "unmatched": [DOC]}

        service = self._service_with_db(db)
        service._candidate_md5s = lambda user_id: [DOC]
        service.sync_user(1, client, "carl", "deadbeef" * 4)
        self.assertIn((1, DOC), service._unmatched)
        client.koreader_exchange_annotations.reset_mock()
        service.sync_user(1, client, "carl", "deadbeef" * 4)
        client.koreader_exchange_annotations.assert_not_called()

    def test_unmatched_hash_is_reprobed_after_ttl(self):
        """A book added to BookOrbit later must be picked up once the TTL lapses."""
        import time as _time
        from src.services import annotation_sync_service as mod
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = {"keys": [], "changes": [], "pending_delete_acks": []}
        client = MagicMock()
        client.koreader_exchange_annotations.return_value = {"results": [], "unmatched": [DOC]}

        service = self._service_with_db(db)
        service._candidate_md5s = lambda user_id: [DOC]
        service.sync_user(1, client, "carl", "deadbeef" * 4)
        # Age the entry past the recheck TTL.
        service._unmatched[(1, DOC)] = _time.time() - mod._UNMATCHED_RECHECK_SECONDS - 1
        client.koreader_exchange_annotations.reset_mock()
        service.sync_user(1, client, "carl", "deadbeef" * 4)
        client.koreader_exchange_annotations.assert_called_once()

    def test_normalize_kosync_key(self):
        from src.api.bookorbit_client import BookOrbitClient
        hashed = "5b5b5bfa3a0b6794b518e9d531f47f8c"
        self.assertEqual(BookOrbitClient.normalize_kosync_key(hashed), hashed)
        import hashlib
        self.assertEqual(
            BookOrbitClient.normalize_kosync_key("hunter2"),
            hashlib.md5(b"hunter2").hexdigest(),
        )
        self.assertEqual(BookOrbitClient.normalize_kosync_key(""), "")


if __name__ == '__main__':
    unittest.main()
