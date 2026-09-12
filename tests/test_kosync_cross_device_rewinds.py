import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from flask import Flask

from src.api import kosync_server
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.time_utils import utcnow


_DOC_HASH = "a" * 32
_ROOT = Path(__file__).resolve().parents[1]


class _FakeDatabase:
    def __init__(self, *, percentage=0.50, device_id="reader-a"):
        self.doc = SimpleNamespace(
            document_hash=_DOC_HASH,
            linked_abs_id=None,
            progress="/body/original",
            percentage=percentage,
            device="KOReader",
            device_id=device_id,
            timestamp=utcnow(),
            user_id=None,
            filename=None,
            source=None,
            booklore_id=None,
            mtime=None,
        )
        self.saved = []
        self.user_progress_updates = []

    def get_kosync_document(self, _document_hash):
        return self.doc

    def get_user_kosync_progress(self, _document_hash, _user_id):
        return self.doc

    def save_kosync_document(self, document):
        self.saved.append((float(document.percentage), document.device_id))
        self.doc = document
        return document

    def upsert_user_kosync_progress(
        self, document_hash, percentage, *, progress, device, device_id,
        timestamp, user_id,
    ):
        self.user_progress_updates.append(
            (document_hash, float(percentage), progress, device, device_id, user_id)
        )

    def get_book_by_kosync_id(self, _document_hash):
        return None


