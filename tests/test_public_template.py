#!/usr/bin/env python3
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_mcp", ROOT / "lib" / "fleet_mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
discovery_spec = importlib.util.spec_from_file_location(
    "fleet_discovery", ROOT / "lib" / "fleet_discovery.py"
)
discovery = importlib.util.module_from_spec(discovery_spec)
discovery_spec.loader.exec_module(discovery)
apply_spec = importlib.util.spec_from_file_location(
    "fleet_apply", ROOT / "lib" / "fleet_apply.py"
)
apply = importlib.util.module_from_spec(apply_spec)
apply_spec.loader.exec_module(apply)
memory_spec = importlib.util.spec_from_file_location(
    "fleet_memory", ROOT / "lib" / "fleet_memory.py"
)
memory = importlib.util.module_from_spec(memory_spec)
memory_spec.loader.exec_module(memory)


class PublicMcpTemplateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        directory = self.root / "mcp"
        directory.mkdir()
        (directory / "codex.toml").write_text("[mcp_servers]\n")
        (directory / "claude.json").write_text(json.dumps({"mcpServers": {}}))
        (directory / "runners.toml").write_text("[servers]\n")
        (directory / "required-secrets.txt").write_text("")
        (directory / "README.md").write_text("MCP configuration.\n")
        (self.root / "hosts").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_mcp_bundle_is_valid(self):
        mcp.validate_all(self.root)
        self.assertEqual(mcp.load_runners(self.root, "test-local"), {})

    def test_mcp_must_exist_in_both_client_manifests(self):
        (self.root / "mcp/codex.toml").write_text(
            '[mcp_servers.docs]\nurl = "https://example.com/mcp"\n'
        )

        with self.assertRaisesRegex(mcp.FleetError, "matching Codex and Claude entries"):
            mcp.validate_all(self.root)

    def test_runner_secrets_must_be_registered(self):
        (self.root / "mcp/codex.toml").write_text(
            '[mcp_servers.docs]\ncommand = "fleet"\n'
            'args = ["mcp", "run", "docs", "--", "docs-mcp"]\n'
        )
        (self.root / "mcp/claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "docs": {
                            "type": "stdio",
                            "command": "fleet",
                            "args": ["mcp", "run", "docs", "--", "docs-mcp"],
                        }
                    }
                }
            )
        )
        (self.root / "mcp/runners.toml").write_text(
            '[servers.docs]\nsecrets = ["DOCS_TOKEN"]\ncommand = ["docs-mcp"]\n'
        )

        with self.assertRaisesRegex(mcp.FleetError, "Unregistered MCP secret: DOCS_TOKEN"):
            mcp.validate_all(self.root)

    def test_runner_needs_an_explicit_command(self):
        (self.root / "mcp/runners.toml").write_text(
            '[servers.docs]\nsecrets = []\ntool = "bundled-binary"\nargs = ["serve"]\n'
        )

        with self.assertRaisesRegex(mcp.FleetError, "Invalid managed MCP runner manifest"):
            mcp.validate_runner_file(self.root, self.root / "mcp/runners.toml")


class PublicMemoryTemplateTest(unittest.TestCase):
    def test_missing_shared_memory_config_uses_local_plugin_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            line, ok = discovery.memory_status_line(ROOT, Path(temporary), "test-local")

        self.assertEqual(line, "memory: local (plugin-managed)")
        self.assertTrue(ok)

    def test_shared_memory_command_explains_how_to_enable_it(self):
        with self.assertRaisesRegex(
            memory.FleetMemoryError,
            "Copy memory.example.toml to memory.toml",
        ):
            memory.load_config(ROOT)


class PublicPluginTemplateTest(unittest.TestCase):
    def test_empty_plugin_manifest_needs_no_client_binaries(self):
        def missing_client(name, home):
            raise AssertionError(f"looked for {name}")

        self.assertEqual(
            apply.sync_plugins(
                ROOT,
                Path("/tmp/fleet-test-home"),
                "test-local",
                clients=(missing_client, None),
            ),
            {"claude": [], "codex": []},
        )


class PublicRepositoryValidationTest(unittest.TestCase):
    def test_home_path_outside_host_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "mcp"
            directory.mkdir()
            (directory / "codex.toml").write_text("[mcp_servers]\n")
            (directory / "claude.json").write_text('{"mcpServers": {}}\n')
            (directory / "runners.toml").write_text("[servers]\n")
            (directory / "required-secrets.txt").write_text("")
            (directory / "README.md").write_text("MCP configuration.\n")
            (root / "hosts").mkdir()
            (root / "plugins.json").write_text(
                '{"codex":{"marketplaces":{},"plugins":[]},'
                '"claude":{"marketplaces":{},"plugins":[]}}\n'
            )
            shutil.copytree(ROOT / "system", root / "system")
            (root / "portable.md").write_text("machine path: /Users/" + "example/project\n")

            with self.assertRaisesRegex(
                discovery.fleet.FleetError,
                "Device path outside host overlay",
            ):
                discovery.fleet.check_repository(root)


class PublicDocumentationTest(unittest.TestCase):
    def test_setup_starts_with_a_user_owned_private_repository(self):
        readme = (ROOT / "README.md").read_text()
        setup = (ROOT / "SETUP_WITH_AN_AGENT.md").read_text()

        self.assertIn("Use this template", readme)
        self.assertIn("git remote get-url origin", setup)
        self.assertIn("Do not fork", readme + setup)
        self.assertNotIn("actions/workflows/check.yml/badge.svg", readme)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
