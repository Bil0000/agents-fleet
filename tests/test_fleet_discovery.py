#!/usr/bin/env python3
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_discovery", ROOT / "lib" / "fleet_discovery.py")
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def run(*arguments, cwd):
    return subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)


class FleetDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        host = self.repo / "hosts/test-mac"
        host.mkdir()
        (host / "host.toml").write_text('id = "test-mac"\nos = "macos"\nrole = "workstation"\n')
        run("git", "init", "-b", "main", cwd=self.repo)
        run("git", "config", "user.email", "test@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        run("git", "config", "gc.auto", "0", cwd=self.repo)
        run("git", "config", "maintenance.auto", "false", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "fixture", cwd=self.repo)
        self.remote = self.base / "remote.git"
        run("git", "init", "--bare", str(self.remote), cwd=self.base)
        run("git", "config", "gc.auto", "0", cwd=self.remote)
        run("git", "config", "maintenance.auto", "false", cwd=self.remote)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        run("git", "push", "-u", "origin", "main", cwd=self.repo)

    def tearDown(self):
        self.temporary.cleanup()

    def scan(self, plugins=()):
        return discovery.scan(
            self.repo,
            self.home,
            "test-mac",
            plugin_provider=lambda root, home, host: list(plugins),
        )

    def remote_checkout(self):
        target = self.base / f"checkout-{len(list(self.base.glob('checkout-*')))}"
        run("git", "clone", "-b", "main", str(self.remote), str(target), cwd=self.base)
        return target

    def adopt(self, kind, name, scope):
        request = discovery.AdoptionRequest(self.repo, self.home, "test-mac", kind, name, scope)
        return discovery.adopt(request)

    def test_scan_baselines_existing_items_and_never_records_contents(self):
        skill = self.home / ".agents/skills/already-here"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text("baseline-private-text\n")

        self.assertEqual(self.scan(), [])

        hook = self.home / ".claude/hooks/new-hook.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/bash\n# hook-private-text\n")
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text(
            '[mcp_servers.new-server]\ncommand = "npx"\nargs = ["-y", "new-server"]\n'
            '[mcp_servers.new-server.env]\nAPI_TOKEN = "mcp-private-text"\n'
        )

        items = self.scan()

        self.assertEqual({(item["kind"], item["name"]) for item in items}, {("hook", "new-hook.sh"), ("mcp", "new-server")})
        state_path = self.home / ".local/state/fleet/discoveries.json"
        state_text = state_path.read_text()
        self.assertNotIn("baseline-private-text", state_text)
        self.assertNotIn("hook-private-text", state_text)
        self.assertNotIn("mcp-private-text", state_text)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

    def test_scheduled_scan_waits_one_hour_but_catches_managed_drift(self):
        def plugins(root, home, host):
            return list(installed)

        installed = []
        discovery.scan(self.repo, self.home, "test-mac", plugin_provider=plugins)
        installed.append(
            {
                "kind": "plugin",
                "name": "later@market",
                "client": "codex",
                "marketplace": "market",
                "source": "github:owner/market",
            }
        )
        items, drift = discovery.scan_if_due(self.repo, self.home, "test-mac", plugin_provider=plugins)
        self.assertEqual(items, [])
        self.assertFalse(drift)

        managed = self.home / ".codex/AGENTS.md"
        managed.parent.mkdir(parents=True)
        managed.write_text("managed\n")
        roots = [".codex/AGENTS.md"]
        last = self.home / ".local/state/fleet/last-applied.json"
        last.write_text(json.dumps({"roots": roots, "fingerprints": discovery.fleet.live_fingerprints(self.home, roots)}))
        managed.write_text("local edit\n")

        items, drift = discovery.scan_if_due(self.repo, self.home, "test-mac", plugin_provider=plugins)

        self.assertTrue(drift)
        self.assertIn({"kind": "drift", "name": "managed-files"}, items)
        self.assertIn(installed[0], items)

    def test_first_scan_never_hides_existing_managed_drift(self):
        managed = self.home / ".codex/AGENTS.md"
        managed.parent.mkdir(parents=True)
        managed.write_text("managed\n")
        roots = [".codex/AGENTS.md"]
        last = self.home / ".local/state/fleet/last-applied.json"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(json.dumps({"roots": roots, "fingerprints": discovery.fleet.live_fingerprints(self.home, roots)}))
        managed.write_text("local edit\n")

        items = self.scan()

        self.assertEqual(items, [{"kind": "drift", "name": "managed-files"}])

    def test_adopt_shared_skill_pushes_one_validated_commit(self):
        self.scan()
        source = self.home / ".agents/skills/fresh-skill"
        source.mkdir(parents=True)
        source.joinpath("SKILL.md").write_text("# Fresh skill\n")
        self.scan()
        before = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()

        result = self.adopt("skill", "fresh-skill", "shared")

        checkout = self.remote_checkout()
        self.assertEqual((checkout / "skills/fresh-skill/SKILL.md").read_text(), "# Fresh skill\n")
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip(), before)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.repo).stdout, "")
        self.assertEqual(result["kind"], "skill")

    def test_adopt_host_hook_pushes_the_discovered_script(self):
        self.scan()
        hook = self.home / ".codex/hooks/fresh-hook.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/bash\nexit 0\n")
        hook.chmod(0o755)
        self.scan()

        self.adopt("hook", "fresh-hook.sh", "host")

        checkout = self.remote_checkout()
        adopted_hook = checkout / "hosts/test-mac/hooks/fresh-hook.sh"
        self.assertEqual(adopted_hook.read_text(), "#!/bin/bash\nexit 0\n")

    def test_adopt_shared_plugin_adds_its_verified_marketplace(self):
        self.scan()
        plugin = {
            "kind": "plugin",
            "name": "fresh@fresh-market",
            "client": "codex",
            "marketplace": "fresh-market",
            "source": "github:owner/fresh-market",
        }
        self.scan([plugin])

        self.adopt("plugin", "fresh@fresh-market", "shared")

        checkout = self.remote_checkout()
        manifest = json.loads((checkout / "plugins.json").read_text())
        self.assertEqual(manifest["codex"]["marketplaces"]["fresh-market"], "owner/fresh-market")
        self.assertIn("fresh@fresh-market", manifest["codex"]["plugins"])

    def test_adopt_secretless_mcp_creates_matching_client_and_runner_entries(self):
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text("")
        self.scan()
        codex.write_text('[mcp_servers.fresh]\ncommand = "npx"\nargs = ["-y", "fresh-mcp@1.0.0"]\n')
        self.scan()

        self.adopt("mcp", "fresh", "host")

        checkout = self.remote_checkout()
        host = checkout / "hosts/test-mac/mcp"
        self.assertEqual(
            tomllib.loads((host / "runners.toml").read_text())["servers"]["fresh"]["command"],
            ["npx", "-y", "fresh-mcp@1.0.0"],
        )
        self.assertEqual(
            json.loads((host / "claude.json").read_text())["mcpServers"]["fresh"]["command"],
            "fleet",
        )

    def test_adopt_mcp_rejects_environment_values_before_git_changes(self):
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text("")
        self.scan()
        codex.write_text(
            '[mcp_servers.leaky]\ncommand = "npx"\nargs = ["leaky"]\n'
            '[mcp_servers.leaky.env]\nAPI_TOKEN = "never-store-this"\n'
        )
        self.scan()
        remote_before = run("git", "rev-parse", "refs/heads/main", cwd=self.remote).stdout.strip()

        with self.assertRaisesRegex(discovery.FleetError, "secret-free"):
            self.adopt("mcp", "leaky", "host")

        self.assertEqual(run("git", "rev-parse", "refs/heads/main", cwd=self.remote).stdout.strip(), remote_before)


class FleetMemoryStatusLineTest(unittest.TestCase):
    def test_memory_status_line_is_available_before_memory_is_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            line, ok = discovery.memory_status_line(ROOT, home, "test-mac")

        self.assertEqual(line, "memory: local (plugin-managed)")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
