"""
Annotation hub tests: DB exchange semantics (two devices + tombstones),
the BookOrbit spoke service, and key/normalization helpers.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_key_is_stable_across_xpointer_reserialization(self):
        """The reported data-loss bug: crengine re-serializes xpointers (drops a
        trailing .0, strips [1]) when a receiving device re-reads an applied
        highlight, so the identity key must normalize both forms to one."""
        dt = "2026-07-01 10:00:00"
        base = "/body/DocFragment[12]/body/section/p[5]/text()"
        self.assertEqual(
            self.db.compute_annotation_key(dt, base + ".0"),
            self.db.compute_annotation_key(dt, base),
        )
        self.assertEqual(
            self.db.compute_annotation_key(dt, "/body/DocFragment[12]/body/section/p[1]/text().0"),
            self.db.compute_annotation_key(dt, "/body/DocFragment[12]/body/section/p/text()"),
        )
        # A genuine text offset is preserved (distinct highlights stay distinct).
        self.assertNotEqual(
            self.db.compute_annotation_key(dt, base + ".331"),
            self.db.compute_annotation_key(dt, base + ".187"),
        )

    def test_received_highlight_not_deleted_after_reserialization(self):
        """Full two-device reproduction: A creates a highlight (pos0 ends .0);
        B receives + acks it, then re-syncs with the re-serialized pos0 (no .0)
        in its complete key list. Pre-fix this tombstoned it (and propagated the
        deletion back to A); with normalization the keys match and it survives."""
        created = _entry(pos0="/body/DocFragment[9]/body/p[4]/text().0",
                         pos1="/body/DocFragment[9]/body/p[4]/text().40")
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="deviceA",
            books=[_book(changes=[created], keys=_keys_for(self.db, [created]))],
        )
        pull = self.db.exchange_koreader_annotations(user_id=None, device_key="deviceB", books=[_book()])
        add = pull["books"][0]["toApply"]["add"][0]
        self.db.ack_koreader_annotations(
            user_id=None, device_key="deviceB",
            books=[{"hash": DOC, "applied": [{"serverId": add["serverId"], "version": add["version"], "status": "applied"}], "deleted": []}],
        )
        # B re-syncs: its sidecar reserialized pos0 to the no-".0" form.
        reserialized = dict(created, pos0="/body/DocFragment[9]/body/p[4]/text()")
        again = self.db.exchange_koreader_annotations(
            user_id=None, device_key="deviceB",
            books=[_book(keys=_keys_for(self.db, [reserialized]), keys_complete=True)],
        )
        # Not deleted, and B is told nothing to delete.
        rows = self.db.get_user_annotations_for_book(None, DOC, include_deleted=True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].deleted)
        self.assertEqual(again["books"][0]["toApply"]["delete"], [])


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


class TestLossyPositionSpokeDb(AnnotationHubBase):
    """Grimmory-style spokes (trust_positions=False): CFI round-trips never
    reproduce the device's xpointer serialization, so a pull echo must not
    rewrite canonical identity — that made the device's next complete key
    list look like a deletion and tombstoned the row everywhere."""

    BOOKLORE_KW = dict(
        server_id_field="booklore_server_id",
        version_field="booklore_version",
        synced_at_field="booklore_synced_at",
    )

    def _echo(self, server_id=501, **kw):
        # What _entry_from_booklore_annotation produces for an annotation the
        # bridge itself pushed: Grimmory's createdAt as datetime and a
        # reconstructed (mangled) xpointer — both differ from the canonical row.
        return dict(_entry(
            datetime="2026-07-01 10:05:09",
            pos0="/body/DocFragment[7]/p[3]/text().1",
            pos1="/body/DocFragment[7]/p[3]/text().41",
        ), serverId=server_id, version=1, **kw)

    def _seed_device_annotation(self):
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        state = self.db.get_annotation_spoke_state(
            None, DOC, "@booklore",
            server_id_field="booklore_server_id", version_field="booklore_version",
        )
        ann_id = state["changes"][0]["_id"]
        self.db.mark_spoke_annotations_uploaded(
            None, "@booklore", [ann_id],
            server_ids_by_annotation_id={str(ann_id): 501},
            **self.BOOKLORE_KW,
        )
        return change, ann_id

    def _fresh_device_adds(self):
        result = self.db.exchange_koreader_annotations(
            user_id=None, device_key="fresh", books=[_book()],
        )
        return result["books"][0]["toApply"]["add"]

    def test_pull_echo_preserves_identity_key(self):
        change, _ = self._seed_device_annotation()
        self.db.apply_spoke_annotations(
            None, DOC, "@booklore",
            adds=[self._echo()], edits=[], deletes=[],
            trust_positions=False, **self.BOOKLORE_KW,
        )
        # The originating device syncs with its complete key list — the echo
        # must not have rewritten the key, or this tombstones the row.
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(keys=_keys_for(self.db, [change]))],
        )
        adds = self._fresh_device_adds()
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["pos0"], change["pos0"])
        self.assertEqual(adds[0]["datetime"], change["datetime"])
        self.assertEqual(adds[0]["version"], 1)  # echo is not an edit

    def test_remote_note_edit_merges_content_only(self):
        change, _ = self._seed_device_annotation()
        self.db.apply_spoke_annotations(
            None, DOC, "@booklore",
            adds=[self._echo(note="from grimmory")], edits=[], deletes=[],
            trust_positions=False, **self.BOOKLORE_KW,
        )
        adds = self._fresh_device_adds()
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["note"], "from grimmory")
        self.assertEqual(adds[0]["version"], 2)
        # position and identity stay canonical
        self.assertEqual(adds[0]["pos0"], change["pos0"])
        self.assertEqual(adds[0]["datetime"], change["datetime"])

    def test_unrecorded_server_id_matches_by_text(self):
        # Server id never recorded (e.g. restart mid-push): the echo must
        # attach by text-within-fragment instead of creating a duplicate.
        change = _entry()
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo",
            books=[_book(changes=[change], keys=_keys_for(self.db, [change]))],
        )
        self.db.apply_spoke_annotations(
            None, DOC, "@booklore",
            adds=[self._echo(server_id=777)], edits=[], deletes=[],
            trust_positions=False, **self.BOOKLORE_KW,
        )
        adds = self._fresh_device_adds()
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["pos0"], change["pos0"])

    def test_stale_echo_does_not_revive_tombstone(self):
        self._seed_device_annotation()
        # kobo deletes the highlight (complete empty key list)
        self.db.exchange_koreader_annotations(
            user_id=None, device_key="kobo", books=[_book(keys=[], keys_complete=True)],
        )
        # Grimmory still echoes its copy — the remote delete hasn't run yet
        self.db.apply_spoke_annotations(
            None, DOC, "@booklore",
            adds=[self._echo()], edits=[], deletes=[],
            trust_positions=False, **self.BOOKLORE_KW,
        )
        self.assertEqual(self._fresh_device_adds(), [])
        state = self.db.get_annotation_spoke_state(
            None, DOC, "@booklore",
            server_id_field="booklore_server_id", version_field="booklore_version",
        )
        self.assertEqual(len(state["pending_deletes"]), 1)
        self.assertEqual(state["pending_deletes"][0]["serverId"], 501)


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
        self.assertNotIn("_spoke_server_id", sent_books[0]["changes"][0])
        self.assertNotIn("_spoke_version", sent_books[0]["changes"][0])
        self.assertNotIn("serverId", sent_books[0]["changes"][0])
        db.apply_spoke_annotations.assert_called_once()
        db.mark_spoke_annotations_uploaded.assert_called_once()
        client.koreader_exchange_annotations_ack.assert_called_once()

    def test_run_cycle_skips_mismatched_bookorbit_owner(self):
        db = MagicMock()
        db.list_users.return_value = [SimpleNamespace(id=1, active=1, is_admin=False)]
        db.get_user_credentials.return_value = {
            "BOOKORBIT_SERVER": "http://bookorbit",
            "BOOKORBIT_USER": "Cporcellijr",
            "BOOKORBIT_KOSYNC_USER": "bridgesync",
            "BOOKORBIT_KOSYNC_KEY": "secret",
        }

        service = self._service_with_db(db)
        service.sync_user = MagicMock()
        result = service.run_cycle()

        self.assertEqual(result["users"], 0)
        service.sync_user.assert_not_called()

    def test_run_cycle_allows_explicit_matching_bookorbit_owner(self):
        db = MagicMock()
        db.list_users.return_value = [SimpleNamespace(id=1, active=1, is_admin=False)]
        db.get_user_credentials.return_value = {
            "BOOKORBIT_SERVER": "http://bookorbit",
            "BOOKORBIT_USER": "Cporcellijr",
            "BOOKORBIT_KOSYNC_USER": "bridgesync",
            "BOOKORBIT_KOSYNC_KEY": "secret",
            "BOOKORBIT_KOSYNC_OWNER": "cporcellijr",
        }

        service = self._service_with_db(db)
        service.sync_user = MagicMock()
        result = service.run_cycle()

        self.assertEqual(result["users"], 1)
        service.sync_user.assert_called_once()

    def test_run_cycle_allows_same_kosync_and_bookorbit_username(self):
        db = MagicMock()
        db.list_users.return_value = [SimpleNamespace(id=1, active=1, is_admin=False)]
        db.get_user_credentials.return_value = {
            "BOOKORBIT_SERVER": "http://bookorbit",
            "BOOKORBIT_USER": "reader",
            "BOOKORBIT_KOSYNC_USER": "Reader",
            "BOOKORBIT_KOSYNC_KEY": "secret",
        }

        service = self._service_with_db(db)
        service.sync_user = MagicMock()
        result = service.run_cycle()

        self.assertEqual(result["users"], 1)
        service.sync_user.assert_called_once()

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


class TestGrimmoryAnnotationSyncService(unittest.TestCase):
    def _service_with_db(self, db):
        from src.services.annotation_sync_service import AnnotationSyncService
        return AnnotationSyncService(db, ebook_parser=MagicMock(), epub_cache_dir=Path("test_data"))

    def test_booklore_pushes_local_add_then_pulls_remote(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = {
            "keys": [],
            "changes": [dict(_entry(), _id=5, _spoke_server_id=None, _spoke_version=None, color="yellow")],
            "pending_delete_acks": [],
            "pending_deletes": [],
        }
        db.get_spoke_server_ids_for_book.side_effect = (
            lambda user_id, doc_md5, server_id_field="bookorbit_server_id":
            [101] if server_id_field == "booklore_server_id" else []
        )
        client = MagicMock()
        client.download_book.return_value = b"epub"
        client.get_book_notes.return_value = []
        client.get_annotations.side_effect = [
            [],
            [{
                "id": 101,
                "bookId": 22,
                "createdAt": "2026-07-01T10:00:00Z",
                "updatedAt": "2026-07-01T10:00:00Z",
                "cfi": "remote-cfi",
                "text": "web words",
                "note": "web note",
                "chapterTitle": "Chapter",
                "color": "#FFC107",
                "style": "highlight",
            }],
        ]
        client.create_annotation.return_value = {"id": 101}

        resolver = MagicMock()
        resolver.xpointer_range_to_cfi.return_value = "local-cfi"
        resolver.cfi_range_to_xpointers.return_value = ("xp0", "xp1")

        service = self._service_with_db(db)
        service._resolve_booklore_epub_path = MagicMock(return_value=Path("book.epub"))
        with patch("src.services.annotation_sync_service.GrimmoryCFIResolver", return_value=resolver):
            did_work = service.sync_booklore_book(
                7,
                client,
                {"doc_md5": DOC, "book_id": "22", "filename": "book.epub", "title": "Book"},
            )

        self.assertTrue(did_work)
        client.create_annotation.assert_called_once_with(
            "22", "local-cfi", None, "highlighted words", "#FFC107", "highlight", None
        )
        db.mark_spoke_annotations_uploaded.assert_called_once()
        db.apply_spoke_annotations.assert_called_once()
        applied = db.apply_spoke_annotations.call_args.kwargs["adds"]
        self.assertEqual(applied[0]["serverId"], 101)
        self.assertEqual(applied[0]["pos0"], "xp0")
        self.assertEqual(applied[0]["drawer"], "lighten")

    def test_booklore_remote_missing_id_tombstones_local(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = {
            "keys": [],
            "changes": [],
            "pending_delete_acks": [],
            "pending_deletes": [],
        }
        db.get_spoke_server_ids_for_book.side_effect = (
            lambda user_id, doc_md5, server_id_field="bookorbit_server_id":
            [101, 102] if server_id_field == "booklore_server_id" else []
        )
        client = MagicMock()
        client.get_book_notes.return_value = []
        client.get_annotations.return_value = [{
            "id": 101,
            "createdAt": "2026-07-01T10:00:00Z",
            "updatedAt": None,
            "cfi": "remote-cfi",
            "text": "web words",
            "note": None,
            "chapterTitle": None,
            "color": "#4ADE80",
            "style": "underline",
        }]
        resolver = MagicMock()
        resolver.cfi_range_to_xpointers.return_value = ("xp0", "xp1")

        service = self._service_with_db(db)
        service._resolve_booklore_epub_path = MagicMock(return_value=Path("book.epub"))
        with patch("src.services.annotation_sync_service.GrimmoryCFIResolver", return_value=resolver):
            service.sync_booklore_book(
                7,
                client,
                {"doc_md5": DOC, "book_id": "22", "filename": "book.epub", "title": "Book"},
            )

        deletes = db.apply_spoke_annotations.call_args.kwargs["deletes"]
        self.assertEqual(deletes, [{"serverId": 102}])


class TestGrimmoryNotesSubSpoke(unittest.TestCase):
    """book_notes_v2 sub-spoke: Grimmory's web reader saves its notes to a
    second store with its own id space — pulled into the hub, device edits
    written back, deletions propagated both ways, never re-exported into the
    annotations store."""

    def _service_with_db(self, db):
        from src.services.annotation_sync_service import AnnotationSyncService
        return AnnotationSyncService(db, ebook_parser=MagicMock(), epub_cache_dir=Path("test_data"))

    @staticmethod
    def _state(changes=None, pending_deletes=None):
        return {
            "keys": [],
            "changes": changes or [],
            "pending_delete_acks": [],
            "pending_deletes": pending_deletes or [],
        }

    @staticmethod
    def _note(note_id=7, **kw):
        note = {
            "id": note_id,
            "bookId": 22,
            "createdAt": "2026-07-04T09:05:41",
            "updatedAt": "2026-07-04T09:05:41",
            "cfi": "note-cfi",
            "selectedText": "picked words",
            "noteContent": "from web",
            "chapterTitle": "Chapter I",
            "color": "#FFC107",
        }
        note.update(kw)
        return note

    def _run(self, db, client):
        resolver = MagicMock()
        resolver.cfi_range_to_xpointers.return_value = ("xp0", "xp1")
        service = self._service_with_db(db)
        return service.sync_booklore_notes(7, client, DOC, "22", resolver)

    def test_web_note_pull_applies_to_hub(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = self._state()
        db.get_spoke_server_ids_for_book.return_value = []
        client = MagicMock()
        client.get_book_notes.return_value = [self._note()]

        self.assertTrue(self._run(db, client))
        kwargs = db.apply_spoke_annotations.call_args.kwargs
        self.assertEqual(kwargs["server_id_field"], "booklore_note_id")
        self.assertFalse(kwargs["trust_positions"])
        add = kwargs["adds"][0]
        self.assertEqual(add["serverId"], 7)
        self.assertEqual(add["note"], "from web")
        self.assertEqual(add["text"], "picked words")
        self.assertEqual(add["pos0"], "xp0")
        client.update_book_note.assert_not_called()
        client.delete_book_note.assert_not_called()

    def test_device_edit_writes_back_to_note(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = self._state(changes=[
            dict(_entry(note="edited on device", color="yellow"),
                 _id=9, _spoke_server_id=7, _spoke_version=None),
        ])
        db.get_spoke_server_ids_for_book.return_value = [7]
        client = MagicMock()
        client.get_book_notes.side_effect = [
            [self._note()],
            [self._note(noteContent="edited on device")],
        ]
        client.update_book_note.return_value = True

        self.assertTrue(self._run(db, client))
        client.update_book_note.assert_called_once_with(
            7, note_content="edited on device", color="#FFC107")
        marked = db.mark_spoke_annotations_uploaded.call_args.kwargs
        self.assertEqual(marked["annotation_ids"], [9])
        self.assertEqual(marked["server_id_field"], "booklore_note_id")
        self.assertEqual(client.get_book_notes.call_count, 2)

    def test_rows_not_owned_by_notes_are_never_pushed(self):
        # A device-authored change without a note id belongs to the
        # annotations store — the notes spoke must not touch it.
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = self._state(changes=[
            dict(_entry(), _id=9, _spoke_server_id=None, _spoke_version=None),
        ])
        db.get_spoke_server_ids_for_book.return_value = []
        client = MagicMock()
        client.get_book_notes.return_value = []

        self._run(db, client)
        client.update_book_note.assert_not_called()
        db.mark_spoke_annotations_uploaded.assert_not_called()

    def test_device_delete_deletes_remote_note(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = self._state(
            pending_deletes=[{"_id": 9, "serverId": 7}])
        db.get_spoke_server_ids_for_book.return_value = []
        client = MagicMock()
        client.get_book_notes.side_effect = [[self._note()], []]
        client.delete_book_note.return_value = True

        self.assertTrue(self._run(db, client))
        client.delete_book_note.assert_called_once_with(7)
        marked = db.mark_spoke_annotations_uploaded.call_args.kwargs
        self.assertEqual(marked["tombstone_ids"], [9])
        db.apply_spoke_annotations.assert_not_called()

    def test_web_deleted_note_tombstones_hub(self):
        db = MagicMock()
        db.get_annotation_spoke_state.return_value = self._state()
        db.get_spoke_server_ids_for_book.return_value = [7]
        client = MagicMock()
        client.get_book_notes.return_value = []

        self.assertTrue(self._run(db, client))
        kwargs = db.apply_spoke_annotations.call_args.kwargs
        self.assertEqual(kwargs["deletes"], [{"serverId": 7}])
        self.assertEqual(kwargs["server_id_field"], "booklore_note_id")


class TestNotesExclusionDb(AnnotationHubBase):
    def test_note_owned_rows_flow_to_devices_but_not_annotations_store(self):
        # A web note lands in the hub under the notes sub-spoke...
        self.db.apply_spoke_annotations(
            None, DOC, "@booklore-notes",
            adds=[dict(_entry(note="web note"), serverId=7, version=1)],
            edits=[], deletes=[],
            server_id_field="booklore_note_id",
            version_field="booklore_version",
            synced_at_field="booklore_synced_at",
            trust_positions=False,
        )
        # ...devices receive it...
        result = self.db.exchange_koreader_annotations(
            user_id=None, device_key="kindle", books=[_book()])
        adds = result["books"][0]["toApply"]["add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["note"], "web note")
        # ...but the annotations-store push must skip it, or every web note
        # would be duplicated into Grimmory's other store.
        state = self.db.get_annotation_spoke_state(
            None, DOC, "@booklore",
            server_id_field="booklore_server_id",
            version_field="booklore_version",
            exclude_if_set="booklore_note_id",
        )
        self.assertEqual(state["changes"], [])
        self.assertEqual(len(state["keys"]), 1)  # still counted as alive


class TestGrimmoryCFIResolver(unittest.TestCase):
    def test_simple_xpointer_cfi_round_trip(self):
        from lxml import html
        from src.utils.grimmory_cfi import GrimmoryCFIResolver

        class Parser:
            def extract_text_and_map(self, path):
                return "Hello world", [{
                    "spine_index": 1,
                    "content": b"<html><body><p>Hello world</p></body></html>",
                    "start": 0,
                    "end": 11,
                }]

            def _split_xpath_char_offset(self, relative_path):
                return self._split(relative_path)

            @staticmethod
            def _split(relative_path):
                import re
                match = re.search(r"\.(\d+)$", relative_path)
                offset = int(match.group(1)) if match else 0
                clean = re.sub(r"(?:/text\(\)(?:\[\d+\])?)?\.\d+$", "", relative_path)
                return clean, offset

            def _resolve_xpath_target_node(self, filename, spine_map, reported_spine_index, clean_xpath):
                item = spine_map[0]
                tree = html.fromstring(item["content"])
                return item, tree, tree.xpath(clean_xpath)[0]

            def _build_xpath(self, element):
                parts = []
                current = element
                while current is not None and current.tag != "html":
                    parts.insert(0, current.tag)
                    current = current.getparent()
                return "/".join(parts)

        resolver = GrimmoryCFIResolver(Parser(), Path("book.epub"))
        cfi = resolver.xpointer_range_to_cfi(
            "/body/DocFragment[1]/body/p/text().0",
            "/body/DocFragment[1]/body/p/text().5",
        )
        self.assertEqual(
            resolver.cfi_range_to_xpointers(cfi),
            (
                "/body/DocFragment[1]/body/p/text().0",
                "/body/DocFragment[1]/body/p/text().5",
            ),
        )


if __name__ == '__main__':
    unittest.main()
