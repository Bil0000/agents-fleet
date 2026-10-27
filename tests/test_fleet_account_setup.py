#!/usr/bin/env python3
import base64
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parents[1]).resolve()
SPEC = importlib.util.spec_from_file_location("fleet_accounts_setup", ROOT / "lib" / "fleet_accounts.py")
ACCOUNTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCOUNTS)


class FleetAccountSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.environment = os.environ.copy()
        self.environment["FLEET_ACCOUNT_HOME"] = str(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def run_fleet(self, *arguments):
        return subprocess.run(
            [str(ROOT / "bin" / "fleet"), "account", *arguments],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def archive(self, version="7.2.149"):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            data = f"#!/bin/sh\necho 'CLIProxyAPI Version: {version}'\n".encode()
            member = tarfile.TarInfo("cli-proxy-api")
            member.mode = 0o755
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return output.getvalue()

    def linux_setup_args(self, native=None, archive=None, **changes):
        archive = self.archive() if archive is None else archive
        arguments = {
            "platform_name": "linux",
            "machine": "amd64",
            "downloader": lambda _url, target, _digest: target.write_bytes(archive),
            "native_runner": native or LinuxNative(self.home),
            "probe": lambda _home: None,
            "existing_proxy": lambda _: False,
        }
        arguments.update(changes)
        return arguments

    def hash_management_secret(self):
        paths = ACCOUNTS.setup_paths(self.home, "linux")
        secret = ACCOUNTS.managed_key(paths, "management_key")
        contents = paths["config"].read_bytes()
        hashed = "$2a$10$" + "A" * 53
        paths["config"].write_bytes(contents.replace(json.dumps(secret).encode(), json.dumps(hashed).encode()))

    def test_hub_setup_and_leave_only_publish_the_router(self):
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args())
        with mock.patch.object(ACCOUNTS.sys, "platform", "linux"), mock.patch.object(
            ACCOUNTS, "tailscale_identity", return_value=("hub.tail123abc.ts.net", "owner@example.com")
        ), mock.patch.object(ACCOUNTS, "restart_proxy_service"), mock.patch.object(
            ACCOUNTS, "wait_for_proxy"
        ), mock.patch.object(ACCOUNTS, "tailscale_serve") as serve:
            ACCOUNTS.hub_setup(self.home)
            self.assertEqual(ACCOUNTS.load_hub_state(self.home)["role"], "hub")
            serve.assert_called_once_with(True)
            ACCOUNTS.hub_leave(self.home)
            self.assertIsNone(ACCOUNTS.load_hub_state(self.home))
            self.assertEqual(serve.call_args_list, [mock.call(True), mock.call(False)])

    def test_setup_check_reports_missing_without_writing(self):
        before = sorted(path.relative_to(self.home) for path in self.home.rglob("*"))

        result = self.run_fleet("setup", "--check")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("not set up", result.stderr)
        self.assertEqual(sorted(path.relative_to(self.home) for path in self.home.rglob("*")), before)

    def test_shell_hook_preserves_the_startup_file_and_is_idempotent(self):
        startup = self.home / ".zshrc"
        startup.write_text("export KEEP=yes\n")

        ACCOUNTS.install_shell_hook(self.home, "/bin/zsh")
        first = startup.read_bytes()
        ACCOUNTS.install_shell_hook(self.home, "/bin/zsh")

        self.assertEqual(
            first,
            b'export KEEP=yes\n[ -r "$HOME/.local/share/fleet/current/bin/fleet-shell" ] && . "$HOME/.local/share/fleet/current/bin/fleet-shell"\n',
        )
        self.assertEqual(startup.read_bytes(), first)
        self.assertTrue(ACCOUNTS.check_shell_hook(self.home, "/bin/zsh"))

    def test_shell_hook_rejects_linked_startup_files(self):
        startup = self.home / ".bashrc"
        target = self.home / "target"
        target.write_text("keep\n")
        startup.symlink_to(target)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "startup file is unsafe"):
            ACCOUNTS.install_shell_hook(self.home, "/bin/bash")

        startup.unlink()
        os.link(target, startup)
        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "startup file is unsafe"):
            ACCOUNTS.install_shell_hook(self.home, "/bin/bash")

    def test_release_asset_matches_all_supported_hosts(self):
        cases = (
            ("darwin", "arm64", "CLIProxyAPI_7.2.149_darwin_aarch64.tar.gz", "90962c9194fe5470dc21f167b0cbf167a4f9ff2961a6bcc88f0b7eec32f1b49b"),
            ("darwin", "x86_64", "CLIProxyAPI_7.2.149_darwin_amd64.tar.gz", "382f800a4d82fe39ee7158ca4f735a1a71d635fe0f1d9a55a4c5d13993ccc04e"),
            ("linux", "aarch64", "CLIProxyAPI_7.2.149_linux_aarch64.tar.gz", "2d290477295eba4e419bc231f1fb5d548edbdd4cd5654b34d26ed12f8dcd0ee7"),
            ("linux", "amd64", "CLIProxyAPI_7.2.149_linux_amd64.tar.gz", "95d865dd17986da7d08cb39ffafe07d050669c5264d4d00115758ab4de752a72"),
        )
        for system, machine, name, digest in cases:
            with self.subTest(system=system, machine=machine):
                self.assertEqual(ACCOUNTS.release_asset(system, machine), (name, digest))

    def test_download_uses_macos_system_certificates_when_python_has_none(self):
        with mock.patch.object(ACCOUNTS.ssl, "get_default_verify_paths", return_value=mock.Mock(cafile=None)), mock.patch.object(
            ACCOUNTS.Path, "is_file", return_value=True
        ):
            self.assertEqual(ACCOUNTS.download_cafile(), "/etc/ssl/cert.pem")

    def test_managed_setup_accepts_management_response_with_hidden_local_settings(self):
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args())
        paths = ACCOUNTS.setup_paths(self.home, "linux")
        client_key = ACCOUNTS.managed_key(paths, "client_key")
        runtime = {
            "api-keys": [client_key],
            "request-retry": 0,
            "max-retry-credentials": 0,
            "routing": {"strategy": "fill-first"},
            "streaming": {},
            "codex": {"stream-bootstrap-buffering": True},
        }
        client = mock.Mock(url="http://127.0.0.1:8317")
        client.config.return_value = runtime
        client.accounts.return_value = []

        with mock.patch.object(ACCOUNTS, "ManagementClient", return_value=client), mock.patch.object(
            ACCOUNTS, "probe_inference_endpoint"
        ) as probe:
            ACCOUNTS.probe_setup(self.home)
            ACCOUNTS.runtime_for_mutation(self.home)

        probe.assert_called_once_with(client.url, client_key)
        self.assertIs(runtime["routing"]["session-affinity"], True)
        runtime["auth-dir"] = str(self.home / "other-auth")
        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "auth directory"):
            ACCOUNTS.managed_runtime_config(paths, runtime)

    def test_setup_installs_linux_service_and_is_idempotent(self):
        calls = []
        native = LinuxNative(self.home)
        archive = self.archive()

        def download(url, target, digest):
            calls.append((url, digest))
            target.write_bytes(archive)

        def probe(home):
            config = home / ".config/fleet/cliproxyapi.yaml"
            self.assertIn('host: "127.0.0.1"', config.read_text())

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="x86_64",
            downloader=download,
            native_runner=native,
            probe=probe,
            existing_proxy=lambda _: False,
        )

        binary = self.home / ".local/share/fleet/cliproxyapi/7.2.149/cli-proxy-api"
        bridge = self.home / ".local/share/fleet/cliproxyapi/7.2.149/fleet-retry-bridge"
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        management_key = self.home / ".local/state/fleet/cliproxyapi-management-key"
        service = self.home / ".config/systemd/user/fleet-cliproxyapi.service"
        self.assertTrue(binary.is_file())
        self.assertEqual(bridge.read_bytes(), (ROOT / "lib/fleet_retry_bridge.py").read_bytes())
        self.assertEqual(stat.S_IMODE(bridge.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(client_key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(management_key.stat().st_mode), 0o600)
        policy = self.home / ".local/state/fleet/account-routing.json"
        self.assertEqual(json.loads(policy.read_text()), {
            "version": 1,
            "providers": {"claude": "limits", "codex": "limits"},
        })
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o600)
        self.assertNotEqual(client_key.read_bytes(), management_key.read_bytes())
        config_text = config.read_text()
        self.assertIn("port: 8318", config_text)
        self.assertIn(f'api-keys:\n  - "{client_key.read_text().strip()}"', config_text)
        self.assertIn(f'secret-key: "{management_key.read_text().strip()}"', config_text)
        self.assertNotIn(f'api-keys:\n  - "{management_key.read_text().strip()}"', config_text)
        self.assertNotIn("claude-header-defaults:", config_text)
        self.assertIn("codex:\n  stream-bootstrap-buffering: true", config_text)
        self.assertIn("  disable-codex-cloaking: true", config_text)
        self.assertIn("provider-model-lists:\n  enabled: true", config_text)
        self.assertIn("oauth-request-scoped-errors:\n  claude:", config_text)
        self.assertIn('      - "rate_limit_error"', config_text)
        self.assertIn("  codex:", config_text)
        self.assertIn('      - "usage_limit_reached"\n        - "turn_rate_limit_exceeded"', config_text)
        configured = ACCOUNTS.configured_error_rules(ACCOUNTS.default_account_routing())
        for provider, status in (("claude", 529), ("codex", 404)):
            self.assertIn(
                {"status": status, "match-regexr": ["(?s).*"], "action": "stop"},
                configured[provider],
            )
        state_text = json.dumps(json.loads((self.home / ".local/state/fleet/cliproxyapi.json").read_text()))
        self.assertNotIn(client_key.read_text().strip(), state_text)
        self.assertNotIn(management_key.read_text().strip(), state_text)
        self.assertEqual(
            service.read_text(),
            "[Unit]\nDescription=Fleet CLIProxyAPI\n\n[Service]\n"
            "ExecStart=%h/.local/share/fleet/cliproxyapi/7.2.149/fleet-retry-bridge %h/.local/share/fleet/cliproxyapi/7.2.149/cli-proxy-api %h/.config/fleet/cliproxyapi.yaml\n"
            "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.149/CLIProxyAPI_7.2.149_linux_amd64.tar.gz")
        first_calls = list(native.calls)

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="x86_64",
            downloader=download,
            native_runner=native,
            probe=probe,
            existing_proxy=lambda _: False,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(native.calls[len(first_calls):], [
            ("/usr/bin/systemctl", "--user", "is-enabled", "fleet-cliproxyapi.service"),
            ("/usr/bin/systemctl", "--user", "is-active", "fleet-cliproxyapi.service"),
        ])


    def test_setup_rerun_keeps_an_explicit_any_policy(self):
        native = LinuxNative(self.home)
        arguments = self.linux_setup_args(native)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        policy = self.home / ".local/state/fleet/account-routing.json"
        policy.write_text(json.dumps({"version": 1, "providers": {"claude": "any", "codex": "limits"}}))
        policy.chmod(0o600)

        ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual(json.loads(policy.read_text())["providers"]["claude"], "any")
        config = (self.home / ".config/fleet/cliproxyapi.yaml").read_text()
        self.assertNotIn("  claude:\n", config.split("oauth-request-scoped-errors:", 1)[-1])
        self.assertIn("  codex:\n", config)

    def test_setup_upgrades_the_previous_managed_release_in_place(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        paths = ACCOUNTS.setup_paths(self.home, "linux")
        old_binary = self.home / ".local/share/fleet/cliproxyapi/7.2.130/cli-proxy-api"
        old_binary.parent.mkdir(parents=True)
        paths["binary"].replace(old_binary)
        paths["config"].write_text(paths["config"].read_text().replace("codex:\n  stream-bootstrap-buffering: true\n", ""))
        paths["service"].write_text(paths["service"].read_text().replace("7.2.149", "7.2.130"))
        paths["state"].write_text('{"version": "7.2.130"}\n')

        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))

        self.assertTrue(paths["binary"].is_file())
        self.assertTrue(old_binary.is_file())
        self.assertEqual(json.loads(paths["state"].read_text()), {"version": "7.2.149"})
        self.assertIn("7.2.149", paths["service"].read_text())
        self.assertIn("codex:\n  stream-bootstrap-buffering: true", paths["config"].read_text())

    def test_setup_accepts_service_hashed_management_secret(self):
        native = LinuxNative(self.home)
        arguments = self.linux_setup_args(native, probe=lambda _home: self.hash_management_secret())

        ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertTrue(
            ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )
        )

    def test_setup_cleans_up_after_service_hashes_secret_then_probe_fails(self):
        def probe(_home):
            self.hash_management_secret()
            raise ACCOUNTS.fleet.FleetError("probe failed")

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "^probe failed$"):
            ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(probe=probe))

        self.assertFalse((self.home / ".local/state/fleet/cliproxyapi.json").exists())
        self.assertFalse((self.home / ".config/fleet/cliproxyapi.yaml").exists())

    def test_setup_check_rejects_changed_config_without_writing(self):
        native = LinuxNative(self.home)
        archive = self.archive()

        def download(_url, target, _digest):
            target.write_bytes(archive)

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=download,
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        config.write_text(config.read_text().replace('host: "127.0.0.1"', 'host: "0.0.0.0"'))
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        error = StringIO()

        with redirect_stderr(error):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertFalse(ok)
        self.assertIn("config", error.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}, before)

    def test_setup_does_not_route_clients_before_an_account_is_enrolled(self):
        native = LinuxNative(self.home)
        archive = self.archive()

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=lambda _url, target, _digest: target.write_bytes(archive),
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )

        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertFalse((self.home / ".codex/config.toml").exists())

    def test_setup_command_refreshes_existing_client_routes(self):
        with (
            mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}),
            mock.patch.object(ACCOUNTS, "account_home", return_value=self.home),
            mock.patch.object(ACCOUNTS, "setup_proxy"),
            mock.patch.object(
                ACCOUNTS,
                "load_client_routing_state",
                return_value={"version": 1, "providers": {"claude": {}, "codex": {}}},
            ),
            mock.patch.object(ACCOUNTS, "configure_provider_routing") as configure,
            mock.patch.object(ACCOUNTS, "check_setup", return_value=True) as check,
        ):
            ACCOUNTS.main(["setup"])

        self.assertTrue(ACCOUNTS.check_shell_hook(self.home, "/bin/zsh"))
        check.assert_called_once_with(self.home, shell="/bin/zsh")
        self.assertEqual(
            configure.call_args_list,
            [
                mock.call(self.home, "claude", "http://127.0.0.1:8317"),
                mock.call(self.home, "codex", "http://127.0.0.1:8317"),
            ],
        )


    def test_provider_routing_preserves_shared_client_config(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        claude = self.home / ".claude/settings.json"
        codex = self.home / ".codex/config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text('{"env":{"KEEP":"yes"},"hooks":{"keep":true}}\n')
        codex.write_text('model = "provider-owned-future-model"\nmodel_reasoning_effort = "xhigh"\nservice_tier = "priority"\n\n[features]\nmemories = true\n')
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)

        claude_value = json.loads(claude.read_text())
        self.assertEqual(
            claude_value["env"],
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "CLAUDE_CODE_MAX_RETRIES": "3", "KEEP": "yes"},
        )
        self.assertEqual(claude_value["apiKeyHelper"], f"/bin/cat {client_key}")
        self.assertEqual(claude_value["hooks"], {"keep": True})
        codex_value = tomllib.loads(codex.read_text())
        self.assertEqual(codex_value["model"], "provider-owned-future-model")
        self.assertEqual(codex_value["model_reasoning_effort"], "xhigh")
        self.assertEqual(codex_value["service_tier"], "priority")
        self.assertTrue(codex_value["features"]["memories"])
        self.assertNotIn("model_provider", codex_value)
        self.assertNotIn("fleet_cliproxyapi", codex_value.get("model_providers", {}))
        self.assertEqual(codex_value["openai_base_url"], "http://127.0.0.1:8317/v1")

    def test_provider_routing_refreshes_legacy_claude_retries_and_restores_prior_config(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        prior = b'{"env":{"KEEP":"yes","CLAUDE_CODE_MAX_RETRIES":"7"}}\n'
        legacy = json.loads(ACCOUNTS.claude_routing_config(prior, "http://127.0.0.1:8317", client_key))
        legacy["env"]["CLAUDE_CODE_MAX_RETRIES"] = "100"
        written = json.dumps(legacy).encode()
        path.write_bytes(written)
        path.chmod(0o600)
        ACCOUNTS.save_client_routing_state(self.home, "claude", (prior, 0o640), written)

        self.assertFalse(ACCOUNTS.provider_routing_valid(self.home, "claude", "http://127.0.0.1:8317"))
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)

        self.assertEqual(json.loads(path.read_bytes())["env"]["CLAUDE_CODE_MAX_RETRIES"], "3")
        self.assertTrue(ACCOUNTS.provider_routing_valid(self.home, "claude", "http://127.0.0.1:8317"))
        ACCOUNTS.restore_client_routing(self.home)
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_provider_routing_can_refresh_existing_codex_route(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        original = b'model = "gpt-5.5"\n'
        config.write_bytes(original)

        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        ACCOUNTS.restore_client_routing(self.home)

        self.assertEqual(config.read_bytes(), original)

    def test_provider_routing_can_refresh_reformatted_codex_route(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model_provider = "old"\nmodel = "gpt-5.5"\n')
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        config.write_text(
            'openai_base_url = "http://127.0.0.1:8317/v1"\nmodel = "gpt-5.6"\n'
        )

        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        ACCOUNTS.restore_client_routing(self.home)

        value = tomllib.loads(config.read_text())
        self.assertEqual(value["model_provider"], "old")
        self.assertEqual(value["model"], "gpt-5.6")
        self.assertNotIn(ACCOUNTS.FLEET_PROVIDER, value.get("model_providers", {}))

    def test_restore_codex_routing_preserves_unrelated_changes(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('model_provider = "old"\nmodel = "gpt-5.5"\n')
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        config.write_text('model_reasoning_effort = "high"\n' + config.read_text())
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)

        ACCOUNTS.restore_client_routing(self.home)

        value = tomllib.loads(config.read_text())
        self.assertEqual(value["model_provider"], "old")
        self.assertEqual(value["model"], "gpt-5.5")
        self.assertEqual(value["model_reasoning_effort"], "high")
        self.assertNotIn("fleet_cliproxyapi", value.get("model_providers", {}))

    def test_restore_codex_routing_migrates_legacy_provider_tables(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        prior = b'model_provider = "old"\nmodel = "gpt-5.5"\n'
        legacy = (
            f"{ACCOUNTS.ROUTING_BEGIN}\n"
            f'model_provider = "{ACCOUNTS.FLEET_PROVIDER}"\n'
            f"{ACCOUNTS.ROUTING_END}\n\n"
            'model = "gpt-5.5"\n\n'
            f"[model_providers.{ACCOUNTS.FLEET_PROVIDER}]\n"
            'name = "Fleet CLIProxyAPI"\n'
            'base_url = "http://127.0.0.1:8317/v1"\n'
            'wire_api = "responses"\n'
            f"[model_providers.{ACCOUNTS.FLEET_PROVIDER}.auth]\n"
            'command = "/bin/cat"\n'
            'args = ["/private/client-key"]\n'
        ).encode()
        config.write_bytes(legacy.replace(b'model = "gpt-5.5"', b'model = "gpt-5.6"'))
        ACCOUNTS.save_client_routing_state(self.home, "codex", (prior, 0o600), legacy)

        ACCOUNTS.restore_client_routing(self.home)

        value = tomllib.loads(config.read_text())
        self.assertEqual(value["model_provider"], "old")
        self.assertEqual(value["model"], "gpt-5.6")
        self.assertNotIn("fleet_cliproxyapi", value.get("model_providers", {}))

    def test_provider_routing_persists_private_exact_restore_state(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        original = b'{"env":{"KEEP":"yes"},"hooks":{"keep":true}}\n'
        path.write_bytes(original)
        path.chmod(0o640)

        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)

        state_path = self.home / ".local/state/fleet/client-routing.json"
        state = json.loads(state_path.read_text())
        record = state["providers"]["claude"]
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(base64.b64decode(record["prior_bytes"]), original)
        self.assertEqual(record["prior_mode"], 0o640)
        self.assertEqual(base64.b64decode(record["written_bytes"]), path.read_bytes())
        self.assertEqual(record["written_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_restore_clients_restores_exact_prior_files_and_modes(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir()
        claude_prior = b'{"apiKeyHelper":"old-helper","env":{"ANTHROPIC_BASE_URL":"https://old.example","KEEP":"yes"}}\n'
        claude.write_bytes(claude_prior)
        claude.chmod(0o640)
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir()
        codex_prior = b'model = "gpt-5.5"\n\n[features]\nmemories = true\n'
        codex.write_bytes(codex_prior)
        codex.chmod(0o640)
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)

        ACCOUNTS.restore_client_routing(self.home)

        self.assertEqual(claude.read_bytes(), claude_prior)
        self.assertEqual(stat.S_IMODE(claude.stat().st_mode), 0o640)
        self.assertEqual(codex.read_bytes(), codex_prior)
        self.assertEqual(stat.S_IMODE(codex.stat().st_mode), 0o640)
        self.assertFalse((self.home / ".local/state/fleet/client-routing.json").exists())

    def test_restore_clients_preserves_unrelated_concurrent_changes(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        claude = self.home / ".claude/settings.json"
        codex = self.home / ".codex/config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text('{"env":{"KEEP":"yes"}}\n')
        codex.write_text('model_provider = "old_provider"\nmodel = "gpt-5.5"\n')
        codex_original = codex.read_bytes()
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        claude_value = json.loads(claude.read_text())
        claude_value["env"]["NEW"] = "yes"
        claude_value["hooks"] = {"keep": True}
        claude.write_text(json.dumps(claude_value) + "\n")

        ACCOUNTS.restore_client_routing(self.home)

        claude_value = json.loads(claude.read_text())
        self.assertEqual(claude_value["env"], {"KEEP": "yes", "NEW": "yes"})
        self.assertEqual(claude_value["hooks"], {"keep": True})
        self.assertNotIn("apiKeyHelper", claude_value)
        self.assertEqual(codex.read_bytes(), codex_original)

    def test_setup_restore_clients_command_restores_routing_only(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir()
        prior = b'{"env":{"KEEP":"yes"}}\n'
        claude.write_bytes(prior)
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        auth = self.home / ".cli-proxy-api/claude-work.json"
        auth.parent.mkdir(mode=0o700)
        auth.write_bytes(b"opaque account")

        result = self.run_fleet("setup", "--restore-clients")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(claude.read_bytes(), prior)
        self.assertEqual(auth.read_bytes(), b"opaque account")
        self.assertIn("client routing restored", result.stdout.lower())

    def test_restore_clients_fails_before_mutation_when_managed_field_changed(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir()
        claude.write_text("{}\n")
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        claude_value = json.loads(claude.read_text())
        claude_value["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
        claude.write_text(json.dumps(claude_value) + "\n")
        before = {
            claude: claude.read_bytes(),
            ACCOUNTS.client_routing_state_path(self.home): ACCOUNTS.client_routing_state_path(self.home).read_bytes(),
        }

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Claude client routing changed"):
            ACCOUNTS.restore_client_routing(self.home)

        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_provider_routing_rollback_does_not_overwrite_a_concurrent_change(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        original = b'{"env":{"KEEP":"yes"}}\n'
        concurrent = b'{"env":{"KEEP":"concurrent"}}\n'
        path.write_bytes(original)

        def fail_after_change(_url):
            path.write_bytes(concurrent)
            raise ACCOUNTS.fleet.FleetError("smoke failed")

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "restore also failed"):
            ACCOUNTS.configure_provider_routing(
                self.home,
                "claude",
                "http://127.0.0.1:8317",
                probe=fail_after_change,
            )

        self.assertEqual(path.read_bytes(), concurrent)
        self.assertFalse(ACCOUNTS.client_routing_state_path(self.home).exists())

    def test_provider_routing_rollback_does_not_delete_a_concurrent_file(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        concurrent = b'{"concurrent":true}\n'

        def fail_after_creation(_url):
            path.unlink()
            path.write_bytes(concurrent)
            raise ACCOUNTS.fleet.FleetError("smoke failed")

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "restore also failed"):
            ACCOUNTS.configure_provider_routing(
                self.home,
                "claude",
                "http://127.0.0.1:8317",
                probe=fail_after_creation,
            )

        self.assertEqual(path.read_bytes(), concurrent)
        self.assertFalse(ACCOUNTS.client_routing_state_path(self.home).exists())

    def test_provider_routing_rollback_does_not_overwrite_a_mode_change(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"

        def fail_after_mode_change(_url):
            path.chmod(0o640)
            raise ACCOUNTS.fleet.FleetError("smoke failed")

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "restore also failed"):
            ACCOUNTS.configure_provider_routing(
                self.home,
                "claude",
                "http://127.0.0.1:8317",
                probe=fail_after_mode_change,
            )

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_provider_routing_reconfigure_rejects_managed_field_drift(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        path.write_text("{}\n")
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        value = json.loads(path.read_text())
        value["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
        path.write_text(json.dumps(value) + "\n")
        before = path.read_bytes()
        state_before = ACCOUNTS.client_routing_state_path(self.home).read_bytes()

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Claude client routing changed"):
            ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ACCOUNTS.client_routing_state_path(self.home).read_bytes(), state_before)

    def test_provider_routing_rejects_an_unsafe_client_key_before_writing(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o644)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "client key"):
            ACCOUNTS.configure_provider_routing(
                self.home,
                "claude",
                "http://127.0.0.1:8317",
                probe=lambda _url: None,
            )

        self.assertFalse((self.home / ".claude/settings.json").exists())

    def test_provider_routing_rolls_back_client_config_when_smoke_fails(self):
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key\n")
        client_key.chmod(0o600)
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        original = b'{"env":{"KEEP":"yes"}}\n'
        path.write_bytes(original)
        path.chmod(0o640)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "smoke"):
            ACCOUNTS.configure_provider_routing(
                self.home,
                "claude",
                "http://127.0.0.1:8317",
                probe=lambda _url: (_ for _ in ()).throw(ACCOUNTS.fleet.FleetError("smoke failed")),
            )

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_setup_check_reports_next_steps_for_unenrolled_providers(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=lambda _url, target, _digest: target.write_bytes(archive),
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )
        output = StringIO()

        with mock.patch("sys.stdout", output):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertTrue(ok)
        self.assertIn("claude auth login", output.getvalue())
        self.assertIn("codex login", output.getvalue())
        self.assertIn("Claude is not routed", output.getvalue())
        self.assertIn("Codex is not routed", output.getvalue())

    def test_setup_check_requires_router_for_newer_managed_proxy(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        old = self.home / ".local/share/fleet/cliproxyapi/7.2.149"
        current = old.with_name("7.2.151")
        old.rename(current)
        (current / "cli-proxy-api").write_text("#!/bin/sh\necho 'CLIProxyAPI Version: 7.2.151'\n")
        (current / "cli-proxy-api").chmod(0o700)
        state = ACCOUNTS.setup_state_path(self.home)
        state.write_text('{"version":"7.2.151"}\n')
        service = self.home / ".config/systemd/user/fleet-cliproxyapi.service"
        service.write_text(service.read_text().replace("7.2.149", "7.2.151"))

        with redirect_stderr(StringIO()):
            self.assertFalse(ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            ))

        paths = ACCOUNTS.setup_paths(self.home, "linux", "7.2.151")
        config = paths["config"]
        plugin_config = (
            "plugins:\n"
            "  enabled: true\n"
            f'  dir: "{current}/plugins"\n'
            "  configs:\n"
            "    fleet-provider-router:\n"
            "      enabled: true\n"
            "      priority: 100\n"
        ).encode()
        config.write_bytes(config.read_bytes().replace(b"api-keys:\n", plugin_config + b"api-keys:\n"))
        plugin = current / "plugins/fleet-provider-router.so"
        plugin.parent.mkdir()
        plugin.write_bytes(b"plugin")
        plugin.chmod(0o700)

        self.assertTrue(ACCOUNTS.check_setup(
            self.home,
            platform_name="linux",
            machine="amd64",
            native_runner=native,
            probe=lambda _home: None,
        ))

    def test_setup_does_not_downgrade_newer_managed_proxy(self):
        state = ACCOUNTS.setup_state_path(self.home)
        state.parent.mkdir(parents=True)
        state.write_text('{"version":"7.2.151"}\n')
        state.chmod(0o600)
        downloader = mock.Mock()

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=downloader,
        )

        downloader.assert_not_called()

    def test_setup_check_accepts_enrolled_codex_with_client_routing(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        credential = self.home / ".cli-proxy-api/codex-work.json"
        credential.write_bytes(b"opaque")
        credential.chmod(0o600)
        state = ACCOUNTS.default_state()
        state["providers"]["codex"].update({
            "accounts": {"codex-work.json": "work"},
            "primary": "codex-work.json",
            "auto": False,
        })
        ACCOUNTS.write_state(self.home, state)
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)
        output = StringIO()

        with mock.patch("sys.stdout", output):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertTrue(ok)
        self.assertNotIn("Codex is enrolled but not routed", output.getvalue())
        self.assertTrue((self.home / ".codex/config.toml").exists())
        self.assertTrue(ACCOUNTS.client_routing_state_path(self.home).exists())

    def test_setup_check_accepts_managed_routing_after_the_last_logout(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        ACCOUNTS.configure_provider_routing(self.home, "codex", "http://127.0.0.1:8317", probe=lambda _url: None)

        output = StringIO()
        with mock.patch("sys.stdout", output):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertTrue(ok)
        self.assertIn("Codex has no logged-in accounts", output.getvalue())

    def test_setup_check_rejects_stale_codex_fleet_routing_without_restore_state(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text(
            'model_provider = "fleet_cliproxyapi"\n\n'
            '[model_providers.fleet_cliproxyapi]\n'
            'base_url = "http://127.0.0.1:9999/v1"\n'
        )
        error = StringIO()

        with redirect_stderr(error):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertFalse(ok)
        self.assertIn("Codex client routing is present", error.getvalue())

    def test_setup_check_rejects_bad_auth_permissions_and_client_routing(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=lambda _url, target, _digest: target.write_bytes(archive),
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )
        auth = self.home / ".cli-proxy-api"
        credential = auth / "claude-work.json"
        credential.write_bytes(b"opaque")
        credential.chmod(0o644)
        state = ACCOUNTS.default_state()
        state["providers"]["claude"].update({
            "accounts": {"claude-work.json": "work"},
            "primary": "claude-work.json",
            "auto": False,
        })
        ACCOUNTS.write_state(self.home, state)

        with redirect_stderr(StringIO()):
            self.assertFalse(ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            ))

        credential.chmod(0o600)
        with redirect_stderr(StringIO()):
            self.assertFalse(ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            ))

        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        self.assertTrue(ACCOUNTS.check_setup(
            self.home,
            platform_name="linux",
            machine="amd64",
            native_runner=native,
            probe=lambda _home: None,
        ))

    def test_setup_check_requires_current_private_restore_record(self):
        native = LinuxNative(self.home)
        ACCOUNTS.setup_proxy(self.home, **self.linux_setup_args(native))
        credential = self.home / ".cli-proxy-api/claude-work.json"
        credential.write_bytes(b"opaque")
        credential.chmod(0o600)
        state = ACCOUNTS.default_state()
        state["providers"]["claude"].update({
            "accounts": {"claude-work.json": "work"},
            "primary": "claude-work.json",
            "auto": False,
        })
        ACCOUNTS.write_state(self.home, state)
        ACCOUNTS.configure_provider_routing(self.home, "claude", "http://127.0.0.1:8317", probe=lambda _url: None)
        restore = ACCOUNTS.client_routing_state_path(self.home)
        good = restore.read_bytes()
        self.assertTrue(ACCOUNTS.check_setup(
            self.home,
            platform_name="linux",
            machine="amd64",
            native_runner=native,
            probe=lambda _home: None,
        ))

        def missing():
            restore.unlink()

        def public_mode():
            restore.chmod(0o644)

        def corrupt_hash():
            value = json.loads(good)
            value["providers"]["claude"]["written_sha256"] = "0" * 64
            restore.write_text(json.dumps(value))
            restore.chmod(0o600)

        def stale_record():
            value = json.loads(good)
            written = json.loads(base64.b64decode(value["providers"]["claude"]["written_bytes"]))
            written["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
            raw = (json.dumps(written, indent=2, sort_keys=True) + "\n").encode()
            value["providers"]["claude"]["written_bytes"] = base64.b64encode(raw).decode()
            value["providers"]["claude"]["written_sha256"] = hashlib.sha256(raw).hexdigest()
            restore.write_text(json.dumps(value))
            restore.chmod(0o600)

        for mutate in (missing, public_mode, corrupt_hash, stale_record):
            with self.subTest(mutate=mutate.__name__):
                restore.parent.mkdir(parents=True, exist_ok=True)
                restore.write_bytes(good)
                restore.chmod(0o600)
                mutate()
                with redirect_stderr(StringIO()):
                    self.assertFalse(ACCOUNTS.check_setup(
                        self.home,
                        platform_name="linux",
                        machine="amd64",
                        native_runner=native,
                        probe=lambda _home: None,
                    ))

    def test_setup_failure_restores_existing_auth_and_removes_new_files(self):
        auth = self.home / ".cli-proxy-api"
        auth.mkdir(mode=0o700)
        credential = auth / "codex-existing.json"
        credential.write_bytes(b"opaque credential")
        native = LinuxNative(self.home)
        native.fail_action = "enable"
        archive = self.archive()

        def download(_url, target, _digest):
            target.write_bytes(archive)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "start"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=download,
                native_runner=native,
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertEqual(credential.read_bytes(), b"opaque credential")
        self.assertEqual(
            sorted(path.relative_to(self.home) for path in self.home.rglob("*") if path.is_file()),
            [Path(".cli-proxy-api/codex-existing.json")],
        )

    def test_setup_cleanup_preserves_a_concurrently_changed_managed_file(self):
        native = LinuxNative(self.home)
        native.fail_action = "enable"
        arguments = self.linux_setup_args(native)

        def change_config_then_fail(command, **kwargs):
            if tuple(command)[2:4] == ("enable", "--now"):
                config = self.home / ".config/fleet/cliproxyapi.yaml"
                config.write_bytes(b"concurrent config\n")
            return native(command, **kwargs)

        arguments["native_runner"] = change_config_then_fail

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Cleanup also failed"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual((self.home / ".config/fleet/cliproxyapi.yaml").read_bytes(), b"concurrent config\n")

    def test_setup_cleanup_preserves_a_concurrently_created_auth_file(self):
        native = LinuxNative(self.home)
        native.fail_action = "enable"
        arguments = self.linux_setup_args(native)

        def add_auth_then_fail(command, **kwargs):
            if tuple(command)[2:4] == ("enable", "--now"):
                (self.home / ".cli-proxy-api/concurrent.json").write_bytes(b"keep")
            return native(command, **kwargs)

        arguments["native_runner"] = add_auth_then_fail

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Cleanup also failed"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual((self.home / ".cli-proxy-api/concurrent.json").read_bytes(), b"keep")

    def test_managed_config_change_is_backed_up_and_restored_on_restart_failure(self):
        native = LinuxNative(self.home)
        archive = self.archive()

        def download(_url, target, _digest):
            target.write_bytes(archive)

        arguments = self.linux_setup_args(native, archive, downloader=download)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        changed = config.read_bytes().replace(b'host: "127.0.0.1"', b'host: "0.0.0.0"')
        config.write_bytes(changed)
        native.fail_once_action = "restart"

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "restart"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual(config.read_bytes(), changed)
        self.assertFalse((self.home / ".local/state/fleet/cliproxyapi-config.backup").exists())
        self.assertTrue(native.active)

    def test_setup_rerun_repairs_binary_config_and_service_drift(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        downloads = []

        def download(_url, target, _digest):
            downloads.append(target)
            target.write_bytes(archive)

        arguments = self.linux_setup_args(native, archive, downloader=download)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        paths = ACCOUNTS.setup_paths(self.home, "linux")
        paths["binary"].write_bytes(b"broken binary")
        paths["config"].write_bytes(b"broken config")
        paths["service"].write_bytes(b"broken service")

        ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual(len(downloads), 2)
        self.assertEqual(
            paths["config"].read_bytes(),
            ACCOUNTS.setup_config(
                paths["auth"],
                ACCOUNTS.managed_key(paths, "client_key"),
                ACCOUNTS.managed_key(paths, "management_key"),
            ),
        )
        self.assertEqual(paths["service"].read_bytes(), ACCOUNTS.setup_service(self.home, "linux"))
        ACCOUNTS.verify_setup_binary(self.home, paths["binary"])

    def test_setup_failure_keeps_files_when_service_cannot_be_stopped(self):
        native = LinuxNative(self.home)
        native.fail_enable_active = True
        native.fail_action = "disable"
        archive = self.archive()

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "Cleanup also failed"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=lambda _url, target, _digest: target.write_bytes(archive),
                native_runner=native,
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        paths = ACCOUNTS.setup_paths(self.home, "linux")
        self.assertTrue(native.active)
        self.assertTrue(paths["binary"].exists())
        self.assertTrue(paths["config"].exists())
        self.assertTrue(paths["service"].exists())
        self.assertTrue(paths["auth"].is_dir())

    def test_setup_refuses_active_unmanaged_fleet_service_before_mutation(self):
        native = LinuxNative(self.home)
        native.enabled = True
        native.active = True

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "user service"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=lambda *_: self.fail("download must not start"),
                native_runner=native,
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertEqual(list(self.home.rglob("*")), [])

    def test_setup_installs_macos_launch_agent(self):
        native = MacNative()
        archive = self.archive()

        def download(_url, target, _digest):
            target.write_bytes(archive)

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="darwin",
            machine="arm64",
            downloader=download,
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )

        service = self.home / "Library/LaunchAgents/com.fleet.cliproxyapi.plist"
        value = plistlib.loads(service.read_bytes())
        self.assertEqual(value["Label"], "com.fleet.cliproxyapi")
        self.assertEqual(value["ProgramArguments"], [
            str(self.home / ".local/share/fleet/cliproxyapi/7.2.149/fleet-retry-bridge"),
            str(self.home / ".local/share/fleet/cliproxyapi/7.2.149/cli-proxy-api"),
            str(self.home / ".config/fleet/cliproxyapi.yaml"),
        ])
        self.assertIs(value["KeepAlive"], True)
        self.assertIs(value["RunAtLoad"], True)
        self.assertEqual(stat.S_IMODE((self.home / ".local/state/fleet/logs").stat().st_mode), 0o700)
        self.assertIn(("/usr/bin/plutil", "-lint"), [call[0:2] for call in native.calls])
        self.assertIn(("/bin/launchctl", "bootstrap"), [call[0:2] for call in native.calls])

    def test_setup_rejects_symlink_and_hard_link_log_paths_before_download(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                home = Path(self.temporary.name) / kind
                home.mkdir()
                logs = home / ".local/state/fleet/logs"
                logs.parent.mkdir(parents=True)
                outside = home / "outside"
                if kind == "symlink":
                    outside.mkdir(mode=0o755)
                    logs.symlink_to(outside, target_is_directory=True)
                else:
                    outside.write_bytes(b"keep")
                    os.link(outside, logs)

                with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "log directory") as error:
                    ACCOUNTS.setup_proxy(
                        home,
                        platform_name="darwin",
                        machine="arm64",
                        downloader=lambda *_: self.fail("download must not start"),
                        native_runner=MacNative(),
                        probe=lambda _home: None,
                        existing_proxy=lambda _: False,
                    )

                self.assertNotIn("Cleanup also failed", str(error.exception))
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755 if kind == "symlink" else 0o644)

    def test_setup_rejects_unsafe_or_unowned_log_directory_on_linux(self):
        logs = self.home / ".local/state/fleet/logs"
        logs.mkdir(parents=True, mode=0o777)
        logs.chmod(0o777)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "log directory"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=lambda *_: self.fail("download must not start"),
                native_runner=LinuxNative(self.home),
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o777)

        logs.chmod(0o700)
        with mock.patch.object(ACCOUNTS.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "log directory"):
                ACCOUNTS.setup_proxy(
                    self.home,
                    platform_name="linux",
                    machine="amd64",
                    downloader=lambda *_: self.fail("download must not start"),
                    native_runner=LinuxNative(self.home),
                    probe=lambda _home: None,
                    existing_proxy=lambda _: False,
                )

    def test_setup_rerun_rejects_log_directory_drift(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        arguments = self.linux_setup_args(native, archive)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        logs = self.home / ".local/state/fleet/logs"
        logs.chmod(0o777)
        calls = list(native.calls)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "log directory"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o777)
        self.assertEqual(native.calls[len(calls):], [])

    def test_setup_failure_removes_log_directory_it_created(self):
        native = LinuxNative(self.home)
        native.fail_action = "enable"
        archive = self.archive()

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "start"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=lambda _url, target, _digest: target.write_bytes(archive),
                native_runner=native,
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertFalse((self.home / ".local/state/fleet/logs").exists())

    def test_setup_refuses_an_unmanaged_config_without_changing_it(self):
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"keep this config exactly\n")

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "not managed"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=lambda *_: self.fail("download must not start"),
                native_runner=LinuxNative(self.home),
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertEqual(config.read_bytes(), b"keep this config exactly\n")
        self.assertEqual(
            sorted(path.relative_to(self.home) for path in self.home.rglob("*") if path.is_file()),
            [Path(".config/fleet/cliproxyapi.yaml")],
        )

    def test_setup_detects_common_user_local_proxy_paths(self):
        with mock.patch.object(ACCOUNTS.shutil, "which", return_value=None), mock.patch.object(
            ACCOUNTS.Path,
            "exists",
            autospec=True,
            side_effect=lambda path: os.path.lexists(path) if self.home in path.parents else False,
        ):
            for relative in (
                ".local/bin/cliproxyapi",
                ".config/cliproxyapi/config.yaml",
                ".config/systemd/user/cliproxyapi.service",
            ):
                with self.subTest(relative=relative):
                    path = self.home / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                    self.assertTrue(ACCOUNTS.existing_unmanaged_proxy(self.home))
                    path.unlink()

    def test_setup_check_rejects_a_hard_linked_management_secret(self):
        native = LinuxNative(self.home)
        archive = self.archive()

        def download(_url, target, _digest):
            target.write_bytes(archive)

        ACCOUNTS.setup_proxy(
            self.home,
            platform_name="linux",
            machine="amd64",
            downloader=download,
            native_runner=native,
            probe=lambda _home: None,
            existing_proxy=lambda _: False,
        )
        secret = self.home / ".local/state/fleet/cliproxyapi-management-key"
        os.link(secret, self.home / "leaked-key")

        with redirect_stderr(StringIO()):
            ok = ACCOUNTS.check_setup(
                self.home,
                platform_name="linux",
                machine="amd64",
                native_runner=native,
                probe=lambda _home: None,
            )

        self.assertFalse(ok)

    def test_setup_rejects_a_wrong_binary_version_before_start(self):
        native = LinuxNative(self.home)
        archive = self.archive("7.2.129")

        def download(_url, target, _digest):
            target.write_bytes(archive)

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "7.2.149"):
            ACCOUNTS.setup_proxy(
                self.home,
                platform_name="linux",
                machine="amd64",
                downloader=download,
                native_runner=native,
                probe=lambda _home: None,
                existing_proxy=lambda _: False,
            )

        self.assertEqual(native.calls, [
            ("/usr/bin/systemctl", "--user", "is-enabled", "fleet-cliproxyapi.service"),
            ("/usr/bin/systemctl", "--user", "is-active", "fleet-cliproxyapi.service"),
        ])
        self.assertFalse((self.home / ".local/state/fleet/cliproxyapi.json").exists())
        self.assertFalse((self.home / ".local/share/fleet/cliproxyapi/7.2.149/cli-proxy-api").exists())

    def test_no_drift_probe_failure_restores_enabled_but_inactive_service(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        arguments = self.linux_setup_args(native, archive)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        native.enabled = True
        native.active = False
        arguments["probe"] = lambda _home: (_ for _ in ()).throw(ACCOUNTS.fleet.FleetError("probe failed"))

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "probe failed"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertTrue(native.enabled)
        self.assertFalse(native.active)

    def test_drift_failure_restores_enabled_but_inactive_service(self):
        native = LinuxNative(self.home)
        archive = self.archive()
        arguments = self.linux_setup_args(native, archive)
        ACCOUNTS.setup_proxy(self.home, **arguments)
        config = self.home / ".config/fleet/cliproxyapi.yaml"
        changed = config.read_bytes().replace(b'host: "127.0.0.1"', b'host: "0.0.0.0"')
        config.write_bytes(changed)
        native.enabled = True
        native.active = False
        arguments["probe"] = lambda _home: (_ for _ in ()).throw(ACCOUNTS.fleet.FleetError("probe failed"))

        with self.assertRaisesRegex(ACCOUNTS.fleet.FleetError, "probe failed"):
            ACCOUNTS.setup_proxy(self.home, **arguments)

        self.assertEqual(config.read_bytes(), changed)
        self.assertTrue(native.enabled)
        self.assertFalse(native.active)


class MacNative:
    def __init__(self):
        self.calls = []
        self.active = False

    def __call__(self, command, **_kwargs):
        command = tuple(command)
        self.calls.append(command)
        action = command[1]
        if action == "bootstrap":
            self.active = True
        if action == "bootout":
            self.active = False
        if action == "print":
            return subprocess.CompletedProcess(command, 0 if self.active else 113, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class LinuxNative:
    def __init__(self, home):
        self.home = home
        self.calls = []
        self.enabled = False
        self.active = False
        self.fail_action = None
        self.fail_once_action = None
        self.fail_enable_active = False

    def __call__(self, command, **kwargs):
        command = tuple(command)
        self.calls.append(command)
        action = command[2]
        if action == "is-enabled":
            return subprocess.CompletedProcess(command, 0 if self.enabled else 1, "", "")
        if action == "is-active":
            return subprocess.CompletedProcess(command, 0 if self.active else 3, "", "")
        if action == "enable":
            if self.fail_enable_active:
                self.enabled = True
                self.active = True
                return subprocess.CompletedProcess(command, 23, "", "private failure")
            if self.fail_action == action:
                return subprocess.CompletedProcess(command, 23, "", "private failure")
            self.enabled = True
            self.active = "--now" in command
        if action == "restart":
            if self.fail_once_action == action:
                self.fail_once_action = None
                return subprocess.CompletedProcess(command, 23, "", "private failure")
            self.active = True
        if action == "start":
            self.active = True
        if action == "stop":
            self.active = False
        if action == "disable":
            if self.fail_action == action:
                return subprocess.CompletedProcess(command, 23, "", "private failure")
            self.enabled = False
            self.active = False
        return subprocess.CompletedProcess(command, 0, "", "")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
