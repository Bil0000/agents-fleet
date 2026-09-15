#!/usr/bin/env python3
import json
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


RELEASE_URL = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
REPOSITORY = "https://github.com/router-for-me/CLIProxyAPI.git"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
LABEL = "com.fleet.cliproxyapi"
UPDATE_LABEL = "com.fleet.cliproxyapi.update"


def version_parts(value):
    if not VERSION.fullmatch(value):
        raise ValueError(f"Invalid CLIProxyAPI version: {value}")
    return tuple(int(part) for part in value.split("."))


def is_newer(candidate, current):
    return version_parts(candidate) > version_parts(current)


def command_run(command, check=True, **options):
    return subprocess.run(command, check=check, **options)


def atomic_write(path, contents, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def service_command(home, action):
    domain = f"gui/{os.getuid()}"
    if action == "bootout":
        return ["/bin/launchctl", "bootout", f"{domain}/{LABEL}"]
    return ["/bin/launchctl", "bootstrap", domain, str(home / "Library/LaunchAgents/com.fleet.cliproxyapi.plist")]


def probe_proxy(home):
    client_key = (home / ".local/state/fleet/cliproxyapi-client-key").read_text().strip()
    management_key = (home / ".local/state/fleet/cliproxyapi-management-key").read_text().strip()
    deadline = time.monotonic() + 15
    last_error = None
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request("http://127.0.0.1:8317/v1/models", headers={"Authorization": f"Bearer {client_key}"})
            with urllib.request.urlopen(request, timeout=2) as response:
                models = json.load(response)
            request = urllib.request.Request(
                "http://127.0.0.1:8318/v0/management/plugins",
                headers={"Authorization": f"Bearer {management_key}"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                plugins = json.load(response).get("plugins", [])
            if not isinstance(models.get("data"), list) or not any(
                plugin.get("id") == "fleet-provider-router" and plugin.get("registered") for plugin in plugins
            ):
                raise RuntimeError("CLIProxyAPI provider router is not ready.")
            return
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"CLIProxyAPI health check failed: {last_error}")


def activate(home, current, version, candidate, run=command_run, probe=probe_proxy, settle=lambda: time.sleep(1)):
    version_parts(current)
    version_parts(version)
    base = home / ".local/share/fleet/cliproxyapi"
    target = base / version
    config = home / ".config/fleet/cliproxyapi.yaml"
    service = home / "Library/LaunchAgents/com.fleet.cliproxyapi.plist"
    state = home / ".local/state/fleet/cliproxyapi.json"
    if target.exists():
        raise RuntimeError(f"CLIProxyAPI candidate already exists: {target}")
    old = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in (config, service, state)}
    old_token = f"/cliproxyapi/{current}/".encode()
    new_token = f"/cliproxyapi/{version}/".encode()
    replacements = {}
    for path in (config, service):
        contents = old[path][0].replace(old_token, new_token)
        if contents == old[path][0]:
            raise RuntimeError(f"CLIProxyAPI {current} path is missing from {path}.")
        replacements[path] = contents
    replacements[state] = (json.dumps({"version": version}, sort_keys=True) + "\n").encode()
    shutil.copytree(candidate, target)
    try:
        run(service_command(home, "bootout"), check=False)
        settle()
        for path, contents in replacements.items():
            atomic_write(path, contents, old[path][1])
        run(service_command(home, "bootstrap"))
        probe(home)
    except BaseException as error:
        rollback_error = None
        try:
            run(service_command(home, "bootout"), check=False)
            settle()
            for path, (contents, mode) in old.items():
                atomic_write(path, contents, mode)
            run(service_command(home, "bootstrap"))
            probe(home)
        except BaseException as rollback:
            rollback_error = rollback
        if rollback_error is None:
            shutil.rmtree(target)
            raise error
        raise RuntimeError(f"{error}; rollback failed: {rollback_error}") from error


def latest_release():
    request = urllib.request.Request(RELEASE_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "fleet-cliproxy-updater"})
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").is_file() else None)
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        value = json.load(response)
    tag = value.get("tag_name", "")
    version = tag[1:] if tag.startswith("v") else ""
    version_parts(version)
    if value.get("draft") or value.get("prerelease"):
        raise RuntimeError("GitHub latest release is not stable.")
    return version, value.get("published_at", "unknown")


