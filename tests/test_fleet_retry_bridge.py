#!/usr/bin/env python3
import http.client
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
SPEC = importlib.util.spec_from_file_location("fleet_retry_bridge", ROOT / "lib" / "fleet_retry_bridge.py")
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class UpstreamHandler(BaseHTTPRequestHandler):
    calls = 0
    responses = []
    protocol_version = "HTTP/1.1"
    status = 200
    body = b"ok"
    headers = {}
    first_chunk = None
    finish_stream = None
    truncate = False
    authorizations = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "websocket")
            self.end_headers()
            self.wfile.write(self.rfile.read(4))
            self.wfile.flush()
            return
        self.respond()

    def do_POST(self):
        type(self).authorizations = self.headers.get_all("Authorization", [])
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.respond()

    def respond(self):
        type(self).calls += 1
        if type(self).responses:
            response = type(self).responses.pop(0)
            type(self).status, type(self).body, type(self).headers = response
        self.send_response(type(self).status)
        for name, value in type(self).headers.items():
            self.send_header(name, value)
        if type(self).first_chunk is None:
            self.send_header("Content-Length", str(len(type(self).body) + (5 if type(self).truncate else 0)))
            self.end_headers()
            self.wfile.write(type(self).body)
            if type(self).truncate:
                self.close_connection = True
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = type(self).first_chunk
        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.wfile.flush()
        type(self).finish_stream.wait(2)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class RetryBridgeTest(unittest.TestCase):
    def setUp(self):
        UpstreamHandler.status = 200
        UpstreamHandler.body = b"ok"
        UpstreamHandler.headers = {}
        UpstreamHandler.calls = 0
        UpstreamHandler.responses = []
        UpstreamHandler.first_chunk = None
        UpstreamHandler.finish_stream = None
        UpstreamHandler.truncate = False
        UpstreamHandler.authorizations = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.policy = Path(self.temporary.name) / "account-routing.json"
        self.policy.write_text(json.dumps({"version": 1, "providers": {"claude": "limits", "codex": "limits"}}))
        self.bridge = BRIDGE.RetryBridgeServer(
            ("127.0.0.1", 0), BRIDGE.RetryBridgeHandler, self.upstream.server_port, policy_path=self.policy
        )
        self.bridge_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.bridge_thread.start()

    def tearDown(self):
        self.bridge.shutdown()
        self.bridge.server_close()
        self.bridge_thread.join()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()
        self.temporary.cleanup()

    def request(self, method="POST", path="/v1/responses", body=b"{}", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        connection.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_provider_requests_use_only_the_local_router_key_upstream(self):
        self.bridge.client_key = "router-key"

        for path in ("/v1/responses", "/v1/messages"):
            with self.subTest(path=path):
                status, _headers, _body = self.request(
                    path=path, headers={"authorization": "Bearer native-provider-token"}
                )

                self.assertEqual(status, 200)
                self.assertEqual(UpstreamHandler.authorizations, ["Bearer router-key"])

    def test_structured_codex_quota_retries_before_output(self):
        bodies = (
            b'{"error":{"type":"usage_limit_reached","resets_in_seconds":60}}',
            b'{"type":"usage_limit_reached","message":"limit"}',
        )
        for body in bodies:
            with self.subTest(body=body):
                UpstreamHandler.calls = 0
                UpstreamHandler.responses = [
                    (429, body, {"Content-Type": "application/json", "Retry-After": "0"}),
                    (200, b"recovered", {"Content-Type": "text/plain"}),
                ]

                status, headers, received = self.request()

                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "text/plain")
                self.assertEqual(received, b"recovered")
                self.assertEqual(UpstreamHandler.calls, 2)

    def test_long_reset_hints_wait_at_most_five_minutes(self):
        body = b'{"error":{"type":"usage_limit_reached","resets_at":4102444800,"resets_in_seconds":444419}}'
        self.assertEqual(BRIDGE.quota_retry_delay("/v1/responses", 429, {}, body), 300)
        self.assertEqual(BRIDGE.quota_retry_delay("/v1/responses", 429, {"Retry-After": "444419"}, body), 300)

    def test_structured_claude_limit_retries_before_output(self):
        UpstreamHandler.responses = [
            (429, b'{"error":{"type":"rate_limit_error"}}', {"Content-Type": "application/json", "Retry-After": "0"}),
            (200, b"recovered", {"Content-Type": "text/plain"}),
        ]

        status, headers, received = self.request(path="/v1/messages")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain")
        self.assertEqual(received, b"recovered")
        self.assertEqual(UpstreamHandler.calls, 2)

    def test_claude_overload_does_not_retry_in_limits_mode(self):
        body = b'{"error":{"type":"overloaded_error"}}'
        UpstreamHandler.status = 529
        UpstreamHandler.body = body

        status, _headers, received = self.request(path="/v1/messages")

        self.assertEqual(status, 529)
        self.assertEqual(received, body)
        self.assertEqual(UpstreamHandler.calls, 1)

    def test_claude_overload_retries_when_any_is_explicit(self):
        self.policy.write_text(json.dumps({"version": 1, "providers": {"claude": "any", "codex": "limits"}}))
        UpstreamHandler.responses = [
            (529, b'{"error":{"type":"overloaded_error"}}', {"Content-Type": "application/json", "Retry-After": "0"}),
            (200, b"recovered", {"Content-Type": "text/plain"}),
        ]

        status, _headers, received = self.request(path="/v1/messages")

        self.assertEqual(status, 200)
        self.assertEqual(received, b"recovered")
        self.assertEqual(UpstreamHandler.calls, 2)

    def test_unrelated_errors_and_paths_stay_unchanged(self):
        cases = (
            ("/v1/responses", b'{"error":{"type":"rate_limit_error"}}'),
            ("/v1/models", b'{"error":{"type":"usage_limit_reached"}}'),
            ("/v1/messages", b'{"error":{"type":"usage_limit_reached"}}'),
            ("/v1/responses", b"not json"),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body):
                UpstreamHandler.status = 429
                UpstreamHandler.body = body
                UpstreamHandler.headers = {"Content-Type": "application/json"}

                status, _headers, received = self.request(path=path)

                self.assertEqual(status, 429)
                self.assertEqual(received, body)

    def test_successful_stream_is_forwarded_before_it_ends(self):
        finished = threading.Event()
        UpstreamHandler.first_chunk = b"first"
        UpstreamHandler.finish_stream = finished
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        connection.request("POST", "/v1/responses", body=b"{}")
        response = connection.getresponse()

        started = time.monotonic()
        received = response.read(5)

        self.assertEqual(response.status, 200)
        self.assertEqual(received, b"first")
        self.assertLess(time.monotonic() - started, 1)
        finished.set()
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_existing_websocket_sessions_pass_through(self):
        connection = socket.create_connection(("127.0.0.1", self.bridge.server_port), timeout=3)
        connection.sendall(
            b"GET /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n\r\n"
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += connection.recv(4096)

        connection.sendall(b"PING")

        self.assertIn(b" 101 ", response)
        self.assertEqual(connection.recv(4), b"PING")
        connection.close()

    def test_client_disconnect_cancels_a_pending_quota_retry(self):
        UpstreamHandler.responses = [
            (
                429,
                b'{"error":{"type":"usage_limit_reached"}}',
                {"Content-Type": "application/json", "Retry-After": "1"},
            ),
            (200, b"must not run", {"Content-Type": "text/plain"}),
        ]
        connection = socket.create_connection(("127.0.0.1", self.bridge.server_port), timeout=3)
        connection.sendall(
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        deadline = time.monotonic() + 2
        while UpstreamHandler.calls != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        connection.close()

        time.sleep(1.2)

        self.assertEqual(UpstreamHandler.calls, 1)

    def test_truncated_upstream_does_not_append_a_second_response(self):
        UpstreamHandler.body = b"first"
        UpstreamHandler.truncate = True
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()

        with self.assertRaises(http.client.IncompleteRead) as error:
            response.read()

        self.assertEqual(error.exception.partial, b"first")
        connection.close()

    def test_clean_child_exit_still_restarts_the_managed_service(self):
        child = type("Child", (), {"returncode": 0, "poll": lambda _self: 0})()
        self.bridge.child = child

        for action in (self.bridge.service_actions, lambda: BRIDGE.wait_for_upstream(child)):
            with self.subTest(action=action):
                with self.assertRaises(SystemExit) as error:
                    action()

                self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
