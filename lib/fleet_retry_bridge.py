#!/usr/bin/env python3
import fcntl
import http.client
import json
import select
import signal
import socket
import subprocess
import sys
import time
import threading
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_ERROR_BODY = 64 * 1024
MAX_RETRY_ATTEMPTS = 3
MAX_RETRY_ELAPSED = 120
FIRST_RESPONSE_TIMEOUT = 180
STREAM_IDLE_TIMEOUT = 300
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
    if isinstance(error, dict) and str(error.get("message", "")).startswith("fleet_credit_"):
        return None
    nested = error.get("type") if isinstance(error, dict) else None
    kind = nested if nested is not None else payload.get("type")
    code = error.get("code") if isinstance(error, dict) else payload.get("code")
    retryable = (
        path == "/v1/responses" and status == 429 and (kind == "usage_limit_reached" or code == "turn_rate_limit_exceeded")
    ) or (
        path == "/v1/messages"
        and (status == 429 and kind == "rate_limit_error" or mode == "any" and status == 529 and kind == "overloaded_error")
    )
    if not retryable:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0, int(retry_after))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    error = error if isinstance(error, dict) else payload
    if type(error.get("resets_at")) in {int, float}:
        return max(0, error["resets_at"] - time.time())
    if type(error.get("resets_in_seconds")) in {int, float}:
        return max(0, error["resets_in_seconds"])
    return 5


class RetryBridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, upstream_port, child=None, policy_path=None, client_key=None, upstream_host="127.0.0.1", relay=False, hub_login=None, management_key=None):
        self.activity_lock = threading.Lock()
        self.active_requests = 0
        self.drain_until = 0
        self.upstream_port = upstream_port
        self.upstream_host = upstream_host
        self.relay = relay
        self.hub_login = hub_login
        self.management_key = management_key
        self.child = child
        self.policy_path = policy_path or Path.home() / ".local/state/fleet/account-routing.json"
        self.client_key = client_key
        super().__init__(address, handler)

    def update_locked(self):
        try:
            with (self.policy_path.parent / "cliproxyapi-activation.lock").open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        except OSError:
            return True
        return False

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
    disable_nagle_algorithm = True

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

    def upstream_headers(self, body, path):
        upgrade = self.headers.get("Upgrade")
        blocked = HOP_HEADERS | {
            value.strip().lower()
            for value in self.headers.get("Connection", "").split(",")
            if value.strip()
        }
        if self.server.client_key and path.startswith("/v1/"):
            blocked.add("authorization")
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in blocked and name.lower() not in {"content-length", "host"} and not name.lower().startswith(("x-forwarded-", "tailscale-"))
        }
        if self.server.client_key and path.startswith("/v1/"):
            headers["Authorization"] = f"Bearer {self.server.client_key}"
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

    def remote_denied(self):
        login = self.headers.get("Tailscale-User-Login")
        if login is None and self.headers.get("X-Forwarded-For") is None:
            return False
        if self.server.hub_login is None or login != self.server.hub_login:
            return True
        if self.path.startswith("/v1/"):
            return self.headers.get("Authorization") != f"Bearer {self.server.client_key}"
        return not self.path.startswith("/v0/management/") or self.headers.get("Authorization") != f"Bearer {self.server.management_key}"

    def forward(self):
        path = urlsplit(self.path).path
        update_path = path == "/v0/management/fleet-update"
        if update_path and not self.server.relay:
            if self.server.management_key is None or self.headers.get("Authorization") != f"Bearer {self.server.management_key}" or self.remote_denied():
                self.send_proxy_error(403, "Fleet update requires the management key.")
                return
            try:
                payload = json.loads(self.request_body()) if self.command == "POST" else {}
                if self.command not in {"GET", "POST"} or not isinstance(payload, dict) or (self.command == "POST" and type(payload.get("drain")) is not bool):
                    raise ValueError
                with self.server.activity_lock:
                    if payload.get("drain") and self.server.active_requests:
                        self.send_proxy_error(409, "Fleet update deferred while requests are active.")
                        return
                    if self.command == "POST":
                        self.server.drain_until = time.monotonic() + 60 if payload["drain"] else 0
                    body = json.dumps({"protocol": 1, "active": self.server.active_requests, "draining": self.server.drain_until > time.monotonic()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, TypeError):
                self.send_proxy_error(400, "Invalid Fleet update request.")
            return
        tracked = not path.startswith(("/v0/management/", "/v8/management/"))
        if tracked:
            with self.server.activity_lock:
                if self.server.drain_until > time.monotonic() or self.server.update_locked():
                    self.send_proxy_error(503, "Fleet proxy update in progress; retry shortly.")
                    self.close_connection = True
                    return
                self.server.active_requests += 1
        try:
            self.forward_request()
        finally:
            if tracked:
                with self.server.activity_lock:
                    self.server.active_requests -= 1

    def forward_request(self):
        connection = None
        response = None
        response_started = False
        if self.remote_denied():
            self.send_proxy_error(403, "Hub access requires the tailnet owner and the hub client key.")
            self.close_connection = True
            return
        try:
            body = self.request_body()
            target = urlsplit(self.path)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            fast_request = False
            if target.path == "/v1/messages":
                betas = self.headers.get("Anthropic-Beta", "").split(",")
                fast_request = "fast-mode-2026-02-01" in {beta.strip() for beta in betas}
                try:
                    payload = json.loads(body)
                    fast_request |= isinstance(payload, dict) and payload.get("speed") == "fast"
                except ValueError:
                    pass
            buffered = b""
            attempts = 0
            retry_response = None
            retry_exhausted = False
            deadline = time.monotonic() + (FIRST_RESPONSE_TIMEOUT if self.server.relay else MAX_RETRY_ELAPSED)
            while True:
                attempts += 1
                connection = http.client.HTTPConnection(self.server.upstream_host, self.server.upstream_port, timeout=min(30, max(0.001, deadline - time.monotonic())))
                timed_out = threading.Event()
                response_timer = None
                try:
                    connection.connect()
                    upstream_socket = connection.sock
                    response_timeout = min(FIRST_RESPONSE_TIMEOUT, max(0.001, deadline - time.monotonic()))
                    upstream_socket.settimeout(response_timeout)

                    def expire_response(sock=upstream_socket, expired=timed_out):
                        expired.set()
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

                    response_timer = threading.Timer(response_timeout, expire_response)
                    response_timer.daemon = True
                    response_timer.start()
                    connection.request(self.command, path, body=body, headers=self.upstream_headers(body, target.path))
                    response = connection.getresponse()
                    if timed_out.is_set():
                        raise TimeoutError
                    upstream_socket.settimeout(STREAM_IDLE_TIMEOUT)
                    status = response.status
                    if self.server.relay or self.command != "POST" or not (
                        target.path == "/v1/responses" and status == 429
                        or target.path == "/v1/messages" and status in {429, 529}
                    ):
                        response_timer.cancel()
                        response_timer.join()
                        if timed_out.is_set():
                            raise TimeoutError
                        break
                    upstream_socket.settimeout(max(0.001, deadline - time.monotonic()))
                    buffered = response.read(MAX_ERROR_BODY + 1)
                    if timed_out.is_set():
                        raise TimeoutError
                except (OSError, http.client.HTTPException) as error:
                    if not isinstance(error, TimeoutError) and not timed_out.is_set():
                        raise
                    if retry_response is None:
                        raise TimeoutError from None
                    if response is not None:
                        response.close()
                    response = None
                    status, headers, buffered = retry_response
                    retry_exhausted = True
                    break
                finally:
                    if response_timer is not None:
                        response_timer.cancel()
                if len(buffered) > MAX_ERROR_BODY:
                    break
                provider = "codex" if target.path == "/v1/responses" else "claude"
                delay = None if fast_request else quota_retry_delay(target.path, status, response.headers, buffered, self.server.routing_mode(provider))
                if delay is None:
                    break
                retry_response = status, self.response_headers(response), buffered
                if attempts >= MAX_RETRY_ATTEMPTS or delay >= deadline - time.monotonic():
                    retry_exhausted = True
                    break
                if not self.wait_for_retry(delay):
                    return
                if time.monotonic() >= deadline:
                    retry_exhausted = True
                    break
                response.close()
                connection.close()
                response = None
                connection = None
                buffered = b""
            if response is not None:
                headers = self.response_headers(response)
            if attempts > 1 or retry_exhausted:
                headers.extend((("X-Fleet-Retry-Attempts", str(attempts)), ("X-Fleet-Retry-State", "exhausted" if retry_exhausted else "completed")))
            if status == 101 and connection.sock is not None:
                connection.sock.settimeout(None)
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
            while response is not None and (data := response.read1(64 * 1024)):
                self.write_body(data, chunked)
            if response is not None and response.length not in (None, 0):
                self.close_connection = True
                return
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, http.client.HTTPException, ValueError) as error:
            self.close_connection = True
            if not response_started and not self.wfile.closed:
                try:
                    if isinstance(error, TimeoutError):
                        self.send_proxy_error(504, "Upstream response timed out.")
                    else:
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