def build_candidate(bundle, version, build_date, temporary):
    source = temporary / "source"
    candidate = temporary / "candidate"
    candidate.mkdir()
    command_run(["/usr/bin/git", "clone", "--quiet", "--depth", "1", "--branch", f"v{version}", REPOSITORY, str(source)])
    command_run(["/usr/bin/git", "-C", str(source), "apply", "--check", str(bundle / "provider-routing.patch")])
    command_run(["/usr/bin/git", "-C", str(source), "apply", str(bundle / "provider-routing.patch")])
    go = shutil.which("go")
    compiler = shutil.which("cc")
    if not go or not compiler:
        raise RuntimeError("CLIProxyAPI updater needs Go and a C compiler.")
    command_run([go, "test", "./sdk/api/handlers", "./sdk/cliproxy/auth"], cwd=source)
    commit = command_run(["/usr/bin/git", "-C", str(source), "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
    flags = f"-s -w -X main.Version={version} -X main.Commit={commit} -X main.BuildDate={build_date}"
    command_run([go, "build", "-trimpath", "-ldflags", flags, "-o", str(candidate / "cli-proxy-api"), "./cmd/server"], cwd=source)
    plugins = candidate / "plugins"
    plugins.mkdir()
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    compile_flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    command_run([compiler, *compile_flags, "-O2", str(bundle / "fleet-provider-router.c"), "-o", str(plugins / f"fleet-provider-router{suffix}")])
    shutil.copy2(bundle / "fleet-retry-bridge", candidate / "fleet-retry-bridge")
    for path in (candidate / "cli-proxy-api", candidate / "fleet-retry-bridge", plugins / f"fleet-provider-router{suffix}"):
        path.chmod(0o700)
    version_output = command_run([str(candidate / "cli-proxy-api"), "-h"], check=False, capture_output=True, text=True)
    if f"CLIProxyAPI Version: {version}" not in version_output.stdout + version_output.stderr:
        raise RuntimeError("Built CLIProxyAPI version check failed.")
    return candidate


def install(home, source, run=command_run):
    target = home / ".local/share/fleet/cliproxyapi-updater"
    target.mkdir(parents=True, exist_ok=True)
    files = {
        source / "lib/fleet_cliproxy_update.py": target / "fleet_cliproxy_update.py",
        source / "lib/fleet_provider_router.c": target / "fleet-provider-router.c",
        source / "lib/fleet_retry_bridge.py": target / "fleet-retry-bridge",
        source / "patches/cliproxyapi-provider-routing.patch": target / "provider-routing.patch",
    }
    for origin, destination in files.items():
        shutil.copy2(origin, destination)
        destination.chmod(0o700 if destination.name != "provider-routing.patch" else 0o600)
    logs = home / ".local/state/fleet/logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist = plistlib.dumps(
        {
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            "Label": UPDATE_LABEL,
            "ProgramArguments": ["/usr/bin/python3", str(target / "fleet_cliproxy_update.py")],
            "RunAtLoad": True,
            "StandardErrorPath": str(logs / "cliproxyapi-update.error.log"),
            "StandardOutPath": str(logs / "cliproxyapi-update.log"),
            "StartInterval": 21600,
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    service = home / "Library/LaunchAgents/com.fleet.cliproxyapi.update.plist"
    atomic_write(service, plist)
    domain = f"gui/{os.getuid()}"
    run(["/bin/launchctl", "bootout", f"{domain}/{UPDATE_LABEL}"], check=False)
    run(["/bin/launchctl", "bootstrap", domain, str(service)])


def main(arguments):
    home = Path.home()
    if arguments == ["--install"]:
        install(home, Path(__file__).resolve().parents[1])
        return
    if arguments:
        raise SystemExit("Usage: fleet_cliproxy_update.py [--install]")
    hub = home / ".local/state/fleet/hub.json"
    if hub.exists() and json.loads(hub.read_text()).get("role") == "spoke":
        print("CLIProxyAPI updates are skipped while this host uses a Fleet hub.")
        return
    state = home / ".local/state/fleet/cliproxyapi.json"
    current = json.loads(state.read_text()).get("version", "")
    version_parts(current)
    version, build_date = latest_release()
    if not is_newer(version, current):
        print(f"CLIProxyAPI {current} is current.")
        return
    bundle = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="fleet-cliproxy-update-") as directory:
        candidate = build_candidate(bundle, version, build_date, Path(directory))
        activate(home, current, version, candidate)
    print(f"CLIProxyAPI updated {current} -> {version}.")


if __name__ == "__main__":
    main(sys.argv[1:])
