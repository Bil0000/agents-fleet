#!/usr/bin/env python3
import importlib.util
import json
import os
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1] if len(sys.argv) < 2 else Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("fleet_memory", ROOT / "lib" / "fleet_memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


VALID_CONFIG = """\
version = "13.15.3"
server_host = "test-linux"
client_host = "test-mac"
ssh_target = "user@server.example"
worker_host = "127.0.0.1"
worker_port = 37702
tunnel_port = 37703
backup_retention_days = 14
"""


class FleetMemoryConfigTest(unittest.TestCase):
    def assert_invalid(self, contents, message=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.toml").write_text(contents)
            if message is None:
                with self.assertRaises(memory.FleetMemoryError):
                    memory.load_config(root)
            else:
                with self.assertRaisesRegex(memory.FleetMemoryError, message):
                    memory.load_config(root)

    def test_load_config_accepts_fleet_memory_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.toml").write_text(VALID_CONFIG)
            config = memory.load_config(root)

        self.assertEqual(config.version, "13.15.3")
        self.assertEqual(config.server_host, "test-linux")
        self.assertEqual(config.client_host, "test-mac")
        self.assertEqual(config.ssh_target, "user@server.example")
        self.assertEqual(config.worker_host, "127.0.0.1")
        self.assertEqual(config.worker_port, 37702)
        self.assertEqual(config.tunnel_port, 37703)
        self.assertEqual(config.backup_retention_days, 14)

    def test_load_config_rejects_public_worker_bind(self):
        self.assert_invalid(VALID_CONFIG.replace('"127.0.0.1"', '"0.0.0.0"'), "loopback")

    def test_load_config_rejects_unsafe_values(self):
        cases = {
            "unknown key": VALID_CONFIG + 'extra = "no"\n',
            "same ports": VALID_CONFIG.replace("tunnel_port = 37703", "tunnel_port = 37702"),
            "low port": VALID_CONFIG.replace("worker_port = 37702", "worker_port = 0"),
            "bad host": VALID_CONFIG.replace('server_host = "test-linux"', 'server_host = "../server"'),
            "bad target": VALID_CONFIG.replace('ssh_target = "user@server.example"', 'ssh_target = "server.example"'),
            "bad version": VALID_CONFIG.replace('version = "13.15.3"', 'version = "latest"'),
            "zero retention": VALID_CONFIG.replace("backup_retention_days = 14", "backup_retention_days = 0"),
            "large retention": VALID_CONFIG.replace("backup_retention_days = 14", "backup_retention_days = 366"),
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(contents)


class FleetMemoryCliTest(unittest.TestCase):
    def test_memory_command_has_scoped_usage(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "fleet"), "memory", "unknown"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: fleet memory", result.stderr)


class FleetMemoryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        (self.root / "memory.toml").write_text(VALID_CONFIG)
        shutil.copytree(ROOT / "system", self.root / "system")
        self.config = memory.load_config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def install_plugin_fixture(self, version="13.15.3"):
        plugin = self.home / ".claude/plugins/cache/thedotmack/claude-mem" / version
        script = plugin / "scripts/worker-service.cjs"
        script.parent.mkdir(parents=True)
        script.write_text("worker")
        registry = self.home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "claude-mem@thedotmack": [
                            {"scope": "user", "installPath": str(plugin), "version": version}
                        ]
                    },
                }
            )
        )
        return script

    def test_resolve_plugin_requires_exact_regular_pinned_worker(self):
        expected = self.install_plugin_fixture()

        self.assertEqual(memory.resolve_plugin(self.home, "13.15.3"), expected.resolve())
        with self.assertRaisesRegex(memory.FleetMemoryError, "13.15.4"):
            memory.resolve_plugin(self.home, "13.15.4")

    def test_configure_client_preserves_unmanaged_settings_and_freezes_only_claude_mem(self):
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps(
                {
                    "PRIVATE": "keep",
                    "CLAUDE_MEM_WORKER_PORT": "1",
                    "CLAUDE_MEM_PROVIDER": "gemini",
                }
            )
        )
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True)
        marketplaces.write_text(
            json.dumps(
                {
                    "thedotmack": {"source": {"repo": "thedotmack/claude-mem"}, "autoUpdate": True},
                    "other": {"source": {"repo": "owner/other"}, "autoUpdate": True},
                }
            )
        )

        memory.configure_client(self.home, self.config)

        applied = json.loads(settings.read_text())
        self.assertEqual(applied["PRIVATE"], "keep")
        self.assertEqual(applied["CLAUDE_MEM_RUNTIME"], "worker")
        self.assertEqual(applied["CLAUDE_MEM_WORKER_HOST"], "127.0.0.1")
        self.assertEqual(applied["CLAUDE_MEM_WORKER_PORT"], "37702")
        self.assertEqual(applied["CLAUDE_MEM_PROVIDER"], "gemini")
        self.assertNotIn("CLAUDE_MEM_CLAUDE_AUTH_METHOD", applied)
        self.assertEqual(os.stat(settings).st_mode & 0o777, 0o600)
        frozen = json.loads(marketplaces.read_text())
        self.assertFalse(frozen["thedotmack"]["autoUpdate"])
        self.assertTrue(frozen["other"]["autoUpdate"])

    def test_ssh_command_uses_batch_loopback_forwarding(self):
        self.assertEqual(
            memory.ssh_command(self.config),
            [
                "/usr/bin/ssh", "-N", "-T", "-o", "BatchMode=yes", "-o",
                "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o",
                "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-L",
                "127.0.0.1:37703:127.0.0.1:37702", "user@server.example",
            ],
        )

    def test_guard_forwards_when_tunnel_is_up_and_keeps_port_when_down(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(inner_self):
                data = inner_self.request.recv(1024)
                inner_self.request.sendall(data)

        echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        echo.daemon_threads = True
        echo_thread = threading.Thread(target=echo.serve_forever, daemon=True)
        echo_thread.start()
        listen_socket = socket.socket()
        listen_socket.bind(("127.0.0.1", 0))
        listen_port = listen_socket.getsockname()[1]
        listen_socket.close()
        stop = threading.Event()
        ready = threading.Event()
        guard_thread = threading.Thread(
            target=memory.serve_guard,
            args=(listen_port, echo.server_address[1], stop, ready),
            daemon=True,
        )
        guard_thread.start()
        self.assertTrue(ready.wait(2))

        with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
            client.sendall(b"shared-memory")
            self.assertEqual(client.recv(1024), b"shared-memory")

        echo.shutdown()
        echo.server_close()
        with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
            client.sendall(b"offline")
            try:
                received = client.recv(1024)
            except ConnectionResetError:
                received = b""
            self.assertEqual(received, b"")
        probe = socket.socket()
        with self.assertRaises(OSError):
            probe.bind(("127.0.0.1", listen_port))
        probe.close()
        self.assertFalse((self.home / ".claude-mem/claude-mem.db").exists())

        stop.set()
        guard_thread.join(2)
        self.assertFalse(guard_thread.is_alive())

    def test_enable_server_installs_owned_services_and_preserves_settings(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"PRIVATE": "keep"}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        memory.enable_memory(
            self.root,
            self.home,
            "test-linux",
            platform_name="linux",
            native_runner=runner,
        )

        units = self.home / ".config/systemd/user"
        self.assertEqual(
            (units / "fleet-memory.service").read_text(),
            (self.root / "system/linux/fleet-memory.service").read_text(),
        )
        self.assertTrue((units / "fleet-memory-backup.service").is_file())
        self.assertTrue((units / "fleet-memory-backup.timer").is_file())
        self.assertIn(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory.service"],
            commands,
        )
        self.assertEqual(json.loads(settings.read_text())["PRIVATE"], "keep")

    def test_enable_server_restores_files_when_native_start_fails(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"PRIVATE": "original"}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        before_settings = settings.read_bytes()
        before_marketplaces = marketplaces.read_bytes()

        def runner(command, home, platform):
            code = 1 if command[-3:] == ["enable", "--now", "fleet-memory.service"] else 0
            return subprocess.CompletedProcess(command, code, "", "failed" if code else "")

        with self.assertRaisesRegex(memory.FleetMemoryError, "enable"):
            memory.enable_memory(
                self.root,
                self.home,
                "test-linux",
                platform_name="linux",
                native_runner=runner,
            )

        self.assertEqual(settings.read_bytes(), before_settings)
        self.assertEqual(marketplaces.read_bytes(), before_marketplaces)
        self.assertFalse((self.home / ".config/systemd/user/fleet-memory.service").exists())

    def test_enable_client_installs_guard_launch_agent(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "", "")

        memory.enable_memory(
            self.root,
            self.home,
            "test-mac",
            platform_name="darwin",
            uid=501,
            native_runner=runner,
        )

        target = self.home / "Library/LaunchAgents/dev.agents-fleet.memory.plist"
        plist = __import__("plistlib").loads(target.read_bytes())
        self.assertEqual(plist["Label"], "dev.agents-fleet.memory")
        self.assertEqual(
            plist["ProgramArguments"],
            [str(self.home / ".local/bin/fleet"), "memory", "proxy"],
        )
        self.assertTrue(plist["KeepAlive"])

    def test_worker_exec_uses_pinned_bundle_and_safe_environment(self):
        worker = self.install_plugin_fixture()
        bun = self.home / ".bun/bin/bun"
        bun.parent.mkdir(parents=True)
        bun.write_text("bun")
        bun.chmod(0o700)
        captured = {}

        def execute(path, arguments, environment):
            captured.update(path=path, arguments=arguments, environment=environment)

        memory.run_worker(self.home, self.config, executor=execute)

        self.assertEqual(captured["path"], str(bun))
        self.assertEqual(captured["arguments"], [str(bun), str(worker.resolve()), "--daemon"])
        self.assertEqual(captured["environment"]["CLAUDE_MEM_WORKER_HOST"], "127.0.0.1")
        self.assertEqual(captured["environment"]["CLAUDE_MEM_WORKER_PORT"], "37702")
        self.assertNotIn("CLAUDE_MEM_PROVIDER", captured["environment"])
        self.assertNotIn("CLAUDE_MEM_CLAUDE_AUTH_METHOD", captured["environment"])

    def test_disable_server_stops_services_without_deleting_memory(self):
        database = self.home / ".claude-mem/claude-mem.db"
        database.parent.mkdir()
        database.write_bytes(b"memory")
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        memory.disable_memory(
            self.root,
            self.home,
            "test-linux",
            platform_name="linux",
            native_runner=runner,
        )

        self.assertIn(
            [
                "/usr/bin/systemctl", "--user", "disable", "--now",
                "fleet-memory-backup.timer", "fleet-memory.service",
            ],
            commands,
        )
        self.assertEqual(database.read_bytes(), b"memory")

    def test_run_proxy_keeps_guard_until_stop_and_terminates_ssh(self):
        listen_socket = socket.socket()
        listen_socket.bind(("127.0.0.1", 0))
        listen_port = listen_socket.getsockname()[1]
        listen_socket.close()
        tunnel_socket = socket.socket()
        tunnel_socket.bind(("127.0.0.1", 0))
        tunnel_port = tunnel_socket.getsockname()[1]
        tunnel_socket.close()
        config = replace(self.config, worker_port=listen_port, tunnel_port=tunnel_port)
        stop = threading.Event()

        class Process:
            def __init__(inner_self):
                inner_self.terminated = False

            def poll(inner_self):
                return None

            def terminate(inner_self):
                inner_self.terminated = True

            def wait(inner_self, timeout=None):
                return 0

        process = Process()
        commands = []

        def factory(command):
            commands.append(command)
            threading.Timer(0.2, stop.set).start()
            return process

        memory.run_proxy(config, stop=stop, process_factory=factory)

        self.assertEqual(commands, [memory.ssh_command(config)])
        self.assertTrue(process.terminated)

    def test_backup_creates_private_dated_snapshot_and_prunes(self):
        source = self.home / ".claude-mem/claude-mem.db"
        source.parent.mkdir()
        connection = sqlite3.connect(source)
        connection.executescript(
            """
            CREATE TABLE schema_versions(version INTEGER);
            INSERT INTO schema_versions VALUES (49);
            CREATE TABLE sdk_sessions(id INTEGER);
            CREATE TABLE observations(id INTEGER);
            CREATE TABLE session_summaries(id INTEGER);
            CREATE TABLE user_prompts(id INTEGER);
            CREATE TABLE pending_messages(status TEXT);
            """
        )
        connection.close()

        path, database_report = memory.backup_memory(
            self.home,
            self.config,
            now=datetime(2026, 8, 22, 10, 11, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            path,
            self.home / ".local/state/fleet/memory/backups/daily-20260822T101112Z.db",
        )
        self.assertEqual(database_report.schema_version, 49)
        self.assertEqual(database_report.quick_check, "ok")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_server_status_is_ok_only_with_pinned_worker_clean_db_and_backup(self):
        self.install_plugin_fixture()
        backup = self.home / ".local/state/fleet/memory/backups/daily-20260822T101112Z.db"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"backup")
        backup.chmod(0o600)
        database = self.home / ".claude-mem/claude-mem.db"
        database.parent.mkdir()
        database.write_bytes(b"database")
        report = SimpleNamespace(
            quick_check="ok",
            foreign_key_errors=(),
            schema_version=49,
            counts={"pending": 0, "processing": 0},
        )

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        with mock.patch.object(memory.db, "check_database", return_value=report), mock.patch.object(
            memory, "worker_health", return_value={"version": "13.15.3"}
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
            line, ok = memory.status_line(self.root, self.home, "test-linux")

        self.assertTrue(ok)
        self.assertEqual(line, "memory: ok")

    def test_client_status_requires_guard_and_remote_worker(self):
        self.install_plugin_fixture()

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "service = running\n", "")

        with mock.patch.object(memory, "worker_health", return_value={"version": "13.15.3"}), mock.patch.object(
            memory, "port_is_bound", return_value=True
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "darwin"):
            line, ok = memory.status_line(self.root, self.home, "test-mac")

        self.assertTrue(ok)
        self.assertEqual(line, "memory: ok")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
