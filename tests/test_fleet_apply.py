#!/usr/bin/env python3
import hashlib
import os
import stat
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
from unittest import mock


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
    def test_codex_agent_limit_preserves_roles_and_is_idempotent(self):
        settings = {"agents": {"max_concurrent_threads_per_session": 64}}
        for current in ('', '[agents]\nmax_concurrent_threads_per_session = 3\n', '[agents]\nmax_concurrent_threads_per_session = 64\n'):
            with self.subTest(current=current):
                current += '[agents.worker]\ndescription = "keep"\n'
                self.assertEqual(tomllib.loads(fleet.render_codex(current, {}, {}, {})), tomllib.loads(current))
                rendered = fleet.render_codex(current, settings, {}, {})
                self.assertEqual(tomllib.loads(rendered), {"agents": {
                    "max_concurrent_threads_per_session": 64,
                    "worker": {"description": "keep"},
                }})
                self.assertEqual(fleet.render_codex(rendered, settings, {}, {}), rendered)


    def test_codex_memories_manage_only_selected_fields(self):
        current = '[memories]\ngenerate_memories = true\nuse_memories = false\nmax_rollouts_per_startup = 20\ncustom = "keep"\n'
        settings = {"memories": {"generate_memories": False, "max_rollouts_per_startup": 0}}
        self.assertEqual(tomllib.loads(fleet.render_codex(current, {}, {}, {})), tomllib.loads(current))
        rendered = fleet.render_codex(current, settings, {}, {})
        self.assertEqual(tomllib.loads(rendered)["memories"], {
            "generate_memories": False, "use_memories": False, "max_rollouts_per_startup": 0, "custom": "keep",
        })
        self.assertEqual(fleet.render_codex(rendered, settings, {}, {}), rendered)

    def test_codex_agent_limit_validation(self):
        path = self.repo / "settings/codex.toml"
        for value in ('true', '0', '-1', '1.5', '"64"'):
            with self.subTest(value=value):
                path.write_text('[agents]\nmax_concurrent_threads_per_session = ' + value + '\n')
                with self.assertRaisesRegex(fleet.FleetError, "Invalid managed Codex agents"):
                    fleet.check_repository(self.repo)


    def test_context_and_shared_hooks_preserve_host_settings(self):
        current = '[features]\ncontext_management = false\ncustom = true\n'
        settings = {"features": {"context_management": True}}
        rendered = fleet.render_codex(current, settings, {}, {})
        self.assertEqual(tomllib.loads(rendered)["features"], {"context_management": True, "custom": True})
        self.assertEqual(fleet.render_codex(rendered, settings, {}, {}), rendered)
        common = {"SessionStart": [{"hooks": [{"type": "command", "command": "shared"}]}]}
        overlay = {"SessionStart": [{"hooks": [{"type": "command", "command": "host"}]}]}
        (self.repo / "settings/codex-hooks.json").write_text(json.dumps({"hooks": common}))
        (self.repo / "hosts/test-linux/codex-hooks.json").write_text(json.dumps({"hooks": overlay}))
        (self.repo / "settings/claude.json").write_text(json.dumps({"hooks": common}))
        (self.repo / "hosts/test-linux/claude.json").write_text(json.dumps({"hooks": overlay}))
        expected = {"SessionStart": common["SessionStart"] + overlay["SessionStart"]}
        self.assertEqual(fleet.load_codex_hooks(self.repo, "test-linux"), {"hooks": expected})
        self.assertEqual(fleet.load_settings(self.repo, "test-linux")[1]["hooks"], expected)

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
        self.assertEqual((self.home / ".local/bin/fleet-codex").readlink(), Path("../share/fleet/current/bin/fleet-client"))
        self.assertEqual((self.home / ".local/bin/fleet-claude").readlink(), Path("../share/fleet/current/bin/fleet-client"))
        self.assertEqual(result["managed_mcp"], [])
        self.assertEqual(result["plugins"], {"claude": [], "codex": []})

    def test_provider_models_and_account_routing_stay_unmanaged(self):
        self.apply()
        state = json.loads((self.home / ".local/state/fleet/last-applied.json").read_text())
        codex = self.home / ".codex/config.toml"
        codex.write_text(
            codex.read_text().replace('model = "keep"', 'model = "new"')
            + '\nmodel_reasoning_effort = "high"\n[model_providers.local]\nname = "Local"\n'
        )
        claude = self.home / ".claude/settings.json"
        value = json.loads(claude.read_text())
        value.update({"model": "new", "effortLevel": "high", "apiKeyHelper": "/bin/true"})
        value.setdefault("env", {})["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8317"
        claude.write_text(json.dumps(value))

        self.assertEqual(
            fleet.live_fingerprints(self.home, state["roots"], state["managed_mcp"]),
            state["fingerprints"],
        )

    def test_template_cannot_hardcode_provider_models(self):
        (self.repo / "settings/codex.toml").write_text('model = "pinned"\n')
        with self.assertRaisesRegex(fleet.FleetError, "Invalid managed Codex settings"):
            fleet.check_repository(self.repo)

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


    def test_plugin_client_finds_home_local_executable(self):
        binary = self.home / ".local" / "bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/bash\nexit 0\n")
        binary.chmod(0o755)

        try:
            found = fleet.plugin_client("codex", self.home)
        except fleet.FleetError as error:
            self.fail(f"Fleet did not find the client executable: {error}")

        self.assertEqual(found, binary)

    def test_plugin_commands_do_not_inherit_credentials(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "secret", "N8N_API_KEY": "secret", "SSH_AUTH_SOCK": "/secret/socket"},
        ):
            with mock.patch.object(subprocess, "run", return_value=completed) as invoked:
                fleet.run_plugin_command(["/usr/bin/true"], self.home)

        environment = invoked.call_args.kwargs["env"]
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("N8N_API_KEY", environment)
        self.assertNotIn("SSH_AUTH_SOCK", environment)

    def test_codex_old_marketplace_output_uses_checked_git_origin(self):
        marketplace = self.home / ".codex/.tmp/marketplaces/caveman"
        marketplace.mkdir(parents=True)
        run("git", "init", cwd=marketplace)
        run("git", "remote", "add", "origin", "https://github.com/JuliusBrussee/caveman.git", cwd=marketplace)

        def old_codex(command, home):
            output = {"marketplaces": [{"name": "caveman-repo", "root": str(marketplace)}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

        result = fleet.plugin_marketplaces("codex", Path("/clients/codex"), self.home, old_codex)

        self.assertEqual(result, {"caveman-repo": "github:juliusbrussee/caveman"})


    def test_recovery_marker_symlink_is_rejected_without_following(self):
        state = self.home / ".local" / "state" / "fleet"
        state.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-marker"
        outside.write_text('{"backup":"unsafe"}\n')
        (state / "in-progress.json").symlink_to(outside)
        with self.assertRaises(fleet.FleetError):
            fleet.recover_interrupted(self.home)
        self.assertEqual(outside.read_text(), '{"backup":"unsafe"}\n')

    def test_restore_preflight_rejects_targets_outside_managed_scope(self):
        document = self.home / "Documents" / "keep.txt"
        document.parent.mkdir()
        document.write_text("keep\n")
        backup = self.home / ".local" / "state" / "fleet" / "backups" / "20260821T120000000000Z-none-1234abcd"
        (backup / "blobs").mkdir(parents=True)
        (backup / "manifest.json").write_text(
            json.dumps({"version": 2, "state": "successful", "roots": ["Documents"], "entries": [], "created_parents": []})
        )
        with self.assertRaises(fleet.FleetError):
            fleet.restore_backup(self.home, backup)
        self.assertEqual(document.read_text(), "keep\n")

    def test_backup_preflight_rejects_dot_empty_and_parent_parts_before_mutation(self):
        target = self.home / ".codex" / "AGENTS.md"
        target.write_bytes(b"keep exactly\x00\xff")
        wanted = target.read_bytes()
        variants = [
            {"roots": [".codex//AGENTS.md"]},
            {"entries": [{"path": ".codex/./AGENTS.md", "mode": 0o600, "type": "file", "blob": "0", "size": 4, "sha256": hashlib.sha256(b"evil").hexdigest()}]},
            {"entries": [{"path": ".codex/AGENTS.md", "mode": 0o600, "type": "file", "blob": "../0", "size": 4, "sha256": hashlib.sha256(b"evil").hexdigest()}]},
        ]
        for index, changes in enumerate(variants):
            backup = self.home / ".local" / "state" / "fleet" / "backups" / f"20260821T12000000000{index}Z-none-1234abc{index}"
            (backup / "blobs").mkdir(parents=True)
            (backup / "blobs" / "0").write_bytes(b"evil")
            manifest = {
                "version": 2,
                "state": "successful",
                "roots": [".codex/AGENTS.md"],
                "entries": [{"path": ".codex/AGENTS.md", "mode": 0o600, "type": "file", "blob": "0", "size": 4, "sha256": hashlib.sha256(b"evil").hexdigest()}],
                "created_parents": [],
            }
            manifest.update(changes)
            (backup / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(fleet.FleetError):
                fleet.restore_backup(self.home, backup)
            self.assertEqual(target.read_bytes(), wanted)

    def test_symlinked_backup_parent_cannot_delete_managed_files(self):
        target = self.home / ".codex" / "AGENTS.md"
        target.write_bytes(b"keep\n")
        state = self.home / ".local" / "state" / "fleet"
        state.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-backups"
        backup = outside / "20260821T120000000000Z-none-1234abcd"
        (backup / "blobs").mkdir(parents=True)
        (backup / "manifest.json").write_text(json.dumps({"version": 2, "state": "successful", "roots": [".codex/AGENTS.md"], "entries": [], "created_parents": []}))
        (state / "backups").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(fleet.FleetError):
            fleet.restore_backup(self.home, state / "backups" / backup.name)
        self.assertEqual(target.read_bytes(), b"keep\n")

    def test_backup_restores_exact_directory_file_symlink_and_missing_types(self):
        directory = self.home / ".agents" / "skills" / "adhd"
        directory.mkdir(parents=True)
        child = directory / "SKILL.md"
        child.write_text("skill\n")
        directory.chmod(0o750)
        child.chmod(0o640)
        regular = self.home / ".codex" / "AGENTS.md"
        regular.write_text("rules\n")
        regular.chmod(0o600)
        link = self.home / ".claude" / "CLAUDE.md"
        link.symlink_to(regular)
        missing = ".codex/prompts/visual-plan.md"
        backup = fleet.create_backup(
            self.home,
            [".agents/skills/adhd", ".codex/AGENTS.md", ".claude/CLAUDE.md", missing],
            None,
        )
        fleet.mark_backup(self.home, backup, "successful")
        shutil.rmtree(directory)
        directory.write_text("wrong\n")
        regular.unlink()
        regular.mkdir()
        link.unlink()
        link.write_text("wrong\n")
        target_missing = self.home / missing
        target_missing.parent.mkdir(parents=True)
        target_missing.write_text("must disappear\n")

        fleet.restore_backup(self.home, backup)

        self.assertTrue(directory.is_dir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)
        self.assertEqual(child.read_text(), "skill\n")
        self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o640)
        self.assertTrue(regular.is_file())
        self.assertEqual(stat.S_IMODE(regular.stat().st_mode), 0o600)
        self.assertTrue(link.is_symlink())
        self.assertFalse(target_missing.exists())

if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
