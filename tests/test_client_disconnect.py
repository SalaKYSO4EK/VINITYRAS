"""Cancelling a response must not turn its already-sent status into a second 500."""
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from server import Handler


class ClientDisconnectTest(unittest.TestCase):
    def request(self, path):
        handler = object.__new__(Handler)
        handler.path = path
        handler.headers = {}
        handler.server = SimpleNamespace(jobs=SimpleNamespace(configured=True, pending=Mock()), frontend_dir=None)
        handler.server.jobs.pending.qsize.return_value = 0
        handler._check_origin = Mock()
        handler._discard_body = Mock()
        handler._headers = Mock()
        handler.wfile = BytesIO()
        return handler

    def test_browser_abort_during_json_file_or_error_response(self):
        for path, expected in (("/api/health", 200), ("/app.js", 200), ("/unknown", 404)):
            for phase in ("headers", "body"):
                with self.subTest(path=path, phase=phase), patch("server.logging.exception") as log:
                    handler = self.request(path)
                    if phase == "body":
                        handler.wfile = Mock()
                        handler.wfile.write.side_effect = ConnectionAbortedError(10053, "browser cancelled")
                    else:
                        handler._headers.side_effect = ConnectionAbortedError(10053, "browser cancelled")
                    handler._handle("GET")
                    self.assertEqual([call.args[0] for call in handler._headers.call_args_list], [expected])
                    log.assert_not_called()

    def test_live_client_receives_real_api_errors(self):
        handler = self.request("/unknown")
        handler._handle("GET")
        self.assertEqual(handler._headers.call_args.args[0], 404)
        self.assertEqual(json.loads(handler.wfile.getvalue())["error"]["code"], "not_found")
        handler = self.request("/api/jobs/unused")
        handler._read_job = Mock(side_effect=RuntimeError("real handler failure"))
        with patch("server.logging.exception") as log:
            handler._handle("GET")
            log.assert_called_once()
        self.assertEqual(handler._headers.call_args.args[0], 500)
        self.assertEqual(json.loads(handler.wfile.getvalue())["error"]["code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
