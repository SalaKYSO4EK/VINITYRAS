"""Check criteria settings and the HTTP exchange with a local Qwen stand-in."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))

from article_analyzer.qwen_api import QwenApi, QwenApiError


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def completion(content, reason="stop"):
    return json_bytes({"choices": [{"finish_reason": reason, "message": {"content": content}}]})


@contextmanager
def endpoint(body, *, status=200, delay=False, missing_bytes=0):
    requests = []
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Content-Type"], payload))
            if delay:
                release.wait(3)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + missing_bytes))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The client returned as soon as its timeout expired.

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 2
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            release.set()
            thread.join(3)


class QwenSettingsTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "qwen-api.json"
        self.enterContext(patch.dict(os.environ, {
            "NEXUS_QWEN_API_BASE": "", "NEXUS_QWEN_API_MODEL": "", "NEXUS_QWEN_API_TIMEOUT": "",
        }))

    def test_local_default_and_explicit_remote_override(self):
        self.assertIsNone(QwenApi.configured(self.path))
        self.path.write_text(json.dumps({"base_url": "http://server:8080/v1/", "model": "Qwen2.5"}),
                             encoding="utf-8-sig")
        self.assertEqual(QwenApi.configured(self.path), QwenApi("http://server:8080/v1", "Qwen2.5", 180))
        with patch.dict(os.environ, {"NEXUS_QWEN_API_MODEL": "Qwen3", "NEXUS_QWEN_API_TIMEOUT": "75"}):
            self.assertEqual(QwenApi.configured(self.path), QwenApi("http://server:8080/v1", "Qwen3", 75))

    def test_one_environment_setting_cannot_silently_select_local(self):
        for name in ("NEXUS_QWEN_API_BASE", "NEXUS_QWEN_API_MODEL", "NEXUS_QWEN_API_TIMEOUT"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "60"}), self.assertRaises(QwenApiError):
                QwenApi.configured(self.path)

    def test_invalid_json_missing_identity_address_and_timeout(self):
        valid = {"base_url": "http://server:8080/v1", "model": "Qwen"}
        documents = ["{?", "[]", "{}", json.dumps({"base_url": valid["base_url"]}),
                     json.dumps({**valid, "modle": "typo"}), json.dumps({**valid, "model": " "})]
        for url in ("http://server", "file:///C:/v1", "http://user:pwd@server/v1", "http://server:65536/v1"):
            documents.append(json.dumps({**valid, "base_url": url}))
        for timeout in (0, -2, True, "nan"):
            documents.append(json.dumps({**valid, "timeout_seconds": timeout}))
        for document in documents:
            with self.subTest(document=document), self.assertRaises(QwenApiError):
                self.path.write_text(document, encoding="utf-8")
                QwenApi.configured(self.path)


class QwenHttpTest(unittest.TestCase):
    def test_qwen3_receives_russian_criteria_and_schema(self):
        instruction = "Извлеки КПД с единицей и источником."
        schema = {"type": "object", "properties": {"правило": {"type": "string"}}}
        answer = '{"правило":"КПД в процентах"}'
        with endpoint(completion(answer)) as (url, calls):
            raw, status = QwenApi(url, "Qwen3-4B", 2).complete("Верни JSON.", instruction, schema, 198)
        self.assertEqual((raw, status), (answer, 0))
        self.assertEqual(len(calls), 1)
        path, content_type, payload = calls[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertIn("application/json", content_type)
        self.assertEqual(payload["model"], "Qwen3-4B")
        self.assertEqual(payload["messages"], [{"role": "system", "content": "Верни JSON."},
                                               {"role": "user", "content": instruction}])
        self.assertEqual(payload["max_tokens"], 198)
        self.assertEqual(payload["temperature"], 0)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])

    def test_ordinary_request_uses_json_object(self):
        with endpoint(completion("{}")) as (url, calls):
            QwenApi(url, "Qwen2.5-1.5B", 2).complete("s", "u", None, 40)
        self.assertEqual(calls[0][2]["response_format"], {"type": "json_object"})
        self.assertNotIn("chat_template_kwargs", calls[0][2])

    def test_server_errors_reach_user_without_the_response_body(self):
        for code in (400, 401, 404, 429, 503, 504):
            with self.subTest(code=code), endpoint(b"private server log", status=code) as (url, _):
                with self.assertRaises(QwenApiError) as caught:
                    QwenApi(url, "model", 2).complete("s", "private request", None, 4)
                self.assertIn("timeout_seconds" if code == 504 else f"HTTP {code}", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    def test_timeout_is_distinct_from_an_unavailable_host(self):
        with endpoint(b"{}", delay=True) as (url, calls):
            with self.assertRaisesRegex(QwenApiError, "timeout_seconds"):
                QwenApi(url, "model", 0.2).complete("s", "u", None, 4)
            self.assertEqual(len(calls), 1)
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))  # Reserve a port without a listening server.
            url = f"http://127.0.0.1:{unavailable.getsockname()[1]}/v1"
            with self.assertRaisesRegex(QwenApiError, "связаться"):
                QwenApi(url, "model", 5).complete("s", "u", None, 4)

    def test_malformed_envelope_and_token_limit(self):
        for body in (b"broken", b"{}", b'{"choices":[]}', completion(None), completion(" ")):
            with self.subTest(body=body), endpoint(body) as (url, _):
                with self.assertRaisesRegex(QwenApiError, "формата"):
                    QwenApi(url, "model", 2).complete("s", "u", None, 4)
        with endpoint(completion('{"partial":', "length")) as (url, _):
            with self.assertRaisesRegex(QwenApiError, "лимиту токенов"):
                QwenApi(url, "model", 2).complete("s", "u", None, 4)

    def test_http_truncation_rejects_even_a_parseable_prefix(self):
        with endpoint(completion("{}"), missing_bytes=20) as (url, calls):
            with self.assertRaises(QwenApiError):
                QwenApi(url, "model", 2).complete("s", "u", None, 4)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
