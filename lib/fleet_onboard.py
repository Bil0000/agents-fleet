#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path


fleet_spec = importlib.util.spec_from_file_location("fleet_apply", Path(__file__).with_name("fleet_apply.py"))
fleet = importlib.util.module_from_spec(fleet_spec)
fleet_spec.loader.exec_module(fleet)


class FleetOnboardError(RuntimeError):
    pass


HOST_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ROLE = re.compile(r"[ -~]{1,80}")
PLUGIN_MANIFEST = {
    "codex": {
        "marketplaces": {"claude-mem-local": "thedotmack/claude-mem"},
        "plugins": ["claude-mem@claude-mem-local"],
    },
    "claude": {"marketplaces": {}, "plugins": []},
}


def add_host(root, host, os_name, role):
    root = Path(root)
    if not isinstance(host, str) or not HOST_ID.fullmatch(host):
        raise FleetOnboardError("Invalid Fleet host name.")
    if os_name not in {"macos", "linux"}:
        raise FleetOnboardError("Fleet host OS must be macos or linux.")
    if not isinstance(role, str) or not ROLE.fullmatch(role) or role != role.strip():
        raise FleetOnboardError("Invalid Fleet host role.")
    target = root / "hosts" / host
    try:
        target.mkdir()
    except FileExistsError as error:
        raise FleetOnboardError(f"Fleet host already exists: {host}") from error
    try:
        (target / "host.toml").write_text(f'id = "{host}"\nos = "{os_name}"\nrole = {json.dumps(role)}\n')
        (target / "plugins.json").write_text(json.dumps(PLUGIN_MANIFEST, indent=2) + "\n")
    except OSError:
        for path in target.iterdir():
            path.unlink()
        target.rmdir()
        raise


def check_prerequisites(home, shared, *, which=shutil.which, runner=subprocess.run):
    home = Path(home)
    search = os.pathsep.join(
        [
            str(home / ".local/bin"),
            str(home / ".bun/bin"),
            str(home / ".npm-global/bin"),
            os.environ.get("PATH", ""),
        ]
    )
    tools = ["bash", "git", "node", "bun", "claude", "codex"]
    if shared:
        tools.append("ssh")
    resolved = {name: which(name, path=search) for name in tools}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        raise FleetOnboardError(f"Fleet onboarding needs: {', '.join(missing)}")
    node_version = runner([resolved["node"], "--version"], cwd=home, capture_output=True, text=True)
    match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)\s*", node_version.stdout or "")
    if node_version.returncode or match is None or tuple(map(int, match.groups())) < (20, 12, 0):
        raise FleetOnboardError("Fleet onboarding needs Node.js 20.12 or newer.")


def read_host(root, host):
    if not isinstance(host, str) or not HOST_ID.fullmatch(host):
        raise FleetOnboardError("Invalid Fleet host name.")
    path = Path(root) / "hosts" / host / "host.toml"
    try:
        details = os.lstat(path)
        manifest = tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise FleetOnboardError(f"Invalid Fleet host: {host}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or manifest.get("id") != host:
        raise FleetOnboardError(f"Invalid Fleet host: {host}")
    return manifest


def onboard_host(
    root,
    host,
    memory_mode,
    *,
    platform_name=None,
    prerequisite_checker=check_prerequisites,
    runner=subprocess.run,
):
    root = Path(root).resolve()
    if memory_mode not in {"local", "shared"}:
        raise FleetOnboardError("Fleet memory mode must be local or shared.")
    profile = read_host(root, host)
    platform = sys.platform if platform_name is None else platform_name
    current_os = "macos" if platform == "darwin" else "linux" if platform.startswith("linux") else None
    if current_os is None:
        raise FleetOnboardError("Fleet onboarding supports only macOS and Linux.")
    if profile.get("os") != current_os:
        required = "macOS" if profile.get("os") == "macos" else "Linux"
        raise FleetOnboardError(f"Fleet host profile requires {required}.")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    prerequisite_checker(home, memory_mode == "shared")
    fleet = str(root / "bin/fleet")
    commands = [
        [fleet, "check"],
        [str(root / "tests/test_fleet.sh")],
        [fleet, "apply", "--host", host, "--dry-run"],
        [fleet, "apply", "--host", host],
        [fleet, "account", "setup"],
    ]
    if memory_mode == "shared":
        commands.append([fleet, "memory", "enable", "--host", host])
    commands.extend(
        [
            [fleet, "enable-updates"],
            [fleet, "update", "--host", host],
            [fleet, "status", "--host", host]
            if memory_mode == "shared"
            else [fleet, "apply", "--host", host, "--dry-run"],
            [fleet, "auth", "status"],
        ]
    )
    for command in commands:
        command_run = runner(command, cwd=root)
        if command_run.returncode:
            raise FleetOnboardError(f"Fleet onboarding failed: {' '.join(command[1:])}")


def parse_arguments(arguments):
    parser = argparse.ArgumentParser(prog="fleet")
    commands = parser.add_subparsers(dest="command", required=True)
    host = commands.add_parser("host").add_subparsers(dest="host_command", required=True).add_parser("add")
    host.add_argument("host")
    host.add_argument("--os", choices=("macos", "linux"), required=True)
    host.add_argument("--role", required=True)
    onboard = commands.add_parser("onboard")
    onboard.add_argument("--host", required=True)
    onboard.add_argument("--memory", choices=("local", "shared"), default="local")
    return parser.parse_args(arguments)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        raise FleetOnboardError("Missing Fleet root.")
    root = Path(arguments[0]).resolve()
    arguments = parse_arguments(arguments[1:])
    root = fleet.source_repository(root, Path(pwd.getpwuid(os.getuid()).pw_dir))
    if arguments.command == "host":
        add_host(root, arguments.host, arguments.os, arguments.role)
        print(f"created: hosts/{arguments.host}")
        return 0
    onboard_host(root, arguments.host, arguments.memory)
    print(f"onboarded: {arguments.host}")
    if arguments.memory == "local":
        print("memory: starts in a fresh Claude or Codex session")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetOnboardError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
