import threading
import time

from src.services.sync_dispatcher import SyncDispatcher


def test_sync_dispatcher_coalesces_pending_requests():
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = []

    def _run(request):
        calls.append(request.reason)
        if len(calls) == 1:
            started.set()
            release.wait(timeout=2)
        else:
            completed.set()

    dispatcher = SyncDispatcher(_run)
    dispatcher.request_sync("book-1", reason="first")
    assert started.wait(timeout=2)

    dispatcher.request_sync("book-1", reason="second")
    dispatcher.request_sync("book-1", reason="third")

    release.set()

    deadline = time.time() + 2
    while time.time() < deadline and len(calls) < 2:
        time.sleep(0.01)

    assert calls[0] == "first"
    assert len(calls) == 2
    assert "second" in calls[1]
    assert "third" in calls[1]
    assert completed.is_set()
