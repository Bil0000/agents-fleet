#!/usr/bin/env python3
import fcntl
import http.client
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from unittest.mock import patch
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
    header_delay = 0
    header_delays = []
    stream_chunks = []
    stream_interval = 0
    header_progress_call = None

    def log_message(self, *_args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
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
        type(self).seen = list(self.headers.keys())
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.respond()

    def respond(self):
        type(self).calls += 1
        time.sleep(type(self).header_delays.pop(0) if type(self).header_delays else type(self).header_delay)
        if type(self).responses:
            response = type(self).responses.pop(0)
            type(self).status, type(self).body, type(self).headers = response
        self.send_response(type(self).status)
        for name, value in type(self).headers.items():
            self.send_header(name, value)
        if type(self).calls == type(self).header_progress_call:
            for _ in range(10):
                self.send_header("X-Test-Progress", "1")
                self.flush_headers()
                time.sleep(0.04)
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
        for chunk in type(self).stream_chunks:
            time.sleep(type(self).stream_interval)
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
        UpstreamHandler.header_delay = 0
        UpstreamHandler.header_delays = []
        UpstreamHandler.stream_chunks = []
        UpstreamHandler.stream_interval = 0
        UpstreamHandler.header_progress_call = None
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

    def test_updates_require_auth_and_idle_requests_and_release_the_gate(self):
        self.bridge.management_key = "update-key"
        path = "/v0/management/fleet-update"
        headers = {"Authorization": "Bearer update-key"}
        self.assertEqual(self.request(path=path, body=b'{"drain":true}')[0], 403)
        self.bridge.active_requests = 1
        self.assertEqual(self.request(path=path, body=b'{"drain":true}', headers=headers)[0], 409)
        self.bridge.active_requests = 0
        self.assertEqual(self.request(path=path, body=b'{"drain":true}', headers=headers)[0], 200)
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.request(path="/backend-api/codex/responses")[0], 503)
        self.assertEqual(self.request(path="/future/native/provider")[0], 503)
        for management in ("/v0/management/plugins", "/v8/management/plugins"):
            self.assertEqual(self.request(method="GET", path=management, body=None)[0], 200)
        self.assertEqual(self.request(path=path, body=b'{"drain":false}', headers=headers)[0], 200)
        self.assertEqual(self.request()[0], 200)
        deadline = time.monotonic() + 1
        while self.bridge.active_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.bridge.active_requests, 0)

    def test_expired_gate_releases_requests_but_activation_lock_survives_a_restart(self):
        self.bridge.management_key = "update-key"
        self.bridge.drain_until = time.monotonic() - 1
        self.assertEqual(self.request()[0], 200)
        with (self.policy.parent / "cliproxyapi-activation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.request()[0], 503)
            replacement = BRIDGE.RetryBridgeServer(("127.0.0.1", 0), BRIDGE.RetryBridgeHandler, self.upstream.server_port, policy_path=self.policy)
            thread = threading.Thread(target=replacement.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", replacement.server_port, timeout=3)
                connection.request("POST", "/v1/responses", body=b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                connection.close()
            finally:
                replacement.shutdown()
                replacement.server_close()
                thread.join()
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(self.request()[0], 200)

    def test_update_gate_rejects_remote_or_invalid_requests(self):
        self.bridge.management_key = "update-key"
        path = "/v0/management/fleet-update"
        headers = {"Authorization": "Bearer update-key"}
        self.assertEqual(self.request(path=path, headers={**headers, "X-Forwarded-For": "127.0.0.1"})[0], 403)
        for body in (b"{}", b'{"drain":1}', b"[]", b"null"):
            with self.subTest(body=body):
                self.assertEqual(self.request(path=path, body=body, headers=headers)[0], 400)
        status, _headers, body = self.request(method="GET", path=path, body=None, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"protocol": 1, "active": 0, "draining": False})

    def test_provider_requests_use_only_the_local_router_key_upstream(self):
        self.bridge.client_key = "router-key"

        for path in ("/v1/responses", "/v1/messages"):
            with self.subTest(path=path):
                status, _headers, _body = self.request(
                    path=path, headers={"authorization": "Bearer native-provider-token"}
                )

                self.assertEqual(status, 200)
                self.assertEqual(UpstreamHandler.authorizations, ["Bearer router-key"])

    def test_streaming_client_socket_sends_small_chunks_without_delay(self):
        options = []
        setup = BRIDGE.RetryBridgeHandler.setup

        def capture_setup(handler):
            setup(handler)
            options.append(handler.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

        with patch.object(BRIDGE.RetryBridgeHandler, "setup", capture_setup):
            self.assertEqual(self.request()[0], 200)

        self.assertEqual(len(options), 1)
        self.assertTrue(options[0])

    def test_connection_burst_can_wait_for_the_accept_loop(self):
        server = BRIDGE.RetryBridgeServer(
            ("127.0.0.1", 0), BRIDGE.RetryBridgeHandler, self.upstream.server_port
        )
        connections = []
        try:
            for _ in range(16):
                connections.append(socket.create_connection(server.server_address, timeout=0.2))
        finally:
            for connection in connections:
                connection.close()
            server.server_close()

    def test_slow_first_response_is_not_limited_by_connection_timeout(self):
        UpstreamHandler.header_delay = 0.2
        connection_type = http.client.HTTPConnection

        def short_connection_timeout(host, port, **options):
            if port == self.upstream.server_port:
                options["timeout"] = 0.05
            return connection_type(host, port, **options)

        with patch.object(BRIDGE.http.client, "HTTPConnection", short_connection_timeout):
            status, _headers, body = self.request()

        self.assertEqual((status, body), (200, b"ok"))

    def test_structured_codex_quota_retries_before_output(self):
        bodies = (
            b'{"error":{"type":"usage_limit_reached","resets_in_seconds":60}}',
            b'{"type":"usage_limit_reached","message":"limit"}',
            b'{"error":{"type":"rate_limit_error","code":"turn_rate_limit_exceeded","message":"Codex is at capacity, please try again later"}}',
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

    def test_repeated_quota_errors_stop_at_the_attempt_budget(self):
        for path, status, kind in (
            ("/v1/responses", 429, "usage_limit_reached"),
            ("/v1/messages", 429, "rate_limit_error"),
            ("/v1/messages", 529, "overloaded_error"),
        ):
            with self.subTest(path=path, status=status):
                self.policy.write_text(json.dumps({"version": 1, "providers": {"claude": "any", "codex": "limits"}}))
                body = json.dumps({"error": {"type": kind}}).encode()
                UpstreamHandler.calls = 0
                UpstreamHandler.responses = [(status, body, {"Retry-After": "0"})] * 5 + [(200, b"replayed", {})]

                received_status, headers, received = self.request(path=path)

                self.assertEqual((received_status, received, UpstreamHandler.calls), (status, body, 3))
                self.assertEqual(headers["Retry-After"], "0")
                self.assertEqual(headers["X-Fleet-Retry-State"], "exhausted")
                self.assertEqual(headers["X-Fleet-Retry-Attempts"], "3")

    def test_retry_after_beyond_the_remaining_budget_returns_without_waiting(self):
        body = b'{"error":{"type":"usage_limit_reached"}}'
        for hint in ("1", "444419", formatdate(time.time() + 3600, usegmt=True)):
            with self.subTest(hint=hint):
                UpstreamHandler.calls = 0
                UpstreamHandler.responses = [(429, body, {"Retry-After": hint}), (200, b"replayed", {})]
                with patch.object(BRIDGE, "MAX_RETRY_ELAPSED", 0.2), patch.object(
                    BRIDGE.RetryBridgeHandler, "wait_for_retry", return_value=True
                ) as wait:
                    status, headers, received = self.request()

                self.assertEqual((status, received, UpstreamHandler.calls), (429, body, 1))
                self.assertEqual(headers["Retry-After"], hint)
                wait.assert_not_called()

    def test_retry_response_wait_cannot_outlive_the_total_budget(self):
        body = b'{"error":{"type":"usage_limit_reached"}}'
        UpstreamHandler.responses = [(429, body, {"Retry-After": "0"}), (200, b"replayed", {})]
        UpstreamHandler.header_delays = [0, 0.5]

        started = time.monotonic()
        with patch.object(BRIDGE, "MAX_RETRY_ELAPSED", 0.15):
            status, headers, received = self.request()

        self.assertEqual((status, received, UpstreamHandler.calls), (429, body, 2))
        self.assertEqual(headers["Retry-After"], "0")
        self.assertEqual(headers["X-Fleet-Retry-State"], "exhausted")
        self.assertLess(time.monotonic() - started, 0.4)

    def test_silent_initial_response_times_out_without_replay(self):
        UpstreamHandler.header_delay = 0.3

        with patch.object(BRIDGE, "FIRST_RESPONSE_TIMEOUT", 0.05):
            status, _headers, received = self.request()

        self.assertEqual((status, UpstreamHandler.calls), (504, 1))
        self.assertEqual(json.loads(received)["error"]["message"], "Upstream response timed out.")

    def test_response_header_progress_cannot_extend_the_total_budget(self):
        body = b'{"error":{"type":"usage_limit_reached"}}'
        UpstreamHandler.responses = [(429, body, {"Retry-After": "0"}), (200, b"replayed", {})]
        UpstreamHandler.header_progress_call = 2

        with patch.object(BRIDGE, "MAX_RETRY_ELAPSED", 0.15):
            status, headers, received = self.request()

        self.assertEqual((status, received, UpstreamHandler.calls), (429, body, 2))
        self.assertEqual(headers["Retry-After"], "0")
        self.assertEqual(headers["X-Fleet-Retry-State"], "exhausted")

    def test_credit_policy_stop_is_returned_without_retry(self):
        for path, kind in (("/v1/messages", "rate_limit_error"), ("/v1/responses", "usage_limit_reached")):
            with self.subTest(path=path):
                body = json.dumps({"error": {"type": kind, "message": "fleet_credit_blocked: No verified subscription allowance or permitted credits available"}}).encode()
                UpstreamHandler.responses = [(429, body, {"Content-Type": "application/json", "Retry-After": "0"})]
                UpstreamHandler.calls = 0
                status, headers, received = self.request(path=path)
                self.assertEqual((status, received, UpstreamHandler.calls), (429, body, 1))

    def test_long_reset_hints_are_not_shortened(self):
        body = b'{"error":{"type":"usage_limit_reached","resets_at":4102444800,"resets_in_seconds":444419}}'
        with patch.object(BRIDGE.time, "time", return_value=4102440000):
            self.assertEqual(BRIDGE.quota_retry_delay("/v1/responses", 429, {}, body), 4800)
        self.assertEqual(BRIDGE.quota_retry_delay("/v1/responses", 429, {"Retry-After": "444419"}, body), 444419)

    def test_fast_mode_refusal_is_returned_without_retry(self):
        refusal = b'{"error":{"type":"rate_limit_error","message":"Error"}}'
        for body, headers in (
            (b'{"speed":"fast"}', {}),
            (b'{"speed":"fast","stream":true}', {}),
            (b'{}', {"Anthropic-Beta": "oauth-2025-04-20,fast-mode-2026-02-01"}),
        ):
            with self.subTest(body=body, headers=headers):
                UpstreamHandler.calls = 0
                UpstreamHandler.responses = [
                    (429, refusal, {"Content-Type": "application/json", "Retry-After": "0"}),
                    (200, b"replayed", {}),
                ]
                status, _, received = self.request(path="/v1/messages", body=body, headers=headers)
                self.assertEqual((status, received, UpstreamHandler.calls), (429, refusal, 1))
                status, _, received = self.request(path="/v1/messages")
                self.assertEqual((status, received, UpstreamHandler.calls), (200, b"replayed", 2))

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
            ("/v1/responses", 429, b'{"error":{"type":"rate_limit_error"}}'),
            ("/v1/models", 429, b'{"error":{"type":"usage_limit_reached"}}'),
            ("/v1/messages", 429, b'{"error":{"type":"usage_limit_reached"}}'),
            ("/v1/responses", 429, b"not json"),
            ("/v1/responses", 400, b'{"error":{"type":"invalid_request_error","code":"model_not_found"}}'),
            ("/v1/messages", 404, b'{"error":{"type":"not_found_error","message":"model unavailable"}}'),
        )
        for path, expected_status, body in cases:
            with self.subTest(path=path, body=body):
                UpstreamHandler.calls = 0
                UpstreamHandler.status = expected_status
                UpstreamHandler.body = body
                UpstreamHandler.headers = {"Content-Type": "application/json"}

                status, _headers, received = self.request(path=path)

                self.assertEqual(status, expected_status)
                self.assertEqual(received, body)
                self.assertEqual(UpstreamHandler.calls, 1)

    def test_successful_stream_is_forwarded_before_it_ends(self):
        self.assert_stream_forwarded("/v1/responses")

    def test_progressing_stream_outlives_header_and_idle_timeouts(self):
        finished = threading.Event()
        finished.set()
        UpstreamHandler.first_chunk = b"first"
        UpstreamHandler.stream_chunks = [b"next"] * 8
        UpstreamHandler.stream_interval = 0.2
        UpstreamHandler.finish_stream = finished

        with patch.object(BRIDGE, "FIRST_RESPONSE_TIMEOUT", 1), patch.object(
            BRIDGE, "STREAM_IDLE_TIMEOUT", 1
        ):
            status, _headers, received = self.request()

        self.assertEqual((status, received, UpstreamHandler.calls), (200, b"first" + b"next" * 8, 1))

    def test_stalled_stream_closes_without_appending_or_replaying_a_response(self):
        finished = threading.Event()
        self.addCleanup(finished.set)
        UpstreamHandler.first_chunk = b"first"
        UpstreamHandler.finish_stream = finished
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        self.addCleanup(connection.close)

        with patch.object(BRIDGE, "STREAM_IDLE_TIMEOUT", 0.05):
            connection.request("POST", "/v1/responses", body=b"{}")
            response = connection.getresponse()
            self.assertEqual(response.read(5), b"first")
            with self.assertRaises(http.client.IncompleteRead) as error:
                response.read()

        self.assertEqual(error.exception.partial, b"")
        self.assertEqual(UpstreamHandler.calls, 1)
        finished.set()

    def test_active_native_alias_stream_defers_updates(self):
        self.assert_stream_forwarded("/backend-api/codex/responses")

    def assert_stream_forwarded(self, path):
        finished = threading.Event()
        self.addCleanup(finished.set)
        UpstreamHandler.first_chunk = b"first"
        UpstreamHandler.finish_stream = finished
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", path, body=b"{}")
        response = connection.getresponse()

        started = time.monotonic()
        received = response.read(5)

        self.assertEqual(response.status, 200)
        self.assertEqual(received, b"first")
        self.assertLess(time.monotonic() - started, 1)
        self.bridge.management_key = "update-key"
        self.assertEqual(self.bridge.active_requests, 1)
        self.assertEqual(self.request(path="/v0/management/fleet-update", body=b'{"drain":true}', headers={"Authorization": "Bearer update-key"})[0], 409)
        finished.set()
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_existing_websocket_sessions_pass_through(self):
        self.assert_websocket_forwarded("/v1/responses")

    def test_idle_websocket_outlives_http_timeouts(self):
        with patch.object(BRIDGE, "STREAM_IDLE_TIMEOUT", 0.05):
            self.assert_websocket_forwarded("/v1/responses", idle=0.15)

    def test_header_deadline_race_cannot_forward_a_dead_websocket_upgrade(self):
        class ExpiringTimer:
            def __init__(self, interval, callback):
                self.callback = callback
                self.expired = False

            def start(self):
                pass

            def cancel(self):
                if not self.expired:
                    self.expired = True
                    self.callback()

            def join(self):
                pass

        with patch.object(BRIDGE.threading, "Timer", ExpiringTimer):
            status, _, body = self.request(method="GET", headers={"Connection": "Upgrade", "Upgrade": "websocket"})
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["error"]["message"], "Upstream response timed out.")

    def test_active_native_alias_websocket_defers_updates(self):
        self.assert_websocket_forwarded("/backend-api/codex/responses")

    def assert_websocket_forwarded(self, path, idle=0):
        connection = socket.create_connection(("127.0.0.1", self.bridge.server_port), timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(
            f"GET {path} HTTP/1.1\r\n".encode()
            + b"Host: 127.0.0.1\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n\r\n"
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += connection.recv(4096)

        self.bridge.management_key = "update-key"
        self.assertEqual(self.bridge.active_requests, 1)
        self.assertEqual(self.request(path="/v0/management/fleet-update", body=b'{"drain":true}', headers={"Authorization": "Bearer update-key"})[0], 409)
        time.sleep(idle)
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

    def test_hub_denies_tailnet_requests_without_owner_and_matching_key(self):
        self.bridge.client_key = "router-key"
        self.bridge.management_key = "mgmt-key"
        self.bridge.hub_login = "owner@example.com"
        cases = (
            ({"Tailscale-User-Login": "owner@example.com", "Authorization": "Bearer router-key"}, "/v1/responses", 200),
            ({"Tailscale-User-Login": "owner@example.com", "Authorization": "Bearer other"}, "/v1/responses", 403),
            ({"Tailscale-User-Login": "guest@example.com", "Authorization": "Bearer router-key"}, "/v1/responses", 403),
            ({"X-Forwarded-For": "100.1.2.3", "Authorization": "Bearer router-key"}, "/v1/responses", 403),
            ({"Tailscale-User-Login": "owner@example.com", "Authorization": "Bearer mgmt-key"}, "/v0/management/auth-files", 200),
            ({"Tailscale-User-Login": "owner@example.com", "Authorization": "Bearer router-key"}, "/v0/management/auth-files", 403),
            ({"Authorization": "Bearer anything"}, "/v1/responses", 200),
        )
        for headers, path, expected in cases:
            with self.subTest(headers=headers, path=path):
                UpstreamHandler.authorizations = []
                status, _headers, _body = self.request(path=path, headers=headers)
                self.assertEqual(status, expected)
        self.bridge.hub_login = None
        status, _headers, _body = self.request(headers={"Tailscale-User-Login": "owner@example.com", "Authorization": "Bearer router-key"})
        self.assertEqual(status, 403)

    def test_hub_strips_tailscale_and_forwarded_headers_before_the_router(self):
        self.bridge.client_key = "router-key"
        self.bridge.hub_login = "owner@example.com"
        UpstreamHandler.seen = None
        status, _headers, _body = self.request(headers={"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.1.2.3", "Authorization": "Bearer router-key"})
        self.assertEqual(status, 200)
        self.assertFalse({name.lower() for name in UpstreamHandler.seen} & {"x-forwarded-for", "tailscale-user-login"})

    def test_relay_forwards_without_local_quota_retries(self):
        relay = BRIDGE.RetryBridgeServer(("127.0.0.1", 0), BRIDGE.RetryBridgeHandler, self.upstream.server_port, client_key="hub-key", relay=True)
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        try:
            UpstreamHandler.status = 429
            UpstreamHandler.body = b'{"error":{"type":"usage_limit_reached","resets_in_seconds":1}}'
            UpstreamHandler.headers = {"Content-Type": "application/json"}
            connection = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=3)
            connection.request("POST", "/v1/responses", body=b"{}", headers={"Authorization": "Bearer native"})
            response = connection.getresponse()
            self.assertEqual(response.status, 429)
            self.assertEqual(UpstreamHandler.calls, 1)
            self.assertEqual(UpstreamHandler.authorizations, ["Bearer hub-key"])
            connection.close()
        finally:
            relay.shutdown()
            relay.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
