#!/usr/bin/env python3
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("fleet_onboard", ROOT / "lib/fleet_onboard.py")
onboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onboard)


class FleetHostAddTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "hosts").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_add_host_creates_minimal_profile_and_codex_memory_plugin(self):
        onboard.add_host(self.root, "new-devbox", "linux", "devbox")

        host = self.root / "hosts/new-devbox"
        self.assertEqual(
            (host / "host.toml").read_text(),
            'id = "new-devbox"\nos = "linux"\nrole = "devbox"\n',
        )
        self.assertEqual(
            (host / "plugins.json").read_text(),
            '{\n'
            '  "codex": {\n'
            '    "marketplaces": {\n'
            '      "claude-mem-local": "thedotmack/claude-mem"\n'
            '    },\n'
            '    "plugins": [\n'
            '      "claude-mem@claude-mem-local"\n'
            '    ]\n'
            '  },\n'
            '  "claude": {\n'
            '    "marketplaces": {},\n'
            '    "plugins": []\n'
            '  }\n'
            '}\n',
        )
        self.assertEqual(sorted(path.name for path in host.iterdir()), ["host.toml", "plugins.json"])

    def test_onboard_can_create_a_profile_before_setup(self):
        with mock.patch.object(onboard.fleet, "source_repository", return_value=self.root), mock.patch.object(
            onboard, "onboard_host"
        ) as setup:
            onboard.main([str(self.root), "onboard", "--host", "new-devbox", "--os", "linux", "--role", "devbox"])
        self.assertEqual(onboard.read_host(self.root, "new-devbox")["os"], "linux")
        setup.assert_called_once_with(self.root, "new-devbox", "local")
        for option, value in (("--os", "linux"), ("--role", "devbox")):
            with self.assertRaises(SystemExit):
                onboard.parse_arguments(["onboard", "--host", "new-devbox", option, value])

    def test_add_host_refuses_to_change_existing_host(self):
        host = self.root / "hosts/existing"
        host.mkdir()
        profile = host / "host.toml"
        profile.write_text("keep\n")

        with self.assertRaisesRegex(onboard.FleetOnboardError, "already exists"):
            onboard.add_host(self.root, "existing", "linux", "devbox")

        self.assertEqual(profile.read_text(), "keep\n")

    def test_add_host_rejects_unsafe_values(self):
        for host, os_name, role in (
            ("../escape", "linux", "devbox"),
            ("safe", "windows", "devbox"),
            ("safe", "linux", ""),
            ("safe", "linux", "bad\nrole"),
        ):
            with self.subTest(host=host, os_name=os_name, role=role):
                with self.assertRaises(onboard.FleetOnboardError):
                    onboard.add_host(self.root, host, os_name, role)

    def test_fleet_cli_routes_host_add(self):
        (self.root / ".git").mkdir()
        (self.root / "bin").mkdir()
        shutil.copy2(ROOT / "bin/fleet", self.root / "bin/fleet")
        shutil.copytree(ROOT / "lib", self.root / "lib")

        result = subprocess.run(
            [str(self.root / "bin/fleet"), "host", "add", "cli-host", "--os", "linux", "--role", "devbox"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "hosts/cli-host/host.toml").is_file())


class FleetOnboardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "hosts/new-devbox").mkdir(parents=True)
        (self.root / "hosts/new-devbox/host.toml").write_text(
            'id = "new-devbox"\nos = "linux"\nrole = "devbox"\n'
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_shared_onboard_runs_existing_safe_steps_in_order(self):
        commands = []

        def runner(command, cwd):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        onboard.onboard_host(
            self.root,
            "new-devbox",
            "shared",
            platform_name="linux",
            prerequisite_checker=lambda *_: None,
            runner=runner,
        )

        root = self.root.resolve()
        fleet = str(root / "bin/fleet")
        self.assertEqual(
            commands,
            [
                [fleet, "check"],
                [str(root / "tests/test_fleet.sh")],
                [fleet, "apply", "--host", "new-devbox", "--dry-run"],
                [fleet, "apply", "--host", "new-devbox"],
                [fleet, "account", "setup"],
                [fleet, "memory", "enable", "--host", "new-devbox"],
                [fleet, "update", "on"],
                [fleet, "update", "--host", "new-devbox"],
                [fleet, "status", "--host", "new-devbox"],
                [fleet, "auth", "status"],
            ],
        )

    def test_local_onboard_skips_shared_memory_and_rechecks_apply(self):
        commands = []

        def runner(command, cwd):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        onboard.onboard_host(
            self.root,
            "new-devbox",
            "local",
            platform_name="linux",
            prerequisite_checker=lambda *_: None,
            runner=runner,
        )

        fleet = str(self.root.resolve() / "bin/fleet")
        self.assertNotIn([fleet, "memory", "enable", "--host", "new-devbox"], commands)
        self.assertEqual(commands[-2], [fleet, "apply", "--host", "new-devbox", "--dry-run"])
        self.assertEqual(commands[-1], [fleet, "auth", "status"])

    def test_onboard_refuses_wrong_platform_before_running_commands(self):
        commands = []

        with self.assertRaisesRegex(onboard.FleetOnboardError, "Linux"):
            onboard.onboard_host(
                self.root,
                "new-devbox",
                "shared",
                platform_name="darwin",
                prerequisite_checker=lambda *_: None,
                runner=lambda command, cwd: commands.append(command),
            )

        self.assertEqual(commands, [])

    def test_onboard_rejects_unsafe_host_before_reading_or_running(self):
        commands = []
        (self.root / "new-devbox").mkdir()
        (self.root / "new-devbox/host.toml").write_text(
            'id = "../new-devbox"\nos = "linux"\nrole = "devbox"\n'
        )

        def runner(command, cwd):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(onboard.FleetOnboardError, "host"):
            onboard.onboard_host(
                self.root,
                "../new-devbox",
                "shared",
                platform_name="linux",
                prerequisite_checker=lambda *_: None,
                runner=runner,
            )

        self.assertEqual(commands, [])

    def test_onboard_stops_at_first_failed_step(self):
        commands = []

        def runner(command, cwd):
            commands.append(command)
            code = 1 if command[-1:] == ["--dry-run"] else 0
            return subprocess.CompletedProcess(command, code, "", "failed")

        with self.assertRaisesRegex(onboard.FleetOnboardError, "apply"):
            onboard.onboard_host(
                self.root,
                "new-devbox",
                "shared",
                platform_name="linux",
                prerequisite_checker=lambda *_: None,
                runner=runner,
            )

        self.assertEqual(len(commands), 3)

    def test_prerequisites_reject_missing_tools(self):
        available = {"bash", "git", "node", "bun", "claude", "ssh"}

        with self.assertRaisesRegex(onboard.FleetOnboardError, "codex"):
            onboard.check_prerequisites(
                self.root,
                True,
                which=lambda name, path=None: f"/bin/{name}" if name in available else None,
                runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "v20.12.0\n", ""),
            )

    def test_prerequisites_reject_old_node(self):
        with self.assertRaisesRegex(onboard.FleetOnboardError, "20.12"):
            onboard.check_prerequisites(
                self.root,
                False,
                which=lambda name, path=None: f"/bin/{name}",
                runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "v20.11.1\n", ""),
            )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