def hub_login():
    try:
        value = json.loads((Path.home() / ".local/state/fleet/hub.json").read_text())
        if value.get("version") == 1 and value.get("role") == "hub" and isinstance(value.get("login"), str):
            return value["login"]
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def read_key(name):
    key = (Path.home() / ".local/state/fleet" / name).read_text().strip()
    if not key or not key.isprintable():
        raise SystemExit("CLIProxyAPI client key is invalid.")
    return key


def relay_main(url):
    target = urlsplit(url)
    if target.scheme != "http" or not target.hostname or target.port is None:
        raise SystemExit("Hub URL must be http://host:port.")
    server = RetryBridgeServer(
        ("127.0.0.1", 8317),
        RetryBridgeHandler,
        target.port,
        client_key=read_key("hub-client-key"),
        upstream_host=target.hostname,
        relay=True,
    )

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(arguments):
    if len(arguments) == 2 and arguments[0] == "--relay":
        relay_main(arguments[1])
        return
    if len(arguments) != 2:
        raise SystemExit("Usage: fleet-retry-bridge PROXY CONFIG | fleet-retry-bridge --relay HUB_URL")
    child = subprocess.Popen([arguments[0], "-config", arguments[1]])
    server = None

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        wait_for_upstream(child)
        server = RetryBridgeServer(("127.0.0.1", 8317), RetryBridgeHandler, 8318, child, client_key=read_key("cliproxyapi-client-key"), hub_login=hub_login(), management_key=read_key("cliproxyapi-management-key"))
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
