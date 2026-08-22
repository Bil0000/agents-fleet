#!/usr/bin/env python3
import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(sys.argv[1]).resolve()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_mcp", ROOT / "lib" / "fleet_mcp.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


class NativeFake:
    def __init__(self):
        self.calls = []
        self.mac = {}

    def __call__(self, arguments, stdin=None):
        self.calls.append((arguments, stdin))
        if arguments[0] == "/usr/bin/expect":
            name, secret = stdin.rstrip(b"\n").split(b"\n", 1)
            self.mac[name.decode()] = secret
            return subprocess.CompletedProcess(arguments, 0, b"", b"")
        if arguments[0] == "/usr/bin/security":
            name = arguments[arguments.index("-a") + 1]
            if name not in self.mac:
                return subprocess.CompletedProcess(arguments, 44, b"", b"")
            return subprocess.CompletedProcess(arguments, 0, self.mac[name], b"")
        if "encrypt" in arguments:
            return subprocess.CompletedProcess(arguments, 0, base64.b64encode(stdin.rstrip(b"\n")), b"")
        if "decrypt" in arguments:
            return subprocess.CompletedProcess(arguments, 0, base64.b64decode(stdin), b"")
        raise AssertionError(arguments)


fake = NativeFake()
secret = b"fleet-test-value-42"
fleet.credential_set("SERVICE_TOKEN", secret, platform="macos", runner=fake)
assert fleet.credential_get("SERVICE_TOKEN", platform="macos", runner=fake) == secret.decode()
assert all(secret.decode() not in argument for call, _ in fake.calls for argument in call)
assert "dev.agents-fleet" in fake.calls[0][0][2]
assert "dev.agents-fleet" in fake.calls[1][0]

with tempfile.TemporaryDirectory() as temporary:
    home = Path(temporary) / "home"
    home.mkdir()
    fleet.credential_set("SERVICE_TOKEN", secret, platform="linux", runner=fake, home=home)
    credential = home / ".config/fleet/credentials/SERVICE_TOKEN.cred"
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert secret not in credential.read_bytes()
    assert fleet.credential_get("SERVICE_TOKEN", platform="linux", runner=fake, home=home) == secret.decode()

    credential.unlink()
    credential.symlink_to(Path(temporary) / "elsewhere")
    try:
        fleet.credential_get("SERVICE_TOKEN", platform="linux", runner=fake, home=home)
    except fleet.FleetError:
        pass
    else:
        raise AssertionError("credential symlink was accepted")


def write_bundle(root, trusted):
    directory = root / "mcp"
    directory.mkdir()
    command = [str(trusted / "docs-mcp"), "serve"]
    wrapper = ["mcp", "run", "docs", "--", *command]
    (directory / "codex.toml").write_text(
        '[mcp_servers.docs]\ncommand = "fleet"\n'
        f"args = {json.dumps(wrapper)}\n"
    )
    (directory / "claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {"type": "stdio", "command": "fleet", "args": wrapper}
                }
            }
        )
    )
    (directory / "runners.toml").write_text(
        '[servers.docs]\nsecrets = ["DOCS_TOKEN"]\n'
        f"command = {json.dumps(command)}\n"
        f'cwd = "{trusted}"\n'
        '[servers.docs.env]\nDOCS_URL = "https://docs.example.com"\n'
    )
    (directory / "required-secrets.txt").write_text("DOCS_TOKEN\n")
    (directory / "README.md").write_text("MCP configuration.\n")
    (root / "hosts").mkdir()
    return command


captured = {}


def execute(command, arguments, environment):
    captured.update(command=command, arguments=arguments, environment=environment, cwd=Path.cwd())


with tempfile.TemporaryDirectory() as temporary:
    fixture = Path(temporary) / "repo"
    trusted = Path(temporary) / "trusted"
    fixture.mkdir()
    trusted.mkdir()
    command = write_bundle(fixture, trusted)
    fleet.validate_all(fixture)

    ambient = {
        "HOME": "/spoofed",
        "PATH": "/spoofed",
        "LANG": "en_US.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "must-not-pass",
    }
    previous = Path.cwd()
    os.chdir(fixture)
    try:
        fleet.mcp(
            fixture,
            ["run", "docs", "--", *command],
            host="test-local",
            credential_reader=lambda name: "docs-secret",
            executor=execute,
            ambient=ambient,
            home=Path("/fixed/home"),
        )
    finally:
        os.chdir(previous)

    assert captured["arguments"] == command
    assert captured["cwd"] == trusted.resolve()
    assert captured["environment"]["DOCS_TOKEN"] == "docs-secret"
    assert captured["environment"]["DOCS_URL"] == "https://docs.example.com"
    assert "AWS_SECRET_ACCESS_KEY" not in captured["environment"]

    for arguments in (["run", "docs"], ["run", "docs", "--", "wrong"]):
        try:
            fleet.mcp(
                fixture,
                arguments,
                host="test-local",
                credential_reader=lambda name: "docs-secret",
                executor=execute,
            )
        except fleet.FleetError:
            pass
        else:
            raise AssertionError("different MCP command was accepted")

    def missing(name):
        raise fleet.MissingCredential(name)

    try:
        fleet.mcp(
            fixture,
            ["run", "docs", "--", *command],
            host="test-local",
            credential_reader=missing,
            executor=execute,
        )
    except fleet.FleetError:
        pass
    else:
        raise AssertionError("missing MCP credential was accepted")

print("Fleet MCP test passed.")
