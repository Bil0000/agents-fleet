#!/usr/bin/env python3
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()


class FleetClientLaunchersTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.shims = Path(self.temporary.name) / "shims"
        self.native = Path(self.temporary.name) / "native"
        self.shims.mkdir()
        self.native.mkdir()
        self.log = Path(self.temporary.name) / "calls"
        for name in ("fleet", "fleet-codex", "fleet-claude"):
            path = self.shims / name
            path.write_text("#!/bin/sh\n" + "printf '%s\\n' \"" + name + ":$*\" >> \"$CALLS\"\n")
            path.chmod(0o755)
        for name in ("codex", "claude"):
            path = self.native / name
            path.write_text("#!/bin/sh\n" + "printf '%s\\n' \"" + name + ":$*\" >> \"$CALLS\"\n")
            path.chmod(0o755)
        self.environment = {
            "PATH": str(self.shims) + ":" + str(self.native) + ":/usr/bin:/bin",
            "CALLS": str(self.log),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_launcher(self, name, *arguments):
        launcher = ROOT / "bin" / "fleet-client"
        self.assertTrue(launcher.exists(), f"missing {launcher}")
        installed = self.shims / name
        shutil.copy2(launcher, installed)
        return subprocess.run([installed, *arguments], capture_output=True, text=True, env=self.environment)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_codex_auth_runs_the_native_command_through_fleet(self):
        result = self.run_launcher("codex", "login", "--device-auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [f"fleet:account native-auth codex {self.native / 'codex'} login --device-auth"],
        )

    def test_claude_auth_runs_the_native_command_through_fleet(self):
        result = self.run_launcher("claude", "auth", "logout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [f"fleet:account native-auth claude {self.native / 'claude'} auth logout"],
        )

    def test_status_and_non_auth_commands_stay_native(self):
        self.assertEqual(self.run_launcher("codex", "login", "status").returncode, 0)
        self.assertEqual(self.run_launcher("codex", "exec", "hello").returncode, 0)
        self.assertEqual(self.run_launcher("claude", "auth", "status").returncode, 0)
        self.assertEqual(self.run_launcher("claude", "--version").returncode, 0)
        self.assertEqual(
            self.calls(),
            ["codex:login status", "codex:exec hello", "claude:auth status", "claude:--version"],
        )

    def test_legacy_launcher_names_keep_working(self):
        self.assertEqual(self.run_launcher("fleet-codex", "logout").returncode, 0)
        self.assertEqual(self.run_launcher("fleet-claude", "auth", "login", "--sso").returncode, 0)
        self.assertEqual(
            self.calls(),
            [
                f"fleet:account native-auth codex {self.native / 'codex'} logout",
                f"fleet:account native-auth claude {self.native / 'claude'} auth login --sso",
            ],
        )

    def test_shell_hook_uses_fleet_only_for_auth(self):
        command = (
            f". {ROOT / 'bin/fleet-shell'}; "
            "codex login --device-auth; codex exec hello; "
            "claude auth login --sso; claude --version"
        )
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "fleet-codex:login --device-auth",
                "codex:exec hello",
                "fleet-claude:auth login --sso",
                "claude:--version",
            ],
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
