"""Regression coverage for audiobook-only sync cycles."""

from unittest.mock import MagicMock

from src.db.models import Book
from src.sync_clients.sync_client_interface import ServiceState
from src.sync_manager import SyncManager


def test_audio_only_cycle_uses_percentage_without_locating_epub(tmp_path):
    """An audiobook-only mapping can persist audio progress without an EPUB."""
    database_service = MagicMock()
    book = Book(
        abs_id="audio-only-1",
        abs_title="Audio Only",
        audio_source="ABS",
        audio_source_id="audio-only-1",
        audio_duration=1200,
        duration=1200,
        sync_mode="audiobook_only",
        status="active",
    )
    database_service.get_books_by_status.return_value = [book]
    database_service.get_states_for_book.return_value = []

    audio_client = MagicMock()
    audio_client.fetch_bulk_state.return_value = None
    audio_client.get_supported_sync_types.return_value = {"audiobook"}
    audio_client.supports_book.return_value = True
    audio_client.can_be_leader.return_value = True
    audio_client.get_service_state.return_value = ServiceState(
        current={"pct": 0.5, "ts": 600.0},
        previous_pct=0.0,
        delta=600.0,
        threshold=60.0,
        is_configured=True,
        display=("ABS", "{prev:.1%} -> {curr:.1%}"),
        value_formatter=lambda value: f"{value:.1%}",
        value_seconds_formatter=lambda value: f"{value:.1f}s",
    )

    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=database_service,
        sync_clients={"ABS": audio_client},
        data_dir=tmp_path,
        books_dir=tmp_path,
    )
    manager._determine_leader = MagicMock(return_value=("ABS", 0.5))
    manager._get_local_epub = MagicMock()

    manager._sync_cycle_internal()

    manager._get_local_epub.assert_not_called()
    assert database_service.save_state.called
    saved_state = database_service.save_state.call_args[0][0]
    assert saved_state.abs_id == "audio-only-1"
    assert saved_state.client_name == "abs"
    assert saved_state.percentage == 0.5


def test_cycle_keeps_every_successful_bulk_read_and_drops_failures(tmp_path):
    """fetch_bulk_state: a dict (even empty) is a successful read for any client; None is a failed one."""
    clients = {"ABS": MagicMock(), "Storyteller": MagicMock(), "BookFusion": MagicMock()}
    clients["ABS"].fetch_bulk_state.return_value = {}
    clients["Storyteller"].fetch_bulk_state.return_value = {}
    clients["BookFusion"].fetch_bulk_state.return_value = None
    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=MagicMock(),
        sync_clients=clients,
        data_dir=tmp_path,
        books_dir=tmp_path,
    )
    manager._filter_books_for_current_user = MagicMock(return_value=[])

    manager._sync_cycle_internal()

    assert manager._filter_books_for_current_user.call_args[0][1] == {"ABS": {}, "Storyteller": {}}
