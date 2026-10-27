#!/usr/bin/env python3
import importlib.util
import json
import plistlib
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).parents[1] / "lib/fleet_cliproxy_update.py"
SPEC = importlib.util.spec_from_file_location("fleet_cliproxy_update", SOURCE)
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


class CLIProxyUpdateTest(unittest.TestCase):
    def setUp(self):
        self.gate = mock.patch.object(UPDATE, "update_gate")
        self.gate_mock = self.gate.start()
        self.addCleanup(self.gate.stop)
        self.platform = mock.patch.object(UPDATE.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.native = mock.patch.object(UPDATE.subprocess, "run", side_effect=AssertionError("unexpected native command"))
        self.native.start()
        self.addCleanup(self.native.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.config = self.home / ".config/fleet/cliproxyapi.yaml"
        self.service = self.home / "Library/LaunchAgents/com.fleet.cliproxyapi.plist"
        self.state = self.home / ".local/state/fleet/cliproxyapi.json"
        self.current = self.home / ".local/share/fleet/cliproxyapi/7.2.149"
        self.candidate = self.home / "candidate"
        for path in (self.config, self.service, self.state, self.current / "plugins/x", self.candidate / "plugins/x"):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("plugins: /cliproxyapi/7.2.149/plugins\n")
        self.service.write_text("bridge=/cliproxyapi/7.2.149/fleet-retry-bridge\nbinary=/cliproxyapi/7.2.149/cli-proxy-api\n")
        self.state.write_text(json.dumps({"version": "7.2.149"}) + "\n")
        for name in ("cli-proxy-api", "fleet-retry-bridge"):
            (self.current / name).write_text("old")
            (self.candidate / name).write_text("new")
        (self.current / "plugins/x").write_text("old")
        (self.candidate / "plugins/x").write_text("new")

    def tearDown(self):
        self.temporary.cleanup()

    def test_bootstrap_builds_the_verified_release_without_latest_lookup(self):
        with mock.patch.object(UPDATE.Path, "home", return_value=self.home), mock.patch.object(
            UPDATE, "latest_release", side_effect=AssertionError("bootstrap must not select latest")
        ), mock.patch.object(UPDATE, "stage_bundle") as stage, mock.patch.object(
            UPDATE, "build_candidate", return_value=self.candidate
        ) as build, mock.patch.object(UPDATE, "activate") as activate:
            UPDATE.main(["--bootstrap"])
        stage.assert_called_once()
        self.assertEqual(build.call_args.args[1:3], ("7.2.151", "fleet-bootstrap"))
        activate.assert_called_once_with(self.home, "7.2.149", "7.2.151", self.candidate)

    def test_bootstrap_keeps_an_existing_newer_release(self):
        self.state.write_text('{"version":"7.2.152"}')
        with mock.patch.object(UPDATE.Path, "home", return_value=self.home), mock.patch.object(
            UPDATE, "build_candidate"
        ) as build, mock.patch.object(UPDATE, "activate") as activate:
            UPDATE.main(["--bootstrap"])
        build.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["version"], "7.2.152")

    def test_spoke_skips_release_lookup_and_activation(self):
        (self.state.parent / "hub.json").write_text(json.dumps({"role": "spoke"}))
        with mock.patch.object(UPDATE.Path, "home", return_value=self.home), mock.patch.object(
            UPDATE, "latest_release"
        ) as release, mock.patch.object(UPDATE, "activate") as activate:
            UPDATE.main([])
        release.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(self.config.read_text(), "plugins: /cliproxyapi/7.2.149/plugins\n")
    def test_major_release_waits_for_config_compatibility(self):
        with self.assertRaisesRegex(RuntimeError, "major"):
            UPDATE.activate(self.home, "7.2.149", "8.0.0", self.candidate, run=mock.Mock(side_effect=AssertionError("service must not change")))
        self.assertIn("7.2.149", self.config.read_text())

    def test_update_lock_rejects_overlap_and_releases_after_failure(self):
        with self.assertRaisesRegex(ValueError, "test failure"):
            with UPDATE.update_lock(self.home):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with UPDATE.update_lock(self.home):
                        self.fail("overlapping update acquired the lock")
                raise ValueError("test failure")
        with UPDATE.update_lock(self.home):
            pass

    def test_main_rejects_a_concurrent_update_before_building(self):
        with UPDATE.update_lock(self.home), mock.patch.object(UPDATE.Path, "home", return_value=self.home), mock.patch.object(UPDATE, "latest_release") as latest:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                UPDATE.main([])
        latest.assert_not_called()

    def test_settle_waits_until_both_old_listeners_stop(self):
        connection = mock.MagicMock()
        with mock.patch.object(UPDATE.socket, "create_connection", side_effect=[connection, ConnectionRefusedError(), ConnectionRefusedError(), ConnectionRefusedError()]) as connect, mock.patch.object(UPDATE.time, "sleep"):
            UPDATE.wait_for_proxy_stop()
        self.assertEqual(connect.call_count, 4)
        self.assertEqual([call.args[0][1] for call in connect.call_args_list], [8317, 8318, 8317, 8318])
        with mock.patch.object(UPDATE.socket, "create_connection", return_value=connection), mock.patch.object(UPDATE.time, "sleep"), mock.patch.object(UPDATE.time, "monotonic", side_effect=[0, 0, 16]):
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                UPDATE.wait_for_proxy_stop()

    def test_health_checks_the_candidate_behind_the_drain_gate(self):
        for name in ("cliproxyapi-client-key", "cliproxyapi-management-key"):
            (self.state.parent / name).write_text("key")
        with mock.patch.object(UPDATE.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.side_effect = [
                '{"data":[]}', '{"plugins":[{"id":"fleet-provider-router","registered":true}]}',
            ]
            UPDATE.probe_proxy(self.home)
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list], [
            "http://127.0.0.1:8318/v1/models", "http://127.0.0.1:8318/v0/management/plugins",
        ])
        self.gate_mock.assert_called_once_with(self.home, True)

    def test_busy_or_old_bridge_cannot_stop_the_service(self):
        self.gate_mock.side_effect = RuntimeError("busy or unsupported")
        run = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "busy or unsupported"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=run)
        run.assert_not_called()
        self.assertIn("7.2.149", self.service.read_text())
        self.assertFalse((self.current.parent / "7.2.151").exists())

    def test_activation_keeps_the_lock_through_probe_and_rollback(self):
        probes = []
        self.state.write_text(json.dumps({"version": "7.2.149", "managed": True}))

        def probe(_home):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with UPDATE.update_lock(self.home, "cliproxyapi-activation.lock"):
                    self.fail("activation lock was released before health check")
            probes.append(True)
            if len(probes) == 1:
                raise RuntimeError("new proxy failed")

        with self.assertRaisesRegex(RuntimeError, "new proxy failed"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=mock.Mock(), probe=probe, settle=lambda: None)
        self.assertEqual(len(probes), 2)
        self.assertEqual(json.loads(self.state.read_text()), {"version": "7.2.149", "managed": True})
        self.assertEqual(self.gate_mock.call_args_list, [mock.call(self.home, True), mock.call(self.home, False)])
        with UPDATE.update_lock(self.home, "cliproxyapi-activation.lock"):
            pass

    def test_failed_rollback_preserves_the_candidate_for_recovery(self):
        with self.assertRaisesRegex(RuntimeError, "rollback failed"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=mock.Mock(), probe=mock.Mock(side_effect=RuntimeError("unhealthy")), settle=lambda: None)
        self.assertTrue((self.current.parent / "7.2.151/cli-proxy-api").is_file())

    def test_gate_release_does_not_hide_a_failed_rollback(self):
        self.gate_mock.side_effect = [None, RuntimeError("bridge unavailable")]
        with self.assertRaisesRegex(RuntimeError, "rollback failed"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=mock.Mock(), probe=mock.Mock(side_effect=RuntimeError("unhealthy")), settle=lambda: None)

    def test_update_gate_requires_the_authenticated_protocol(self):
        self.gate.stop()
        key = self.home / ".local/state/fleet/cliproxyapi-management-key"
        key.write_text("secret")
        for result in ({"draining": True, "active": 0}, {"protocol": 1, "draining": True, "active": 1}, []):
            with self.subTest(result=result), mock.patch.object(UPDATE.urllib.request, "urlopen") as opened:
                opened.return_value.__enter__.return_value.read.return_value = json.dumps(result)
                with self.assertRaisesRegex(RuntimeError, "requested update state"):
                    UPDATE.update_gate(self.home, True)
                request = opened.call_args.args[0]
                self.assertEqual(request.headers["Authorization"], "Bearer secret")
                self.assertEqual(json.loads(request.data), {"drain": True})
        with mock.patch.object(UPDATE.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = '{"protocol":1,"draining":true,"active":0}'
            UPDATE.update_gate(self.home, True)

    def test_linux_activation_reloads_and_rolls_back_the_user_service(self):
        service = self.home / ".config/systemd/user/fleet-cliproxyapi.service"
        service.parent.mkdir(parents=True)
        service.write_bytes(self.service.read_bytes())
        probes = mock.Mock(side_effect=[RuntimeError("new proxy failed"), None])
        run = mock.Mock()
        with mock.patch.object(UPDATE.sys, "platform", "linux"), self.assertRaisesRegex(RuntimeError, "new proxy failed"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=run, probe=probes, settle=lambda: None)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["stop", "daemon-reload", "start", "stop", "daemon-reload", "start"])
        self.assertIn("7.2.149", service.read_text())
        self.assertFalse((self.current.parent / "7.2.151").exists())
        with mock.patch.object(UPDATE.sys, "platform", "linux"):
            UPDATE.activate(self.home, "7.2.149", "7.2.151", self.candidate, run=run, probe=lambda _home: None, settle=lambda: None)
        self.assertIn("7.2.151", service.read_text())

    def test_check_candidate_builds_without_installing_or_touching_services(self):
        with mock.patch.object(UPDATE, "stage_bundle") as stage, mock.patch.object(UPDATE, "build_candidate") as build, mock.patch.object(UPDATE, "install") as install:
            UPDATE.main(["--check-candidate", "7.2.151"])
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(build.call_args.args[1:3], ("7.2.151", "test"))
        install.assert_not_called()

    def test_release_version_order(self):
        self.assertTrue(UPDATE.is_newer("7.2.151", "7.2.149"))
        self.assertFalse(UPDATE.is_newer("7.2.149", "7.2.149"))
        with self.assertRaises(ValueError):
            UPDATE.is_newer("main", "7.2.149")

    def test_build_requires_cgo_even_when_disabled_in_the_environment(self):
        commands = []

        def run(command, **options):
            commands.append((command, options))
            if command[1] == "build":
                raise RuntimeError("build reached")
            return mock.Mock(stdout="revision")

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(UPDATE.os.environ, {"CGO_ENABLED": "0"}), mock.patch.object(
            UPDATE.shutil, "which", side_effect=lambda name: name
        ), mock.patch.object(UPDATE, "command_run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "build reached"):
                UPDATE.build_candidate(self.home, "7.2.151", "test", Path(directory))

        for command, options in commands:
            if command[0] == "go" and command[1] in ("test", "build"):
                self.assertEqual(options.get("env", {}).get("CGO_ENABLED"), "1", command)
            if command[:2] == ["go", "test"]:
                self.assertEqual(Path(options["env"]["FLEET_ACCOUNT_ROUTING_FILE"]).name, "test-account-routing.json")
        self.assertTrue(any(command[-1] == str(self.home / "response-interrupt.patch") for command, _ in commands))
        self.assertTrue(any(command[:2] == ["go", "test"] and "./sdk/api/handlers/openai" in command for command, _ in commands))

    def test_failed_activation_restores_the_running_version(self):
        commands = []
        probes = 0
        settles = []

        def run(command, check=True):
            commands.append(command)

        def probe(_home):
            nonlocal probes
            probes += 1
            if probes == 1:
                raise RuntimeError("new proxy failed")

        with self.assertRaisesRegex(RuntimeError, "new proxy failed"):
            UPDATE.activate(
                self.home,
                "7.2.149",
                "7.2.151",
                self.candidate,
                run=run,
                probe=probe,
                settle=lambda: settles.append(True),
            )

        self.assertIn("7.2.149", self.config.read_text())
        self.assertIn("7.2.149", self.service.read_text())
        self.assertEqual(json.loads(self.state.read_text()), {"version": "7.2.149"})
        self.assertFalse((self.home / ".local/share/fleet/cliproxyapi/7.2.151").exists())
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap", "bootout", "bootstrap"])
        self.assertEqual(probes, 2)
        self.assertEqual(len(settles), 2)

    def test_candidate_is_rejected_when_native_claude_versions_are_downgraded(self):
        (self.home / "fleet-retry-bridge").write_text("test bridge")

        def run(command, **options):
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_text("test binary")
            if command[0] == UPDATE.sys.executable:
                self.assertEqual(Path(command[-1]).name, "test_cliproxy_failover.py")
                self.assertEqual(Path(options["env"]["FLEET_CLIPROXYAPI"]).name, "cli-proxy-api")
                self.assertEqual(Path(options["env"]["FLEET_CLIPROXY_PLUGIN"]).name, "fleet-provider-router.dylib")
                raise RuntimeError("native Claude version was downgraded")
            return mock.Mock(stdout="CLIProxyAPI Version: 7.2.151", stderr="")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(UPDATE.shutil, "which", side_effect=lambda name: name), mock.patch.object(UPDATE, "command_run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "native Claude version was downgraded"):
                UPDATE.build_candidate(self.home, "7.2.151", "test", Path(directory))

    def test_successful_activation_switches_all_managed_paths(self):
        commands = []
        settles = []

        UPDATE.activate(
            self.home,
            "7.2.149",
            "7.2.151",
            self.candidate,
            run=lambda command, check=True: commands.append(command),
            probe=lambda _home: None,
            settle=lambda: settles.append(True),
        )

        target = self.home / ".local/share/fleet/cliproxyapi/7.2.151"
        self.assertEqual((target / "cli-proxy-api").read_text(), "new")
        self.assertIn("7.2.151", self.config.read_text())
        self.assertIn("7.2.151", self.service.read_text())
        self.assertEqual(json.loads(self.state.read_text()), {"version": "7.2.151"})
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap"])
        self.assertEqual(len(settles), 1)

    def bundle_source(self):
        source = self.home / "source"
        (source / "lib").mkdir(parents=True)
        (source / "patches").mkdir()
        (source / "tests").mkdir()
        for relative in (
            "lib/fleet_cliproxy_update.py",
            "lib/fleet_provider_router.c",
            "lib/fleet_retry_bridge.py",
            "patches/cliproxyapi-provider-routing.patch",
            "patches/cliproxyapi-credit-routing.patch",
            "patches/cliproxyapi-session-affinity.patch",
            "patches/cliproxyapi-response-interrupt.patch",
            "tests/test_cliproxy_failover.py",
        ):
            (source / relative).write_text(relative)
        (source / "lib/fleet_accounts.py").write_text("def limit_error_rules(provider):\n    return [{'provider': provider}]\n")
        return source

    def test_linux_installer_adds_a_six_hour_user_timer(self):
        run = mock.Mock()
        with mock.patch.object(UPDATE.sys, "platform", "linux"):
            UPDATE.install(self.home, self.bundle_source(), run=run)
        unit = self.home / ".config/systemd/user/fleet-cliproxyapi-update.service"
        timer = unit.with_suffix(".timer")
        self.assertIn("OnUnitActiveSec=6h", timer.read_text())
        self.assertIn("Type=oneshot", unit.read_text())
        self.assertIn(str(self.home / ".local/share/fleet/cliproxyapi-updater/fleet_cliproxy_update.py"), unit.read_text())
        self.assertEqual([call.args[0][2:] for call in run.call_args_list], [["daemon-reload"], ["enable", "--now", timer.name]])
        self.assertFalse((self.home / "Library/LaunchAgents/com.fleet.cliproxyapi.update.plist").exists())

    def test_installer_adds_a_six_hour_launch_job(self):
        source = self.bundle_source()
        commands = []

        UPDATE.install(self.home, source, run=lambda command, check=True: commands.append(command))

        target = self.home / ".local/share/fleet/cliproxyapi-updater"
        self.assertEqual((target / "fleet_cliproxy_update.py").read_text(), "lib/fleet_cliproxy_update.py")
        self.assertEqual((target / "provider-routing.patch").read_text(), "patches/cliproxyapi-provider-routing.patch")
        self.assertEqual((target / "credit-routing.patch").read_text(), "patches/cliproxyapi-credit-routing.patch")
        self.assertEqual((target / "response-interrupt.patch").read_text(), "patches/cliproxyapi-response-interrupt.patch")
        self.assertEqual((target / "test_cliproxy_failover.py").read_text(), "tests/test_cliproxy_failover.py")
        self.assertEqual(json.loads((target / "codex-error-rules.json").read_text()), [{"provider": "codex"}])
        self.assertEqual((target / "codex-error-rules.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((target / "credit-routing.patch").stat().st_mode & 0o777, 0o600)
        value = plistlib.loads((self.home / "Library/LaunchAgents/com.fleet.cliproxyapi.update.plist").read_bytes())
        self.assertEqual(value["StartInterval"], 21600)
        self.assertTrue(value["RunAtLoad"])
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
