#!/usr/bin/env python3
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_apply", ROOT / "lib" / "fleet_apply.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


class Native:
    def __init__(self, platform, home):
        self.platform = platform
        self.home = Path(home)
        self.active = False
        self.enabled = False
        self.calls = []
        self.fail_action = None
        self.fail_once_action = None
        self.foreign = False

    def __call__(self, command, **kwargs):
        command = tuple(str(value) for value in command)
        self.calls.append(command)
        if not command[0].startswith("/"):
            raise AssertionError(f"non-absolute native tool: {command[0]}")
        wanted_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C",
        }
        if self.platform == "linux":
            wanted_env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        if kwargs.get("env") != wanted_env:
            raise AssertionError(f"unsafe native environment: {kwargs.get('env')}")
        action = command[2] if command[0] == "/usr/bin/systemctl" else (command[1] if len(command) > 1 else "")
        if self.platform == "darwin":
            if action == "-lint":
                plistlib.loads(Path(command[-1]).read_bytes())
            elif action == "print":
                if self.fail_action == "print":
                    return subprocess.CompletedProcess(command, 77, "", "Not permitted")
                if not self.active:
                    return subprocess.CompletedProcess(command, 113, "", "Could not find service")
                executable = "/tmp/foreign" if self.foreign else str(self.home / ".local/bin/fleet")
                output = f"program = {executable}\narguments = {{\n\t{executable}\n\tupdate\n}}\n"
                return subprocess.CompletedProcess(command, 0, output, "")
            elif action == "bootstrap":
                self.active = True
            elif action == "bootout":
                self.active = False
        else:
            if action == "is-active":
                return subprocess.CompletedProcess(command, 0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
            if action == "is-enabled":
                return subprocess.CompletedProcess(command, 0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n", "")
            if action == "show":
                name = command[-1]
                if name == "fleet-update.timer":
                    path = "/tmp/foreign.timer" if self.foreign else str(self.home / ".config/systemd/user/fleet-update.timer")
                    output = f"FragmentPath={path}\n"
                else:
                    fragment = "/tmp/foreign.service" if self.foreign else str(self.home / ".config/systemd/user/fleet-update.service")
                    executable = "/tmp/foreign" if self.foreign else str(self.home / ".local/bin/fleet")
                    output = f"FragmentPath={fragment}\nExecStart={{ path={executable} ; argv[]={executable} update ; }}\n"
                return subprocess.CompletedProcess(command, 0, output, "")
            if action == "enable":
                self.enabled = True
                if "--now" in command:
                    self.active = True
            elif action == "disable":
                self.enabled = False
                if "--now" in command:
                    self.active = False
            elif action == "start":
                self.active = True
        if action == self.fail_action or action == self.fail_once_action:
            if action == self.fail_once_action:
                self.fail_once_action = None
            return subprocess.CompletedProcess(command, 23, "", "native error")
        return subprocess.CompletedProcess(command, 0, "", "")


class FleetSchedulerTest(unittest.TestCase):
    def init_repo(self):
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "gc.auto", "0"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "maintenance.auto", "false"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=self.repo, check=True, capture_output=True)

    def install_fleet(self, home):
        sha = "a" * 40
        release = home / ".local/share/fleet/releases" / sha
        executable = release / "bin/fleet"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/bash\nexit 0\n")
        executable.chmod(0o755)
        current = home / ".local/share/fleet/current"
        current.symlink_to(f"releases/{sha}")
        installed = home / ".local/bin/fleet"
        installed.parent.mkdir(parents=True)
        installed.symlink_to("../share/fleet/current/bin/fleet")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        host = self.repo / "hosts/test-mac"
        host.mkdir(parents=True, exist_ok=True)
        (host / "host.toml").write_text(
            'id = "test-mac"\nos = "macos"\nrole = "workstation"\n'
        )
        self.install_fleet(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def test_macos_enable_renders_one_minute_job_and_is_idempotent(self):
        native = Native("darwin", self.home)

        fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)

        target = self.home / "Library" / "LaunchAgents" / "dev.agents-fleet.update.plist"
        value = plistlib.loads(target.read_bytes())
        self.assertEqual(value["Label"], "dev.agents-fleet.update")
        self.assertEqual(value["ProgramArguments"], [str(self.home / ".local/bin/fleet"), "update"])
        self.assertEqual(value["StartInterval"], 60)
        self.assertIs(value["RunAtLoad"], True)
        self.assertEqual(value["StandardOutPath"], str(self.home / ".local/state/fleet/logs/update.log"))
        self.assertEqual(value["StandardErrorPath"], str(self.home / ".local/state/fleet/logs/update.error.log"))
        first_calls = list(native.calls)
        first_bytes = target.read_bytes()

        fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)

        self.assertEqual(target.read_bytes(), first_bytes)
        self.assertEqual([call for call in native.calls if call[1] == "bootstrap"], [
            ("/bin/launchctl", "bootstrap", "gui/501", str(target)),
        ])
        self.assertGreater(len(native.calls), len(first_calls))

    def test_linux_enable_installs_static_units_and_is_idempotent(self):
        native = Native("linux", self.home)

        fleet.enable_updates(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native)

        units = self.home / ".config" / "systemd" / "user"
        self.assertEqual((units / "fleet-update.service").read_bytes(), (self.repo / "system/linux/fleet-update.service").read_bytes())
        self.assertEqual((units / "fleet-update.timer").read_bytes(), (self.repo / "system/linux/fleet-update.timer").read_bytes())
        timer_text = (units / "fleet-update.timer").read_text()
        self.assertIn("OnActiveSec=1m", timer_text)
        self.assertIn("OnUnitActiveSec=1m", timer_text)
        self.assertIn("AccuracySec=1us", timer_text)
        self.assertNotIn("OnBootSec", timer_text)
        self.assertIn(("/usr/bin/systemctl", "--user", "daemon-reload"), native.calls)
        self.assertIn(("/usr/bin/systemctl", "--user", "enable", "--now", "fleet-update.timer"), native.calls)
        calls = list(native.calls)

        fleet.enable_updates(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native)

        systemctl_calls = [call for call in native.calls[len(calls):] if call[0] == "/usr/bin/systemctl"]
        self.assertEqual(systemctl_calls, [
            ("/usr/bin/systemctl", "--user", "is-enabled", "fleet-update.timer"),
            ("/usr/bin/systemctl", "--user", "is-active", "fleet-update.timer"),
            ("/usr/bin/systemctl", "--user", "show", "--property=FragmentPath", "fleet-update.timer"),
            (
                "/usr/bin/systemctl", "--user", "show", "--property=FragmentPath",
                "--property=ExecStart", "fleet-update.service",
            ),
        ])

    def test_enable_migrates_owned_hourly_jobs(self):
        mac_home = self.home / "legacy-mac"
        mac_home.mkdir()
        self.install_fleet(mac_home)
        mac_native = Native("darwin", mac_home)
        fleet.enable_updates(self.repo, mac_home, platform_name="darwin", uid=501, native_runner=mac_native)
        mac_target = mac_home / "Library/LaunchAgents/dev.agents-fleet.update.plist"
        old_plist = plistlib.loads(mac_target.read_bytes())
        old_plist["StartInterval"] = 3600
        mac_target.write_bytes(plistlib.dumps(old_plist, fmt=plistlib.FMT_XML, sort_keys=True))

        fleet.enable_updates(self.repo, mac_home, platform_name="darwin", uid=501, native_runner=mac_native)

        self.assertEqual(plistlib.loads(mac_target.read_bytes())["StartInterval"], 60)
        self.assertEqual(len([call for call in mac_native.calls if call[1] == "bootout"]), 1)
        self.assertEqual(len([call for call in mac_native.calls if call[1] == "bootstrap"]), 2)

        linux_home = self.home / "legacy-linux"
        linux_home.mkdir()
        self.install_fleet(linux_home)
        linux_native = Native("linux", linux_home)
        fleet.enable_updates(self.repo, linux_home, platform_name="linux", uid=1000, native_runner=linux_native)
        timer = linux_home / ".config/systemd/user/fleet-update.timer"
        timer.write_text(
            "[Unit]\nDescription=Check for Fleet updates hourly\n\n"
            "[Timer]\nOnBootSec=5m\nOnUnitActiveSec=1h\nRandomizedDelaySec=10m\n"
            "Persistent=true\nUnit=fleet-update.service\n\n[Install]\nWantedBy=timers.target\n"
        )

        fleet.enable_updates(self.repo, linux_home, platform_name="linux", uid=1000, native_runner=linux_native)

        self.assertEqual(timer.read_bytes(), (self.repo / "system/linux/fleet-update.timer").read_bytes())
        self.assertIn(("/usr/bin/systemctl", "--user", "disable", "--now", "fleet-update.timer"), linux_native.calls)

    def test_disable_removes_only_exact_fleet_files(self):
        for platform, uid in (("darwin", 501), ("linux", 1000)):
            with self.subTest(platform=platform):
                home = self.home / platform
                home.mkdir()
                self.install_fleet(home)
                native = Native(platform, home)
                fleet.enable_updates(self.repo, home, platform_name=platform, uid=uid, native_runner=native)
                if platform == "darwin":
                    unrelated = home / "Library/LaunchAgents/com.example.keep.plist"
                    owned = home / "Library/LaunchAgents/dev.agents-fleet.update.plist"
                else:
                    unrelated = home / ".config/systemd/user/example.service"
                    owned = home / ".config/systemd/user/fleet-update.timer"
                unrelated.write_text("keep exactly\n")

                fleet.disable_updates(self.repo, home, platform_name=platform, uid=uid, native_runner=native)

                self.assertFalse(owned.exists())
                self.assertEqual(unrelated.read_text(), "keep exactly\n")
                fleet.disable_updates(self.repo, home, platform_name=platform, uid=uid, native_runner=native)

    def test_disable_refuses_foreign_file(self):
        target = self.home / "Library/LaunchAgents/dev.agents-fleet.update.plist"
        target.parent.mkdir(parents=True)
        target.write_text("not Fleet\n")
        native = Native("darwin", self.home)

        with self.assertRaisesRegex(fleet.FleetError, "not owned"):
            fleet.disable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)

        self.assertEqual(target.read_text(), "not Fleet\n")
        self.assertEqual(native.calls, [])

    def test_status_requires_active_updater(self):
        self.init_repo()
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        managed = self.home / ".codex/AGENTS.md"
        managed.parent.mkdir()
        managed.write_text("managed\n")
        state = self.home / ".local/state/fleet/last-applied.json"
        state.parent.mkdir(parents=True)
        roots = [".codex/AGENTS.md"]
        state.write_text(json.dumps({
            "sha": sha,
            "host": "test-mac",
            "repository": str(self.repo),
            "roots": roots,
            "fingerprints": fleet.live_fingerprints(self.home, roots),
            "skipped": {},
            "applied_credentials": [],
        }))
        native = Native("darwin", self.home)
        fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)
        output = StringIO()

        with redirect_stdout(output):
            code = fleet.status(
                (self.home / ".local/share/fleet/current").resolve(),
                self.home,
                host="test-mac",
                credential_reader=lambda name: "set",
                platform_name="darwin",
                uid=501,
                native_runner=native,
            )
        self.assertEqual(code, 0)
        self.assertIn("updater: active", output.getvalue())

        native.active = False
        with redirect_stdout(StringIO()):
            code = fleet.status(
                self.repo,
                self.home,
                host="test-mac",
                credential_reader=lambda name: "set",
                platform_name="darwin",
                uid=501,
                native_runner=native,
            )
        self.assertEqual(code, 1)

    def test_native_failure_is_clear_and_restores_new_file(self):
        for platform, uid in (("darwin", 501), ("linux", 1000)):
            with self.subTest(platform=platform):
                home = self.home / f"failure-{platform}"
                home.mkdir()
                self.install_fleet(home)
                native = Native(platform, home)
                native.fail_once_action = "bootstrap" if platform == "darwin" else "enable"
                target = home / ("Library/LaunchAgents/dev.agents-fleet.update.plist" if platform == "darwin" else ".config/systemd/user/fleet-update.timer")

                with self.assertRaisesRegex(fleet.FleetError, "Could not enable Fleet updates") as caught:
                    fleet.enable_updates(self.repo, home, platform_name=platform, uid=uid, native_runner=native)

                self.assertNotIn("native error", str(caught.exception))
                self.assertFalse(target.exists())
                self.assertFalse(native.active)
                self.assertFalse(native.enabled)

    def test_failed_linux_enable_restores_prior_enabled_inactive_state(self):
        native = Native("linux", self.home)
        fleet.enable_updates(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native)
        native.active = False
        native.enabled = True
        native.fail_once_action = "enable"

        with self.assertRaisesRegex(fleet.FleetError, "Could not enable Fleet updates"):
            fleet.enable_updates(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native)

        self.assertTrue(native.enabled)
        self.assertFalse(native.active)
        self.assertTrue((self.home / ".config/systemd/user/fleet-update.timer").is_file())

    def test_macos_status_and_disable_fail_closed_for_unverified_job(self):
        native = Native("darwin", self.home)
        fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)
        target = self.home / "Library/LaunchAgents/dev.agents-fleet.update.plist"

        native.foreign = True
        self.assertEqual(fleet.updater_status(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native), "invalid")
        with self.assertRaisesRegex(fleet.FleetError, "not owned"):
            fleet.disable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)
        self.assertTrue(target.is_file())

        native.foreign = False
        native.fail_action = "print"
        self.assertEqual(fleet.updater_status(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native), "error")
        with self.assertRaisesRegex(fleet.FleetError, "could not be verified"):
            fleet.disable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)
        self.assertTrue(target.is_file())

    def test_linux_status_rejects_foreign_loaded_units(self):
        native = Native("linux", self.home)
        fleet.enable_updates(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native)
        native.foreign = True

        self.assertEqual(fleet.updater_status(self.repo, self.home, platform_name="linux", uid=1000, native_runner=native), "invalid")

    def test_enable_tightens_macos_log_directory_mode(self):
        logs = self.home / ".local/state/fleet/logs"
        logs.mkdir(parents=True)
        logs.chmod(0o777)

        fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=Native("darwin", self.home))

        self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o700)

    def test_enable_rejects_non_fleet_executable_chain(self):
        installed = self.home / ".local/bin/fleet"
        installed.unlink()
        installed.write_text("#!/bin/bash\nexit 0\n")
        installed.chmod(0o755)

        with self.assertRaisesRegex(fleet.FleetError, "fleet apply"):
            fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=Native("darwin", self.home))

        installed.unlink()
        installed.symlink_to("/bin/true")
        with self.assertRaisesRegex(fleet.FleetError, "fleet apply"):
            fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=Native("darwin", self.home))

    def test_enable_reports_original_and_cleanup_failures(self):
        native = Native("darwin", self.home)
        native.fail_once_action = "bootstrap"
        native.fail_action = "bootout"

        with self.assertRaisesRegex(fleet.FleetError, "Could not enable Fleet updates.*Cleanup also failed"):
            fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=native)

    def test_check_rejects_changed_scheduler_units(self):
        timer = self.repo / "system/linux/fleet-update.timer"
        timer.write_text(timer.read_text().replace("OnUnitActiveSec=1m", "OnUnitActiveSec=2m"))

        with self.assertRaisesRegex(fleet.FleetError, "Invalid Fleet updater unit files"):
            fleet.check_repository(self.repo)

    def test_check_rejects_extra_scheduler_directive(self):
        service = self.repo / "system/linux/fleet-update.service"
        service.write_text(service.read_text() + "ExecStartPre=/bin/true\n")

        with self.assertRaisesRegex(fleet.FleetError, "Invalid Fleet updater unit files"):
            fleet.check_repository(self.repo)

    def test_enable_rejects_symlinked_release_bin(self):
        current = self.home / ".local/share/fleet/current"
        release = current.resolve()
        outside = self.home / "outside-bin"
        outside.mkdir()
        executable = outside / "fleet"
        executable.write_text("#!/bin/bash\nexit 0\n")
        executable.chmod(0o755)
        shutil.rmtree(release / "bin")
        (release / "bin").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(fleet.FleetError, "fleet apply"):
            fleet.enable_updates(self.repo, self.home, platform_name="darwin", uid=501, native_runner=Native("darwin", self.home))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
