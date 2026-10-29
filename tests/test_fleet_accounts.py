#!/usr/bin/env python3
import base64
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
SPEC = importlib.util.spec_from_file_location("fleet_accounts", ROOT / "lib" / "fleet_accounts.py")
ACCOUNTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCOUNTS)


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.signature"


def decode_jwt(value):
    payload = value.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class ManagementHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def response(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.server.version_header is not None:
            self.send_header("X-CPA-VERSION", self.server.version_header)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get("Authorization") == "Bearer test-management-key":
            return True
        self.response(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if self.path == "/v1/models":
            if self.server.reject_models:
                self.response(503, {"error": "unavailable"})
                return
            if self.headers.get("Authorization") != "Bearer test-client-key":
                self.response(401, {"error": "unauthorized"})
                return
            self.response(200, {"object": "list", "data": []})
            return
        if not self.authorized():
            return
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        if self.path == "/v0/management/config":
            self.response(200, self.server.config)
            return
        if self.path == "/v0/management/auth-files":
            self.response(200, {"files": list(self.server.accounts.values())})
            return
        self.response(404, {"error": "not found"})

    def do_PATCH(self):
        if not self.authorized():
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        if self.path == "/v0/management/oauth-request-scoped-errors":
            provider = request["provider"]
            self.server.config.setdefault("oauth-request-scoped-errors", {})[provider] = request["rules"]
            self.server.history.append((self.path, provider, request["rules"]))
            self.response(200, {"status": "ok"})
            return
        self.server.mutation_count += 1
        if self.server.fail_at == self.server.mutation_count:
            self.server.fail_at = None
            self.response(500, {"error": "injected failure"})
            return
        account = self.server.accounts.get(request.get("name"))
        if account is None:
            self.response(404, {"error": "auth file not found"})
            return
        if self.path == "/v0/management/auth-files/status":
            if self.server.ignore_status or self.server.ignore_at == self.server.mutation_count:
                self.response(200, {"status": "ok", "disabled": request["disabled"]})
                return
            account["disabled"] = request["disabled"]
            self.server.history.append((self.path, account["name"], request["disabled"]))
            self.response(200, {"status": "ok", "disabled": request["disabled"]})
            return
        if self.path == "/v0/management/auth-files/fields":
            if self.server.ignore_at == self.server.mutation_count:
                self.response(200, {"status": "ok"})
                return
            field = "websockets" if "websockets" in request else "priority"
            if request[field] is None:
                account.pop(field, None)
            else:
                account[field] = request[field]
            self.server.history.append((self.path, account["name"], request[field]))
            self.response(200, {"status": "ok"})
            return
        self.response(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v0/management/oauth-request-scoped-errors":
            provider = urllib.parse.parse_qs(parsed.query)["provider"][0]
            self.server.config.get("oauth-request-scoped-errors", {}).pop(provider, None)
            self.server.history.append((parsed.path, provider, None))
            self.response(200, {"status": "ok"})
            return
        if self.path != "/v0/management/auth-files":
            self.response(404, {"error": "not found"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        names = request.get("names", [])
        if self.server.delete_fail:
            self.response(500, {"error": "injected failure"})
            return
        if len(names) != 1 or names[0] not in self.server.accounts:
            self.response(404, {"error": "auth file not found"})
            return
        name = names[0]
        Path(self.server.accounts[name]["path"]).unlink()
        del self.server.accounts[name]
        self.server.history.append((self.path, name, None))
        self.response(200, {"status": "ok", "deleted": 1, "files": [name], "failed": []})

    def do_POST(self):
        if self.path == "/v0/management/routing/session-affinity/reset":
            if not self.authorized():
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.server.affinity_reset_fail:
                self.response(500, {"error": "injected reset failure"})
                return
            self.server.history.append((self.path, request["provider"], True))
            self.response(200, {"status": "ok"})
            return
        if not self.authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v0/management/auth-files":
            name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
            size = int(self.headers.get("Content-Length", "0"))
            contents = self.rfile.read(size)
            try:
                credential = json.loads(contents)
            except json.JSONDecodeError:
                self.response(400, {"error": "invalid auth file"})
                return
            path = Path(self.server.config["auth-dir"]) / name
            path.write_bytes(contents)
            path.chmod(0o600)
            entry = {
                "id": name,
                "auth_index": f"index-{name}",
                "name": name,
                "provider": credential["type"],
                "disabled": credential.get("disabled", False),
                "runtime_only": False,
                "source": "file",
                "path": str(path),
                "email": credential.get("email", ""),
            }
            if "priority" in credential:
                entry["priority"] = credential["priority"]
            if credential["type"] == "codex":
                entry["id_token"] = decode_jwt(credential["id_token"])
            self.server.accounts[name] = entry
            self.response(200, {"status": "ok"})
            return
        if parsed.path == "/v0/management/api-call":
            size = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(size))
            body = getattr(self.server, "usage", {}).get(request.get("authIndex"))
            self.server.history.append((parsed.path, request.get("authIndex"), request.get("url")))
            if body is None:
                self.response(200, {"status_code": 401, "body": {"detail": "Unauthorized"}})
            else:
                self.response(200, {"status_code": 200, "body": json.dumps(body)})
            return
        if self.path != "/test/add":
            self.response(404, {"error": "not found"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        name = request["name"]
        provider = request["provider"]
        existing = self.server.accounts.get(name, {})
        self.server.accounts[name] = existing | {
            "id": name,
            "auth_index": f"index-{name}",
            "name": name,
            "provider": provider,
            "disabled": existing.get("disabled", False),
            "runtime_only": self.server.new_source != "file",
            "source": self.server.new_source,
            "path": str(Path(self.server.config["auth-dir"]) / name),
        }
        self.response(200, {"status": "ok"})


class FakeManagement:
    def __init__(self, auth):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ManagementHandler)
        self.server.accounts = {}
        self.server.config = {
            "fleet-session-affinity-supported": True,
            "fleet-priority-selection-supported": True,
            "host": "127.0.0.1",
            "auth-dir": str(auth),
            "api-keys": ["test-client-key"],
            "request-retry": 0,
            "max-retry-credentials": 0,
            "routing": {"strategy": "fill-first", "session-affinity": True},
            "streaming": {"bootstrap-retries": 0},
            "codex": {"stream-bootstrap-buffering": True},
            "remote-management": {"allow-remote": False},
        }
        self.server.history = []
        self.server.mutation_count = 0
        self.server.delete_fail = False
        self.server.affinity_reset_fail = False
        self.server.fail_at = None
        self.server.ignore_at = None
        self.server.ignore_status = False
        self.server.redirect_to = None
        self.server.version_header = "7.2.149"
        self.server.reject_models = False
        self.server.new_source = "file"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def add(self, provider, name, *, disabled=False, priority=None):
        entry = {
            "id": name,
            "auth_index": f"index-{name}",
            "name": name,
            "provider": provider,
            "disabled": disabled,
            "runtime_only": False,
            "source": "file",
            "path": str(Path(self.server.config["auth-dir"]) / name),
        }
        if priority is not None:
            entry["priority"] = priority
        self.server.accounts[name] = entry
        return entry

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FleetAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.auth = self.home / ".cli-proxy-api"
        self.auth.mkdir()
        self.config = self.base / "cliproxyapi.conf"
        self.write_config()
        self.state = self.home / ".local" / "state" / "fleet" / "accounts.json"
        self.policy = self.home / ".local" / "state" / "fleet" / "account-routing.json"
        self.client_key = self.home / ".local" / "state" / "fleet" / "cliproxyapi-client-key"
        self.client_key.parent.mkdir(parents=True)
        self.client_key.write_text("test-client-key\n")
        self.client_key.chmod(0o600)
        self.management = FakeManagement(self.auth)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "FLEET_ACCOUNT_HOME": str(self.home),
                "FLEET_CLIPROXY_CONFIG": str(self.config),
                "FLEET_CLIPROXY_URL": self.management.url,
                "FLEET_CLIPROXY_MANAGEMENT_KEY": "test-management-key",
            }
        )

    def write_config(self, auth=None):
        self.config.write_text(
            'host: "127.0.0.1"\n'
            f'auth-dir: "{auth or self.auth}"\n'
            "api-keys:\n  - test-client-key\n"
            "request-retry: 0\n"
            "max-retry-credentials: 0\n"
            "routing:\n  strategy: fill-first\n  session-affinity: true\n"
            "streaming:\n  bootstrap-retries: 0\n"
            "codex:\n  stream-bootstrap-buffering: true\n"
            "remote-management:\n  allow-remote: false\n  secret-key: test-management-key\n"
        )

    def tearDown(self):
        self.management.close()
        self.temporary.cleanup()

    def run_fleet(self, *arguments, input_text=None):
        return subprocess.run(
            [str(ROOT / "bin" / "fleet"), "account", *arguments],
            cwd=ROOT,
            env=self.environment,
            input=input_text,
            capture_output=True,
            text=True,
        )

    def add_account(self, provider, stem, *, disabled=False, priority=None, contents=b"private token bytes"):
        name = f"{provider}-{stem}.json"
        path = self.auth / name
        path.write_bytes(contents)
        self.management.add(provider, name, disabled=disabled, priority=priority)
        return path

    def write_state(self, *, claude=None, codex=None):
        providers = {
            "claude": {"accounts": {}, "primary": None, "auto": None},
            "codex": {"accounts": {}, "primary": None, "auto": None},
        }
        for provider, policy in (("claude", claude), ("codex", codex)):
            if policy:
                providers[provider].update(policy)
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"version": 1, "providers": providers}))
        self.state.chmod(0o600)

    def fake_login(self, provider, name, *, expected_config=None, block_state_save=False):
        self.environment["FLEET_ENABLE_SUBSCRIPTION_ROUTING"] = "1"
        expected_config = (expected_config or self.config).resolve()
        binary = self.base / "cliproxyapi"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, urllib.request\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] == ['-h']:\n"
            " print('CLIProxyAPI Version: 7.2.149')\n"
            " raise SystemExit\n"
            f"assert sys.argv[1:] == ['-config', {str(expected_config)!r}, '-{provider}-login']\n"
            f"Path({str(self.auth)!r}, {name!r}).write_bytes(b'opaque oauth bytes')\n"
            f"body = json.dumps({{'provider': {provider!r}, 'name': {name!r}}}).encode()\n"
            "request = urllib.request.Request(os.environ['FLEET_CLIPROXY_URL'] + '/test/add', data=body, headers={'Authorization': 'Bearer ' + os.environ['FLEET_CLIPROXY_MANAGEMENT_KEY'], 'Content-Type': 'application/json'})\n"
            "urllib.request.urlopen(request).close()\n"
            + (f"Path({str(self.state)!r}).mkdir(parents=True)\n" if block_state_save else "")
        )
        binary.chmod(0o755)
        self.environment["FLEET_CLIPROXYAPI"] = str(binary)

    def native_client(self, body="", exit_code=0):
        path = self.base / "native-client"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"Path({str(self.base / 'native-calls')!r}).write_text(' '.join(sys.argv[1:]))\n"
            + body
            + f"\nraise SystemExit({exit_code})\n"
        )
        path.chmod(0o755)
        return path

    def codex_native_auth(self, email="work@example.com", account_id="account-1", access="native-access"):
        id_token = jwt(
            {
                "email": email,
                "exp": 1893456000,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                    "chatgpt_plan_type": "pro",
                },
            }
        )
        return {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": "2026-09-04T10:00:00Z",
            "tokens": {
                "access_token": access,
                "account_id": account_id,
                "id_token": id_token,
                "refresh_token": "native-refresh",
            },
        }

    def test_fresh_list_is_read_only_and_reports_unconfigured(self):
        credential = self.add_account("claude", "work")
        self.auth.chmod(0o755)
        credential.chmod(0o644)

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "PROVIDER  ACCOUNT  ORDER  AUTO          5H USED · RESET  WEEK USED · RESET  CURRENT\n"
            "claude    work     —      unconfigured  —                —                  —\n",
        )
        self.assertFalse(self.state.exists())
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o644)

    def test_current_and_list_show_the_account_that_handled_the_latest_request(self):
        self.add_account("codex", "primary", priority=100)
        self.add_account("codex", "fallback", priority=0)
        self.management.server.accounts["codex-primary.json"]["recent_requests"] = [
            {"time": "19:20-19:30", "success": 2, "failed": 1},
            {"time": "19:30-19:40", "success": 0, "failed": 1},
        ]
        self.management.server.accounts["codex-fallback.json"]["recent_requests"] = [
            {"time": "19:20-19:30", "success": 0, "failed": 0},
            {"time": "19:30-19:40", "success": 1, "failed": 0},
        ]
        self.write_state(codex={
            "accounts": {"codex-primary.json": "20x", "codex-fallback.json": "5x"},
            "primary": "codex-primary.json",
            "auto": True,
        })

        listed = self.run_fleet("list", "codex")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertRegex(listed.stdout, r"5x\s+— · Fallback\s+limits\s+—\s+—\s+✓")
        self.assertRegex(listed.stdout, r"20x\s+— · Main\s+limits\s+—\s+—\s+—")

    def test_add_uses_exact_binary_and_management_api_without_parsing_oauth_file(self):
        self.fake_login("claude", "claude-new.json")

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 0, result.stderr)
        credential = self.auth / "claude-new.json"
        self.assertEqual(credential.read_bytes(), b"opaque oauth bytes")
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(policy["primary"], "claude-new.json")
        self.assertIs(policy["auto"], False)
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], self.management.url)
        self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_RETRIES"], "3")

    def test_add_codex_enables_first_account_and_routes_shared_home(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-5"\n')
        self.fake_login("codex", "codex-new.json")

        result = self.run_fleet("add", "codex", "new")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Auto switching is off", result.stdout)
        self.assertFalse(self.management.server.accounts["codex-new.json"]["disabled"])
        value = tomllib.loads(config.read_text())
        self.assertEqual(value["model"], "gpt-5")
        self.assertNotIn("model_provider", value)
        self.assertNotIn("fleet_cliproxyapi", value.get("model_providers", {}))
        self.assertEqual(value["openai_base_url"], self.management.url + "/v1")
        self.assertTrue((self.home / ".local/state/fleet/client-routing.json").exists())
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy["accounts"], {"codex-new.json": "new"})
        self.assertEqual(policy["primary"], "codex-new.json")
        self.assertIs(policy["auto"], False)

    def test_add_requires_explicit_subscription_routing_flag_before_login(self):
        self.fake_login("claude", "claude-new.json")
        self.environment.pop("FLEET_ENABLE_SUBSCRIPTION_ROUTING")

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("FLEET_ENABLE_SUBSCRIPTION_ROUTING=1", result.stderr)
        self.assertFalse((self.auth / "claude-new.json").exists())
        self.assertFalse(self.state.exists())
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertFalse(self.management.server.history)

    def test_login_is_explicit_consent_without_an_environment_flag(self):
        self.fake_login("codex", "codex-new.json")
        self.environment.pop("FLEET_ENABLE_SUBSCRIPTION_ROUTING")

        result = self.run_fleet("login", "codex", "work")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Added codex account work", result.stdout)
        self.assertTrue((self.auth / "codex-new.json").exists())

    def test_add_rejects_a_login_config_with_a_different_live_auth_directory(self):
        other = self.home / "other-auth"
        other.mkdir()
        self.write_config(other)
        self.fake_login("claude", "claude-wrong.json")

        result = self.run_fleet("add", "claude", "wrong")

        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match the running CLIProxyAPI auth-dir", result.stderr)
        self.assertFalse((self.auth / "claude-wrong.json").exists())
        self.assertFalse(self.state.exists())

    def test_add_requires_preexisting_credentials_to_be_enrolled_before_login(self):
        unmanaged = self.add_account("claude", "unmanaged", contents=b"secret must not print")
        unmanaged.chmod(0o644)
        self.fake_login("claude", "claude-new.json")

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("claude-unmanaged.json", result.stderr)
        self.assertIn("fleet account enroll claude <account>", result.stderr)
        self.assertNotIn("secret must not print", result.stderr)
        self.assertFalse((self.auth / "claude-new.json").exists())
        self.assertEqual(stat.S_IMODE(unmanaged.stat().st_mode), 0o644)
        self.assertEqual(self.management.server.accounts["claude-unmanaged.json"].get("priority"), None)
        self.assertFalse(self.management.server.accounts["claude-unmanaged.json"]["disabled"])

    def test_second_add_stays_disabled_until_auto_is_enabled(self):
        self.fake_login("claude", "claude-primary.json")
        first = self.run_fleet("add", "claude", "primary")
        self.fake_login("claude", "claude-backup.json")

        second = self.run_fleet("add", "claude", "backup")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(self.management.server.accounts["claude-primary.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["claude-backup.json"]["disabled"])

    def test_add_refreshes_the_same_account_without_a_duplicate(self):
        credential = self.add_account("codex", "same", priority=100, contents=b"old oauth bytes")
        self.write_state(codex={"accounts": {"codex-same.json": "20x"}, "primary": "codex-same.json", "auto": True})
        self.fake_login("codex", "codex-same.json")

        result = self.run_fleet("add", "codex")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Refreshed codex account 20x", result.stdout)
        self.assertEqual(credential.read_bytes(), b"opaque oauth bytes")
        self.assertEqual(list(self.management.server.accounts), ["codex-same.json"])
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {"codex-same.json": "20x"}, "primary": "codex-same.json", "auto": True})

    def test_add_refresh_can_rename_the_same_account(self):
        self.add_account("claude", "same", disabled=True, contents=b"old oauth bytes")
        self.write_state(claude={"accounts": {"claude-same.json": "old"}, "primary": "claude-same.json", "auto": False})
        self.fake_login("claude", "claude-same.json")

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Refreshed claude account new", result.stdout)
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(policy, {"accounts": {"claude-same.json": "new"}, "primary": "claude-same.json", "auto": False})

    def test_native_codex_login_syncs_the_same_oauth_credential(self):
        native_auth = self.codex_native_auth()
        native = self.native_client(
            f"home = Path(os.environ['FLEET_ACCOUNT_HOME'])\n"
            f"(home / '.codex').mkdir()\n"
            f"(home / '.codex/auth.json').write_text({json.dumps(native_auth)!r})\n"
        )

        result = self.run_fleet("native-auth", "codex", str(native), "login", "--device-auth")

        self.assertEqual(result.returncode, 0, result.stderr)
        name = "codex-07e99801-work@example.com-pro.json"
        credential = json.loads((self.auth / name).read_text())
        self.assertEqual(credential["access_token"], "native-access")
        self.assertEqual(credential["refresh_token"], "native-refresh")
        self.assertEqual(credential["account_id"], "account-1")
        self.assertEqual(credential["type"], "codex")
        self.assertEqual(json.loads((self.home / ".codex/auth.json").read_text()), native_auth)
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {name: "work@example.com"}, "primary": name, "auto": False})
        self.assertEqual((self.base / "native-calls").read_text(), "login --device-auth")

    def test_native_claude_login_bypasses_only_fleet_settings_and_syncs_oauth(self):
        credentials = {
            "claudeAiOauth": {
                "accessToken": "claude-access",
                "refreshToken": "claude-refresh",
                "expiresAt": 1893456000000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
        profile = {
            "oauthAccount": {
                "emailAddress": "claude@example.com",
                "accountUuid": "account-uuid",
                "organizationUuid": "organization-uuid",
                "organizationName": "Work",
            }
        }
        native = self.native_client(
            f"home = Path(os.environ['FLEET_ACCOUNT_HOME'])\n"
            f"(home / '.claude').mkdir()\n"
            f"(home / '.claude/.credentials.json').write_text({json.dumps(credentials)!r})\n"
            f"(home / '.claude.json').write_text({json.dumps(profile)!r})\n"
        )

        result = self.run_fleet("native-auth", "claude", str(native), "auth", "login", "--sso")

        self.assertEqual(result.returncode, 0, result.stderr)
        credential = json.loads((self.auth / "claude-claude@example.com.json").read_text())
        self.assertEqual(credential["access_token"], "claude-access")
        self.assertEqual(credential["refresh_token"], "claude-refresh")
        self.assertEqual(credential["account_uuid"], "account-uuid")
        self.assertEqual(credential["expired"], "2030-01-01T00:00:00Z")
        self.assertEqual(credential["type"], "claude")
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(
            policy,
            {
                "accounts": {"claude-claude@example.com.json": "claude@example.com"},
                "primary": "claude-claude@example.com.json",
                "auto": False,
            },
        )
        self.assertEqual((self.base / "native-calls").read_text(), "--setting-sources  auth login --sso")

    def test_native_claude_import_uses_keychain_when_file_only_has_mcp_logins(self):
        directory = self.home / ".claude"
        directory.mkdir()
        path = directory / ".credentials.json"
        path.write_text('{"mcpOAuth":{}}')
        (self.home / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"claude@example.com"}}')
        credentials = {"claudeAiOauth": {"accessToken": "max-access", "refreshToken": "max-refresh", "expiresAt": 1893456000000}}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory)}), mock.patch.object(ACCOUNTS, "claude_keychain_credential", return_value=credentials) as keychain:
            name, _, _, payload = ACCOUNTS.native_claude_credential(self.home)
        keychain.assert_called_once_with(self.home)
        self.assertEqual(name, "claude-claude@example.com.json")
        self.assertEqual(payload["access_token"], "max-access")
        self.assertEqual(payload["refresh_token"], "max-refresh")
        self.assertEqual(path.read_text(), '{"mcpOAuth":{}}')

    def test_native_login_refreshes_the_matching_account_without_losing_its_label(self):
        old = self.codex_native_auth(access="old-access")
        name = "codex-07e99801-work@example.com-pro.json"
        credential = self.add_account("codex", name.removeprefix("codex-").removesuffix(".json"), priority=100, contents=json.dumps(old).encode())
        self.management.server.accounts[name]["email"] = "work@example.com"
        self.management.server.accounts[name]["id_token"] = decode_jwt(old["tokens"]["id_token"])
        self.write_state(codex={"accounts": {name: "20x"}, "primary": name, "auto": True})
        refreshed = self.codex_native_auth(access="new-access")
        native = self.native_client(
            f"home = Path(os.environ['FLEET_ACCOUNT_HOME'])\n"
            f"(home / '.codex').mkdir()\n"
            f"(home / '.codex/auth.json').write_text({json.dumps(refreshed)!r})\n"
        )

        result = self.run_fleet("native-auth", "codex", str(native), "login")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(credential.read_text())["access_token"], "new-access")
        self.assertEqual(list(self.management.server.accounts), [name])
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {name: "20x"}, "primary": name, "auto": True})

    def test_failed_native_login_does_not_change_fleet(self):
        native = self.native_client(exit_code=7)

        result = self.run_fleet("native-auth", "codex", str(native), "login")

        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.state.exists())
        self.assertFalse(list(self.auth.iterdir()))

    def test_native_logout_removes_the_matching_fleet_account(self):
        native_auth = self.codex_native_auth()
        auth_file = self.home / ".codex/auth.json"
        auth_file.parent.mkdir()
        auth_file.write_text(json.dumps(native_auth))
        name = "codex-07e99801-work@example.com-pro.json"
        primary = "codex-primary.json"
        self.add_account("codex", name.removeprefix("codex-").removesuffix(".json"), priority=100, contents=b"proxy credential")
        self.add_account("codex", "primary", priority=0, contents=b"primary credential")
        self.management.server.accounts[name]["email"] = "work@example.com"
        self.management.server.accounts[name]["id_token"] = decode_jwt(native_auth["tokens"]["id_token"])
        self.write_state(codex={"accounts": {name: "20x", primary: "main"}, "primary": primary, "auto": True})
        native = self.native_client("Path(os.environ['FLEET_ACCOUNT_HOME'], '.codex/auth.json').unlink()")

        result = self.run_fleet("native-auth", "codex", str(native), "logout")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(auth_file.exists())
        self.assertFalse((self.auth / name).exists())
        self.assertTrue((self.auth / primary).exists())
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {primary: "main"}, "primary": primary, "auto": True})

    def test_list_requires_management_api_instead_of_reading_oauth_files(self):
        credential = self.add_account("codex", "private", contents=b"not json at all")
        self.environment["FLEET_CLIPROXY_URL"] = "http://127.0.0.1:1"

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Enable CLIProxyAPI remote-management.secret-key", result.stderr)
        self.assertEqual(credential.read_bytes(), b"not json at all")

    def test_list_does_not_warn_that_enrolled_codex_is_unrouted(self):
        self.add_account("codex", "work", disabled=True)
        self.write_state(codex={"accounts": {"codex-work.json": "work"}, "primary": "codex-work.json", "auto": False})

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("not routed", result.stdout)

    def test_list_formats_roles_and_auto_policy(self):
        self.add_account("claude", "admin")
        self.add_account("codex", "backup")
        self.add_account("codex", "primary")
        self.write_state(
            claude={"accounts": {"claude-admin.json": "admin@sampleapp.com"}, "primary": "claude-admin.json", "auto": False},
            codex={
                "accounts": {"codex-backup.json": "5x", "codex-primary.json": "20x"},
                "primary": "codex-primary.json",
                "auto": True,
            },
        )

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "PROVIDER  ACCOUNT              ORDER         AUTO    5H USED · RESET  WEEK USED · RESET  CURRENT\n"
            "claude    admin@sampleapp.com  — · Main      off     —                —                  —\n"
            "codex     20x                  — · Main      limits  —                —                  —\n"
            "codex     5x                   — · Fallback  limits  —                —                  —\n",
        )

        codex = self.run_fleet("list", "codex")

        self.assertEqual(codex.returncode, 0, codex.stderr)
        self.assertNotIn("claude", codex.stdout)
        self.assertIn("codex", codex.stdout)

    def test_list_shows_numeric_saved_order_and_current_account_for_both_providers(self):
        policies = {}
        for provider in ("claude", "codex"):
            labels = [f"account-{index:02}" for index in range(12, 0, -1)]
            names = [f"{provider}-{label}.json" for label in labels]
            for label in reversed(labels):
                self.add_account(provider, label, priority=0)
            policies[provider] = {"accounts": dict(zip(names, labels)), "primary": names[0], "auto": provider == "codex", "order": names}
            self.management.server.accounts[names[1]]["recent_requests"] = [{"time": "19:30-19:40", "success": 1}]
        self.write_state(**policies)
        before = self.state.read_bytes()
        result = self.run_fleet("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = result.stdout.splitlines()[1:]
        self.assertEqual(len(rows), 24)
        for index, provider in enumerate(("claude", "codex")):
            for rank, row in enumerate(rows[index * 12:(index + 1) * 12], 1):
                role = "Main" if rank == 1 else "Fallback"
                auto = "off" if provider == "claude" else "limits"
                current = "✓" if rank == 2 else "—"
                self.assertRegex(row, rf"^{provider}\s+account-{13-rank:02}\s+{rank} · {role}\s+{auto}\s+—\s+—\s+{current}$")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse([entry for entry in self.management.server.history if not entry[0].endswith("api-call")])

    def test_rename_changes_only_the_saved_account_label(self):
        self.add_account("codex", "work", disabled=True, contents=b"opaque codex token")
        self.write_state(codex={"accounts": {"codex-work.json": "old"}, "primary": "codex-work.json", "auto": False})

        result = self.run_fleet("rename", "codex", "old", "Work Pro")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.management.server.history)
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {"codex-work.json": "Work Pro"}, "primary": "codex-work.json", "auto": False})

    def test_remove_fallback_deletes_only_that_account(self):
        primary = self.add_account("codex", "primary", priority=100, contents=b"primary oauth")
        fallback = self.add_account("codex", "fallback", priority=0, contents=b"fallback oauth")
        self.write_state(
            codex={
                "accounts": {"codex-primary.json": "20x", "codex-fallback.json": "5x"},
                "primary": "codex-primary.json",
                "auto": True,
            }
        )

        result = self.run_fleet("logout", "codex", "5x")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Removed codex account 5x.\n")
        self.assertTrue(primary.exists())
        self.assertFalse(fallback.exists())
        self.assertEqual(list(self.management.server.accounts), ["codex-primary.json"])
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {"codex-primary.json": "20x"}, "primary": "codex-primary.json", "auto": True})

    def test_remove_primary_promotes_and_enables_a_fallback(self):
        primary = self.add_account("claude", "primary", priority=100, contents=b"primary oauth")
        fallback = self.add_account("claude", "fallback", disabled=True, priority=0, contents=b"fallback oauth")
        self.write_state(
            claude={
                "accounts": {"claude-primary.json": "main", "claude-fallback.json": "backup"},
                "primary": "claude-primary.json",
                "auto": False,
            }
        )

        result = self.run_fleet("remove", "claude", "main")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Primary is now backup", result.stdout)
        self.assertFalse(primary.exists())
        self.assertTrue(fallback.exists())
        live = self.management.server.accounts["claude-fallback.json"]
        self.assertFalse(live["disabled"])
        self.assertEqual(live["priority"], 100)
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(policy, {"accounts": {"claude-fallback.json": "backup"}, "primary": "claude-fallback.json", "auto": False})

    def test_remove_last_account_clears_the_provider_policy(self):
        credential = self.add_account("codex", "only", priority=100)
        self.write_state(codex={"accounts": {"codex-only.json": "only"}, "primary": "codex-only.json", "auto": True})

        result = self.run_fleet("remove", "codex", "only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(credential.exists())
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy, {"accounts": {}, "primary": None, "auto": None})

    def test_logout_failure_keeps_the_account_and_restores_the_primary(self):
        primary = self.add_account("claude", "primary", priority=100)
        fallback = self.add_account("claude", "fallback", disabled=True, priority=0)
        original = {
            "accounts": {"claude-primary.json": "main", "claude-fallback.json": "backup"},
            "primary": "claude-primary.json",
            "auto": False,
        }
        self.write_state(claude=original)
        self.management.server.delete_fail = True

        result = self.run_fleet("logout", "claude", "main")

        self.assertEqual(result.returncode, 2)
        self.assertTrue(primary.exists())
        self.assertTrue(fallback.exists())
        self.assertEqual(json.loads(self.state.read_text())["providers"]["claude"], original)
        self.assertFalse(self.management.server.accounts["claude-primary.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["claude-fallback.json"]["disabled"])

    def test_state_version_must_be_an_integer(self):
        self.write_state()
        value = json.loads(self.state.read_text())
        value["version"] = True
        self.state.write_text(json.dumps(value))

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid Fleet account state", result.stderr)

    def test_invalid_management_url_fails_without_traceback(self):
        self.environment["FLEET_CLIPROXY_URL"] = "http://localhost:not-a-port"

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("localhost HTTP or HTTPS URL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_use_changes_priority_through_management_api_without_rewriting_credentials(self):
        first = self.add_account("claude", "first", priority=7, contents=b"first opaque bytes")
        second = self.add_account("claude", "second", contents=b"second opaque bytes")
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new sessions try it first", result.stdout)
        self.assertEqual(first.read_bytes(), b"first opaque bytes")
        self.assertEqual(second.read_bytes(), b"second opaque bytes")
        self.assertEqual(self.management.server.accounts["claude-first.json"]["priority"], 0)
        self.assertEqual(self.management.server.accounts["claude-second.json"]["priority"], 100)
        self.assertFalse(self.management.server.accounts["claude-second.json"]["disabled"])
        saved = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(saved["primary"], "claude-second.json")

    def test_order_sets_and_keeps_the_full_account_sequence_for_both_providers(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                names = [f"{provider}-{name}.json" for name in ("main", "last", "next")]
                for name in ("main", "last", "next"):
                    self.add_account(provider, name, priority=100 if name == "main" else 0)
                self.write_state(**{provider: {"accounts": dict(zip(names, ("Main", "Last", "Next"))), "primary": names[0], "auto": False}})
                before = self.state.read_bytes()
                shown = self.run_fleet("order", provider)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertIn("No fixed", shown.stdout)
                self.assertEqual(self.state.read_bytes(), before)

                result = self.run_fleet("order", provider, "Next", "Main", "Last")
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = [names[2], names[0], names[1]]
                for arguments in ((), ("auto", provider, "on"), ("auto", provider, "off"), ("auto", provider, "on")):
                    if arguments:
                        result = self.run_fleet(*arguments)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    policy = json.loads(self.state.read_text())["providers"][provider]
                    self.assertEqual(policy["order"], expected)
                    self.assertEqual(policy["primary"], expected[0])
                    for index, name in enumerate(expected):
                        account = self.management.server.accounts[name]
                        self.assertEqual(account["priority"], len(expected) - index)
                        self.assertEqual(account["disabled"], not policy["auto"] and index != 0)
                result = self.run_fleet("use", provider, "Last")
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.run_fleet("rename", provider, "Next", "Backup")
                self.assertEqual(result.returncode, 0, result.stderr)
                shown = self.run_fleet("order", provider)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertIn("Last -> Backup -> Main", shown.stdout)
                self.assertEqual(json.loads(self.state.read_text())["providers"][provider]["order"], [names[1], names[2], names[0]])

    def test_order_rejects_missing_duplicate_unknown_and_cross_provider_accounts(self):
        self.add_account("codex", "first", priority=100)
        self.add_account("codex", "second", priority=0)
        self.add_account("claude", "other", priority=100)
        self.write_state(codex={"accounts": {"codex-first.json": "First", "codex-second.json": "Second"}, "primary": "codex-first.json", "auto": True})
        before = self.state.read_bytes()
        for arguments in (("First",), ("First", "codex-first.json"), ("First", "unknown"), ("First", "claude-other.json")):
            with self.subTest(arguments=arguments):
                result = self.run_fleet("order", "codex", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertFalse(self.management.server.history)

    def test_order_does_not_remove_primary_when_next_account_is_missing(self):
        self.add_account("codex", "first", priority=2)
        self.write_state(codex={"accounts": {"codex-first.json": "First", "codex-second.json": "Second"}, "primary": "codex-first.json", "auto": True, "order": ["codex-first.json", "codex-second.json"]})
        before = self.state.read_bytes()
        result = self.run_fleet("remove", "codex", "First")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no longer available", result.stderr)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.management.server.history)

    def test_order_rolls_back_when_proxy_or_state_save_fails(self):
        self.add_account("codex", "first", priority=100)
        self.add_account("codex", "second", disabled=True, priority=0)
        self.write_state(codex={"accounts": {"codex-first.json": "First", "codex-second.json": "Second"}, "primary": "codex-first.json", "auto": False})
        before = self.state.read_bytes()
        accounts = json.loads(json.dumps(self.management.server.accounts))
        self.management.server.fail_at = 2
        result = self.run_fleet("order", "codex", "Second", "First")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.management.server.accounts, accounts)
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(ACCOUNTS, "write_state_transaction", side_effect=ACCOUNTS.fleet.FleetError("save failed")):
            with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "save failed"):
                ACCOUNTS.use_account(self.home, "codex", "Second", order=["Second", "First"])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.management.server.accounts, accounts)

    def test_order_survives_login_refresh_enrollment_and_removal(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                for name in ("first", "second"):
                    self.add_account(provider, name, priority=100 if name == "first" else 0)
                first, second, new = [f"{provider}-{name}.json" for name in ("first", "second", "new")]
                self.write_state(**{provider: {"accounts": {first: "First", second: "Second"}, "primary": first, "auto": True}})
                result = self.run_fleet("order", provider, "Second", "First")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.fake_login(provider, new)
                for _ in range(2):
                    result = self.run_fleet("add", provider, "New")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(self.state.read_text())["providers"][provider]["order"], [second, first, new])
                    self.assertEqual([self.management.server.accounts[name]["priority"] for name in (second, first, new)], [3, 2, 1])
                self.add_account(provider, "enrolled", disabled=True, priority=0)
                result = self.run_fleet("login", provider, "--existing", "enrolled")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.state.read_text())["providers"][provider]["order"], [second, first, new, f"{provider}-enrolled.json"])
                for name in (new, second, first, f"{provider}-enrolled.json"):
                    result = self.run_fleet("remove", provider, name)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    policy = json.loads(self.state.read_text())["providers"][provider]
                    self.assertNotIn(name, policy["order"])
                    self.assertEqual(policy["primary"], policy["order"][0] if policy["order"] else None)
                    for index, remaining in enumerate(policy["order"]):
                        self.assertEqual(self.management.server.accounts[remaining]["priority"], len(policy["order"]) - index)
                result = self.run_fleet("add", provider, "New")
                self.assertEqual(result.returncode, 0, result.stderr)
                policy = json.loads(self.state.read_text())["providers"][provider]
                self.assertEqual(policy["order"], [new])
                self.assertEqual(policy["primary"], new)
                self.assertFalse(policy["auto"])

    def test_order_state_requires_each_account_once_with_primary_first(self):
        self.write_state(codex={"accounts": {"codex-first.json": "First", "codex-second.json": "Second"}, "primary": "codex-first.json", "auto": True})
        state = json.loads(self.state.read_text())
        for order in (None, "bad", [], ["codex-first.json"], ["codex-first.json"] * 2, ["codex-first.json", {}], ["codex-second.json", "codex-first.json"]):
            with self.subTest(order=order):
                state["providers"]["codex"]["order"] = order
                self.assertFalse(ACCOUNTS.valid_state(state))
        state["providers"]["codex"]["order"] = ["codex-first.json", "codex-second.json"]
        self.assertTrue(ACCOUNTS.valid_state(state))

    def test_codex_use_changes_primary_and_disables_others_when_auto_is_off(self):
        first = self.add_account("codex", "first", priority=100)
        second = self.add_account("codex", "second", priority=0)
        first.chmod(0o644)
        second.chmod(0o644)
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json", "auto": False})
        result = self.run_fleet("use", "codex", "second")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Primary Codex account: second", result.stdout)
        self.assertTrue(self.management.server.accounts["codex-first.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["codex-second.json"]["disabled"])
        self.assertEqual(self.management.server.accounts["codex-first.json"]["priority"], 0)
        self.assertEqual(self.management.server.accounts["codex-second.json"]["priority"], 100)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o600)
        saved = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(saved["primary"], "codex-second.json")

    def test_manual_choice_resets_only_its_provider(self):
        self.add_account("codex", "first")
        self.add_account("codex", "second")
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json", "auto": True})
        self.assertEqual(self.run_fleet("use", "codex", "second").returncode, 0)
        resets = [entry for entry in self.management.server.history if entry[0].endswith("/session-affinity/reset")]
        self.assertEqual(resets, [("/v0/management/routing/session-affinity/reset", "codex", True)])

    def test_manual_choice_rejects_unpatched_proxy_before_changing_accounts(self):
        self.add_account("claude", "first", priority=9)
        self.add_account("claude", "second", priority=4)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        before = self.state.read_bytes()
        self.management.server.config.pop("fleet-session-affinity-supported")

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Update the Fleet proxy", result.stderr)
        self.assertEqual(self.management.server.history, [])
        self.assertEqual(self.state.read_bytes(), before)

    def test_manual_choice_reset_failure_rolls_back_accounts_and_saved_state(self):
        self.add_account("claude", "first", priority=9)
        self.add_account("claude", "second", priority=4)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        before = self.state.read_bytes()
        self.management.server.affinity_reset_fail = True

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 2)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual(self.management.server.accounts["claude-first.json"]["priority"], 9)
        self.assertEqual(self.management.server.accounts["claude-second.json"]["priority"], 4)
        self.assertEqual(self.state.read_bytes(), before)

    def test_codex_use_allows_a_routing_no_op_and_persists_auto_off(self):
        self.add_account("codex", "first", disabled=False, priority=100)
        self.management.server.accounts["codex-first.json"]["websockets"] = True
        self.write_state(codex={"accounts": {"codex-first.json": "first"}, "primary": "codex-first.json"})

        result = self.run_fleet("use", "codex", "first")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.management.server.history)
        saved = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertIs(saved["auto"], False)

    def test_first_claude_use_treats_unconfigured_auto_as_off(self):
        self.add_account("claude", "first", disabled=False, priority=100)
        self.add_account("claude", "second", disabled=False, priority=0)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json"})

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("existing Claude session can rebind on its next request", result.stdout)
        self.assertTrue(self.management.server.accounts["claude-first.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["claude-second.json"]["disabled"])
        saved = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertIs(saved["auto"], False)

    def test_use_rejects_an_unenrolled_account_without_mutation(self):
        managed = self.add_account("claude", "managed", priority=100)
        unmanaged = self.add_account("claude", "unmanaged", priority=0)
        managed.chmod(0o644)
        unmanaged.chmod(0o644)
        self.write_state(claude={"accounts": {"claude-managed.json": "managed"}, "primary": "claude-managed.json", "auto": False})

        result = self.run_fleet("use", "claude", "unmanaged")

        self.assertEqual(result.returncode, 2)
        self.assertIn("enroll", result.stderr.lower())
        self.assertFalse(self.management.server.history)
        self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(unmanaged.stat().st_mode), 0o644)

    def test_use_rejects_a_live_unenrolled_backup_before_mutation(self):
        managed = self.add_account("claude", "managed", priority=0)
        unmanaged = self.add_account("claude", "unmanaged", priority=100, contents=b"secret must not print")
        managed.chmod(0o644)
        unmanaged.chmod(0o644)
        self.write_state(claude={"accounts": {"claude-managed.json": "managed"}, "primary": "claude-managed.json", "auto": True})

        result = self.run_fleet("use", "claude", "managed")

        self.assertEqual(result.returncode, 2)
        self.assertIn("claude-unmanaged.json", result.stderr)
        self.assertIn("fleet account enroll claude <account>", result.stderr)
        self.assertNotIn("secret must not print", result.stderr)
        self.assertFalse(self.management.server.history)
        self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(unmanaged.stat().st_mode), 0o644)

    def test_use_with_auto_off_enables_selected_primary_then_disables_old_primary(self):
        self.add_account("claude", "first", disabled=False, priority=100)
        self.add_account("claude", "second", disabled=True, priority=0)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": False})

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.management.server.accounts["claude-first.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["claude-second.json"]["disabled"])
        self.assertEqual(self.management.server.history[0], ("/v0/management/auth-files/status", "claude-second.json", False))
        saved = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(saved["primary"], "claude-second.json")

    def test_use_with_auto_off_rolls_back_enabled_selected_account_on_failure(self):
        self.add_account("claude", "first", disabled=False, priority=100)
        self.add_account("claude", "second", disabled=True, priority=0)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": False})
        before_state = self.state.read_bytes()
        self.management.server.fail_at = 2

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 2)
        self.assertIn("rolled back", result.stderr)
        self.assertFalse(self.management.server.accounts["claude-first.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["claude-second.json"]["disabled"])
        self.assertEqual(self.state.read_bytes(), before_state)

    def test_use_rolls_back_management_changes_and_does_not_save_state_on_failure(self):
        self.add_account("claude", "first", priority=9)
        self.add_account("claude", "second", priority=4)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        before_state = self.state.read_bytes()
        self.management.server.fail_at = 2

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 2)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual(self.management.server.accounts["claude-first.json"]["priority"], 9)
        self.assertEqual(self.management.server.accounts["claude-second.json"]["priority"], 4)
        self.assertEqual(self.state.read_bytes(), before_state)

    def test_state_save_failure_rolls_back_completed_management_changes(self):
        self.add_account("claude", "first", priority=9)
        self.add_account("claude", "second", priority=4)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        before_state = self.state.read_bytes()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(ACCOUNTS.fleet, "atomic_write", side_effect=PermissionError):
            with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "rolled back"):
                ACCOUNTS.auto_account(self.home, "claude", False)
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(self.management.server.accounts["claude-first.json"]["priority"], 9)
        self.assertEqual(self.management.server.accounts["claude-second.json"]["priority"], 4)
        self.assertFalse(self.management.server.accounts["claude-second.json"]["disabled"])

    def test_management_postcondition_failure_rolls_back_before_state_save(self):
        self.add_account("claude", "first", priority=9)
        self.add_account("claude", "second", priority=4)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        before_state = self.state.read_bytes()
        self.management.server.ignore_at = 1

        result = self.run_fleet("use", "claude", "second")

        self.assertEqual(result.returncode, 2)
        self.assertIn("postcondition", result.stderr)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual(self.management.server.accounts["claude-first.json"]["priority"], 9)
        self.assertEqual(self.management.server.accounts["claude-second.json"]["priority"], 4)
        self.assertEqual(self.state.read_bytes(), before_state)

    def test_management_forward_change_stops_on_a_concurrent_field_change(self):
        account = {"name": "claude-first.json", "provider": "claude", "disabled": False, "priority": 9}

        class ConcurrentClient:
            def __init__(self):
                self.live = 4
                self.writes = []

            def priority(self, _, value):
                self.writes.append(value)
                self.live = value

            def accounts(self):
                return [{**account, "priority": self.live}]

        client = ConcurrentClient()
        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "changed"):
            ACCOUNTS.apply_transaction(client, [("priority", account, 100, 9)], lambda: None)
        self.assertEqual(client.live, 4)
        self.assertEqual(client.writes, [])

    def test_management_rollback_does_not_overwrite_a_concurrent_field_change(self):
        account = {"name": "claude-first.json", "provider": "claude", "disabled": False, "priority": 9}

        class ConcurrentClient:
            def __init__(self):
                self.live = 9
                self.writes = []

            def priority(self, _, value):
                self.writes.append(value)
                if value == 100:
                    self.live = 77
                    raise ACCOUNTS.fleet.FleetError("injected failure")
                self.live = value

            def accounts(self):
                return [{**account, "priority": self.live}]

        client = ConcurrentClient()
        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "rollback failed"):
            ACCOUNTS.apply_transaction(client, [("priority", account, 100, 9)], lambda: None)
        self.assertEqual(client.live, 77)
        self.assertEqual(client.writes, [100])

    def test_keyboard_interrupt_rolls_back_attempted_management_change(self):
        account = {"name": "claude-first.json", "provider": "claude", "disabled": False, "priority": 9}

        class InterruptedClient:
            def __init__(self):
                self.live = 9

            def priority(self, _, value):
                self.live = value
                if value == 100:
                    raise KeyboardInterrupt

            def accounts(self):
                return [{**account, "priority": self.live}]

        client = InterruptedClient()
        with self.assertRaises(KeyboardInterrupt):
            ACCOUNTS.apply_transaction(client, [("priority", account, 100, 9)], lambda: None)
        self.assertEqual(client.live, 9)

    def test_credit_modes_preserve_accounts_and_other_routing_choices(self):
        self.management.server.config["fleet-credit-routing-supported"] = True
        self.write_state()
        state_before = self.state.read_bytes()
        for provider in ("claude", "codex"):
            for mode in ("never", "last", "same-account"):
                with self.subTest(provider=provider, mode=mode):
                    result = self.run_fleet("credits", provider, mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    saved = json.loads(self.policy.read_text())
                    self.assertEqual(saved["credits"][provider], mode)
                    self.assertEqual(saved["providers"], {"claude": "limits", "codex": "limits"})
                    self.assertEqual(self.policy.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(self.state.read_bytes(), state_before)
                    self.assertFalse(self.management.server.history)
        self.assertEqual(saved["credits"], {"claude": "same-account", "codex": "same-account"})

    def test_credit_changes_reject_invalid_modes_and_unpatched_router(self):
        for args in (("codex", "never"), ("codex", "invalid"), ("other", "last")):
            with self.subTest(args=args):
                result = self.run_fleet("credits", *args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.policy.exists())
                self.assertFalse(self.management.server.history)

    def test_invalid_saved_credit_policy_cannot_be_overwritten(self):
        self.management.server.config["fleet-credit-routing-supported"] = True
        self.policy.write_text('{"version":1,"providers":{"claude":"limits","codex":"limits"},"credits":{"codex":"invalid"}}')
        self.policy.chmod(0o600)
        before = self.policy.read_bytes()
        result = self.run_fleet("credits", "codex", "never")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.policy.read_bytes(), before)



    def test_spoke_credit_change_requires_the_hub_without_writing_local_policy(self):
        ACCOUNTS.hub_state_path(self.home).write_bytes(ACCOUNTS.hub_state_bytes("spoke", "owner@example.com", "http://hub.example.ts.net:8317"))
        ACCOUNTS.hub_state_path(self.home).chmod(0o600)
        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Run credit changes on the Fleet hub"):
            ACCOUNTS.credit_account(self.home, "codex", "last")
        self.assertFalse(self.policy.exists())
        self.assertFalse(self.management.server.history)

    def test_auto_on_enables_every_claude_account_in_one_bounded_pass(self):
        accounts = {}
        for number in range(12):
            name = f"claude-{number:02}.json"
            self.add_account("claude", f"{number:02}", disabled=number != 0, priority=number)
            accounts[name] = str(number)
        self.write_state(claude={"accounts": accounts, "primary": "claude-00.json", "auto": False})

        result = self.run_fleet("auto", "claude", "on")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("New sessions try the primary first", result.stdout)
        live = self.management.server.accounts
        self.assertTrue(all(not item["disabled"] for item in live.values()))
        self.assertEqual(live["claude-00.json"]["priority"], 100)
        self.assertTrue(all(live[f"claude-{number:02}.json"]["priority"] == 0 for number in range(1, 12)))
        self.assertLessEqual(self.management.server.mutation_count, 24)

    def test_auto_rejects_live_unenrolled_accounts_before_mutation(self):
        first = self.add_account("claude", "first", disabled=True, priority=7)
        second = self.add_account("claude", "second", disabled=True, priority=4)
        unmanaged = self.add_account("claude", "unmanaged", disabled=False, priority=55)
        unmanaged.chmod(0o644)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": False})

        result = self.run_fleet("auto", "claude", "off")

        self.assertEqual(result.returncode, 2)
        self.assertIn("claude-unmanaged.json", result.stderr)
        self.assertIn("fleet account enroll claude <account>", result.stderr)
        self.assertTrue(self.management.server.accounts["claude-first.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["claude-second.json"]["disabled"])
        self.assertEqual(self.management.server.accounts["claude-unmanaged.json"]["priority"], 55)
        self.assertFalse(self.management.server.accounts["claude-unmanaged.json"]["disabled"])
        self.assertFalse(self.management.server.history)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(unmanaged.stat().st_mode), 0o644)

    def test_auto_on_requires_exact_same_thread_safety_config(self):
        self.add_account("claude", "first")
        self.write_state(claude={"accounts": {"claude-first.json": "first"}, "primary": "claude-first.json"})
        cases = (
            (("api-keys",), ["wrong-client-key"], "api-keys: managed client key"),
            (("routing", "strategy"), "round-robin", "routing.strategy: fill-first"),
            (("routing", "session-affinity"), False, "routing.session-affinity: true"),
            (("max-retry-credentials",), 2, "max-retry-credentials: 0"),
            (("request-retry",), 1, "request-retry: 0"),
            (("streaming", "bootstrap-retries"), 1, "streaming.bootstrap-retries: 0"),
            (("codex", "stream-bootstrap-buffering"), False, "codex.stream-bootstrap-buffering: true"),
        )
        for keys, value, message in cases:
            with self.subTest(keys=keys):
                target = self.management.server.config
                for key in keys[:-1]:
                    target = target[key]
                original = target[keys[-1]]
                target[keys[-1]] = value
                result = self.run_fleet("auto", "claude", "on")
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.management.server.history)
                target[keys[-1]] = original

    def test_mutation_requires_literal_loopback_and_local_management_before_credentials_change(self):
        credential = self.add_account("claude", "first", priority=100)
        credential.chmod(0o644)
        self.auth.chmod(0o755)
        self.write_state(claude={"accounts": {"claude-first.json": "first"}, "primary": "claude-first.json", "auto": False})
        cases = (
            (("host",), "localhost", "host: 127.0.0.1 or ::1"),
            (("host",), "0.0.0.0", "host: 127.0.0.1 or ::1"),
            (("remote-management", "allow-remote"), True, "remote-management.allow-remote: false"),
        )
        for keys, value, message in cases:
            with self.subTest(keys=keys, value=value):
                target = self.management.server.config
                for key in keys[:-1]:
                    target = target[key]
                original = target[keys[-1]]
                target[keys[-1]] = value
                result = self.run_fleet("auto", "claude", "on")
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.management.server.history)
                self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o644)
                self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o755)
                target[keys[-1]] = original

    def test_add_runs_the_full_safety_preflight_before_login(self):
        self.management.server.config["remote-management"]["allow-remote"] = True
        self.fake_login("claude", "claude-new.json")

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("remote-management.allow-remote: false", result.stderr)
        self.assertFalse((self.auth / "claude-new.json").exists())
        self.assertFalse(self.state.exists())

    def test_auto_codex_on_enables_all_accounts_for_quota_failover(self):
        self.add_account("codex", "first", disabled=False, priority=100)
        self.add_account("codex", "second", disabled=True, priority=0)
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json"})

        result = self.run_fleet("auto", "codex", "on")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex auto switching: on (limits only)", result.stdout)
        self.assertFalse(self.management.server.accounts["codex-first.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["codex-second.json"]["disabled"])
        rules = self.management.server.config["oauth-request-scoped-errors"]["codex"]
        self.assertEqual(rules[0], {"status": 429, "match": ["usage_limit_reached", "turn_rate_limit_exceeded"], "action": "continue-and-cooldown"})
        self.assertEqual(rules[1], {"status": 400, "match": ["model is not supported when using Codex with a ChatGPT account."], "action": "continue-and-cooldown"})
        self.assertEqual(rules[2], {"status": 400, "match-regexr": ["(?s).*"], "action": "stop"})
        self.assertEqual(json.loads(self.policy.read_text())["providers"]["codex"], "limits")
        saved = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertIs(saved["auto"], True)

    def test_auto_any_is_explicit_and_warns_that_other_errors_can_spend_fallback_quota(self):
        self.add_account("claude", "first", priority=100)
        self.add_account("claude", "second", disabled=True, priority=0)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json"})

        result = self.run_fleet("auto", "claude", "on", "--when", "any")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("limits + temporary errors", result.stdout)
        self.assertIn("fallback quota", result.stdout)
        self.assertNotIn("claude", self.management.server.config.get("oauth-request-scoped-errors", {}))
        self.assertEqual(json.loads(self.policy.read_text())["providers"]["claude"], "any")

    def test_auto_policy_rolls_back_when_an_account_change_fails(self):
        self.add_account("codex", "first", disabled=False, priority=100)
        self.add_account("codex", "second", disabled=True, priority=0)
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json", "auto": False})
        self.management.server.fail_at = 1

        result = self.run_fleet("auto", "codex", "on")

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("codex", self.management.server.config.get("oauth-request-scoped-errors", {}))
        self.assertFalse(self.policy.exists())
        self.assertTrue(self.management.server.accounts["codex-second.json"]["disabled"])

    def test_auto_help_and_invalid_when_show_supported_options_without_changes(self):
        help_result = self.run_fleet("auto", "--help")
        invalid = self.run_fleet("auto", "codex", "on", "--when", "weekly")

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("limits", help_result.stdout)
        self.assertIn("any", help_result.stdout)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("Valid --when options: limits, any", invalid.stderr)
        self.assertFalse(self.management.server.history)

    def test_auto_codex_off_keeps_only_primary_available(self):
        first = self.add_account("codex", "first", disabled=False, priority=7)
        second = self.add_account("codex", "second", disabled=False, priority=4)
        first.chmod(0o644)
        second.chmod(0o644)
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json", "auto": True})
        result = self.run_fleet("auto", "codex", "off")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.management.server.accounts["codex-first.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["codex-second.json"]["disabled"])
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o600)
        self.assertIs(json.loads(self.state.read_text())["providers"]["codex"]["auto"], False)

    def test_auto_codex_off_normalizes_priorities_when_primary_is_already_sole_enabled_account(self):
        self.add_account("codex", "first", disabled=False, priority=7)
        self.add_account("codex", "second", disabled=True, priority=4)
        self.write_state(codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json", "auto": True})

        result = self.run_fleet("auto", "codex", "off")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.management.server.accounts["codex-first.json"]["priority"], 100)
        self.assertEqual(self.management.server.accounts["codex-second.json"]["priority"], 0)
        saved = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertIs(saved["auto"], False)

    def test_auto_off_never_disables_every_account_when_primary_is_stale(self):
        self.add_account("claude", "live")
        self.write_state(claude={"accounts": {"claude-gone.json": "gone", "claude-live.json": "live"}, "primary": "claude-gone.json", "auto": True})

        result = self.run_fleet("auto", "claude", "off")

        self.assertEqual(result.returncode, 2)
        self.assertIn("selected primary is no longer available", result.stderr)
        self.assertFalse(self.management.server.accounts["claude-live.json"]["disabled"])
        self.assertFalse(self.management.server.history)

    def test_auto_off_changes_only_the_selected_provider(self):
        self.add_account("claude", "first")
        self.add_account("claude", "second")
        codex = self.add_account("codex", "only")
        codex.chmod(0o644)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})

        result = self.run_fleet("auto", "claude", "off")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("only the primary account is available", result.stdout)
        self.assertFalse(self.management.server.accounts["claude-first.json"]["disabled"])
        self.assertTrue(self.management.server.accounts["claude-second.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["codex-only.json"]["disabled"])
        self.assertEqual(codex.read_bytes(), b"private token bytes")
        self.assertEqual(stat.S_IMODE(codex.stat().st_mode), 0o644)
        self.assertFalse(any("codex" in item[1] for item in self.management.server.history))

    def test_mutation_secures_only_managed_credentials_after_safe_scope_validation(self):
        credential = self.add_account("claude", "work")
        codex = self.add_account("codex", "work")
        unrelated = self.auth / "notes.json"
        unrelated.write_text("not a credential")
        self.auth.chmod(0o755)
        credential.chmod(0o644)
        codex.chmod(0o644)
        unrelated.chmod(0o644)
        self.write_state(claude={"accounts": {"claude-work.json": "work"}, "primary": "claude-work.json"})

        result = self.run_fleet("auto", "claude", "on")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(codex.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o644)

    def test_enroll_hardens_only_the_selected_credential_and_enables_routing(self):
        credential = self.add_account("claude", "existing", disabled=True, priority=7, contents=b"not json or readable oauth data")
        other = self.add_account("codex", "other", contents=b"other opaque data")
        self.auth.chmod(0o755)
        credential.chmod(0o644)
        other.chmod(0o644)

        result = self.run_fleet("login", "claude", "--existing", "existing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(credential.read_bytes(), b"not json or readable oauth data")
        self.assertEqual(other.read_bytes(), b"other opaque data")
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o644)
        self.assertFalse(self.management.server.accounts["claude-existing.json"]["disabled"])
        self.assertEqual(self.management.server.accounts["claude-existing.json"]["priority"], 100)
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], self.management.url)
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertEqual(policy["accounts"], {"claude-existing.json": "existing"})
        self.assertEqual(policy["primary"], "claude-existing.json")
        self.assertIs(policy["auto"], False)

    def test_enroll_codex_enables_account_and_routes_shared_home(self):
        credential = self.add_account("codex", "existing", disabled=True, priority=7)
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-5"\n')
        result = self.run_fleet("login", "codex", "--existing", "existing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex now uses CLIProxyAPI", result.stdout)
        self.assertFalse(self.management.server.accounts["codex-existing.json"]["disabled"])
        self.assertEqual(self.management.server.accounts["codex-existing.json"]["priority"], 100)
        value = tomllib.loads(config.read_text())
        self.assertEqual(value["model"], "gpt-5")
        self.assertNotIn("model_provider", value)
        self.assertNotIn("fleet_cliproxyapi", value.get("model_providers", {}))
        self.assertEqual(value["openai_base_url"], self.management.url + "/v1")
        self.assertTrue((self.home / ".local/state/fleet/client-routing.json").exists())
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy["accounts"], {"codex-existing.json": "existing"})
        self.assertEqual(policy["primary"], "codex-existing.json")
        self.assertIs(policy["auto"], False)

    def test_restore_record_failure_rolls_back_account_proxy_and_client_state(self):
        credential = self.add_account("claude", "existing", disabled=True, priority=7)

        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(
            ACCOUNTS,
            "save_client_routing_state",
            side_effect=ACCOUNTS.fleet.FleetError("restore record save failed"),
        ):
            with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "restore record save failed"):
                ACCOUNTS.enroll_account(self.home, "claude", "existing")

        account = self.management.server.accounts["claude-existing.json"]
        self.assertTrue(account["disabled"])
        self.assertEqual(account["priority"], 7)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertFalse((self.home / ".local/state/fleet/client-routing.json").exists())
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)

    def test_enroll_does_not_route_until_all_provider_accounts_are_enrolled(self):
        first = self.add_account("claude", "first", contents=b"first opaque data")
        second = self.add_account("claude", "second", contents=b"second opaque data")

        result = self.run_fleet("login", "claude", "--existing", "first")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertFalse(self.management.server.history)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o644)
        policy = json.loads(self.state.read_text())["providers"]["claude"]
        self.assertIsNone(policy["auto"])

    def test_final_enroll_rolls_back_management_when_client_routing_fails(self):
        credential = self.add_account("claude", "existing", disabled=True, priority=7)
        self.management.server.reject_models = True

        result = self.run_fleet("login", "claude", "--existing", "existing")

        self.assertEqual(result.returncode, 2)
        self.assertIn("smoke check failed", result.stderr)
        account = self.management.server.accounts["claude-existing.json"]
        self.assertTrue(account["disabled"])
        self.assertEqual(account["priority"], 7)
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)

    def test_managed_management_key_requires_exact_private_file_validation(self):
        self.environment.pop("FLEET_CLIPROXY_MANAGEMENT_KEY")
        secret = self.home / ".local/state/fleet/cliproxyapi-management-key"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("test-management-key\n")
        secret.chmod(0o644)

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("missing or unsafe", result.stderr)
        secret.chmod(0o600)
        result = self.run_fleet("list")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutation_rejects_auth_directory_outside_account_home(self):
        outside = self.base / "outside-auth"
        outside.mkdir()
        self.management.server.config["auth-dir"] = str(outside)
        self.write_state(claude={"accounts": {"claude-work.json": "work"}, "primary": "claude-work.json"})

        result = self.run_fleet("auto", "claude", "on")

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be inside the account home", result.stderr)
        self.assertFalse(self.management.server.history)

    def test_mutation_rejects_malformed_auth_directory_without_traceback(self):
        self.management.server.config["auth-dir"] = "bad\0path"
        self.write_state(claude={"accounts": {"claude-work.json": "work"}, "primary": "claude-work.json"})

        result = self.run_fleet("auto", "claude", "on")

        self.assertEqual(result.returncode, 2)
        self.assertIn("auth-dir must be absolute", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_accepts_an_explicit_config_symlink(self):
        real = self.base / "real.conf"
        contents = self.config.read_bytes()
        real.write_bytes(contents)
        self.config.unlink()
        self.config.symlink_to(real)
        self.fake_login("codex", "codex-symlink.json", expected_config=real)

        result = self.run_fleet("add", "codex", "symlink")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(real.read_bytes(), contents)

    def test_add_with_auto_off_disables_new_backup_immediately(self):
        self.add_account("claude", "primary", disabled=False, priority=100)
        self.write_state(claude={"accounts": {"claude-primary.json": "primary"}, "primary": "claude-primary.json", "auto": False})
        self.fake_login("claude", "claude-backup.json")

        result = self.run_fleet("add", "claude", "backup")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.management.server.accounts["claude-backup.json"]["disabled"])
        self.assertFalse(self.management.server.accounts["claude-primary.json"]["disabled"])

    def test_add_disables_new_login_when_state_save_fails(self):
        self.fake_login("claude", "claude-new.json", block_state_save=True)

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Could not save Fleet account state", result.stderr)
        self.assertIn("new login remains disabled", result.stderr)
        self.assertTrue(self.management.server.accounts["claude-new.json"]["disabled"])
        self.assertNotIn("priority", self.management.server.accounts["claude-new.json"])

    def test_add_disables_new_login_when_client_routing_fails(self):
        self.fake_login("claude", "claude-new.json")
        self.management.server.reject_models = True

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("new login remains disabled", result.stderr)
        self.assertTrue(self.management.server.accounts["claude-new.json"]["disabled"])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.home / ".claude/settings.json").exists())

    def test_add_reports_when_new_login_cannot_be_disabled(self):
        self.fake_login("claude", "claude-new.json")
        self.management.server.ignore_status = True

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("could not be disabled", result.stderr.lower())
        self.assertFalse(self.management.server.accounts["claude-new.json"]["disabled"])
        self.assertFalse(self.state.exists())

    def test_add_disables_new_login_when_credential_validation_fails(self):
        self.fake_login("claude", "claude-new.json")
        self.management.server.new_source = "runtime"

        result = self.run_fleet("add", "claude", "new")

        self.assertEqual(result.returncode, 2)
        self.assertIn("file credential", result.stderr)
        self.assertIn("new login remains disabled", result.stderr)
        self.assertTrue(self.management.server.accounts["claude-new.json"]["disabled"])

    def test_management_rejects_nonliteral_hostname_without_dns(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")) as resolver:
            with mock.patch.dict(os.environ, {"FLEET_CLIPROXY_URL": "http://proxy.local:8317", "FLEET_CLIPROXY_MANAGEMENT_KEY": "secret"}):
                with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "127.0.0.1, ::1, or localhost"):
                    ACCOUNTS.ManagementClient()
            resolver.assert_not_called()

    def test_management_accepts_bracketed_ipv6_loopback(self):
        with mock.patch.dict(os.environ, {"FLEET_CLIPROXY_URL": "http://[::1]:8317", "FLEET_CLIPROXY_MANAGEMENT_KEY": "secret"}):
            client = ACCOUNTS.ManagementClient()
        self.assertEqual(client.url, "http://[::1]:8317")

    def test_management_rejects_invalid_key_without_disclosing_it(self):
        secret = "private-first-line\nprivate-second-line"
        self.environment["FLEET_CLIPROXY_MANAGEMENT_KEY"] = secret

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("one non-empty printable line", result.stderr)
        self.assertNotIn("private-first-line", result.stderr)
        self.assertNotIn("private-second-line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_management_does_not_use_environment_proxy(self):
        received = []

        class ProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(502)
                self.end_headers()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            self.environment.update({
                "HTTP_PROXY": f"http://127.0.0.1:{proxy.server_port}",
                "HTTPS_PROXY": f"http://127.0.0.1:{proxy.server_port}",
                "http_proxy": f"http://127.0.0.1:{proxy.server_port}",
                "https_proxy": f"http://127.0.0.1:{proxy.server_port}",
                "NO_PROXY": "",
                "no_proxy": "",
            })
            result = self.run_fleet("list")
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, [])

    def test_management_rejects_missing_or_wrong_proxy_version_before_mutation(self):
        self.add_account("claude", "first", priority=100)
        self.add_account("claude", "second", priority=0)
        self.write_state(claude={"accounts": {"claude-first.json": "first", "claude-second.json": "second"}, "primary": "claude-first.json", "auto": True})
        for version in (None, "7.2.146"):
            with self.subTest(version=version):
                self.management.server.version_header = version
                result = self.run_fleet("use", "claude", "second")
                self.assertEqual(result.returncode, 2)
                self.assertIn("requires CLIProxyAPI 7.2.149", result.stderr)
                self.assertFalse(self.management.server.history)

    def test_management_accepts_newer_proxy_version(self):
        self.management.server.version_header = "7.2.151"

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_newer_setup_state_is_accepted_by_runtime(self):
        path = ACCOUNTS.setup_state_path(self.home)
        path.write_text('{"version":"7.2.151"}\n')
        path.chmod(0o600)
        management_key = self.home / ".local/state/fleet/cliproxyapi-management-key"
        management_key.write_text("test-management-key\n")
        management_key.chmod(0o600)
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        config.parent.mkdir(parents=True)
        contents = ACCOUNTS.setup_config(self.auth, "test-client-key", "test-management-key")
        platform_name = "darwin" if sys.platform == "darwin" else "linux"
        paths = ACCOUNTS.setup_paths(self.home, platform_name, "7.2.151")
        config.write_bytes(contents.replace(b"api-keys:\n", ACCOUNTS.setup_plugin_config(paths) + b"api-keys:\n"))
        config.chmod(0o600)
        self.management.server.version_header = "7.2.151"

        with self.assertRaises(ACCOUNTS.fleet.FleetError):
            ACCOUNTS.load_setup_state(self.home)

        self.assertEqual(
            ACCOUNTS.load_setup_state(self.home, allow_newer=True), {"version": "7.2.151"}
        )
        with mock.patch.dict(os.environ, self.environment, clear=True):
            _client, _config, entries = ACCOUNTS.runtime_for_mutation(self.home)
        self.assertEqual(entries, [])

    def test_management_does_not_follow_redirect_or_forward_key(self):
        received = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        self.management.server.redirect_to = f"http://127.0.0.1:{target.server_port}/stolen"
        try:
            with mock.patch.dict(os.environ, self.environment, clear=True):
                client = ACCOUNTS.ManagementClient()
                with self.assertRaises(ACCOUNTS.fleet.FleetError):
                    client.config()
        finally:
            target.shutdown()
            target.server_close()
            target_thread.join()
        self.assertEqual(received, [])

    def test_codex_accounts_get_upstream_websockets_on_every_routing_change(self):
        self.add_account("codex", "first", priority=100)
        self.add_account("codex", "second", disabled=True, priority=0)
        self.add_account("claude", "other", priority=100)
        self.write_state(
            codex={"accounts": {"codex-first.json": "first", "codex-second.json": "second"}, "primary": "codex-first.json"},
            claude={"accounts": {"claude-other.json": "other"}, "primary": "claude-other.json", "auto": False},
        )

        result = self.run_fleet("auto", "codex", "on")

        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("codex-first.json", "codex-second.json"):
            self.assertIs(self.management.server.accounts[name]["websockets"], True)
        self.assertNotIn("websockets", self.management.server.accounts["claude-other.json"])
        history = list(self.management.server.history)
        result = self.run_fleet("auto", "codex", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry for entry in self.management.server.history[len(history):] if entry[2] is True and entry[0].endswith("fields")], [])

    def test_native_codex_login_enables_upstream_websockets_for_the_new_account(self):
        native_auth = self.codex_native_auth()
        native = self.native_client(
            f"home = Path(os.environ['FLEET_ACCOUNT_HOME'])\n"
            f"(home / '.codex').mkdir()\n"
            f"(home / '.codex/auth.json').write_text({json.dumps(native_auth)!r})\n"
        )
        result = self.run_fleet("native-auth", "codex", str(native), "login")
        self.assertEqual(result.returncode, 0, result.stderr)
        account = next(iter(self.management.server.accounts.values()))
        self.assertIs(account["websockets"], True)

    def test_reset_order_puts_the_soonest_weekly_reset_first_and_keeps_manual_override(self):
        names = [f"codex-{stem}.json" for stem in ("early", "late", "mid")]
        for stem, priority in (("early", 0), ("late", 100), ("mid", 0)):
            self.add_account("codex", stem, priority=priority)
        resets = {"codex-early.json": 1_000, "codex-late.json": 9_000, "codex-mid.json": 5_000}
        self.management.server.usage = {
            f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": 10, "reset_at": reset}, "secondary_window": {"used_percent": 1, "reset_at": 100}}}
            for name, reset in resets.items()
        }
        self.write_state(codex={"accounts": dict(zip(names, ("Early", "Late", "Mid"))), "primary": "codex-late.json", "auto": True})

        result = self.run_fleet("order", "codex", "reset")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Early -> Mid -> Late", result.stdout)
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertEqual(policy["order"], ["codex-early.json", "codex-mid.json", "codex-late.json"])
        self.assertEqual(policy["rebalance"], "reset")
        self.assertEqual([self.management.server.accounts[name]["priority"] for name in policy["order"]], [3, 2, 1])
        shown = self.run_fleet("order", "codex")
        self.assertIn("soonest reset", shown.stdout)

        self.management.server.usage[f"index-codex-late.json"]["rate_limit"]["primary_window"]["reset_at"] = 10
        again = self.run_fleet("order", "rebalance")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(self.state.read_text())["providers"]["codex"]["order"][0], "codex-late.json")
        noop = self.run_fleet("order", "codex", "rebalance")
        self.assertIn("already balanced", noop.stdout)

        manual = self.run_fleet("order", "codex", "Mid", "Early", "Late")
        self.assertEqual(manual.returncode, 0, manual.stderr)
        policy = json.loads(self.state.read_text())["providers"]["codex"]
        self.assertNotIn("rebalance", policy)
        self.assertEqual(policy["order"][0], "codex-mid.json")
        off = self.run_fleet("order", "codex", "manual")
        self.assertEqual(off.returncode, 0, off.stderr)

    def test_reset_fallbacks_keeps_selected_main_and_reorders_only_fallbacks(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                names = [f"{provider}-{stem}.json" for stem in ("main", "early", "late")]
                for stem in ("main", "early", "late"):
                    self.add_account(provider, stem)
                self.write_state(**{provider: {"accounts": dict(zip(names, ("Main", "Early", "Late"))), "primary": names[0], "auto": True}})
                usage = {name: [("weekly", used, reset)] for name, used, reset in zip(names, (0, 100, 3), (9000, 1000, 9000))}
                self.management.server.usage = {
                    f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": used, "reset_at": reset}}} if provider == "codex" else {"seven_day": {"utilization": used, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))}}
                    for name, [(_, used, reset)] in usage.items()
                }
                self.management.server.history.clear()
                result = self.run_fleet("order", provider, "reset-fallbacks")
                self.assertEqual(result.returncode, 0, result.stderr)
                with mock.patch.dict(os.environ, self.environment), mock.patch.object(ACCOUNTS, "account_usage", side_effect=lambda _client, entry: usage[entry["name"]]):
                    policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                    self.assertEqual(policy["order"], names)
                    self.assertEqual(policy["rebalance"], "reset-fallbacks")
                    self.assertEqual([self.management.server.accounts[name]["priority"] for name in names], [3, 2, 1])
                    usage[names[2]] = [("weekly", 3, 500)]
                    ACCOUNTS.rebalance_provider(self.home, provider)
                    policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                    self.assertEqual(policy["primary"], names[0])
                    self.assertEqual(policy["order"], [names[0], names[2], names[1]])
                    self.assertFalse(any(entry[0].endswith("/session-affinity/reset") for entry in self.management.server.history))
                    ACCOUNTS.use_account(self.home, provider, names[2])
                    ACCOUNTS.rebalance_provider(self.home, provider)
                    policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                    self.assertEqual(policy["primary"], names[2])
                    self.assertEqual(policy["order"], [names[2], names[1], names[0]])
                    resets = [entry for entry in self.management.server.history if entry[0].endswith("/session-affinity/reset")]
                    self.assertEqual(resets, [("/v0/management/routing/session-affinity/reset", provider, True)])
                shown = self.run_fleet("order", provider)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertIn("primary first", shown.stdout)
                manual = self.run_fleet("order", provider, "manual")
                self.assertEqual(manual.returncode, 0, manual.stderr)
                self.assertNotIn("rebalance", ACCOUNTS.load_state(self.home)["providers"][provider])

    def test_reset_order_preserves_preference_across_primary_changes_and_ties(self):
        for provider in ACCOUNTS.PROVIDERS:
            with self.subTest(provider=provider):
                names = [f"{provider}-{stem}.json" for stem in ("z-main", "a-first", "b-next")]
                for stem in ("z-main", "a-first", "b-next"):
                    self.add_account(provider, stem)
                self.write_state(**{provider: {"accounts": dict(zip(names, ("Main", "First", "Next"))), "primary": names[0], "auto": True}})
                self.management.server.usage = {
                    f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": 0, "reset_at": session, "limit_window_seconds": 18000}, "secondary_window": {"used_percent": 10, "reset_at": reset, "limit_window_seconds": 604800}}} if provider == "codex" else {"seven_day": {"utilization": 10, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))}, "five_hour": {"utilization": 0, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(session))}}
                    for name, reset, session in zip(names, (9000, 1000, 5000), (1, 3, 2))
                }
                with mock.patch.dict(os.environ, self.environment):
                    ACCOUNTS.set_rebalance(self.home, provider, "reset")
                    policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                    self.assertEqual(policy["order"], [names[1], names[2], names[0]])
                    self.assertEqual(policy["preferred"], names[0])
                    for body in self.management.server.usage.values():
                        if provider == "codex":
                            body["rate_limit"]["secondary_window"]["reset_at"] = 7000
                        else:
                            body["seven_day"]["resets_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(7000))
                    ACCOUNTS.rebalance_provider(self.home, provider)
                    policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                    self.assertEqual(policy["order"], [names[0], names[1], names[2]])
                    self.assertEqual(policy["preferred"], names[0])
                    resets = [item for item in self.management.server.history if item[0].endswith("/session-affinity/reset") and item[1] == provider]
                    self.assertEqual(len(resets), 2)

    def test_reset_order_ignores_session_only_accounts(self):
        for name in ("main", "session", "weekly"):
            self.add_account("claude", name)
        entries = list(self.management.server.accounts.values())
        names = [entry["name"] for entry in entries]
        policy = {"primary": names[0], "preferred": names[0]}
        self.management.server.usage = {
            f"index-{name}": {"limits": [{"kind": "session" if index < 2 else "weekly_all", "percent": 0, "resets_at": f"2999-01-0{index + 1}T00:00:00Z"}]}
            for index, name in enumerate(names)
        }
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(ACCOUNTS.reset_policy_order(policy, entries, ACCOUNTS.ManagementClient()), [names[2], names[0], names[1]])

    def test_claude_reset_order_uses_earliest_weekly_or_fable_expiry(self):
        for stem in ("main", "fable", "weekly"):
            self.add_account("claude", stem)
        names = [f"claude-{stem}.json" for stem in ("main", "fable", "weekly")]
        for model in (None, {"id": None, "display_name": "Fable"}, {"id": "claude-fable-1", "display_name": "Other"}):
            with self.subTest(model=model):
                self.write_state(claude={"accounts": dict(zip(names, ("Main", "Fable", "Weekly"))), "primary": names[0], "auto": True})
                self.management.server.usage = {}
                for name, weekly, fable in zip(names, (9, 8, 3), (10, 2, 7)):
                    body = {
                        "seven_day": {"utilization": 10, "resets_at": f"2999-01-{weekly:02d}T00:00:00Z"},
                        "five_hour": {"utilization": 0, "resets_at": "2999-01-01T00:00:00Z"},
                    }
                    if model is None:
                        body["seven_day_fable"] = {"utilization": 20, "resets_at": f"2999-01-{fable:02d}T00:00:00Z"}
                    else:
                        body["limits"] = [
                            {"kind": "session", "scope": None, "percent": 63, "resets_at": "2999-01-01T00:00:00.812665+00:00"},
                            {"kind": "weekly_all", "scope": None, "percent": 78, "resets_at": f"2999-01-{weekly:02d}T00:00:00.812683+00:00"},
                            {"kind": "weekly_scoped", "scope": {"model": model, "surface": None}, "percent": 0, "resets_at": f"2999-01-{fable:02d}T00:00:00+00:00", "is_active": False},
                        ]
                    self.management.server.usage[f"index-{name}"] = body
                result = self.run_fleet("order", "claude", "reset")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(ACCOUNTS.load_state(self.home)["providers"]["claude"]["order"], [names[1], names[2], names[0]])
                if model is not None:
                    for name in names:
                        body = self.management.server.usage[f"index-{name}"]
                        body["seven_day"]["resets_at"] = "2999-01-05T00:00:00Z" if name == names[0] else "2999-01-04T23:59:59Z"
                        body["limits"][1]["resets_at"] = "2999-01-05T00:00:00.812683+00:00"
                        body["limits"][2]["resets_at"] = "2999-01-10T00:00:00+00:00"
                    result = self.run_fleet("order", "claude", "rebalance")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(ACCOUNTS.load_state(self.home)["providers"]["claude"]["order"], [names[0], names[1], names[2]])

    def test_override_survives_rebalance_and_off_restores_reset_order(self):
        for provider in ACCOUNTS.PROVIDERS:
            with self.subTest(provider=provider):
                names = [f"{provider}-{stem}.json" for stem in ("main", "early", "override")]
                for stem in ("main", "early", "override"):
                    self.add_account(provider, stem)
                self.write_state(**{provider: {"accounts": dict(zip(names, ("Main", "Early", "Chosen"))), "primary": names[0], "auto": True, "order": names, "rebalance": "reset"}})
                self.policy.write_bytes(ACCOUNTS.account_routing_bytes({"version": 1, "providers": {"claude": "limits", "codex": "limits"}, "credits": {"claude": "never", "codex": "last"}}))
                self.policy.chmod(0o600)
                routing_before = self.policy.read_bytes()
                self.management.server.usage = {
                    f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": 10, "reset_at": reset}}} if provider == "codex" else {"seven_day": {"utilization": 10, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))}}
                    for name, reset in zip(names, (9000, 1000, 5000))
                }
                result = self.run_fleet("override", provider, "Chosen")
                self.assertEqual(result.returncode, 0, result.stderr)
                policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                self.assertEqual(policy["override"], names[2])
                self.assertEqual(policy["preferred"], names[0])
                self.assertEqual(policy["primary"], names[2])
                result = self.run_fleet("order", provider, "rebalance")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(ACCOUNTS.load_state(self.home)["providers"][provider]["order"], [names[2], names[1], names[0]])
                self.assertTrue(all(not self.management.server.accounts[name]["disabled"] for name in names))
                self.assertEqual(ACCOUNTS.load_account_routing(self.home), json.loads(routing_before) | {"selection": {provider: "priority"}})
                result = self.run_fleet("override", provider, "off")
                self.assertEqual(result.returncode, 0, result.stderr)
                policy = ACCOUNTS.load_state(self.home)["providers"][provider]
                self.assertNotIn("override", policy)
                self.assertEqual(policy["preferred"], names[0])
                self.assertEqual(policy["order"], [names[1], names[2], names[0]])
                self.assertEqual(ACCOUNTS.load_account_routing(self.home), json.loads(routing_before) | {"selection": {provider: "priority"}})
                resets = [item for item in self.management.server.history if item[0].endswith("/session-affinity/reset") and item[1] == provider]
                self.assertEqual(len(resets), 2)

    def test_override_reset_failure_restores_policy_and_priorities(self):
        for wanted in ("Second", "off"):
            with self.subTest(wanted=wanted):
                for stem in ("first", "second"):
                    self.add_account("claude", stem, priority=9 if stem == "first" else 4)
                names = ["claude-first.json", "claude-second.json"]
                policy = {"accounts": dict(zip(names, ("First", "Second"))), "primary": names[0], "auto": True, "order": names, "rebalance": "reset"}
                if wanted == "off":
                    policy["override"] = names[0]
                    policy.pop("rebalance")
                self.write_state(claude=policy)
                before = self.state.read_bytes()
                routing = ACCOUNTS.default_account_routing() | {"credits": {"claude": "never"}, "selection": {"codex": "priority", **({"claude": "priority"} if wanted == "off" else {})}}
                self.policy.write_bytes(ACCOUNTS.account_routing_bytes(routing))
                self.policy.chmod(0o600)
                routing_before = self.policy.read_bytes()
                self.management.server.affinity_reset_fail = True
                self.management.server.usage = {f"index-{name}": {"seven_day": {"utilization": 0, "resets_at": f"2999-01-0{day}T00:00:00Z"}} for name, day in zip(names, (9, 1))}
                result = self.run_fleet("override", "claude", wanted)
                self.assertEqual(result.returncode, 2)
                self.assertIn("rolled back", result.stderr)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertEqual(self.policy.read_bytes(), routing_before)
                self.assertEqual([self.management.server.accounts[name]["priority"] for name in names], [9, 4])

    def test_optional_preference_and_override_state_is_validated(self):
        names = ["codex-first.json", "codex-second.json"]
        self.write_state(codex={"accounts": dict(zip(names, ("First", "Second"))), "primary": names[0], "auto": True})
        state = ACCOUNTS.load_state(self.home)
        policy = state["providers"]["codex"]
        for field in ("preferred", "override"):
            policy[field] = names[1]
            self.assertTrue(ACCOUNTS.valid_state(state))
            for invalid in ("codex-missing.json", [], {}, True, None):
                policy[field] = invalid
                self.assertFalse(ACCOUNTS.valid_state(state))
            del policy[field]

    def test_reset_switch_failure_restores_the_previous_mode(self):
        for failure in ("priority", "override", "unsupported", "reset"):
            with self.subTest(failure=failure):
                for stem in ("main", "early"):
                    self.add_account("codex", stem, priority=9 if stem == "main" else 4)
                names = ["codex-main.json", "codex-early.json"]
                self.write_state(codex={"accounts": dict(zip(names, ("Main", "Early"))), "primary": names[0], "auto": True})
                before = self.state.read_bytes()
                routing_before = ACCOUNTS.client_config_snapshot(self.policy)
                history_start = len(self.management.server.history)
                self.management.server.config["fleet-priority-selection-supported"] = True
                if failure in {"priority", "override"}:
                    self.management.server.config.pop("fleet-priority-selection-supported")
                self.management.server.config["fleet-session-affinity-supported"] = failure != "unsupported"
                self.management.server.affinity_reset_fail = failure == "reset"
                self.management.server.usage = {f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": 10, "reset_at": reset}}} for name, reset in zip(names, (9000, 1000))}
                result = self.run_fleet("override", "codex", "Early") if failure == "override" else self.run_fleet("order", "codex", "reset")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertEqual([self.management.server.accounts[name]["priority"] for name in names], [9, 4])
                self.assertEqual(ACCOUNTS.client_config_snapshot(self.policy), routing_before)
                if failure in {"priority", "override", "unsupported"}:
                    self.assertEqual([entry for entry in self.management.server.history[history_start:] if not entry[0].endswith("/api-call")], [])
                    self.assertIn("Update the Fleet proxy", result.stderr)

    def test_priority_selection_tracks_reset_and_override_without_changing_other_policies(self):
        for provider in ACCOUNTS.PROVIDERS:
            with self.subTest(provider=provider):
                other = next(item for item in ACCOUNTS.PROVIDERS if item != provider)
                names = [f"{provider}-{stem}.json" for stem in ("main", "early")]
                for stem in ("main", "early"):
                    self.add_account(provider, stem)
                self.write_state(**{provider: {"accounts": dict(zip(names, ("Main", "Early"))), "primary": names[0], "auto": True}})
                routing = {"version": 1, "providers": {"claude": "any", "codex": "limits"}, "credits": {"claude": "never", "codex": "last"}, "selection": {other: "priority"}}
                self.policy.write_bytes(ACCOUNTS.account_routing_bytes(routing))
                self.policy.chmod(0o600)
                self.management.server.usage = {f"index-{name}": {"rate_limit": {"primary_window": {"used_percent": 10, "reset_at": reset}}} if provider == "codex" else {"seven_day": {"utilization": 10, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))}} for name, reset in zip(names, (9000, 1000))}
                history_start = len(self.management.server.history)
                for command, selected in (
                    (("order", provider, "reset"), True),
                    (("order", provider, "reset-fallbacks"), False),
                    (("order", provider, "reset"), True),
                    (("order", provider, "manual"), False),
                    (("override", provider, "Main"), True),
                    (("order", provider, "manual"), True),
                    (("override", provider, "off"), False),
                    (("override", provider, "Main"), True),
                    (("order", provider, "Early", "Main"), False),
                ):
                    if selected:
                        self.management.server.config["fleet-priority-selection-supported"] = True
                    else:
                        self.management.server.config.pop("fleet-priority-selection-supported", None)
                    result = self.run_fleet(*command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    saved = ACCOUNTS.load_account_routing(self.home)
                    self.assertEqual(saved["providers"], routing["providers"])
                    self.assertEqual(saved["credits"], routing["credits"])
                    self.assertEqual(saved["selection"], {other: "priority", **({provider: "priority"} if selected else {})})
                resets = [item[1] for item in self.management.server.history[history_start:] if item[0].endswith("/session-affinity/reset")]
                self.assertEqual(set(resets), {provider})

    def test_priority_selection_rejects_invalid_saved_policy(self):
        for invalid in (None, [], "priority", {"other": "priority"}, {"claude": "invalid"}, {"claude": []}):
            with self.subTest(selection=invalid):
                self.policy.write_bytes(ACCOUNTS.account_routing_bytes(ACCOUNTS.default_account_routing() | {"selection": invalid}))
                self.policy.chmod(0o600)
                with self.assertRaises(ACCOUNTS.fleet.FleetError):
                    ACCOUNTS.load_account_routing(self.home)

    def test_selection_save_failure_rolls_back_priorities_and_account_state(self):
        for stem in ("main", "early"):
            self.add_account("claude", stem, priority=9 if stem == "main" else 4)
        names = ["claude-main.json", "claude-early.json"]
        self.write_state(claude={"accounts": dict(zip(names, ("Main", "Early"))), "primary": names[0], "auto": True})
        before = self.state.read_bytes()
        atomic_write = ACCOUNTS.fleet.atomic_write

        def fail_routing(home, path, *args, **kwargs):
            if path == self.policy:
                raise OSError("injected policy save failure")
            return atomic_write(home, path, *args, **kwargs)

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(ACCOUNTS.fleet, "atomic_write", side_effect=fail_routing):
            with self.assertRaises(ACCOUNTS.fleet.FleetError):
                ACCOUNTS.override_account(self.home, "claude", "Early")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.policy.exists())
        self.assertEqual([self.management.server.accounts[name]["priority"] for name in names], [9, 4])
        self.assertFalse(any(item[0].endswith("/session-affinity/reset") for item in self.management.server.history))

    def test_list_shows_usage_windows_and_reset_countdown(self):
        self.add_account("codex", "work", priority=100)
        self.add_account("claude", "main", priority=100)
        now = int(time.time())
        self.management.server.usage = {
            "index-codex-work.json": {"rate_limit": {"primary_window": {"used_percent": 42, "reset_at": now + 2 * 86400 + 3600}, "secondary_window": {"used_percent": 7, "reset_at": now + 1800}}},
            "index-claude-main.json": {"five_hour": {"utilization": 32.0, "resets_at": "2999-01-01T00:00:00+00:00"}, "seven_day": {"utilization": 59.0, "resets_at": "2999-01-03T00:00:00+00:00"}},
        }
        self.write_state(
            codex={"accounts": {"codex-work.json": "Work"}, "primary": "codex-work.json", "auto": True},
            claude={"accounts": {"claude-main.json": "Main"}, "primary": "claude-main.json", "auto": False},
        )

        result = self.run_fleet("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertRegex(lines[1], r"^claude\s+Main\s+— · Main\s+off\s+32% · \d+d \d+h\s+59% · \d+d \d+h\s+—$")
        self.assertRegex(lines[2], r"^codex\s+Work\s+— · Main\s+limits\s+7% · (29|30)m\s+42% · 2d [01]h\s+—$")
        del self.management.server.usage["index-claude-main.json"]
        result = self.run_fleet("list", "claude")
        self.assertRegex(result.stdout.splitlines()[1], r"off\s+—\s+—\s+—$")

    def test_state_accepts_reset_rebalance_only_with_an_order(self):
        self.write_state(codex={"accounts": {"codex-a.json": "A", "codex-b.json": "B"}, "primary": "codex-a.json", "auto": True})
        state = json.loads(self.state.read_text())
        state["providers"]["codex"]["rebalance"] = "reset"
        self.assertFalse(ACCOUNTS.valid_state(state))
        state["providers"]["codex"]["order"] = ["codex-a.json", "codex-b.json"]
        self.assertTrue(ACCOUNTS.valid_state(state))
        state["providers"]["codex"]["rebalance"] = "reset-fallbacks"
        self.assertTrue(ACCOUNTS.valid_state(state))
        for invalid in ("other", [], {}):
            state["providers"]["codex"]["rebalance"] = invalid
            self.assertFalse(ACCOUNTS.valid_state(state))

    def test_spoke_uses_hub_keys_and_refuses_local_proxy_logins(self):
        self.add_account("codex", "work", priority=100)
        self.write_state(codex={"accounts": {"codex-work.json": "Work"}, "primary": "codex-work.json", "auto": True})
        hub_dir = self.home / ".local/state/fleet"
        (hub_dir / "hub.json").write_text(ACCOUNTS.hub_state_bytes("spoke", "me@example.com", "http://hub.tail123abc.ts.net:8317").decode())
        (hub_dir / "hub.json").chmod(0o600)
        for name, value in (("hub-client-key", "test-client-key"), ("hub-management-key", "test-management-key"), ("cliproxyapi-management-key", "wrong-key")):
            (hub_dir / name).write_text(value + "\n")
            (hub_dir / name).chmod(0o600)
        del self.environment["FLEET_CLIPROXY_MANAGEMENT_KEY"]
        self.environment["FLEET_ENABLE_SUBSCRIPTION_ROUTING"] = "1"

        listed = self.run_fleet("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Work", listed.stdout)
        with mock.patch.dict(os.environ, self.environment):
            output = StringIO()
            with mock.patch("sys.stdout", output), mock.patch.object(ACCOUNTS, "managed_setup_version", return_value="7.2.149"), mock.patch.object(ACCOUNTS, "probe_inference_endpoint"):
                ACCOUNTS.hub_status(self.home)
        self.assertIn("hub: spoke", output.getvalue())
        self.assertIn("reach: ok", output.getvalue())
        added = self.run_fleet("add", "codex")
        self.assertEqual(added.returncode, 2)
        self.assertIn("Fleet hub", added.stderr)
        self.assertFalse(ACCOUNTS.valid_hub_url("http://evil.example.com:8317"))
        self.assertFalse(ACCOUNTS.valid_hub_url("https://hub.tail123abc.ts.net:8317"))
        self.assertTrue(ACCOUNTS.valid_hub_url("http://hub.tail123abc.ts.net:8317"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
