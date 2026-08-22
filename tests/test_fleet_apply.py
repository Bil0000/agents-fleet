#!/usr/bin/env python3
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_apply", ROOT / "lib/fleet_apply.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class Missing:
    def __call__(self, name):
        raise fleet.MissingCredential(name)


class FleetApplyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        host = self.repo / "hosts/test-linux"
        host.mkdir()
        (host / "host.toml").write_text('id = "test-linux"\nos = "linux"\nrole = "workstation"\n')
        run("git", "init", "-b", "main", cwd=self.repo)
        run("git", "config", "user.email", "test@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "fixture", cwd=self.repo)
        (self.home / ".codex").mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".codex/config.toml").write_text(
            'model = "keep"\nnotify = ["keep"]\n[mcp_servers.mine]\ncommand = "mine"\n'
        )
        (self.home / ".claude/settings.json").write_text(
            json.dumps({"model": "keep", "env": {"PRIVATE": "keep"}})
        )
        (self.home / ".claude.json").write_text(
            json.dumps({"projects": {"keep": {}}, "mcpServers": {"mine": {"command": "mine"}}})
        )

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self):
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "test change", cwd=self.repo)

    def apply(self, **kwargs):
        return fleet.apply(
            self.repo,
            self.home,
            host="test-linux",
            credential_reader=Missing(),
            **kwargs,
        )

    def test_blank_template_preserves_unmanaged_configuration(self):
        result = self.apply()
        codex = tomllib.loads((self.home / ".codex/config.toml").read_text())
        claude = json.loads((self.home / ".claude/settings.json").read_text())
        state = json.loads((self.home / ".claude.json").read_text())

        self.assertEqual(codex["model"], "keep")
        self.assertEqual(codex["notify"], ["keep"])
        self.assertEqual(codex["mcp_servers"], {"mine": {"command": "mine"}})
        self.assertEqual(claude, {"model": "keep", "env": {"PRIVATE": "keep"}})
        self.assertEqual(state["mcpServers"], {"mine": {"command": "mine"}})
        self.assertEqual((self.home / ".codex/AGENTS.md").read_text(), "\n")
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_text(), "\n")
        self.assertEqual(result["managed_mcp"], [])
        self.assertEqual(result["plugins"], {"claude": [], "codex": []})

    def test_rules_stay_in_their_client_scope(self):
        (self.repo / "rules/common.md").write_text("COMMON\n")
        (self.repo / "rules/claude.md").write_text("CLAUDE\n")
        (self.repo / "rules/codex.md").write_text("CODEX\n")
        (self.repo / "hosts/test-linux/rules.md").write_text("HOST\n")

        self.assertEqual(fleet.compose_rules(self.repo, "test-linux", "codex"), "COMMON\n\nCODEX\n\nHOST\n")
        self.assertEqual(fleet.compose_rules(self.repo, "test-linux", "claude"), "COMMON\n\nCLAUDE\n\nHOST\n")

    def test_shared_content_is_linked_and_existing_skills_are_kept(self):
        existing = self.home / ".agents/skills/existing/SKILL.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("existing\n")
        skill = self.repo / "skills/example"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Example\n")
        (self.repo / "commands/example.md").write_text("# Example\n")
        (self.repo / "hooks/example.sh").write_text("#!/bin/bash\nexit 0\n")
        self.commit()

        self.apply()

        self.assertEqual(existing.read_text(), "existing\n")
        for path in (
            ".agents/skills/example",
            ".codex/skills/example",
            ".claude/skills/example",
            ".codex/prompts/example.md",
            ".claude/commands/example.md",
            ".codex/hooks/example.sh",
            ".claude/hooks/example.sh",
        ):
            self.assertTrue((self.home / path).is_symlink(), path)

    def test_dry_run_writes_nothing(self):
        before = sorted(path.relative_to(self.home) for path in self.home.rglob("*"))
        with redirect_stdout(StringIO()) as output:
            result = self.apply(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertIn("host: test-linux", output.getvalue())
        self.assertEqual(before, sorted(path.relative_to(self.home) for path in self.home.rglob("*")))

    def test_failed_apply_restores_existing_files(self):
        original = (self.home / ".codex/config.toml").read_bytes()

        def fail_second(index, target):
            if index == 2:
                raise RuntimeError("stop")

        with self.assertRaisesRegex(fleet.FleetError, "previous state was restored"):
            self.apply(fault=fail_second)

        self.assertEqual((self.home / ".codex/config.toml").read_bytes(), original)

    def test_status_reports_managed_drift(self):
        self.apply()
        (self.home / ".codex/AGENTS.md").write_text("changed\n")
        with redirect_stdout(StringIO()) as output:
            healthy = fleet.status(
                self.repo,
                self.home,
                host="test-linux",
                platform_name="linux",
                native_runner=lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "active\n", ""),
            )

        self.assertNotEqual(healthy, 0)
        self.assertIn("drift: yes", output.getvalue())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
