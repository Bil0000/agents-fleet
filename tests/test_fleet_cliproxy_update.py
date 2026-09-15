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

    def test_spoke_skips_release_lookup_and_activation(self):
        (self.state.parent / "hub.json").write_text(json.dumps({"role": "spoke"}))
        with mock.patch.object(UPDATE.Path, "home", return_value=self.home), mock.patch.object(
            UPDATE, "latest_release"
        ) as release, mock.patch.object(UPDATE, "activate") as activate:
            UPDATE.main([])
        release.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(self.config.read_text(), "plugins: /cliproxyapi/7.2.149/plugins\n")

    def test_release_version_order(self):
        self.assertTrue(UPDATE.is_newer("7.2.151", "7.2.149"))
        self.assertFalse(UPDATE.is_newer("7.2.149", "7.2.149"))
        with self.assertRaises(ValueError):
            UPDATE.is_newer("main", "7.2.149")

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

    def test_installer_adds_a_six_hour_launch_job(self):
        source = self.home / "source"
        (source / "lib").mkdir(parents=True)
        (source / "patches").mkdir()
        for relative in (
            "lib/fleet_cliproxy_update.py",
            "lib/fleet_provider_router.c",
            "lib/fleet_retry_bridge.py",
            "patches/cliproxyapi-provider-routing.patch",
        ):
            (source / relative).write_text(relative)
        commands = []

        UPDATE.install(self.home, source, run=lambda command, check=True: commands.append(command))

        target = self.home / ".local/share/fleet/cliproxyapi-updater"
        self.assertEqual((target / "fleet_cliproxy_update.py").read_text(), "lib/fleet_cliproxy_update.py")
        self.assertEqual((target / "provider-routing.patch").read_text(), "patches/cliproxyapi-provider-routing.patch")
        value = plistlib.loads((self.home / "Library/LaunchAgents/com.fleet.cliproxyapi.update.plist").read_bytes())
        self.assertEqual(value["StartInterval"], 21600)
        self.assertTrue(value["RunAtLoad"])
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
