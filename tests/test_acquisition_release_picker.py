"""Get the missing half (#585) Phase 3b: BookOrbit release picker and stalled-download note."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.api.bookorbit_client import BookOrbitClient
from src.db.database_service import DatabaseService
from src.services.acquisition_service import STALLED_AFTER, AcquisitionService, is_stalled
from src.utils.time_utils import utcnow
from tests.test_acquisition_bookorbit import AcquisitionBase, _bookorbit, _response
from tests.test_acquisition_linking import LinkingBase

_MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/a7e3c9d1b5f2_acquisition_progress_mark.py"
_SVC = "src.services.acquisition_service.utcnow"


def _release(indexer=3, guid="g-1", **extra):
    base = {
        "indexerId": indexer, "indexerName": "Tracker A", "guid": guid, "title": "Example Book",
        "sizeBytes": 2 * 1024 * 1024, "seeders": 12, "leechers": 1, "format": "epub", "formats": ["epub", "pdf"],
        "language": "en", "freeleech": True, "vipOnly": False, "alreadyGrabbed": False,
    }
    base.update(extra)
    return base


def _result(releases=None, indexers=None, media="ebook"):
    return {
        "releases": [_release()] if releases is None else releases,
        "criteria": {"mediaKind": media},
        "indexers": [{"indexerId": 3, "indexerName": "Tracker A", "ok": True, "count": 1, "delivery": "torrent"}]
        if indexers is None else indexers,
    }


class TestFulfilmentClient(unittest.TestCase):
    def setUp(self):
        self.client = BookOrbitClient()
        patcher = patch.object(self.client, "_make_request", return_value=_response(200, {}))
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_can_pick_probes_download_clients(self):
        self.assertTrue(self.client.can_pick_releases())
        self.assertEqual(self.request.call_args.args, ("GET", "/api/v1/book-request-fulfilment/download-clients"))
        self.request.return_value = _response(403)
        self.assertFalse(self.client.can_pick_releases())
        self.request.return_value = None
        self.assertFalse(self.client.can_pick_releases())

    def test_grab_sends_only_indexer_and_guid(self):
        self.request.return_value = _response(200, {"id": 7, "status": "grabbed"})
        item, error = self.client.grab_request_release(7, 3, "g-1")
        self.assertEqual((item, error), ({"id": 7, "status": "grabbed"}, None))
        self.assertEqual(
            self.request.call_args.args,
            ("POST", "/api/v1/book-request-fulfilment/7/grab", {"indexerId": 3, "releaseGuid": "g-1"}))

    def test_status_codes_map_to_error_codes(self):
        cases = [
            (403, {}, "no_pick_permission"), (404, {}, "request_state_changed"), (409, {}, "already_picked"),
            (400, {"message": "A request that is rejected cannot be sent"}, "request_state_changed"),
            (400, {"message": "Set a destination library for this request"}, "no_destination_library"),
            (500, {}, "unreachable"),
        ]
        for status, body, code in cases:
            self.request.return_value = _response(status, body)
            self.assertEqual(self.client.grab_request_release(7, 3, "g"), (None, code), status)
            self.assertEqual(self.client.list_request_releases(7), (None, code), status)
        self.request.return_value = None
        self.assertEqual(self.client.list_request_releases(7), (None, "unreachable"))

    def test_list_route_and_result(self):
        self.request.return_value = _response(200, _result())
        result, error = self.client.list_request_releases(7)
        self.assertIsNone(error)
        self.assertEqual(self.request.call_args.args[:2], ("GET", "/api/v1/book-request-fulfilment/7/releases?refresh=false"))
        self.assertEqual(len(result["releases"]), 1)


class PickerBase(AcquisitionBase):
    def setUp(self):
        super().setUp()
        os.environ["ACQUISITION_ALLOW_READERS"] = "true"
        self.bo.list_request_releases = MagicMock(return_value=(_result(), None))
        self.bo.grab_request_release = MagicMock(return_value=({"id": 11, "status": "grabbed"}, None))
        self.login()

    def add(self, user=None, **extra):
        fields = dict(
            service="BookOrbit", media_kind="ebook", remote_kind="request", remote_id="11", title="Example Book",
            status="in_progress", remote_status="approved", partner_source="BookOrbit", partner_source_id="2167",
            partner_filename="Example Book.m4b", group_key="grp")
        fields.update(extra)
        return self.svc.add_acquisition_requests((user or self.reg).id, [fields])[0]

    def releases(self, row_id):
        return self.client.get(f"/api/acquisition/{row_id}/releases")

    def grab(self, row_id, key="3:g-1"):
        return self.client.post(f"/api/acquisition/{row_id}/grab", json={"key": key})


class TestOptions(PickerBase):
    def test_can_pick_reported_only_when_usable(self):
        service = lambda: self.client.get("/api/acquisition/options").get_json()["services"]["bookorbit"]
        self.assertTrue(service()["can_pick"])
        self.bo.can_pick_releases.return_value = False
        self.assertFalse(service()["can_pick"])
        os.environ["ACQUISITION_ALLOW_READERS"] = "false"
        self.assertNotIn("can_pick", service())


class TestReleaseList(PickerBase):
    def test_release_is_mapped_to_the_picker_shape(self):
        row = self.add()
        data = self.releases(row["id"]).get_json()
        self.bo.list_request_releases.assert_called_once_with("11")
        self.assertEqual(data["indexer_problems"], [])
        self.assertEqual(data["releases"], [{
            "key": "3:g-1", "title": "Example Book", "format": "epub", "formats_more": 1, "size": "2.0 MB",
            "source": "Tracker A", "language": "en", "detail": "", "author": "", "kind": "torrent",
            "group": "3", "group_label": "Tracker A", "peers": "12/1", "flags": ["freeleech"],
        }])

    def test_mixed_delivery_has_no_kind_and_flags_are_short_words(self):
        indexers = [
            {"indexerId": 3, "indexerName": "Tracker A", "ok": True, "delivery": "torrent"},
            {"indexerId": 4, "indexerName": "News", "ok": True, "delivery": "usenet"},
        ]
        self.bo.list_request_releases.return_value = (
            _result([_release(vipOnly=True, alreadyGrabbed=True, seeders=None)], indexers), None)
        release = self.releases(self.add()["id"]).get_json()["releases"][0]
        self.assertEqual((release["kind"], release["peers"], release["flags"]), ("", "", ["freeleech", "VIP", "grabbed before"]))

    def test_empty_results_are_a_normal_200(self):
        self.bo.list_request_releases.return_value = (_result([]), None)
        resp = self.releases(self.add()["id"])
        self.assertEqual((resp.status_code, resp.get_json()["releases"]), (200, []))

    def test_indexer_failure_is_surfaced(self):
        indexers = [{"indexerId": 3, "indexerName": "Tracker A", "ok": False, "failure": "unauthorized", "delivery": "torrent"}]
        self.bo.list_request_releases.return_value = (_result([], indexers), None)
        data = self.releases(self.add()["id"]).get_json()
        self.assertEqual(data["indexer_problems"], [{"name": "Tracker A", "failure": "unauthorized"}])

    def test_other_format_release_is_dropped(self):
        audio = _release(guid="g-2", format="m4b", formats=["m4b"])
        self.bo.list_request_releases.return_value = (_result([_release(), audio]), None)
        keys = [r["key"] for r in self.releases(self.add()["id"]).get_json()["releases"]]
        self.assertEqual(keys, ["3:g-1"])

    def test_media_mismatch_is_refused(self):
        self.bo.list_request_releases.return_value = (_result(media="audiobook"), None)
        resp = self.releases(self.add()["id"])
        self.assertEqual((resp.status_code, resp.get_json()), (409, {"error": "request_media_mismatch"}))

    def test_missing_permission_and_state_errors(self):
        row = self.add()
        for failure, status in (("no_pick_permission", 403), ("request_state_changed", 409), ("unreachable", 502)):
            self.bo.list_request_releases.return_value = (None, failure)
            resp = self.releases(row["id"])
            self.assertEqual((resp.status_code, resp.get_json()), (status, {"error": failure}))

    def test_another_users_row_is_404_and_their_client_is_never_called(self):
        row = self.add(user=self.svc.create_user("other", "pw", role="user"))
        self.assertEqual(self.releases(row["id"]).status_code, 404)
        self.assertEqual(self.grab(row["id"]).status_code, 404)
        self.bo.list_request_releases.assert_not_called()
        self.bo.grab_request_release.assert_not_called()

    def test_non_request_or_closed_rows_are_refused(self):
        shelf = self.add(service="Shelfmark", remote_kind="download")
        self.assertEqual(self.releases(shelf["id"]).status_code, 404)
        done = self.add(remote_id="12", status="paired")
        self.assertEqual(self.releases(done["id"]).get_json(), {"error": "request_state_changed"})
        self.bo.list_request_releases.assert_not_called()

    def test_reader_uses_their_own_client_not_the_admins(self):
        other = _bookorbit()
        other.list_request_releases = MagicMock(return_value=(_result([]), None))
        seen = []

        def clients():
            seen.append("reader")
            return SimpleNamespace(bookorbit_client=self.bo)

        with patch("src.web_server.uc", side_effect=clients):
            self.releases(self.add()["id"])
        self.assertEqual(seen, ["reader"])
        other.list_request_releases.assert_not_called()
        self.bo.list_request_releases.assert_called_once()


class TestGrab(PickerBase):
    def test_success_updates_the_existing_row_in_place(self):
        row = self.add(progress_mark="x", progress_changed_at=utcnow())
        resp = self.grab(row["id"], key="3:https://t.example/dl?id=1:2")
        self.assertEqual(resp.status_code, 200)
        self.bo.grab_request_release.assert_called_once_with("11", 3, "https://t.example/dl?id=1:2")
        after = self.svc.get_acquisition_request(self.reg.id, row["id"])
        self.assertEqual(len(self.svc.get_acquisition_requests(self.reg.id)), 1)
        self.assertEqual((after["remote_status"], after["status"]), ("grabbed", "in_progress"))
        for field in ("partner_source", "partner_source_id", "partner_filename", "group_key", "user_id", "remote_id"):
            self.assertEqual(after[field], row[field], field)
        self.assertEqual((after["progress_mark"], after["progress_changed_at"]), (None, None))
        self.assertEqual(resp.get_json()["request"]["remote_status"], "grabbed")

    def test_invalid_keys_are_400_and_never_reach_bookorbit(self):
        row = self.add()
        for key in ("", "nocolon", "0:g", "-1:g", "abc:g", "3:", "3:" + "x" * 2049, None, 5):
            self.assertEqual(self.grab(row["id"], key=key).get_json(), {"error": "invalid_release"}, key)
        self.assertEqual(self.client.post(f"/api/acquisition/{row['id']}/grab", json=[]).status_code, 400)
        self.bo.grab_request_release.assert_not_called()

    def test_bookbridge_blocks_a_second_pick(self):
        for remote in ("grabbed", "downloading", "importing", "available"):
            row = self.add(remote_status=remote, remote_id=remote)
            self.assertEqual((self.grab(row["id"]).status_code, self.grab(row["id"]).get_json()), (409, {"error": "already_picked"}))
        self.bo.grab_request_release.assert_not_called()

    def test_bookorbit_errors_map_to_http_codes(self):
        row = self.add()
        for failure, status in (
            ("no_pick_permission", 403), ("already_picked", 409), ("no_destination_library", 409),
            ("request_state_changed", 409), ("unreachable", 502),
        ):
            self.bo.grab_request_release.return_value = (None, failure)
            resp = self.grab(row["id"])
            self.assertEqual((resp.status_code, resp.get_json()), (status, {"error": failure}))
        self.assertEqual(self.svc.get_acquisition_request(self.reg.id, row["id"])["remote_status"], "approved")

    def test_changed_request_state_is_refreshed_on_the_next_poll(self):
        row = self.add(last_checked_at=utcnow())
        self.bo.grab_request_release.return_value = (None, "request_state_changed")
        self.grab(row["id"])
        self.assertIsNone(self.svc.get_acquisition_request(self.reg.id, row["id"])["last_checked_at"])
        self.bo.get_request.return_value = {"id": 11, "status": "rejected"}
        mine = self.client.get("/api/acquisition/mine").get_json()["requests"]
        self.assertEqual((mine[0]["remote_status"], mine[0]["status"]), ("rejected", "declined"))


class TestStall(LinkingBase):
    def seed(self, **extra):
        return self.add_row(status="in_progress", remote_status="downloading", **extra)

    def remote(self, bytes_=100, attempt="a1", status="downloading"):
        self.bo.get_request.return_value = {
            "status": status, "download": {"id": attempt, "grabbedAt": "t0", "downloadedBytes": bytes_, "progressPercent": 5},
        }

    def tick(self, when, row_id):
        self.age(row_id)
        with patch(_SVC, return_value=when):
            self.refresh()
        return self.row(row_id)

    def test_mark_set_kept_reset_and_cleared(self):
        row = self.seed()
        t0 = utcnow()
        self.remote(100)
        first = self.tick(t0, row["id"])
        self.assertIsNotNone(first["progress_mark"])
        self.assertEqual(first["progress_changed_at"], t0)
        same = self.tick(t0 + timedelta(minutes=40), row["id"])
        self.assertEqual(same["progress_changed_at"], t0)
        self.remote(200)
        moved = self.tick(t0 + timedelta(minutes=41), row["id"])
        self.assertEqual(moved["progress_changed_at"], t0 + timedelta(minutes=41))
        self.remote(200, attempt="a2")
        again = self.tick(t0 + timedelta(minutes=50), row["id"])
        self.assertEqual(again["progress_changed_at"], t0 + timedelta(minutes=50))
        self.remote(status="importing")
        done = self.tick(t0 + timedelta(minutes=60), row["id"])
        self.assertEqual((done["progress_mark"], done["progress_changed_at"]), (None, None))

    def test_request_stuck_at_grabbed_stalls_and_downloading_resets_it(self):
        row = self.seed()
        t0 = utcnow()
        self.bo.get_request.return_value = {"status": "grabbed", "download": {"id": "a1", "grabbedAt": "t0"}}
        self.tick(t0, row["id"])
        stuck = self.tick(t0 + timedelta(minutes=31), row["id"])
        self.assertEqual(stuck["progress_changed_at"], t0)
        with patch(_SVC, return_value=t0 + timedelta(minutes=31)):
            self.assertTrue(is_stalled(stuck))
        self.remote(500)
        moved = self.tick(t0 + timedelta(minutes=32), row["id"])
        self.assertEqual(moved["progress_changed_at"], t0 + timedelta(minutes=32))

    def test_percent_is_used_when_bytes_are_unknown(self):
        row = self.seed()
        self.bo.get_request.return_value = {"status": "downloading", "download": {"progressPercent": 42}}
        self.assertTrue(self.tick(utcnow(), row["id"])["progress_mark"].endswith("|42"))

    def test_stalled_only_past_thirty_minutes_and_only_in_progress(self):
        now = utcnow()
        row = {"status": "in_progress", "progress_mark": "m", "progress_changed_at": now - STALLED_AFTER + timedelta(seconds=5)}
        self.assertFalse(is_stalled(row))
        row["progress_changed_at"] = now - STALLED_AFTER - timedelta(seconds=5)
        self.assertTrue(is_stalled(row))
        self.assertFalse(is_stalled({**row, "status": "arrived"}))
        self.assertFalse(is_stalled({**row, "progress_mark": None}))

    def test_stalled_flag_is_public(self):
        from src.api.acquisition_routes import _public

        row = self.seed(progress_mark="m", progress_changed_at=utcnow() - timedelta(hours=1))
        self.assertTrue(_public(self.row(row["id"]))["stalled"])
        fresh = self.db.update_acquisition_request(self.user.id, row["id"], progress_changed_at=utcnow())
        self.assertFalse(_public(fresh)["stalled"])

    def test_shelfmark_progress_marks_the_same_way(self):
        row = self.add_row(service="Shelfmark", remote_kind="download", remote_id="rel1", status="in_progress", remote_status="downloading")
        t0 = utcnow()

        def poll(progress, when):
            activity = {"status": {"downloading": {"rel1": {"id": "rel1", "progress": progress}}}, "requests": []}
            shelfmark = SimpleNamespace(get_activity=lambda: (activity, None))
            self.age(row["id"])
            with patch(_SVC, return_value=when):
                self.service.refresh_user(self.user.id, None, shelfmark)
            return self.row(row["id"])

        self.assertEqual(poll(10, t0)["progress_changed_at"], t0)
        self.assertEqual(poll(10, t0 + timedelta(minutes=45))["progress_changed_at"], t0)
        self.assertEqual(poll(11, t0 + timedelta(minutes=46))["progress_changed_at"], t0 + timedelta(minutes=46))


class TestProgressMigration(unittest.TestCase):
    def _load(self):
        spec = importlib.util.spec_from_file_location("progress_migration", _MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_adds_both_columns_idempotently_and_chains_to_the_previous_head(self):
        module = self._load()
        self.assertEqual(module.down_revision, "c4d9a2e6b8f1")
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE TABLE acquisition_requests (id INTEGER PRIMARY KEY)"))
            for _ in range(2):
                with engine.begin() as conn:
                    module.op = Operations(MigrationContext.configure(conn))
                    module.upgrade()
            columns = {c["name"]: c for c in sa.inspect(engine).get_columns("acquisition_requests")}
            self.assertTrue({"progress_mark", "progress_changed_at"} <= set(columns))
            self.assertTrue(columns["progress_mark"]["nullable"])
            with engine.begin() as conn:
                module.op = Operations(MigrationContext.configure(conn))
                module.downgrade()
            self.assertEqual(set(c["name"] for c in sa.inspect(engine).get_columns("acquisition_requests")), {"id"})
        finally:
            engine.dispose()

    def test_fresh_database_reaches_head_with_the_columns(self):
        tmp = tempfile.mkdtemp()
        svc = DatabaseService(os.path.join(tmp, "fresh.db"))
        try:
            columns = {c["name"] for c in sa.inspect(svc.db_manager.engine).get_columns("acquisition_requests")}
            self.assertTrue({"progress_mark", "progress_changed_at"} <= columns)
        finally:
            svc.db_manager.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
