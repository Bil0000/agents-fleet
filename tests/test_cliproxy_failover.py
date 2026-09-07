#!/usr/bin/env python3
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).parents[1].resolve()


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.server.calls.append((self.headers.get("Authorization") or self.headers.get("x-api-key"), body))
        self.server.paths.append(self.path)
        request = json.loads(body)
        texts = []
        for message in request.get("messages", []):
            content = message["content"]
            if isinstance(content, str):
                texts.append(content)
            else:
                texts.extend(part["text"] for part in content if part.get("type") == "text")
        if not texts and isinstance(request.get("input"), str):
            texts.append(request["input"])
        turn = next((text for text in texts if text.startswith(("turn-", "claude-", "bounded-"))), texts[-1] if texts else "request")
        if self.server.mode == "sequence" and len(self.server.calls) == 2:
            payload = b'{"error":{"message":"quota reached","type":"rate_limit_error","code":"rate_limit_exceeded"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", "60")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.server.mode == "all-quota":
            payload = b'{"error":{"message":"quota reached","type":"rate_limit_error","code":"rate_limit_exceeded"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", "60")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.server.mode == "partial":
            payload = b'data: {"id":"partial","choices":[{"delta":{"content":"part"}}]}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload) + 100))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True
            return
        self.server.successes += 1
        self.server.turn_successes[turn] = self.server.turn_successes.get(turn, 0) + 1
        if urllib.parse.urlsplit(self.path).path.endswith("/v1/messages"):
            payload = json.dumps({
                "id": f"accepted-{turn}",
                "type": "message",
                "role": "assistant",
                "model": "fake-model",
                "content": [{"type": "text", "text": f"accepted-{turn}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()
        elif urllib.parse.urlsplit(self.path).path.endswith("/responses"):
            response = {
                "id": f"accepted-{turn}",
                "object": "response",
                "status": "completed",
                "model": request["model"],
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            if request.get("stream"):
                payload = f'data: {json.dumps({"type": "response.completed", "response": response})}\n\ndata: [DONE]\n\n'.encode()
            else:
                payload = json.dumps(response).encode()
        else:
            payload = json.dumps(
                {
                    "id": f"accepted-{turn}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": f"accepted-{turn}"}, "finish_reason": "stop"}],
                }
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def installed_binary():
    configured = os.environ.get("FLEET_CLIPROXYAPI")
    state = Path.home() / ".local/state/fleet/cliproxyapi.json"
    try:
        managed_version = json.loads(state.read_text())["version"]
    except (OSError, KeyError, json.JSONDecodeError):
        managed_version = "7.2.149"
    managed = Path.home() / f".local/share/fleet/cliproxyapi/{managed_version}/cli-proxy-api"
    candidates = (configured, managed, shutil.which("cliproxyapi"), "/opt/homebrew/bin/cliproxyapi", "/usr/local/bin/cliproxyapi")
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        version = subprocess.run([candidate, "-h"], capture_output=True, text=True)
        match = re.search(r"CLIProxyAPI Version: ([0-9]+\.[0-9]+\.[0-9]+)", version.stdout + version.stderr)
        if match and tuple(map(int, match.group(1).split("."))) >= (7, 2, 149):
            return candidate, match.group(1)
    return None


class CLIProxyFailoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        installed = installed_binary()
        if installed is None:
            raise unittest.SkipTest("CLIProxyAPI 7.2.149 or newer is not installed")
        cls.binary, cls.version = installed

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.calls = []
        self.upstream.paths = []
        self.upstream.successes = 0
        self.upstream.turn_successes = {}
        self.upstream.mode = "sequence"
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.proxy = None
        self.start_proxy(["account-a", "account-b"])

    def start_proxy(self, accounts, protocol="openai", plugin=None):
        self.proxy_port = free_port()
        auth = self.base / "auth"
        auth.mkdir(exist_ok=True)
        config = self.base / f"config-{self.proxy_port}.yaml"
        if protocol == "claude":
            provider = "claude-api-key:\n" + "".join(
                f"  - api-key: {account}\n"
                f"    priority: {100 if index == 0 else 0}\n"
                + ("    disable-cooling: true\n" if index == 0 else "")
                + f'    base-url: "http://127.0.0.1:{self.upstream.server_port}"\n'
                + "    models:\n"
                + "      - name: fake-model\n"
                + "        alias: fake-model\n"
                for index, account in enumerate(accounts)
            )
        elif protocol == "codex":
            provider = "codex-api-key:\n" + "".join(
                f"  - api-key: {account}\n"
                f'    base-url: "http://127.0.0.1:{self.upstream.server_port}/v1"\n'
                for account in accounts
            )
        else:
            provider = (
                "openai-compatibility:\n" + "".join(
                    f"  - name: fake-{index}\n"
                    f"    priority: {100 if index == 0 else 0}\n"
                    + ("    disable-cooling: true\n" if index == 0 else "")
                    + f'    base-url: "http://127.0.0.1:{self.upstream.server_port}/v1"\n'
                    + "    api-key-entries:\n"
                    + f"      - api-key: {account}\n"
                    + "    models:\n"
                    + "      - name: fake-model\n"
                    + "        alias: fake-model\n"
                    for index, account in enumerate(accounts)
                )
            )
        config.write_text(
            f'host: "127.0.0.1"\n'
            f"port: {self.proxy_port}\n"
            f'auth-dir: "{auth}"\n'
            "api-keys:\n  - client-key\n"
            "request-retry: 0\n"
            "max-retry-credentials: 0\n"
            "routing:\n  strategy: fill-first\n  session-affinity: false\n"
            "streaming:\n  bootstrap-retries: 0\n"
            "codex:\n  stream-bootstrap-buffering: true\n"
            "remote-management:\n  allow-remote: false\n  secret-key: test-management-key\n"
            f"{provider}"
            + (
                "plugins:\n  enabled: true\n"
                f'  dir: "{plugin.parent}"\n'
                "  configs:\n    fleet-provider-router:\n      enabled: true\n      priority: 100\n"
                if plugin else ""
            )
        )
        self.proxy = subprocess.Popen(
            [self.binary, "-config", str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.proxy_port}/v1/models",
                    headers={"Authorization": "Bearer client-key"},
                )
                urllib.request.urlopen(request, timeout=0.5).close()
                break
            except (urllib.error.URLError, TimeoutError):
                if self.proxy.poll() is not None:
                    self.fail(self.proxy.stdout.read())
                time.sleep(0.05)
        else:
            self.fail("CLIProxyAPI did not start")

    def test_client_endpoint_requires_the_separate_client_key(self):
        url = f"http://127.0.0.1:{self.proxy_port}/v1/models"
        for headers in ({}, {"Authorization": "Bearer wrong-key"}):
            with self.subTest(headers=bool(headers)):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)
                self.assertEqual(raised.exception.code, 401)

        request = urllib.request.Request(url, headers={"Authorization": "Bearer client-key"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertIsInstance(json.load(response)["data"], list)

    def stop_proxy(self):
        if self.proxy is None:
            return
        self.proxy.terminate()
        try:
            self.proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proxy.kill()
            self.proxy.wait()
        self.proxy.stdout.close()
        self.proxy = None

    def tearDown(self):
        self.stop_proxy()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()
        self.temporary.cleanup()

    def request(self, turn="turn-1", *, stream=False):
        body = json.dumps(
            {
                "model": "fake-model",
                "messages": [{"role": "user", "content": turn}],
                "session_id": "same-thread-123",
                "stream": stream,
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer client-key",
                "Content-Type": "application/json",
                "Session-Id": "same-thread-123",
            },
        )
        return urllib.request.urlopen(request, timeout=5)

    def claude_request(self, turn, model="fake-model"):
        body = json.dumps({
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": turn}],
            "metadata": {"user_id": "same-claude-thread-123"},
        }).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}/v1/messages",
            data=body,
            headers={
                "x-api-key": "client-key",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "x-session-id": "same-claude-thread-123",
            },
        )
        return urllib.request.urlopen(request, timeout=5)

    def provider_request(self, model):
        body = json.dumps({"model": model, "input": "future-model", "stream": False}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}/v1/responses",
            data=body,
            headers={"Authorization": "Bearer client-key", "Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_future_codex_model_is_routed_without_a_fleet_model_entry(self):
        source = ROOT / "lib/fleet_provider_router.c"
        self.assertTrue(source.is_file())
        plugin = self.base / "fleet-provider-router.dylib"
        subprocess.run(["cc", "-dynamiclib", "-O2", str(source), "-o", str(plugin)], check=True)
        self.stop_proxy()
        self.upstream.calls.clear()
        self.upstream.mode = "normal"
        self.start_proxy(["account-a"], protocol="codex", plugin=plugin)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}/v0/management/plugins",
            headers={"Authorization": "Bearer test-management-key"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            plugins = json.load(response)
        self.assertIn("fleet-provider-router", json.dumps(plugins), plugins)
        if not plugins["plugins"][0]["registered"]:
            self.proxy.terminate()
            output = self.proxy.communicate(timeout=5)[0]
            self.proxy = None
            self.fail(output)

        try:
            with self.provider_request("provider-owned-future-model") as response:
                self.assertEqual(response.status, 200)
        except urllib.error.HTTPError as error:
            self.fail(error.read().decode())

        self.assertEqual(json.loads(self.upstream.calls[0][1])["model"], "provider-owned-future-model")

    def test_future_claude_model_is_routed_without_a_fleet_model_entry(self):
        source = ROOT / "lib/fleet_provider_router.c"
        plugin = self.base / "fleet-provider-router.dylib"
        subprocess.run(["cc", "-dynamiclib", "-O2", str(source), "-o", str(plugin)], check=True)
        self.stop_proxy()
        self.upstream.calls.clear()
        self.upstream.mode = "normal"
        self.start_proxy(["account-a"], protocol="claude", plugin=plugin)

        try:
            with self.claude_request("claude-future", "provider-owned-future-model") as response:
                self.assertEqual(response.status, 200)
        except urllib.error.HTTPError as error:
            self.fail(error.read().decode())

        self.assertEqual(json.loads(self.upstream.calls[0][1])["model"], "provider-owned-future-model")

    def test_management_response_identifies_exact_proxy_version(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}/v0/management/config",
            headers={"Authorization": "Bearer test-management-key"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get("X-CPA-VERSION"), self.version)
            self.assertEqual(json.load(response)["api-keys"], ["client-key"])

    def test_same_thread_returns_to_account_a_after_account_a_recovers(self):
        outputs = []
        for turn in ("turn-1", "turn-2"):
            with self.request(turn) as response:
                outputs.append(json.load(response)["choices"][0]["message"]["content"])
        with self.request("turn-3") as response:
            outputs.append(json.load(response)["choices"][0]["message"]["content"])

        self.assertEqual(outputs, ["accepted-turn-1", "accepted-turn-2", "accepted-turn-3"])
        first = self.upstream.calls[0][0]
        second = ({"Bearer account-a", "Bearer account-b"} - {first}).pop()
        self.assertEqual([call[0] for call in self.upstream.calls], [first, first, second, first])
        self.assertEqual(self.upstream.calls[1][1], self.upstream.calls[2][1])
        self.assertTrue(all(b'"session_id":"same-thread-123"' in call[1].replace(b" ", b"") for call in self.upstream.calls))
        self.assertEqual(self.upstream.turn_successes, {"turn-1": 1, "turn-2": 1, "turn-3": 1})

    def test_all_limited_accounts_are_tried_once_then_stop(self):
        self.stop_proxy()
        self.upstream.calls.clear()
        self.upstream.mode = "all-quota"
        self.start_proxy([f"account-{number:02}" for number in range(12)])

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("bounded-turn")

        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(len(self.upstream.calls), 12)
        self.assertEqual(len({call[0] for call in self.upstream.calls}), 12)
        self.assertEqual(self.upstream.turn_successes, {})

    def test_native_claude_messages_returns_to_recovered_primary_account(self):
        self.stop_proxy()
        self.upstream.calls.clear()
        self.upstream.paths.clear()
        self.upstream.turn_successes.clear()
        self.start_proxy(["account-a", "account-b"], protocol="claude")

        outputs = []
        for turn in ("claude-1", "claude-2"):
            with self.claude_request(turn) as response:
                result = json.load(response)
                self.assertIn("content", result, result)
                outputs.append(result["content"][0]["text"])
        with self.claude_request("claude-3") as response:
            result = json.load(response)
            self.assertIn("content", result, result)
            outputs.append(result["content"][0]["text"])

        self.assertEqual(outputs, ["accepted-claude-1", "accepted-claude-2", "accepted-claude-3"])
        first = self.upstream.calls[0][0]
        second = ({"Bearer account-a", "Bearer account-b"} - {first}).pop()
        self.assertEqual([call[0] for call in self.upstream.calls], [first, first, second, first])
        self.assertTrue(all(urllib.parse.urlsplit(path).path.endswith("/v1/messages") for path in self.upstream.paths))
        failed = json.loads(self.upstream.calls[1][1])
        retried = json.loads(self.upstream.calls[2][1])
        failed.pop("metadata", None)
        retried.pop("metadata", None)
        self.assertEqual(failed, retried)
        self.assertEqual(self.upstream.turn_successes, {"claude-1": 1, "claude-2": 1, "claude-3": 1})

    def test_partial_stream_is_not_replayed_to_another_account(self):
        self.stop_proxy()
        self.upstream.calls.clear()
        self.upstream.paths.clear()
        self.start_proxy(["account-a", "account-b"], protocol="claude")
        self.upstream.mode = "partial"
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        body = json.dumps(
            {
                "model": "fake-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "one safe operation"}],
                "metadata": {"user_id": "same-thread-partial"},
                "stream": True,
            }
        )
        connection.request(
            "POST",
            "/v1/messages",
            body=body,
            headers={
                "x-api-key": "client-key",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "x-session-id": "same-thread-partial",
            },
        )
        response = connection.getresponse()
        try:
            response.read()
        except http.client.IncompleteRead:
            pass
        connection.close()
        time.sleep(0.2)

        self.assertEqual(len(self.upstream.calls), 1)
        self.assertTrue(all(urllib.parse.urlsplit(path).path.endswith("/v1/messages") for path in self.upstream.paths))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
