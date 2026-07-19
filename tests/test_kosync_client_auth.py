import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.api.api_clients import KoSyncClient


class _CwaKoSyncHandler(BaseHTTPRequestHandler):
    expected_auth = "Basic " + base64.b64encode(b"reader:cwa-password").decode("ascii")
    last_put = None

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == self.expected_auth

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/kosync/healthcheck":
            self._json_response(404, {})
            return
        if not self._authorized():
            self._json_response(401, {"error": 2001, "message": "Unauthorized"})
            return
        if self.path == "/kosync/syncs/progress/test-connection":
            self._json_response(200, {})
            return
        if self.path == "/kosync/syncs/progress/doc-1":
            self._json_response(200, {
                "document": "doc-1",
                "percentage": 0.42,
                "progress": "/body/DocFragment[1]/body/p[1]/text().0",
            })
            return
        self._json_response(404, {})

    def do_PUT(self) -> None:
        if not self._authorized():
            self._json_response(401, {"error": 2001, "message": "Unauthorized"})
            return
        if self.path != "/kosync/syncs/progress":
            self._json_response(404, {})
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_put = json.loads(self.rfile.read(length))
        self._json_response(200, {"document": "doc-1", "timestamp": 1700000000})

    def log_message(self, format: str, *args) -> None:
        pass


class TestKoSyncClientBasicAuth(unittest.TestCase):
    def setUp(self) -> None:
        _CwaKoSyncHandler.last_put = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CwaKoSyncHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = KoSyncClient(credentials={
            "KOSYNC_ENABLED": "true",
            "KOSYNC_SERVER": f"http://{host}:{port}/kosync",
            "KOSYNC_USER": "reader",
            "KOSYNC_KEY": "cwa-password",
            "KOSYNC_AUTH_METHOD": "basic",
        })

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_cwa_basic_auth_connection_read_and_write(self) -> None:
        self.assertTrue(
            self.client.check_connection(),
            "CWA credentials should not fail with KoSync connection Response: 401",
        )

        percentage, progress, metadata = self.client.get_progress_with_metadata("doc-1")
        self.assertEqual(percentage, 0.42)
        self.assertEqual(progress, "/body/DocFragment[1]/body/p[1]/text().0")
        self.assertEqual(metadata["document"], "doc-1")

        self.assertTrue(self.client.update_progress("doc-1", 0.5, progress))
        self.assertEqual(_CwaKoSyncHandler.last_put["document"], "doc-1")
        self.assertEqual(_CwaKoSyncHandler.last_put["percentage"], 0.5)


if __name__ == "__main__":
    unittest.main()