class TestKoSyncCrossDeviceRewinds:
    @staticmethod
    def _put(db, *, percentage, device_id, furthest_wins):
        app = Flask(__name__)
        payload = {
            "document": _DOC_HASH,
            "progress": f"/body/{device_id}/{percentage}",
            "percentage": percentage,
            "device": "KOReader",
            "device_id": device_id,
        }

        with app.test_request_context("/syncs/progress", method="PUT", json=payload):
            with patch.dict(
                os.environ,
                {"KOSYNC_FURTHEST_WINS": furthest_wins},
                clear=False,
            ), patch.object(
                kosync_server, "_database_service", db
            ), patch.object(
                kosync_server, "_flush_stale_kosync_sessions"
            ), patch.object(
                kosync_server, "_record_recent_external_kosync_put"
            ), patch.object(
                kosync_server, "_schedule_auto_discovery"
            ):
                return kosync_server.kosync_put_progress.__wrapped__()

    @staticmethod
    def _settings_template_source():
        return (_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

    def test_setting_is_managed_and_defaults_to_safe_behavior(self):
        assert "KOSYNC_FURTHEST_WINS" in ALL_SETTINGS
        assert DEFAULT_CONFIG["KOSYNC_FURTHEST_WINS"] == "true"

    def test_safe_default_rejects_backward_put_from_different_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-b",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.50
        assert db.saved == []
        assert db.user_progress_updates == []

    def test_safe_default_still_accepts_intentional_rewind_on_same_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-a",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.30
        assert db.saved == [(0.30, "reader-a")]
        assert db.user_progress_updates[0][1] == 0.30

    def test_rewind_is_allowed_when_the_higher_position_was_never_claimed(self):
        """The #215 deadlock, live-diagnosed on a real install.

        A device's identity is only recorded when a PUT is ACCEPTED, but a backward
        PUT is only accepted from an already-recorded device. So a reader that has
        only ever RECEIVED positions from BookBridge can never rewind: its rewind is
        judged against an echo of our own write-back, with no device_id attached.
        Measured on the developer's library: 424 of 479 linked documents had no
        device_id at all, so this was nearly every book.

        Furthest-wins defends one DEVICE against another. With nobody claiming the
        higher position there is no peer to defend, so it must not fire."""
        db = _FakeDatabase(percentage=0.50, device_id=None)
        db.doc.device = None

        _response, status = self._put(
            db,
            percentage=0.25,
            device_id="reader-b",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.25
        assert db.saved == [(0.25, "reader-b")]
        assert db.user_progress_updates[0][1] == 0.25

    def test_rewind_is_allowed_when_the_higher_position_is_our_own_sync_bot(self):
        """Same shape, but the write-back did stamp the internal device id."""
        db = _FakeDatabase(percentage=0.50, device_id="abs-sync-bot")
        db.doc.device = "abs-sync-bot"

        _response, status = self._put(
            db,
            percentage=0.25,
            device_id="reader-b",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.25

    def test_a_real_peer_device_still_blocks_a_rewind(self):
        """The protection itself is unchanged: once another real device has claimed
        the position, furthest-wins still defends it."""
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.25,
            device_id="reader-b",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.50
        assert db.saved == []

    def test_opt_in_accepts_backward_put_from_different_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-b",
            furthest_wins="false",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.30
        assert db.saved == [(0.30, "reader-b")]
        assert db.user_progress_updates[0][1] == 0.30

    def test_ui_renders_safe_default_and_explicit_opt_in(self):
        template_source = self._settings_template_source()

        assert 'name="KOSYNC_FURTHEST_WINS"' in template_source
        assert 'value="true"' in template_source
        assert 'value="false"' in template_source
        assert "get_val('KOSYNC_FURTHEST_WINS'" in template_source
        assert "out-of-date second" in template_source

    def test_setting_lives_in_the_main_settings_template(self):
        settings_template = (_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        base_template = (_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        partial_path = _ROOT / "templates" / "_kosync_cross_device_rewinds.html"

        assert 'name="KOSYNC_FURTHEST_WINS"' in settings_template
        assert "KOSYNC_FURTHEST_WINS" not in base_template
        assert "_kosync_cross_device_rewinds" not in base_template
        assert not partial_path.exists()
        assert "kosync_cross_device_rewinds_template" not in settings_template

    def test_checkbox_style_truthy_spellings_keep_the_protection_on(self):
        truthy_spellings = ["on", "1", "yes", "On", "TRUE"]
        for value in truthy_spellings:
            # env_truthy("on"), env_truthy("1"), env_truthy("yes"), etc. all return True.
            # Previously, == "true" treated these as False and silently disabled the guard.
            # The failure direction is unsafe: backward cross-device PUT would be accepted.
            db = _FakeDatabase(percentage=0.50, device_id="reader-a")

            _response, status = self._put(
                db,
                percentage=0.30,
                device_id="reader-b",
                furthest_wins=value,
            )

            assert status == 200, f"value={value}"
            assert float(db.doc.percentage) == 0.50, f"value={value}"
            assert db.saved == [], f"value={value}"
            assert db.user_progress_updates == [], f"value={value}"

    def test_falsy_spellings_still_allow_the_backward_move(self):
        falsy_spellings = ["false", "0", "no", "off"]
        for value in falsy_spellings:
            db = _FakeDatabase(percentage=0.50, device_id="reader-a")

            _response, status = self._put(
                db,
                percentage=0.30,
                device_id="reader-b",
                furthest_wins=value,
            )

            assert status == 200, f"value={value}"
            assert float(db.doc.percentage) == 0.30, f"value={value}"
            assert db.saved == [(0.30, "reader-b")], f"value={value}"
            assert db.user_progress_updates[0][1] == 0.30, f"value={value}"


class TestRecentExternalPutMarkerReachesLinkedBooks:
    """The `_kosync_recent_external_put` marker must reach a LINKED book's response.

    `_determine_leader` has a path built for exactly this signal — "Trusting recent
    external KoSync PUT during zero-delta discrepancy resolution". It never fired on
    a real install. The reason is routing, not the signal: Step 1 of the GET handler
    resolves a linked book and returns `_respond_from_book_states(...)` immediately,
    and that function's normal exit was the only response that did not attach the
    marker. Every book the bridge actually syncs takes that exit.
    """

    @staticmethod
    def _respond(monkeypatch_targets, doc_hash, latest_pct, recorded_pct):
        import time as _time
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from flask import Flask

        book = SimpleNamespace(abs_id="abs-1", abs_title="Linked Book")
        state = SimpleNamespace(
            client_name="kosync", percentage=latest_pct, xpath="/body/p[1].0",
            cfi=None, last_updated=_time.time(),
        )
        db = MagicMock()
        db.get_states_for_book.return_value = [state]
        db.get_user_kosync_progress_for_book.return_value = []
        db.get_kosync_documents_for_book.return_value = []

        app = Flask(__name__)
        with app.test_request_context("/syncs/progress/" + doc_hash):
            with patch.object(kosync_server, "_database_service", db), \
                 patch.object(kosync_server, "_resolve_book_by_sibling_hash", return_value=None), \
                 patch.object(kosync_server, "_suppress_empty_progress_response", return_value=None):
                kosync_server._record_recent_external_kosync_put(
                    doc_hash, "Kobo", "dev-1", recorded_pct, _time.time(), None,
                )
                response, status = kosync_server._respond_from_book_states(doc_hash, book)
        return response.get_json(), status

    def test_marker_is_attached_when_the_returned_position_is_the_device_put(self):
        payload, status = self._respond(None, "b" * 32, latest_pct=0.25, recorded_pct=0.25)
        assert status == 200
        assert payload.get("_bridge_recent_external_put") is True
        assert payload.get("_bridge_recent_external_put_device") == "Kobo"

    def test_marker_is_withheld_when_the_returned_position_is_not_the_device_put(self):
        """The guard that keeps a bridge-synced position from being labelled a
        device report."""
        payload, status = self._respond(None, "c" * 32, latest_pct=0.60, recorded_pct=0.25)
        assert status == 200
        assert "_bridge_recent_external_put" not in payload


class TestStaleDevicePositionDoesNotResurrect:
    """A bridge-synced BACKWARD move must not be undone by the next GET.

    `upsert_user_kosync_progress` runs only for external PUTs, so the bridge's own
    sync-push advances the synced State but never the per-user row. After an audio
    rewind propagates to the ebook side, that row still holds the device's last
    self-reported (higher) position, and the GET used to hand it straight back.

    Live sequence on 'Children of Memory':
        18:42:22  Readest PUT            -> user_progress 51.52%
        18:49:07  audio rewind propagates -> synced State 31.96%
        18:50:14  GET returned 51.52%, KoSync "changed", everything dragged forward
    """

    @staticmethod
    def _get(synced_pct, synced_epoch, device_pct, device_dt, rewind_at=None, canonical=False):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from flask import Flask

        book = SimpleNamespace(abs_id="abs-1", abs_title="Book", kosync_doc_id=_DOC_HASH,
                               ebook_filename="book.epub")
        synced = SimpleNamespace(
            client_name="kosync", percentage=synced_pct, xpath="/body/synced.0",
            cfi=None, last_updated=synced_epoch,
            locator_json=json.dumps({"kosync_approved_rewind_at": rewind_at}),
        )
        device_row = SimpleNamespace(
            document_hash=_DOC_HASH, percentage=device_pct,
            progress="/body/device.0", timestamp=device_dt,
        )
        db = MagicMock()
        db.get_states_for_book.return_value = [synced]
        db.get_user_kosync_progress_for_book.return_value = [device_row]
        db.get_kosync_document.return_value = SimpleNamespace(filename="book.epub")
        container = MagicMock()
        container.ebook_parser.return_value.resolve_xpath_to_index.side_effect = [490094, 489851]

        app = Flask(__name__)
        with app.test_request_context(f"/syncs/progress/{_DOC_HASH}"):
            with patch.object(kosync_server, "_database_service", db), \
                 patch.object(kosync_server, "_container", container), \
                 patch.object(kosync_server, "_xpath_index_cache_get", return_value=None), \
                 patch.object(kosync_server, "_xpath_index_cache_put"), \
                 patch.dict(os.environ, {"KOSYNC_XPATH_ORDER_ENABLED": str(canonical).lower()}), \
                 patch.object(kosync_server, "_suppress_empty_progress_response", return_value=None):
                response, status = kosync_server._respond_from_book_states(_DOC_HASH, book)
        return response.get_json(), status

    def test_an_ahead_but_older_device_position_is_refused(self):
        import time as _time
        from datetime import timedelta

        now = _time.time()
        payload, status = self._get(
            synced_pct=0.3196, synced_epoch=now,
            device_pct=0.5152, device_dt=utcnow() - timedelta(minutes=7),
            rewind_at=now,
        )
        assert status == 200
        assert payload["percentage"] == 0.3196

    def test_an_ahead_and_newer_device_position_still_wins(self):
        """The case this branch exists for is preserved."""
        import time as _time
        from datetime import timedelta

        payload, status = self._get(
            synced_pct=0.3196, synced_epoch=_time.time() - 600,
            device_pct=0.5152, device_dt=utcnow(),
        )
        assert status == 200
        assert payload["percentage"] == 0.5152

    def test_434_older_readest_position_survives_unapproved_locator_drift(self):
        import time as _time
        from datetime import timedelta

        payload, status = self._get(
            synced_pct=0.6237, synced_epoch=_time.time(),
            device_pct=0.6272, device_dt=utcnow() - timedelta(minutes=7),
        )
        assert status == 200
        assert payload["percentage"] == 0.6272
        assert payload["progress"] == "/body/device.0"

    def test_ordinary_sync_does_not_extend_an_earlier_rewind_cutoff(self):
        import time as _time
        from datetime import timedelta

        now = _time.time()
        payload, status = self._get(
            synced_pct=0.6237, synced_epoch=now,
            device_pct=0.6272, device_dt=utcnow() - timedelta(minutes=3),
            rewind_at=now - 600,
        )
        assert status == 200
        assert payload["percentage"] == 0.6272

    @pytest.mark.parametrize("approved", [False, True])
    def test_xpath_order_respects_intentional_rewind_but_protects_against_drift(self, approved):
        import time as _time
        from datetime import timedelta

        now = _time.time()
        payload, status = self._get(
            synced_pct=0.6237, synced_epoch=now,
            device_pct=0.6272, device_dt=utcnow() - timedelta(minutes=7),
            rewind_at=now if approved else None, canonical=True,
        )
        assert status == 200
        assert payload["percentage"] == (0.6237 if approved else 0.6272)
