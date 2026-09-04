#!/usr/bin/env python3
import http.client
import json
import select
import signal
import socket
import subprocess
import sys
import time
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_ERROR_BODY = 64 * 1024
MAX_RETRY_DELAY = 31 * 24 * 60 * 60
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def quota_retry_delay(path, status, headers, response_body, mode="limits"):
    try:
        payload = json.loads(response_body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    nested = error.get("type") if isinstance(error, dict) else None
    kind = nested if nested is not None else payload.get("type")
    retryable = (
        path == "/v1/responses" and status == 429 and kind == "usage_limit_reached"
    ) or (
        path == "/v1/messages"
        and (status == 429 and kind == "rate_limit_error" or mode == "any" and status == 529 and kind == "overloaded_error")
    )
    if not retryable:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0, min(int(retry_after), MAX_RETRY_DELAY))
        except ValueError:
            try:
                return max(0, min(parsedate_to_datetime(retry_after).timestamp() - time.time(), MAX_RETRY_DELAY))
            except (TypeError, ValueError, OverflowError):
                pass
    error = error if isinstance(error, dict) else payload
    if type(error.get("resets_at")) in {int, float}:
        return max(0, min(error["resets_at"] - time.time(), MAX_RETRY_DELAY))
    if type(error.get("resets_in_seconds")) in {int, float}:
        return max(0, min(error["resets_in_seconds"], MAX_RETRY_DELAY))
    return 5


class RetryBridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, upstream_port, child=None, policy_path=None):
        self.upstream_port = upstream_port
        self.child = child
        self.policy_path = policy_path or Path.home() / ".local/state/fleet/account-routing.json"
        super().__init__(address, handler)

    def routing_mode(self, provider):
        try:
            value = json.loads(self.policy_path.read_text())
            mode = value["providers"][provider]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            return "limits"
        return mode if value.get("version") == 1 and mode in {"limits", "any"} else "limits"

    def service_actions(self):
        if self.child is not None and self.child.poll() is not None:
            raise SystemExit(self.child.returncode or 1)


class RetryBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def request_body(self):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line or len(line) > 65536:
                    raise ValueError
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError
                chunks.append(chunk)
        length = self.headers.get("Content-Length")
        if length is None:
            return None
        length = int(length)
        if length < 0:
            raise ValueError
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError
        return body

    def upstream_headers(self, body):
        upgrade = self.headers.get("Upgrade")
        blocked = HOP_HEADERS | {
            value.strip().lower()
            for value in self.headers.get("Connection", "").split(",")
            if value.strip()
        }
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in blocked and name.lower() not in {"content-length", "host"}
        }
        if upgrade:
            headers["Connection"] = "Upgrade"
            headers["Upgrade"] = upgrade
        if body is not None:
            headers["Content-Length"] = str(len(body))
        return headers

    def response_headers(self, response):
        upgrade = response.headers.get("Upgrade")
        blocked = HOP_HEADERS | {
            value.strip().lower()
            for value in response.headers.get("Connection", "").split(",")
            if value.strip()
        }
        headers = [(name, value) for name, value in response.getheaders() if name.lower() not in blocked]
        if upgrade:
            headers.extend((("Connection", "Upgrade"), ("Upgrade", upgrade)))
        return headers

    def send_proxy_error(self, status, message):
        body = json.dumps({"error": {"message": message}}, separators=(",", ":")).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def forward(self):
        connection = None
        response = None
        response_started = False
        try:
            body = self.request_body()
            target = urlsplit(self.path)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            buffered = b""
            while True:
                connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=30)
                connection.request(self.command, path, body=body, headers=self.upstream_headers(body))
                response = connection.getresponse()
                if connection.sock is not None:
                    connection.sock.settimeout(None)
                status = response.status
                if self.command != "POST" or not (
                    target.path == "/v1/responses" and status == 429
                    or target.path == "/v1/messages" and status in {429, 529}
                ):
                    break
                buffered = response.read(MAX_ERROR_BODY + 1)
                if len(buffered) > MAX_ERROR_BODY:
                    break
                provider = "codex" if target.path == "/v1/responses" else "claude"
                delay = quota_retry_delay(target.path, status, response.headers, buffered, self.server.routing_mode(provider))
                if delay is None:
                    break
                response.close()
                connection.close()
                response = None
                connection = None
                buffered = b""
                if not self.wait_for_retry(delay):
                    return
            headers = self.response_headers(response)
            if status == 101 and connection.sock is not None:
                self.send_response_only(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                response_started = True
                self.wfile.flush()
                self.close_connection = True
                self.tunnel(connection.sock)
                return
            has_length = any(name.lower() == "content-length" for name, _value in headers)
            has_body = self.command != "HEAD" and status not in {204, 304} and not 100 <= status < 200
            self.send_response_only(status)
            for name, value in headers:
                self.send_header(name, value)
            chunked = has_body and not has_length
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            response_started = True
            if not has_body:
                return
            if buffered:
                self.write_body(buffered, chunked)
            while data := response.read1(64 * 1024):
                self.write_body(data, chunked)
            if response.length not in (None, 0):
                self.close_connection = True
                return
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, http.client.HTTPException, ValueError):
            self.close_connection = True
            if not response_started and not self.wfile.closed:
                try:
                    self.send_proxy_error(502, "Local account router is unavailable.")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def write_body(self, data, chunked):
        if chunked:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    def tunnel(self, upstream):
        sockets = (self.connection, upstream)
        while True:
            ready, _writable, _failed = select.select(sockets, (), (), 60)
            if not ready:
                continue
            for source in ready:
                data = source.recv(64 * 1024)
                if not data:
                    return
                (upstream if source is self.connection else self.connection).sendall(data)

    def wait_for_retry(self, delay):
        deadline = time.monotonic() + delay
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            ready, _writable, _failed = select.select((self.connection,), (), (), min(remaining, 1))
            if not ready:
                continue
            try:
                if not self.connection.recv(1, socket.MSG_PEEK):
                    self.close_connection = True
                    return False
            except OSError:
                self.close_connection = True
                return False
            time.sleep(min(remaining, 0.1))

    do_DELETE = do_GET = do_HEAD = do_OPTIONS = do_PATCH = do_POST = do_PUT = forward


def wait_for_upstream(child):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SystemExit(child.returncode or 1)
        try:
            with socket.create_connection(("127.0.0.1", 8318), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("CLIProxyAPI did not start.")


def main(arguments):
    if len(arguments) != 2:
        raise SystemExit("Usage: fleet-retry-bridge PROXY CONFIG")
    child = subprocess.Popen([arguments[0], "-config", arguments[1]])
    server = None

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        wait_for_upstream(child)
        server = RetryBridgeServer(("127.0.0.1", 8317), RetryBridgeHandler, 8318, child)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main(sys.argv[1:])
