import copy
import fcntl
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_mcp_cache", ROOT / "lib/fleet_mcp_cache.py")
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)


class MetadataCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.script = self.home / "server.py"
        self.script.write_text("pass\n")
        self.command = [sys.executable, "-B", str(self.script)]
        self.environment = {"PATH": os.defpath, "CLIENT": "one", "API_TOKEN": "private-test-credential"}
        self.request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
        self.result = {"resultType": "complete", "cacheScope": "public", "ttlMs": 10000,
                       "tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
                       "_meta": {"extra": "kept"}, "nextCursor": "page-two"}
        self.clock = patch.object(CACHE.time, "time", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.cache = self.make_cache()
        self.path = self.home / ".local/state/fleet/mcp-cache"

    def make_cache(self, **changes):
        return CACHE.MetadataCache(**{"home": self.home, "command": self.command,
                                      "environment": self.environment, "cwd": self.home, **changes})

    def entries(self):
        return list(self.path.glob("*.json"))

    def test_ttl_preserves_full_result_and_does_not_mutate_inputs(self):
        original = copy.deepcopy(self.result)
        self.cache.put(self.request, self.result)
        self.now.return_value += 1.25
        expected = {**original, "ttlMs": 8750}
        self.assertEqual(self.cache.get(self.request), expected)
        self.assertEqual(self.make_cache().get(self.request), expected)
        self.assertEqual(self.result, original)
        self.now.return_value = 1010
        self.assertIsNone(self.cache.get(self.request))

    def test_ttl_cap_and_clock_rollback(self):
        self.cache.put(self.request, {**self.result, "ttlMs": 900000})
        self.assertEqual(self.cache.get(self.request)["ttlMs"], 300000)
        self.now.return_value = 999
        self.assertIsNone(self.cache.get(self.request))
        self.now.return_value = 1300
        self.assertIsNone(self.cache.get(self.request))

    def test_cursor_and_all_parameters_are_part_of_key(self):
        self.cache.put(self.request, self.result)
        for params in ({"cursor": "next"}, {"cursor": None}, {"filter": "x"},
                       {"_meta": {**self.request["params"]["_meta"], "client": "two"}}):
            with self.subTest(params=params):
                request = {**self.request, "params": {**self.request["params"], **params}}
                self.assertIsNone(self.cache.get(request))
                self.cache.put(request, {**self.result, "tools": []})
                self.assertEqual(self.cache.get(request)["tools"], [])
        self.assertEqual(self.cache.get(self.request)["tools"], self.result["tools"])

    def test_only_progress_token_and_transport_id_are_ignored(self):
        request = copy.deepcopy(self.request)
        request["params"]["_meta"]["progressToken"] = "first"
        self.cache.put(request, self.result)
        request["id"] = 99
        request["params"]["_meta"]["progressToken"] = "second"
        self.assertIsNotNone(self.cache.get(request))
        self.assertIsNotNone(self.cache.get(self.request))
        self.assertEqual(request["params"]["_meta"]["progressToken"], "second")

    def test_all_five_metadata_methods_and_nothing_else(self):
        for method in ("server/discover", "tools/list", "prompts/list", "resources/list", "resources/templates/list"):
            request = {**self.request, "method": method}
            self.cache.put(request, self.result)
            self.assertIsNotNone(self.cache.get(request), method)
        count = len(self.entries())
        for method in ("initialize", "tools/call", "resources/read", "prompts/get", "ping"):
            request = {**self.request, "method": method}
            self.cache.put(request, self.result)
            self.assertIsNone(self.cache.get(request), method)
        self.assertEqual(len(self.entries()), count)

    def test_old_protocol_and_continuations_are_never_cached(self):
        requests = [{**self.request, "params": params} for params in (
            {}, None, [], {"_meta": {}}, {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}})]
        for name in ("inputResponses", "requestState"):
            requests.extend(({**self.request, name: {}},
                             {**self.request, "params": {**self.request["params"], name: None}}))
        for request in requests:
            self.cache.put(request, self.result)
            self.assertIsNone(self.cache.get(request))
        self.assertEqual(self.entries(), [])

    def test_noncacheable_results(self):
        results = [{**self.result, "ttlMs": ttl} for ttl in (None, 0, -1, True, 1.5, "100")]
        results += [{**self.result, **change} for change in (
            {"cacheScope": "private"}, {"cacheScope": None}, {"resultType": "input_required"},
            {"resultType": None}, {"error": {"code": -1}}, {"isError": True},
            {"requestState": "state"}, {"inputResponses": []})]
        results += [{key: value for key, value in self.result.items() if key != field}
                    for field in ("ttlMs", "cacheScope", "resultType")]
        for result in results:
            self.cache.put(self.request, result)
            self.assertIsNone(self.cache.get(self.request))
        self.assertEqual(self.entries(), [])

    def test_secret_values_and_credential_patterns_are_not_written(self):
        for secret in (self.environment["API_TOKEN"], "Bearer abcdefghijklmnop", "sk-proj-" + "1" * 24,
                       "ghp_" + "1" * 36, "https://user:pass@example.com",
                       "token=abcdefg", "-----BEGIN " + "PRIVATE KEY-----", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"):
            with self.subTest(secret=secret):
                self.cache.put(self.request, {**self.result, "description": secret})
                self.assertIsNone(self.cache.get(self.request))
        self.cache.put(self.request, {**self.result, "_meta": {"accessToken": "unknown-value"}})
        self.assertEqual(self.entries(), [])

    def test_auth_environment_and_common_auth_fields_are_private(self):
        for name in ("SERVICE_AUTH", "MCP_AUTH_HEADER", "SESSION_COOKIE", "ENCRYPTION_KEY"):
            cache = self.make_cache(environment={name: "opaque-private-value"})
            cache.put(self.request, {**self.result, "description": "opaque-private-value"})
            self.assertIsNone(cache.get(self.request), name)
        for field in ("x-api-key", "x-auth-token", "cookie", "set-cookie", "client_secret"):
            self.cache.put(self.request, {**self.result, "_meta": {field: "opaque-private-value"}})
            self.assertIsNone(self.cache.get(self.request), field)
        for text in ("access_token=opaque-private-value", "client_secret=opaque-private-value"):
            self.cache.put(self.request, {**self.result, "description": text})
            self.assertIsNone(self.cache.get(self.request), text)
        self.assertEqual(self.entries(), [])

    def test_environment_command_cwd_and_input_files_isolate(self):
        self.cache.put(self.request, self.result)
        other = self.home / "other"
        other.mkdir()
        for change in ({"environment": {**self.environment, "CLIENT": "two"}},
                       {"environment": {**self.environment, "API_TOKEN": "different"}},
                       {"environment": {**self.environment, "OTHER": "new"}},
                       {"command": [*self.command, "--new"]}, {"cwd": other}):
            self.assertIsNone(self.make_cache(**change).get(self.request), change)
        self.script.write_text("print('changed')\n")
        self.assertIsNone(self.cache.get(self.request))
        self.assertIsNone(self.make_cache().get(self.request))

    def test_relative_path_resolution_and_option_file_identity(self):
        executable = self.home / "runner"
        executable.write_text("first")
        executable.chmod(0o700)
        config = self.home / "config.json"
        config.write_text("{}")
        cache = self.make_cache(command=["runner", "--config=config.json"], environment={"PATH": "."})
        cache.put(self.request, self.result)
        self.assertIsNotNone(cache.get(self.request))
        config.write_text('{"changed":true}')
        self.assertIsNone(cache.get(self.request))
        cache.put(self.request, self.result)
        executable.write_text("second")
        self.assertIsNone(cache.get(self.request))

    def test_private_payload_and_key_permissions(self):
        self.cache.put(self.request, self.result)
        payload = json.loads(self.entries()[0].read_text())
        self.assertEqual(set(payload), {"result", "created", "expires"})
        for path in self.path.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertNotIn(self.environment["API_TOKEN"].encode(), path.read_bytes())
            self.assertNotIn(str(self.script).encode(), path.read_bytes())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        self.assertEqual(len((self.path / "key").read_bytes()), 32)

    def test_invalidate_only_exact_context(self):
        other = self.make_cache(environment={**self.environment, "CLIENT": "two"})
        self.cache.put(self.request, self.result)
        page = {**self.request, "params": {**self.request["params"], "cursor": "page"}}
        self.cache.put(page, self.result)
        other.put(self.request, self.result)
        self.cache.invalidate()
        self.assertIsNone(self.cache.get(self.request))
        self.assertIsNone(self.cache.get(page))
        self.assertIsNotNone(other.get(self.request))

    def test_busy_cache_lock_cannot_keep_invalidated_metadata(self):
        for read_first in (False, True):
            with self.subTest(read_first=read_first):
                cache = self.make_cache()
                cache.put(self.request, self.result)
                with (self.path / "lock").open("r+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if read_first:
                        self.assertIsNone(cache.get(self.request))
                    cache.invalidate()
                self.assertIsNone(self.make_cache().get(self.request))

    def test_native_hint_is_private_bounded_and_context_specific(self):
        self.assertFalse(self.cache.prefer_native())
        self.cache.mark_native()
        self.assertTrue(self.make_cache().prefer_native())
        self.assertTrue(self.make_cache(environment={"CLIENT": "two"}).prefer_native())
        self.assertFalse(self.make_cache(command=[*self.command, "--other"]).prefer_native())
        other = self.make_cache(environment={"CLIENT": "two"})
        self.cache.put(self.request, self.result)
        self.assertIsNone(other.get(self.request))
        hint = list(self.path.glob("*.native"))[0]
        self.assertEqual(set(json.loads(hint.read_text())), {"created", "expires"})
        self.now.return_value = 1299.999
        self.assertTrue(self.cache.prefer_native())
        self.now.return_value = 1300
        self.assertFalse(self.cache.prefer_native())

    def test_corrupt_entry_and_key_are_misses(self):
        self.cache.put(self.request, self.result)
        entry = self.entries()[0]
        for data in (b"{", b"[]", b'{"result":{}}', b"x" * (1024 * 1024 + 1)):
            entry.write_bytes(data)
            self.assertIsNone(self.cache.get(self.request))
        (self.path / "key").write_bytes(b"bad")
        broken = self.make_cache()
        self.assertFalse(broken.enabled)
        self.assertIsNone(broken.get(self.request))
        broken.put(self.request, self.result)
        broken.invalidate()
        broken.mark_native()
        self.assertFalse(broken.prefer_native())

    def test_unsafe_cache_directory_fails_closed(self):
        self.path.chmod(0o755)
        self.assertFalse(self.make_cache().enabled)
        self.assertIsNone(self.cache.get(self.request))
        self.assertFalse(self.cache.enabled)

    def test_symlink_parent_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / "target"
            target.mkdir()
            (home / ".local").symlink_to(target, target_is_directory=True)
            self.assertFalse(self.make_cache(home=home).enabled)
            self.assertEqual(list(target.iterdir()), [])

    def test_unsafe_entry_is_never_read_or_replaced(self):
        for kind in ("symlink", "hardlink", "mode"):
            with self.subTest(kind=kind):
                cache = self.make_cache()
                cache.put(self.request, self.result)
                entry = self.entries()[0]
                entry.unlink()
                target = self.home / kind
                target.write_text("preserve")
                target.chmod(0o600)
                if kind == "symlink":
                    entry.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, entry)
                else:
                    entry.write_text("preserve")
                    entry.chmod(0o644)
                self.assertIsNone(cache.get(self.request))
                cache.put(self.request, self.result)
                cache.invalidate()
                self.assertEqual(target.read_text(), "preserve")
                self.assertEqual(entry.read_text(), "preserve")
                entry.unlink()

    def test_foreign_owner_and_io_errors_disable_without_raising(self):
        with patch.object(CACHE.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(self.make_cache().enabled)
        with patch.object(CACHE.os, "open", side_effect=PermissionError):
            self.assertIsNone(self.cache.get(self.request))
            self.assertFalse(self.cache.enabled)
            self.assertFalse(self.make_cache().enabled)

    def test_bounded_storage_preserves_unrelated_files(self):
        unrelated = self.path / "keep.json"
        unrelated.write_text("keep")
        for index in range(140):
            cache = self.make_cache(environment={"CLIENT": str(index)})
            cache.put(self.request, self.result)
            cache.mark_native()
        disposable = [path for path in self.path.iterdir() if path.name.startswith("v1-")]
        self.assertLessEqual(len(disposable), 128)
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertTrue(cache.prefer_native())
        self.assertIsNotNone(cache.get(self.request))
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_oversized_result_is_not_written(self):
        self.cache.put(self.request, {**self.result, "description": "x" * (1024 * 1024)})
        self.assertEqual(self.entries(), [])

    def test_large_allowed_text_does_not_stall_privacy_check(self):
        result = {**self.result, "description": "x" * (512 * 1024)}
        self.cache.put(self.request, result)
        self.assertEqual(self.cache.get(self.request), result)

    def test_corrupt_timestamps_never_extend_lifetime(self):
        self.cache.put(self.request, self.result)
        entry = self.entries()[0]
        original = json.loads(entry.read_text())
        for changes in ({"expires": 999999999}, {"created": True}, {"expires": "later"},
                        {"created": 2000000}, {"expires": 999000}):
            entry.write_text(json.dumps({**original, **changes}))
            self.assertIsNone(self.cache.get(self.request))
        self.cache.mark_native()
        hint = list(self.path.glob("*.native"))[0]
        hint.write_text('{"created":1000000,"expires":999999999}')
        self.assertFalse(self.cache.prefer_native())
        hint.write_text("{")
        self.assertFalse(self.cache.prefer_native())

    def test_unsafe_key_and_lock_do_not_change_target(self):
        for name in ("key", "lock"):
            for kind in ("symlink", "hardlink", "fifo"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    self.assertTrue(self.make_cache(home=home).enabled)
                    path = home / ".local/state/fleet/mcp-cache" / name
                    path.unlink()
                    target = home / "preserve"
                    target.write_bytes(b"x" * 32)
                    target.chmod(0o600)
                    if kind == "symlink":
                        path.symlink_to(target)
                    elif kind == "hardlink":
                        os.link(target, path)
                    else:
                        os.mkfifo(path, 0o600)
                    self.assertFalse(self.make_cache(home=home).enabled)
                    self.assertEqual(target.read_bytes(), b"x" * 32)

    def test_failed_atomic_write_leaves_previous_entry_and_no_temporary_file(self):
        self.cache.put(self.request, self.result)
        with patch.object(CACHE.os, "replace", side_effect=OSError):
            self.cache.put(self.request, {**self.result, "tools": []})
        self.assertFalse(self.cache.enabled)
        self.assertEqual(self.make_cache().get(self.request), self.result)
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_concurrent_writers_keep_the_global_bound(self):
        def write(index):
            cache = self.make_cache(environment={"CLIENT": str(index)})
            cache.put(self.request, self.result)
            return cache.get(self.request)
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(write, range(160)))
        self.assertTrue(any(result is not None for result in results))
        self.assertLessEqual(len(self.entries()), 128)
        self.assertFalse(list(self.path.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
