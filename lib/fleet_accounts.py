#!/usr/bin/env python3
import base64
import datetime
import importlib.util
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ACCOUNT_FILE = re.compile(r"^(claude|codex)-([A-Za-z0-9][A-Za-z0-9_.@+-]{0,199})\.json$")
PROVIDERS = ("claude", "codex")
ROUTING_MODES = ("limits", "any")
CREDIT_MODES = ("never", "last", "same-account")
MANAGEMENT_HELP = (
    "Enable CLIProxyAPI remote-management.secret-key, set FLEET_CLIPROXY_MANAGEMENT_KEY, "
    "and keep FLEET_CLIPROXY_URL on localhost."
)
FLEET_PROVIDER = "fleet_cliproxyapi"
ROUTING_BEGIN = "# Fleet account routing: begin"
ROUTING_END = "# Fleet account routing: end"
SHELL_HOOK = b'[ -r "$HOME/.local/share/fleet/current/bin/fleet-shell" ] && . "$HOME/.local/share/fleet/current/bin/fleet-shell"\n'
SETUP_VERSION = "7.2.149"
UPGRADABLE_SETUP_VERSIONS = {"7.2.130", "7.2.147"}
RELEASES = {
    ("darwin", "aarch64"): ("CLIProxyAPI_7.2.149_darwin_aarch64.tar.gz", "90962c9194fe5470dc21f167b0cbf167a4f9ff2961a6bcc88f0b7eec32f1b49b"),
    ("darwin", "amd64"): ("CLIProxyAPI_7.2.149_darwin_amd64.tar.gz", "382f800a4d82fe39ee7158ca4f735a1a71d635fe0f1d9a55a4c5d13993ccc04e"),
    ("linux", "aarch64"): ("CLIProxyAPI_7.2.149_linux_aarch64.tar.gz", "2d290477295eba4e419bc231f1fb5d548edbdd4cd5654b34d26ed12f8dcd0ee7"),
    ("linux", "amd64"): ("CLIProxyAPI_7.2.149_linux_amd64.tar.gz", "95d865dd17986da7d08cb39ffafe07d050669c5264d4d00115758ab4de752a72"),
}
spec = importlib.util.spec_from_file_location("fleet_apply", Path(__file__).with_name("fleet_apply.py"))
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


def supported_setup_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return False
    return tuple(map(int, value.split("."))) >= tuple(map(int, SETUP_VERSION.split(".")))


def managed_setup_version(home):
    return load_setup_state(home, allow_newer=True)["version"]


def account_home():
    configured = os.environ.get("FLEET_ACCOUNT_HOME")
    return Path(configured) if configured else Path.home()


def setup_state_path(home):
    return fleet.safe_target(home, ".local/state/fleet/cliproxyapi.json")


def client_routing_state_path(home):
    return fleet.safe_target(home, ".local/state/fleet/client-routing.json")


def account_routing_path(home):
    return fleet.safe_target(home, ".local/state/fleet/account-routing.json")


def hub_state_path(home):
    return fleet.safe_target(home, ".local/state/fleet/hub.json")


def load_hub_state(home):
    path = hub_state_path(home)
    if not os.path.lexists(path):
        return None
    if not regular_private_file(path, 0o600):
        raise fleet.FleetError("Invalid Fleet hub state.")
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid Fleet hub state.") from None
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("role") not in {"hub", "spoke"}
        or not isinstance(value.get("login"), str)
        or not valid_hub_url(value.get("url"))
    ):
        raise fleet.FleetError("Invalid Fleet hub state.")
    return value


def valid_hub_url(value):
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlsplit(value)
    return (
        parsed.scheme == "http"
        and parsed.hostname is not None
        and parsed.hostname.endswith(".ts.net")
        and parsed.port == 8317
        and not (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password)
    )


def hub_state_bytes(role, login, url):
    return (json.dumps({"login": login, "role": role, "url": url, "version": 1}, indent=2, sort_keys=True) + "\n").encode()


def is_spoke(home):
    hub = load_hub_state(home)
    return hub is not None and hub["role"] == "spoke"


def default_account_routing():
    return {"version": 1, "providers": {provider: "limits" for provider in PROVIDERS}}


def load_account_routing(home, *, allow_missing=False):
    path = account_routing_path(home)
    try:
        details = os.lstat(path)
        value = json.loads(path.read_text())
    except FileNotFoundError:
        if allow_missing:
            return default_account_routing()
        raise fleet.FleetError("Fleet account routing policy is missing. Run fleet account setup.") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid Fleet account routing policy.") from None
    providers = value.get("providers") if isinstance(value, dict) else None
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(providers, dict)
        or set(providers) != set(PROVIDERS)
        or any(providers[provider] not in ROUTING_MODES for provider in PROVIDERS)
        or not isinstance(value.get("credits", {}), dict)
        or any(provider not in PROVIDERS or mode not in CREDIT_MODES for provider, mode in value.get("credits", {}).items())
        or not isinstance(value.get("selection", {}), dict)
        or any(provider not in PROVIDERS or mode != "priority" for provider, mode in value.get("selection", {}).items())
    ):
        raise fleet.FleetError("Invalid Fleet account routing policy.")
    return value


def account_routing_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def limit_error_rules(provider):
    kinds = ["rate_limit_error"] if provider == "claude" else ["usage_limit_reached", "turn_rate_limit_exceeded"]
    return [
        {"status": 429, "match": kinds, "action": "continue-and-cooldown"},
        *([
            {"status": 400, "match": ["model is not supported when using Codex with a ChatGPT account."], "action": "continue-and-cooldown"}
        ] if provider == "codex" else []),
        *(
            {"status": status, "match-regexr": ["(?s).*"], "action": "stop"}
            for status in range(400, 600)
        ),
    ]


def configured_error_rules(routing):
    return {
        provider: limit_error_rules(provider)
        for provider in PROVIDERS
        if routing["providers"][provider] == "limits"
    }


def error_rules_yaml(routing):
    configured = configured_error_rules(routing)
    if not configured:
        return ""
    lines = ["oauth-request-scoped-errors:"]
    for provider, rules in configured.items():
        lines.append(f"  {provider}:")
        for rule in rules:
            lines.append(f"    - status: {rule['status']}")
            key = "match" if "match" in rule else "match-regexr"
            lines.extend((f"      {key}:", *(f"        - {json.dumps(value)}" for value in rule[key]), f"      action: {json.dumps(rule['action'])}"))
    return "\n".join(lines) + "\n"


def shell_startup_path(home, shell):
    name = Path(shell).name
    if name not in {"bash", "zsh"}:
        raise fleet.FleetError("Fleet native auth commands require bash or zsh.")
    return fleet.safe_target(home, ".zshrc" if name == "zsh" else ".bashrc")


def shell_startup_snapshot(path):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.getuid():
        raise fleet.FleetError("Shell startup file is unsafe.")
    try:
        return path.read_bytes(), stat.S_IMODE(details.st_mode)
    except OSError:
        raise fleet.FleetError("Could not read the shell startup file.") from None


def install_shell_hook(home, shell):
    path = shell_startup_path(home, shell)
    snapshot = shell_startup_snapshot(path)
    if snapshot is not None and SHELL_HOOK.rstrip(b"\n") in snapshot[0].splitlines():
        return
    contents = b"" if snapshot is None else snapshot[0]
    if contents and not contents.endswith(b"\n"):
        contents += b"\n"
    replace_snapshot(
        home,
        path,
        snapshot,
        (contents + SHELL_HOOK, 0o600 if snapshot is None else snapshot[1]),
        shell_startup_snapshot,
        "Shell startup file changed during setup.",
    )


def check_shell_hook(home, shell):
    snapshot = shell_startup_snapshot(shell_startup_path(home, shell))
    return snapshot is not None and SHELL_HOOK.rstrip(b"\n") in snapshot[0].splitlines()


def release_asset(system=None, machine=None):
    system = sys.platform if system is None else system
    system = "linux" if system.startswith("linux") else system
    machine = platform.machine().lower() if machine is None else machine.lower()
    machine = {"arm64": "aarch64", "x86_64": "amd64"}.get(machine, machine)
    try:
        return RELEASES[(system, machine)]
    except KeyError:
        raise fleet.FleetError("CLIProxyAPI setup supports macOS and Linux on arm64 or x86_64.") from None


def setup_paths(home, platform_name, version=SETUP_VERSION):
    binary = fleet.safe_target(home, f".local/share/fleet/cliproxyapi/{version}/cli-proxy-api")
    paths = {
        "binary": binary,
        "bridge": binary.with_name("fleet-retry-bridge"),
        "config": fleet.safe_target(home, ".config/fleet/cliproxyapi.yaml"),
        "client_key": fleet.safe_target(home, ".local/state/fleet/cliproxyapi-client-key"),
        "management_key": fleet.safe_target(home, ".local/state/fleet/cliproxyapi-management-key"),
        "hub_client_key": fleet.safe_target(home, ".local/state/fleet/hub-client-key"),
        "hub_management_key": fleet.safe_target(home, ".local/state/fleet/hub-management-key"),
        "hub": hub_state_path(home),
        "state": setup_state_path(home),
        "policy": account_routing_path(home),
        "auth": fleet.safe_target(home, ".cli-proxy-api"),
    }
    if platform_name == "darwin":
        paths["service"] = fleet.safe_target(home, "Library/LaunchAgents/com.fleet.cliproxyapi.plist")
    else:
        paths["service"] = fleet.safe_target(home, ".config/systemd/user/fleet-cliproxyapi.service")
    return paths


def setup_config(auth, client_key, management_key, routing=None):
    routing = default_account_routing() if routing is None else routing
    return (
        'host: "127.0.0.1"\n'
        "port: 8318\n"
        f"auth-dir: {json.dumps(str(auth))}\n"
        "api-keys:\n"
        f"  - {json.dumps(client_key)}\n"
        "request-retry: 0\n"
        "max-retry-credentials: 0\n"
        "routing:\n"
        "  strategy: fill-first\n"
        "  session-affinity: true\n"
        "streaming:\n"
        "  bootstrap-retries: 0\n"
        "codex:\n"
        "  stream-bootstrap-buffering: true\n"
        "  disable-codex-cloaking: true\n"
        "provider-model-lists:\n"
        "  enabled: true\n"
        + error_rules_yaml(routing)
        + "remote-management:\n"
        "  allow-remote: false\n"
        f"  secret-key: {json.dumps(management_key)}\n"
        "  disable-control-panel: true\n"
    ).encode()


def setup_plugin_config(paths):
    return (
        "plugins:\n"
        "  enabled: true\n"
        f'  dir: {json.dumps(str(paths["binary"].parent / "plugins"))}\n'
        "  configs:\n"
        "    fleet-provider-router:\n"
        "      enabled: true\n"
        "      priority: 100\n"
    ).encode()


def valid_setup_config(paths, client_key, management_key):
    if not regular_private_file(paths["config"], 0o600):
        return False
    expected = setup_config(paths["auth"], client_key, management_key, load_account_routing(paths["auth"].parent, allow_missing=True))
    try:
        current = paths["config"].read_bytes()
    except OSError:
        return False
    with_plugin = expected.replace(b"api-keys:\n", setup_plugin_config(paths) + b"api-keys:\n", 1)
    if current in {expected, with_plugin}:
        return True
    normalized, count = re.subn(
        rb'(?m)^  secret-key: "\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}"$',
        b"  secret-key: " + json.dumps(management_key).encode(),
        current,
    )
    return count == 1 and normalized in {expected, with_plugin}


def setup_service(home, platform_name, version=SETUP_VERSION, hub_url=None):
    bridge = f"%h/.local/share/fleet/cliproxyapi/{version}/fleet-retry-bridge"
    binary = f"%h/.local/share/fleet/cliproxyapi/{version}/cli-proxy-api"
    config = "%h/.config/fleet/cliproxyapi.yaml"
    arguments = f"--relay {hub_url}" if hub_url else f"{binary} {config}"
    if platform_name == "linux":
        return (
            "[Unit]\nDescription=Fleet CLIProxyAPI\n\n[Service]\n"
            f"ExecStart={bridge} {arguments}\n"
            "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n"
        ).encode()
    logs = fleet.safe_target(home, ".local/state/fleet/logs")
    program = [str(fleet.safe_target(home, f".local/share/fleet/cliproxyapi/{version}/fleet-retry-bridge"))]
    if hub_url:
        program += ["--relay", hub_url]
    else:
        program += [
            str(fleet.safe_target(home, f".local/share/fleet/cliproxyapi/{version}/cli-proxy-api")),
            str(fleet.safe_target(home, ".config/fleet/cliproxyapi.yaml")),
        ]
    return plistlib.dumps(
        {
            "Label": "com.fleet.cliproxyapi",
            "ProgramArguments": program,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "cliproxyapi.log"),
            "StandardErrorPath": str(logs / "cliproxyapi.error.log"),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def download_cafile():
    fallback = "/etc/ssl/cert.pem"
    return fallback if ssl.get_default_verify_paths().cafile is None and Path(fallback).is_file() else None


def download_release(url, target, digest):
    request = urllib.request.Request(url, headers={"User-Agent": "fleet-account-setup"})
    checksum = hashlib.sha256()
    total = 0
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=download_cafile()))
        )
        with opener.open(request, timeout=30) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise fleet.FleetError("CLIProxyAPI release is too large.")
                checksum.update(chunk)
                output.write(chunk)
    except fleet.FleetError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError):
        raise fleet.FleetError(f"Could not download CLIProxyAPI {SETUP_VERSION}.") from None
    if checksum.hexdigest() != digest:
        raise fleet.FleetError("CLIProxyAPI release checksum did not match.")


def extract_proxy(home, archive, target):
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember("cli-proxy-api")
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise fleet.FleetError("Invalid CLIProxyAPI release archive.")
            source = bundle.extractfile(member)
            if source is None:
                raise fleet.FleetError("Invalid CLIProxyAPI release archive.")
            fleet.atomic_write(home, target, source.read(), 0o700)
            target.chmod(0o700)
    except fleet.FleetError:
        raise
    except (OSError, KeyError, tarfile.TarError):
        raise fleet.FleetError("Invalid CLIProxyAPI release archive.") from None


def existing_unmanaged_proxy(home):
    candidates = (
        Path("/opt/homebrew/etc/cliproxyapi.conf"),
        Path("/usr/local/etc/cliproxyapi.conf"),
        home / ".cli-proxy-api/config.yaml",
        home / ".config/cliproxyapi/config.yaml",
        home / ".config/systemd/user/cliproxyapi.service",
        home / ".local/bin/cliproxyapi",
    )
    if any(path.exists() for path in candidates) or shutil.which("cliproxyapi") or shutil.which("cli-proxy-api"):
        return True
    return False


def load_setup_state(home, *, allow_upgrade=False, allow_newer=False):
    path = setup_state_path(home)
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid Fleet CLIProxyAPI setup state.") from None
    if not regular_private_file(path, 0o600):
        raise fleet.FleetError("Invalid Fleet CLIProxyAPI setup state.")
    valid_upgrade = allow_upgrade and value.get("version") in UPGRADABLE_SETUP_VERSIONS
    valid_newer = allow_newer and supported_setup_version(value.get("version"))
    if set(value) != {"version"} or not (value["version"] == SETUP_VERSION or valid_upgrade or valid_newer):
        raise fleet.FleetError("Invalid Fleet CLIProxyAPI setup state.")
    return value


def native_setup_status(home, platform_name, native_runner=None):
    enabled, active = native_service_state(home, platform_name, native_runner)
    return enabled and active if platform_name == "linux" else active


def native_service_state(home, platform_name, native_runner=None):
    if platform_name == "linux":
        enabled = fleet.run_native(["/usr/bin/systemctl", "--user", "is-enabled", "fleet-cliproxyapi.service"], home, "linux", native_runner)
        active = fleet.run_native(["/usr/bin/systemctl", "--user", "is-active", "fleet-cliproxyapi.service"], home, "linux", native_runner)
        return enabled.returncode == 0, active.returncode == 0
    uid = os.getuid()
    result = fleet.run_native(["/bin/launchctl", "print", f"gui/{uid}/com.fleet.cliproxyapi"], home, "darwin", native_runner)
    return False, result.returncode == 0


def enable_proxy_service(home, platform_name, service, native_runner=None):
    if platform_name == "linux":
        if fleet.run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not load the CLIProxyAPI user service.")
        if fleet.run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not start the CLIProxyAPI user service.")
        return
    uid = os.getuid()
    if fleet.run_native(["/usr/bin/plutil", "-lint", str(service)], home, "darwin", native_runner).returncode:
        raise fleet.FleetError("Invalid CLIProxyAPI launch agent.")
    if fleet.run_native(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(service)], home, "darwin", native_runner).returncode:
        raise fleet.FleetError("Could not start the CLIProxyAPI launch agent.")


def disable_proxy_service(home, platform_name, service, native_runner=None):
    enabled, active = native_service_state(home, platform_name, native_runner)
    if platform_name == "linux":
        if (enabled or active) and fleet.run_native(["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not stop the CLIProxyAPI user service.")
        if fleet.run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not reload CLIProxyAPI user services.")
        if any(native_service_state(home, platform_name, native_runner)):
            raise fleet.FleetError("CLIProxyAPI user service is still active.")
        return
    if active and fleet.run_native(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(service)], home, "darwin", native_runner).returncode:
        raise fleet.FleetError("Could not stop the CLIProxyAPI launch agent.")
    if native_service_state(home, platform_name, native_runner)[1]:
        raise fleet.FleetError("CLIProxyAPI launch agent is still active.")


def restart_proxy_service(home, platform_name, service, native_runner=None):
    if platform_name == "linux":
        if fleet.run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not reload the CLIProxyAPI user service.")
        if fleet.run_native(["/usr/bin/systemctl", "--user", "restart", "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not restart the CLIProxyAPI user service.")
        return
    uid = os.getuid()
    if native_service_state(home, platform_name, native_runner)[1]:
        if fleet.run_native(["/bin/launchctl", "bootout", f"gui/{uid}", str(service)], home, "darwin", native_runner).returncode:
            raise fleet.FleetError("Could not stop the CLIProxyAPI launch agent.")
    if fleet.run_native(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(service)], home, "darwin", native_runner).returncode:
        raise fleet.FleetError("Could not restart the CLIProxyAPI launch agent.")


def restore_proxy_service_state(home, platform_name, service, enabled, active, native_runner=None):
    current_enabled, current_active = native_service_state(home, platform_name, native_runner)
    if platform_name == "linux":
        if fleet.run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
            raise fleet.FleetError("Could not reload the CLIProxyAPI user service.")
        if current_active and not active:
            if fleet.run_native(["/usr/bin/systemctl", "--user", "stop", "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
                raise fleet.FleetError("Could not restore the CLIProxyAPI user service state.")
            current_active = False
        if current_enabled != enabled:
            action = "enable" if enabled else "disable"
            if fleet.run_native(["/usr/bin/systemctl", "--user", action, "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
                raise fleet.FleetError("Could not restore the CLIProxyAPI user service state.")
        if not current_active and active:
            if fleet.run_native(["/usr/bin/systemctl", "--user", "start", "fleet-cliproxyapi.service"], home, "linux", native_runner).returncode:
                raise fleet.FleetError("Could not restore the CLIProxyAPI user service state.")
    elif current_active != active:
        if active:
            command = ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(service)]
        else:
            command = ["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(service)]
        if fleet.run_native(command, home, "darwin", native_runner).returncode:
            raise fleet.FleetError("Could not restore the CLIProxyAPI launch agent state.")
    if native_service_state(home, platform_name, native_runner) != (enabled, active):
        raise fleet.FleetError("Could not restore the CLIProxyAPI user service state.")


def wait_for_proxy(home):
    for _ in range(20):
        try:
            probe_setup(home)
            return
        except fleet.FleetError:
            time.sleep(0.25)
    raise fleet.FleetError("CLIProxyAPI did not pass its local management check.")


def managed_key(paths, name):
    label = "client key" if name == "client_key" else "management secret"
    path = paths[name]
    if not regular_private_file(path, 0o600):
        raise fleet.FleetError(f"CLIProxyAPI {label} is missing or unsafe.")
    try:
        value = path.read_text()
    except (OSError, UnicodeError):
        raise fleet.FleetError(f"CLIProxyAPI {label} is missing or unsafe.") from None
    if not value.endswith("\n") or "\n" in value[:-1] or not value[:-1].isprintable():
        raise fleet.FleetError(f"CLIProxyAPI {label} is missing or unsafe.")
    return value[:-1]


def verify_setup_binary(home, path, version=SETUP_VERSION):
    if not regular_private_file(path, 0o700):
        raise fleet.FleetError("CLIProxyAPI binary is missing or unsafe.")
    try:
        result = subprocess.run(
            [str(path), "-h"],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise fleet.FleetError("Could not verify the CLIProxyAPI binary.") from None
    if f"CLIProxyAPI Version: {version}" not in result.stdout + result.stderr:
        raise fleet.FleetError(f"CLIProxyAPI {version} is required.")


def setup_bridge():
    return Path(__file__).with_name("fleet_retry_bridge.py").read_bytes()


def valid_setup_bridge(path):
    try:
        return regular_private_file(path, 0o700) and path.read_bytes() == setup_bridge()
    except OSError:
        return False


def artifact_snapshot(path):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid():
        raise fleet.FleetError("CLIProxyAPI managed file is unsafe.")
    return path.read_bytes(), stat.S_IMODE(details.st_mode)


def credential_identity(path):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid():
        raise fleet.FleetError("CLIProxyAPI managed file is unsafe.")
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns


def replace_snapshot(home, path, expected, replacement, snapshotter, message):
    if snapshotter(path) != expected:
        raise fleet.FleetError(message)
    if replacement is None:
        if expected is not None:
            path.unlink()
        return
    fleet.atomic_write(home, path, replacement[0], replacement[1])
    path.chmod(replacement[1])


def restore_artifacts(home, snapshots, written):
    for path, expected in written.items():
        if artifact_snapshot(path) != expected:
            raise fleet.FleetError("A managed setup file changed during restore.")
    for path, expected in written.items():
        replace_snapshot(
            home,
            path,
            expected,
            snapshots[path],
            artifact_snapshot,
            "A managed setup file changed during restore.",
        )


def prepare_setup_logs(home):
    logs = fleet.safe_target(home, ".local/state/fleet/logs")
    fleet.ensure_parent(home, logs)
    try:
        details = os.lstat(logs)
    except FileNotFoundError:
        os.mkdir(logs, 0o700)
        return logs, True
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise fleet.FleetError("CLIProxyAPI log directory is unsafe.")
    return logs, False


def directory_identity(path):
    details = os.lstat(path)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise fleet.FleetError("CLIProxyAPI managed directory is unsafe.")
    return details.st_dev, details.st_ino, stat.S_IMODE(details.st_mode)


def remove_setup_logs(logs, expected):
    try:
        if directory_identity(logs) != expected:
            raise OSError
        logs.rmdir()
    except (OSError, fleet.FleetError):
        raise fleet.FleetError("Could not restore the CLIProxyAPI log directory.") from None


def restore_setup_auth_directory(path, expected, existed, prior_mode):
    try:
        if directory_identity(path) != expected:
            raise OSError
        if existed:
            path.chmod(prior_mode)
        else:
            path.rmdir()
    except OSError:
        raise fleet.FleetError("Could not restore the CLIProxyAPI auth directory.") from None


def install_setup_binary(home, path, platform_name, machine, downloader, expected):
    name, digest = release_asset(platform_name, machine)
    url = f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{SETUP_VERSION}/{name}"
    download = download_release if downloader is None else downloader
    with tempfile.TemporaryDirectory(prefix="cliproxyapi-setup-") as temporary:
        archive = Path(temporary) / name
        download(url, archive, digest)
        if artifact_snapshot(path) != expected:
            raise fleet.FleetError("CLIProxyAPI managed binary changed during setup.")
        extract_proxy(home, archive, path)


def refresh_config_snapshot(paths, snapshots, client_key, management_key):
    if paths["config"] in snapshots and valid_setup_config(paths, client_key, management_key):
        snapshots[paths["config"]] = artifact_snapshot(paths["config"])


def repair_managed_setup(home, platform_name, machine, paths, downloader, native_runner, probe):
    client_key = managed_key(paths, "client_key")
    management_key = managed_key(paths, "management_key")
    routing = load_account_routing(home, allow_missing=True)
    hub = load_hub_state(home)
    expected = {
        paths["bridge"]: (setup_bridge(), 0o700),
        paths["config"]: (setup_config(paths["auth"], client_key, management_key, routing), 0o600),
        paths["service"]: (setup_service(home, platform_name, hub_url=hub["url"] if hub and hub["role"] == "spoke" else None), 0o600),
        paths["state"]: ((json.dumps({"version": SETUP_VERSION}, sort_keys=True) + "\n").encode(), 0o600),
        paths["policy"]: (account_routing_bytes(routing), 0o600),
    }
    binary_ok = True
    try:
        verify_setup_binary(home, paths["binary"])
    except fleet.FleetError:
        binary_ok = False
    drift = not binary_ok or not valid_setup_bridge(paths["bridge"]) or not valid_setup_config(paths, client_key, management_key)
    if not regular_private_file(paths["service"], 0o600) or paths["service"].read_bytes() != expected[paths["service"]][0]:
        drift = True
    if artifact_snapshot(paths["state"]) != expected[paths["state"]]:
        drift = True
    if artifact_snapshot(paths["policy"]) != expected[paths["policy"]]:
        drift = True
    if not drift:
        previous_enabled, previous_active = native_service_state(home, platform_name, native_runner)
        try:
            if not (previous_enabled and previous_active if platform_name == "linux" else previous_active):
                enable_proxy_service(home, platform_name, paths["service"], native_runner)
            (wait_for_proxy if probe is None else probe)(home)
        except BaseException as error:
            try:
                restore_proxy_service_state(home, platform_name, paths["service"], previous_enabled, previous_active, native_runner)
            except BaseException:
                raise fleet.FleetError(f"{error} Managed setup restore also failed.") from None
            raise
        return
    snapshots = {
        path: artifact_snapshot(path)
        for path in (paths["binary"], paths["bridge"], paths["config"], paths["service"], paths["state"], paths["policy"])
    }
    written = {}
    previous_enabled, previous_active = native_service_state(home, platform_name, native_runner)
    try:
        if not binary_ok:
            install_setup_binary(home, paths["binary"], platform_name, machine, downloader, snapshots[paths["binary"]])
            written[paths["binary"]] = artifact_snapshot(paths["binary"])
            verify_setup_binary(home, paths["binary"])
        for path, (contents, mode) in expected.items():
            replace_snapshot(
                home,
                path,
                snapshots[path],
                (contents, mode),
                artifact_snapshot,
                "CLIProxyAPI managed file changed during setup.",
            )
            written[path] = artifact_snapshot(path)
        if previous_active:
            restart_proxy_service(home, platform_name, paths["service"], native_runner)
        else:
            enable_proxy_service(home, platform_name, paths["service"], native_runner)
        (wait_for_proxy if probe is None else probe)(home)
    except BaseException as error:
        try:
            disable_proxy_service(home, platform_name, paths["service"], native_runner)
            stopped = True
        except BaseException:
            stopped = False
        try:
            if not stopped:
                raise fleet.FleetError("CLIProxyAPI service could not be stopped for restore.")
            refresh_config_snapshot(paths, written, client_key, management_key)
            restore_artifacts(home, snapshots, written)
            restore_proxy_service_state(home, platform_name, paths["service"], previous_enabled, previous_active, native_runner)
        except BaseException:
            stopped = False
        if not stopped:
            raise fleet.FleetError(f"{error} Managed setup restore also failed.") from None
        raise


def setup_proxy(home, *, platform_name=None, machine=None, downloader=None, native_runner=None, probe=None, existing_proxy=None):
    platform_name = sys.platform if platform_name is None else platform_name
    platform_name = "linux" if platform_name.startswith("linux") else platform_name
    release_asset(platform_name, machine)
    paths = setup_paths(home, platform_name)
    if os.path.lexists(paths["state"]):
        setup = load_setup_state(home, allow_upgrade=True, allow_newer=True)
        if setup["version"] != SETUP_VERSION and setup["version"] not in UPGRADABLE_SETUP_VERSIONS:
            return
        logs, logs_created = prepare_setup_logs(home)
        logs_identity = directory_identity(logs)
        try:
            repair_managed_setup(home, platform_name, machine, paths, downloader, native_runner, probe)
        except BaseException as error:
            if logs_created:
                try:
                    remove_setup_logs(logs, logs_identity)
                except BaseException:
                    raise fleet.FleetError(f"{error} Managed setup restore also failed.") from None
            raise
        return
    if any(native_service_state(home, platform_name, native_runner)):
        raise fleet.FleetError("Existing CLIProxyAPI user service is not managed by Fleet.")
    detector = existing_unmanaged_proxy if existing_proxy is None else existing_proxy
    if detector(home):
        raise fleet.FleetError("Existing CLIProxyAPI setup is not managed by Fleet. Move it with the migration steps in README.md first.")
    for key in ("binary", "bridge", "config", "client_key", "management_key", "service", "policy"):
        if os.path.lexists(paths[key]):
            raise fleet.FleetError("Existing CLIProxyAPI setup is not managed by Fleet. Move it with the migration steps in README.md first.")
    auth_existed = os.path.lexists(paths["auth"])
    if auth_existed:
        auth_details = os.lstat(paths["auth"])
        if stat.S_ISLNK(auth_details.st_mode) or not stat.S_ISDIR(auth_details.st_mode) or auth_details.st_uid != os.getuid():
            raise fleet.FleetError("Existing CLIProxyAPI auth directory is unsafe.")
        auth_mode = stat.S_IMODE(auth_details.st_mode)
    else:
        auth_mode = None
    created = {}
    logs = None
    logs_created = False
    auth_created = False
    auth_setup_identity = None
    logs_setup_identity = None
    service_attempted = False
    try:
        logs, logs_created = prepare_setup_logs(home)
        logs_setup_identity = directory_identity(logs)
        if not auth_existed:
            paths["auth"].mkdir(mode=0o700, parents=True)
            auth_created = True
        elif directory_identity(paths["auth"]) != (auth_details.st_dev, auth_details.st_ino, auth_mode):
            raise fleet.FleetError("Existing CLIProxyAPI auth directory changed during setup.")
        paths["auth"].chmod(0o700)
        auth_setup_identity = directory_identity(paths["auth"])
        client_key = secrets.token_urlsafe(32)
        management_key = secrets.token_urlsafe(32)
        while management_key == client_key:
            management_key = secrets.token_urlsafe(32)
        for path, value in (
            (paths["client_key"], client_key),
            (paths["management_key"], management_key),
        ):
            replace_snapshot(
                home,
                path,
                None,
                ((value + "\n").encode(), 0o600),
                artifact_snapshot,
                "CLIProxyAPI managed file changed during setup.",
            )
            created[path] = artifact_snapshot(path)
        managed_key(paths, "client_key")
        managed_key(paths, "management_key")
        for path, contents, mode in (
            (paths["bridge"], setup_bridge(), 0o700),
            (paths["config"], setup_config(paths["auth"], client_key, management_key), 0o600),
            (paths["service"], setup_service(home, platform_name), 0o600),
            (paths["policy"], account_routing_bytes(default_account_routing()), 0o600),
        ):
            replace_snapshot(
                home,
                path,
                None,
                (contents, mode),
                artifact_snapshot,
                "CLIProxyAPI managed file changed during setup.",
            )
            created[path] = artifact_snapshot(path)
        install_setup_binary(home, paths["binary"], platform_name, machine, downloader, None)
        created[paths["binary"]] = artifact_snapshot(paths["binary"])
        verify_setup_binary(home, paths["binary"])
        service_attempted = True
        enable_proxy_service(home, platform_name, paths["service"], native_runner)
        if not native_setup_status(home, platform_name, native_runner):
            raise fleet.FleetError("CLIProxyAPI service did not become active.")
        (wait_for_proxy if probe is None else probe)(home)
        replace_snapshot(
            home,
            paths["state"],
            None,
            ((json.dumps({"version": SETUP_VERSION}, sort_keys=True) + "\n").encode(), 0o600),
            artifact_snapshot,
            "CLIProxyAPI setup state changed during setup.",
        )
    except BaseException as error:
        cleanup_failed = False
        service_stopped = True
        if service_attempted:
            try:
                disable_proxy_service(home, platform_name, paths["service"], native_runner)
            except BaseException:
                cleanup_failed = True
                service_stopped = False
        if service_stopped:
            try:
                if paths["config"] in created:
                    refresh_config_snapshot(paths, created, client_key, management_key)
                restore_artifacts(home, {path: None for path in created}, created)
            except BaseException:
                cleanup_failed = True
        if service_stopped:
            try:
                if auth_existed or auth_created:
                    restore_setup_auth_directory(paths["auth"], auth_setup_identity, auth_existed, auth_mode)
                if logs_created:
                    remove_setup_logs(logs, logs_setup_identity)
            except (OSError, fleet.FleetError):
                cleanup_failed = True
        if cleanup_failed:
            raise fleet.FleetError(f"{error} Cleanup also failed.") from None
        raise


def regular_private_file(path, mode):
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and details.st_nlink == 1 and details.st_uid == os.getuid() and stat.S_IMODE(details.st_mode) == mode


def validate_proxy_url(raw_url):
    raw_url = raw_url.rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        parsed.port
    except ValueError:
        raise fleet.FleetError("FLEET_CLIPROXY_URL must be a localhost HTTP or HTTPS URL.") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise fleet.FleetError("FLEET_CLIPROXY_URL must be a localhost HTTP or HTTPS URL.")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise fleet.FleetError("FLEET_CLIPROXY_URL must contain only the localhost server address.")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise fleet.FleetError("FLEET_CLIPROXY_URL must use 127.0.0.1, ::1, or localhost.")
    return raw_url


def probe_inference_endpoint(url, client_key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ManagementClient.NoRedirect()).open
    try:
        request = urllib.request.Request(url + "/v1/models", headers={"Authorization": f"Bearer {client_key}"})
        with opener(request, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise fleet.FleetError("CLIProxyAPI client endpoint smoke check failed.") from None
    if len(raw) > 4 * 1024 * 1024:
        raise fleet.FleetError("CLIProxyAPI client endpoint response is too large.")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid CLIProxyAPI client endpoint response.") from None
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise fleet.FleetError("Invalid CLIProxyAPI client endpoint response.")


def client_config_snapshot(path):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.getuid():
        raise fleet.FleetError("Client config is missing or unsafe.")
    try:
        return path.read_bytes(), stat.S_IMODE(details.st_mode)
    except OSError:
        raise fleet.FleetError("Could not read client config.") from None


def claude_routing_config(current, url, client_key_path):
    try:
        value = json.loads(current.decode()) if current.strip() else {}
    except (UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid existing Claude settings.") from None
    if not isinstance(value, dict):
        raise fleet.FleetError("Invalid existing Claude settings.")
    environment = value.get("env", {})
    if not isinstance(environment, dict):
        raise fleet.FleetError("Ambiguous existing Claude settings environment.")
    environment = dict(environment)
    environment["ANTHROPIC_BASE_URL"] = url
    environment["CLAUDE_CODE_MAX_RETRIES"] = "3"
    value["env"] = environment
    value["apiKeyHelper"] = f"/bin/cat {shlex.quote(str(client_key_path))}"
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def codex_routing_config(current, url, client_key_path):
    try:
        value = tomllib.loads(current.decode()) if current.strip() else {}
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise fleet.FleetError("Invalid existing Codex config.") from None
    if not isinstance(value, dict) or FLEET_PROVIDER in value.get("model_providers", {}):
        raise fleet.FleetError("Existing Codex config conflicts with Fleet routing.")
    lines = current.decode().splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("["):
            break
        if re.match(r"^(model_provider|openai_base_url)\s*=", stripped):
            lines[index] = ""
    existing = "".join(lines)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"
    rendered = (
        f"{ROUTING_BEGIN}\n"
        f'openai_base_url = "{url}/v1"\n'
        f"{ROUTING_END}\n\n"
        f"{existing}"
    ).encode()
    try:
        tomllib.loads(rendered.decode())
    except tomllib.TOMLDecodeError:
        raise fleet.FleetError("Could not configure Codex client routing.") from None
    return rendered


def client_config_path(home, provider):
    relative = ".claude/settings.json" if provider == "claude" else ".codex/config.toml"
    return fleet.safe_target(home, relative)


def load_client_routing_state(home):
    path = client_routing_state_path(home)
    if not os.path.lexists(path):
        return {"version": 1, "providers": {}}
    if not regular_private_file(path, 0o600):
        raise fleet.FleetError("Fleet client routing restore state is unsafe.")
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("providers"), dict):
            raise ValueError
        for provider, record in value["providers"].items():
            if provider not in PROVIDERS or not isinstance(record, dict):
                raise ValueError
            required = {"prior_exists", "prior_bytes", "prior_mode", "written_bytes", "written_sha256"}
            if set(record) != required or type(record["prior_exists"]) is not bool:
                raise ValueError
            prior = base64.b64decode(record["prior_bytes"], validate=True)
            written = base64.b64decode(record["written_bytes"], validate=True)
            if record["prior_exists"] != (record["prior_mode"] is not None):
                raise ValueError
            if record["prior_mode"] is not None and (type(record["prior_mode"]) is not int or not 0 <= record["prior_mode"] <= 0o777):
                raise ValueError
            if hashlib.sha256(written).hexdigest() != record["written_sha256"]:
                raise ValueError
            if not record["prior_exists"] and prior:
                raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        raise fleet.FleetError("Invalid Fleet client routing restore state.") from None
    return value


def save_client_routing_state(home, provider, snapshot, rendered):
    path = client_routing_state_path(home)
    state_snapshot = client_config_snapshot(path)
    state = load_client_routing_state(home)
    record = state["providers"].get(provider)
    if record is None:
        record = {
            "written_bytes": "",
            "written_sha256": "",
        }
        state["providers"][provider] = record
    prior = b"" if snapshot is None else snapshot[0]
    record["prior_exists"] = snapshot is not None
    record["prior_bytes"] = base64.b64encode(prior).decode()
    record["prior_mode"] = None if snapshot is None else snapshot[1]
    record["written_bytes"] = base64.b64encode(rendered).decode()
    record["written_sha256"] = hashlib.sha256(rendered).hexdigest()
    contents = (json.dumps(state, sort_keys=True) + "\n").encode()
    replace_snapshot(
        home,
        path,
        state_snapshot,
        (contents, 0o600),
        client_config_snapshot,
        "Fleet client routing restore state changed during update.",
    )


def same_entry(left, right, key):
    return (key in left) == (key in right) and left.get(key) == right.get(key)


def restore_claude_contents(current, prior, written):
    try:
        current_value = json.loads(current.decode())
        prior_value = json.loads(prior.decode()) if prior.strip() else {}
        written_value = json.loads(written.decode())
        current_env = current_value.get("env", {})
        prior_env = prior_value.get("env", {})
        written_env = written_value.get("env", {})
        if not all(isinstance(value, dict) for value in (current_value, prior_value, written_value, current_env, prior_env, written_env)):
            raise ValueError
    except (UnicodeError, json.JSONDecodeError, ValueError, AttributeError):
        raise fleet.FleetError("Claude client routing changed after Fleet wrote it. Restore stopped.") from None
    if (
        not same_entry(current_value, written_value, "apiKeyHelper")
        or not same_entry(current_env, written_env, "ANTHROPIC_BASE_URL")
        or not same_entry(current_env, written_env, "CLAUDE_CODE_MAX_RETRIES")
    ):
        raise fleet.FleetError("Claude client routing changed after Fleet wrote it. Restore stopped.")
    if "apiKeyHelper" in prior_value:
        current_value["apiKeyHelper"] = prior_value["apiKeyHelper"]
    else:
        current_value.pop("apiKeyHelper", None)
    current_env = dict(current_env)
    if "ANTHROPIC_BASE_URL" in prior_env:
        current_env["ANTHROPIC_BASE_URL"] = prior_env["ANTHROPIC_BASE_URL"]
    else:
        current_env.pop("ANTHROPIC_BASE_URL", None)
    if "CLAUDE_CODE_MAX_RETRIES" in prior_env:
        current_env["CLAUDE_CODE_MAX_RETRIES"] = prior_env["CLAUDE_CODE_MAX_RETRIES"]
    else:
        current_env.pop("CLAUDE_CODE_MAX_RETRIES", None)
    if current_env or "env" in prior_value:
        current_value["env"] = current_env
    else:
        current_value.pop("env", None)
    if not current_value and not prior.strip():
        return None
    return (json.dumps(current_value, indent=2, sort_keys=True) + "\n").encode()


def restore_codex_contents(current, prior, written):
    begin = (ROUTING_BEGIN + "\n").encode()
    marker = (ROUTING_END + "\n").encode()

    def split(contents):
        if contents.count(begin) != 1 or contents.count(marker) != 1:
            raise ValueError
        start = contents.index(begin)
        end = contents.index(marker) + len(marker)
        if end <= start:
            raise ValueError
        return contents[:start], contents[start:end], contents[end:]

    try:
        current_value = tomllib.loads(current.decode())
        written_value = tomllib.loads(written.decode())
        if "openai_base_url" in written_value:
            if current_value.get("openai_base_url") != written_value["openai_base_url"]:
                raise ValueError
            reformatted = begin not in current and marker not in current
            if reformatted:
                current_prefix, current_rest = b"", current
            else:
                current_prefix, current_block, current_rest = split(current)
                if current_block != split(written)[1]:
                    raise ValueError
            lines = []
            top_level = True
            removed = reformatted is False
            for line in current_rest.decode().splitlines(keepends=True):
                if re.match(r"^\s*\[", line):
                    top_level = False
                if reformatted and top_level and re.match(r"^\s*openai_base_url\s*=", line):
                    removed = True
                    continue
                lines.append(line)
            if not removed:
                raise ValueError
            prior_keys = []
            for line in prior.decode().splitlines(keepends=True):
                stripped = line.lstrip()
                if stripped.startswith("["):
                    break
                if re.match(r"^(model_provider|openai_base_url)\s*=", stripped):
                    prior_keys.append(line if line.endswith("\n") else line + "\n")
            restored = current_prefix + "".join(prior_keys).encode() + "".join(lines).encode()
            value = tomllib.loads(restored.decode()) if restored.strip() else {}
            if value.get("model_provider") == FLEET_PROVIDER or FLEET_PROVIDER in value.get("model_providers", {}):
                raise ValueError
            return restored
        written_prefix, written_block, _ = split(written)
        if current_value.get("model_provider") != FLEET_PROVIDER:
            raise ValueError
        if current_value.get("model_providers", {}).get(FLEET_PROVIDER) != written_value.get("model_providers", {}).get(FLEET_PROVIDER):
            raise ValueError
        reformatted = begin not in current and marker not in current
        if reformatted:
            current_prefix, current_rest = b"", current
        else:
            current_prefix, current_block, current_rest = split(current)
            if current_block != written_block:
                raise ValueError
        if written_prefix:
            raise ValueError
        legacy_sections = {
            f"model_providers.{FLEET_PROVIDER}",
            f"model_providers.{FLEET_PROVIDER}.auth",
        }
        if reformatted or f"[model_providers.{FLEET_PROVIDER}]\n".encode() in written:
            lines = []
            skip = False
            top_level = True
            removed_provider = False
            for line in current_rest.decode().splitlines(keepends=True):
                table = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line.rstrip("\r\n"))
                if table:
                    top_level = False
                    skip = table.group(1) in legacy_sections
                if reformatted and top_level and re.match(r"^\s*model_provider\s*=", line):
                    removed_provider = True
                    continue
                if not skip:
                    lines.append(line)
            if reformatted and not removed_provider:
                raise ValueError
            current_rest = "".join(lines).encode()
        prior_provider = None
        for line in prior.decode().splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith("["):
                break
            if re.match(r"^model_provider\s*=", stripped):
                prior_provider = line
                break
        if prior_provider and not prior_provider.endswith("\n"):
            prior_provider += "\n"
        restored = current_prefix + (prior_provider or "").encode() + current_rest
        value = tomllib.loads(restored.decode()) if restored.strip() else {}
        if value.get("model_provider") == FLEET_PROVIDER or FLEET_PROVIDER in value.get("model_providers", {}):
            raise ValueError
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError, AttributeError):
        raise fleet.FleetError("Codex client routing changed after Fleet wrote it. Restore stopped.") from None
    return restored


def restored_client_snapshot(provider, current, record):
    written = base64.b64decode(record["written_bytes"])
    prior = base64.b64decode(record["prior_bytes"])
    if current[0] == written:
        if not record["prior_exists"]:
            return None
        return prior, record["prior_mode"]
    if provider == "codex":
        return restore_codex_contents(current[0], prior, written), record["prior_mode"] if record["prior_exists"] else current[1]
    if provider != "claude":
        raise fleet.FleetError("Invalid Fleet client routing restore state.")
    rendered = restore_claude_contents(current[0], prior, written)
    if rendered is None:
        return None
    return rendered, record["prior_mode"] if record["prior_exists"] else current[1]


def restore_client_routing(home):
    state_path = client_routing_state_path(home)
    state_snapshot = client_config_snapshot(state_path)
    if state_snapshot is None:
        raise fleet.FleetError("Fleet client routing restore state is missing.")
    state = load_client_routing_state(home)
    plans = []
    for provider, record in state["providers"].items():
        path = client_config_path(home, provider)
        current = client_config_snapshot(path)
        if current is None:
            raise fleet.FleetError(f"{provider} client routing changed after Fleet wrote it. Restore stopped.")
        restored = restored_client_snapshot(provider, current, record)
        plans.append((path, current, restored))
    changed = []
    try:
        for path, current, restored in plans:
            replace_snapshot(
                home,
                path,
                current,
                restored,
                client_config_snapshot,
                "Client config changed during restore.",
            )
            changed.append((path, current, restored))
        replace_snapshot(
            home,
            state_path,
            state_snapshot,
            None,
            client_config_snapshot,
            "Fleet client routing restore state changed during restore.",
        )
    except BaseException as error:
        rollback_failed = False
        for path, current, restored in reversed(changed):
            try:
                replace_snapshot(
                    home,
                    path,
                    restored,
                    current,
                    client_config_snapshot,
                    "Client config changed during restore rollback.",
                )
            except BaseException:
                rollback_failed = True
        if rollback_failed:
            raise fleet.FleetError(f"{error} Client routing restore rollback also failed.") from None
        raise


def provider_routing_valid(home, provider, url):
    path = client_config_path(home, provider)
    if not regular_private_file(path, 0o600):
        return False
    try:
        client_key_path = setup_paths(home, "darwin" if sys.platform == "darwin" else "linux")["client_key"]
        raw = path.read_bytes()
        if provider == "codex":
            value = tomllib.loads(raw.decode())
            return (
                value.get("openai_base_url") == url + "/v1"
                and "model_provider" not in value
                and FLEET_PROVIDER not in value.get("model_providers", {})
            )
        value = json.loads(raw)
        return (
            isinstance(value, dict)
            and isinstance(value.get("env"), dict)
            and value["env"].get("ANTHROPIC_BASE_URL") == url
            and value["env"].get("CLAUDE_CODE_MAX_RETRIES") == "3"
            and value.get("apiKeyHelper") == f"/bin/cat {shlex.quote(str(client_key_path))}"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, AttributeError):
        return False


def codex_fleet_routing_present(home):
    snapshot = client_config_snapshot(client_config_path(home, "codex"))
    return snapshot is not None and (
        FLEET_PROVIDER.encode() in snapshot[0]
        or ROUTING_BEGIN.encode() in snapshot[0]
        or ROUTING_END.encode() in snapshot[0]
    )


def configure_provider_routing(home, provider, url, *, probe=None, action=None):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    url = validate_proxy_url(url)
    paths = setup_paths(home, "darwin" if sys.platform == "darwin" else "linux")
    client_key = managed_key(paths, "client_key")
    path = client_config_path(home, provider)
    snapshot = client_config_snapshot(path)
    prior_snapshot = snapshot
    routing_state = load_client_routing_state(home)
    if provider in routing_state["providers"]:
        if snapshot is None:
            raise fleet.FleetError(f"{provider.title()} client routing changed after Fleet wrote it. Restore stopped.")
        prior_snapshot = restored_client_snapshot(provider, snapshot, routing_state["providers"][provider])
        current = b"" if prior_snapshot is None else prior_snapshot[0]
    else:
        current = b"" if snapshot is None else snapshot[0]
    rendered = (claude_routing_config if provider == "claude" else codex_routing_config)(current, url, paths["client_key"])
    wrote = False
    rollback_action = None
    try:
        replace_snapshot(
            home,
            path,
            snapshot,
            (rendered, 0o600),
            client_config_snapshot,
            "Client config changed during routing update.",
        )
        wrote = True
        if not provider_routing_valid(home, provider, url):
            raise fleet.FleetError(f"Could not configure {provider} client routing.")
        if probe is None:
            probe_inference_endpoint(url, client_key)
        else:
            probe(url)
        if action is not None:
            rollback_action = action()
        save_client_routing_state(home, provider, prior_snapshot, rendered)
    except BaseException as error:
        if not wrote:
            raise
        rollback_failed = False
        if rollback_action is not None:
            try:
                rollback_action()
            except BaseException:
                rollback_failed = True
        try:
            replace_snapshot(
                home,
                path,
                (rendered, 0o600),
                snapshot,
                client_config_snapshot,
                "Client config changed during rollback.",
            )
        except BaseException:
            rollback_failed = True
        if rollback_failed:
            raise fleet.FleetError(f"{error} Account state or client config restore also failed.") from None
        raise


def verify_account_files(home):
    paths = setup_paths(home, "darwin" if sys.platform == "darwin" else "linux")
    try:
        details = os.lstat(paths["auth"])
    except OSError:
        raise fleet.FleetError("CLIProxyAPI auth directory is missing or unsafe.") from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise fleet.FleetError("CLIProxyAPI auth directory must use mode 0700.")
    state = load_state(home)
    for policy in state["providers"].values():
        for name in policy["accounts"]:
            if not regular_private_file(paths["auth"] / name, 0o600):
                raise fleet.FleetError(f"Enrolled CLIProxyAPI credential is missing or unsafe: {name}.")
    return state


def verify_managed_config(paths):
    client_key = managed_key(paths, "client_key")
    management_key = managed_key(paths, "management_key")
    if not valid_setup_config(paths, client_key, management_key):
        raise fleet.FleetError("CLIProxyAPI config does not match Fleet setup.")
    return client_key, management_key


def managed_runtime_config(paths, config):
    client_key, _ = verify_managed_config(paths)
    spoke = os.path.lexists(paths["hub"]) and load_hub_state(paths["hub"].parents[3])["role"] == "spoke"
    if spoke:
        client_key = managed_key(paths, "hub_client_key")
    config.setdefault("host", "127.0.0.1")
    if config.setdefault("auth-dir", str(paths["auth"])) != str(paths["auth"]) and not spoke:
        raise fleet.FleetError("CLIProxyAPI auth directory does not match Fleet setup.")
    config.setdefault("remote-management", {"allow-remote": False})
    if isinstance(config.get("routing"), dict):
        config["routing"].setdefault("session-affinity", True)
    return client_key


def probe_setup(home):
    client = ManagementClient(home=home)
    config = client.config()
    paths = setup_paths(home, "darwin" if sys.platform == "darwin" else "linux", managed_setup_version(home))
    client_key = managed_runtime_config(paths, config)
    require_safe_failover_config(config, client_key)
    probe_inference_endpoint(client.url, client_key)


def check_setup(home, *, platform_name=None, machine=None, native_runner=None, probe=None, shell=None):
    platform_name = sys.platform if platform_name is None else platform_name
    platform_name = "linux" if platform_name.startswith("linux") else platform_name
    try:
        release_asset(platform_name, machine)
        if not setup_state_path(home).exists():
            raise fleet.FleetError("CLIProxyAPI is not set up. Run fleet account setup.")
        setup = load_setup_state(home, allow_newer=True)
        paths = setup_paths(home, platform_name, setup["version"])
        client_key, management_key = verify_managed_config(paths)
        if setup["version"] != SETUP_VERSION:
            suffix = ".dylib" if platform_name == "darwin" else ".so"
            plugin = paths["binary"].parent / f"plugins/fleet-provider-router{suffix}"
            if not regular_private_file(plugin, 0o700) or setup_plugin_config(paths) not in paths["config"].read_bytes():
                raise fleet.FleetError("CLIProxyAPI provider router does not match Fleet setup.")
        hub = load_hub_state(home)
        hub_url = hub["url"] if hub and hub["role"] == "spoke" else None
        if not regular_private_file(paths["service"], 0o600) or paths["service"].read_bytes() != setup_service(home, platform_name, setup["version"], hub_url):
            raise fleet.FleetError("CLIProxyAPI user service does not match Fleet setup.")
        verify_setup_binary(home, paths["binary"], setup["version"])
        if not valid_setup_bridge(paths["bridge"]):
            raise fleet.FleetError("Fleet retry bridge does not match Fleet setup.")
        if not native_setup_status(home, platform_name, native_runner):
            raise fleet.FleetError("CLIProxyAPI user service is not active.")
        if shell is not None and not check_shell_hook(home, shell):
            raise fleet.FleetError("Fleet native auth shell hook is missing. Run fleet account setup.")
        (probe_setup if probe is None else probe)(home)
        state = load_state(home) if hub_url else verify_account_files(home)
        if hub is not None:
            print(f"Fleet hub: {hub['role']} for {hub['url']} ({hub['login']}).")
        routing_state = load_client_routing_state(home)
        for provider in PROVIDERS:
            accounts = state["providers"][provider]["accounts"]
            routed = provider in routing_state["providers"]
            if accounts or routed:
                if not provider_routing_valid(home, provider, "http://127.0.0.1:8317"):
                    raise fleet.FleetError(f"{provider.title()} is enrolled but its client is not routed through CLIProxyAPI.")
                record = routing_state["providers"].get(provider)
                current = client_config_snapshot(client_config_path(home, provider))
                if record is None or current is None:
                    raise fleet.FleetError(f"{provider.title()} client routing restore state is missing.")
                restored_client_snapshot(provider, current, record)
                if not accounts:
                    command = "claude auth login" if provider == "claude" else "codex login"
                    print(f"{provider.title()} has no logged-in accounts. Next: {command}.")
            else:
                if provider == "codex" and codex_fleet_routing_present(home):
                    raise fleet.FleetError("Codex client routing is present without Fleet restore state.")
                command = "claude auth login" if provider == "claude" else "codex login"
                print(
                    f"{provider.title()} is not routed. Next: "
                    f"{command}."
                )
    except fleet.FleetError as error:
        print(error, file=sys.stderr)
        return False
    return True


def state_path(home):
    return fleet.safe_target(home, ".local/state/fleet/accounts.json")


def default_state():
    return {
        "version": 1,
        "providers": {
            provider: {"accounts": {}, "primary": None, "auto": None}
            for provider in PROVIDERS
        },
    }


def valid_label(label):
    return isinstance(label, str) and label == label.strip() and 0 < len(label) <= 80 and label.isprintable()


def valid_state(state):
    if not isinstance(state, dict) or set(state) != {"version", "providers"} or type(state["version"]) is not int or state["version"] != 1:
        return False
    if not isinstance(state["providers"], dict) or set(state["providers"]) != set(PROVIDERS):
        return False
    for provider in PROVIDERS:
        policy = state["providers"][provider]
        if not isinstance(policy, dict) or set(policy) - {"order", "rebalance", "preferred", "override"} != {"accounts", "primary", "auto"}:
            return False
        if "rebalance" in policy and (policy["rebalance"] not in ("reset", "reset-fallbacks") or "order" not in policy):
            return False
        accounts = policy["accounts"]
        if not isinstance(accounts, dict) or any(
            not isinstance(account_id, str)
            or not (match := ACCOUNT_FILE.fullmatch(account_id))
            or match.group(1) != provider
            or not valid_label(label)
            for account_id, label in accounts.items()
        ):
            return False
        if policy["primary"] is not None and policy["primary"] not in accounts:
            return False
        for field in ("preferred", "override"):
            if field in policy and (not isinstance(policy[field], str) or policy[field] not in accounts):
                return False
        if policy["auto"] is not None and type(policy["auto"]) is not bool:
            return False
        if "order" in policy:
            order = policy["order"]
            if (
                not isinstance(order, list)
                or any(not isinstance(name, str) for name in order)
                or len(order) != len(accounts)
                or set(order) != set(accounts)
                or (order[0] if order else None) != policy["primary"]
            ):
                return False
    return True


def load_state(home):
    path = state_path(home)
    try:
        details = os.lstat(path)
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return default_state()
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid Fleet account state.") from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
        raise fleet.FleetError("Invalid Fleet account state.")
    if not valid_state(state):
        raise fleet.FleetError("Invalid Fleet account state.")
    return state


def write_state(home, state):
    try:
        fleet.atomic_write(home, state_path(home), (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    except (OSError, fleet.FleetError):
        raise fleet.FleetError("Could not save Fleet account state.") from None


def write_state_transaction(home, state, expected):
    replacement = ((json.dumps(state, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    path = state_path(home)
    try:
        current = client_config_snapshot(path)
    except fleet.FleetError:
        raise fleet.FleetError("Could not save Fleet account state.") from None
    if current != expected:
        raise fleet.FleetError("Fleet account state changed during update.")
    try:
        fleet.atomic_write(home, path, replacement[0], replacement[1])
    except (OSError, fleet.FleetError):
        raise fleet.FleetError("Could not save Fleet account state.") from None

    def rollback():
        replace_snapshot(
            home,
            path,
            replacement,
            expected,
            client_config_snapshot,
            "Fleet account state changed during rollback.",
        )

    return rollback


def write_account_routing_transaction(home, routing, expected):
    path = account_routing_path(home)
    replacement = (account_routing_bytes(routing), 0o600)
    if client_config_snapshot(path) != expected:
        raise fleet.FleetError("Fleet account routing policy changed during update.")
    try:
        fleet.atomic_write(home, path, replacement[0], replacement[1])
    except (OSError, fleet.FleetError):
        raise fleet.FleetError("Could not save Fleet account routing policy.") from None

    def rollback():
        replace_snapshot(
            home,
            path,
            replacement,
            expected,
            client_config_snapshot,
            "Fleet account routing policy changed during rollback.",
        )

    return rollback


def write_policy_transaction(home, provider, state, expected):
    routing_snapshot = client_config_snapshot(account_routing_path(home))
    routing = load_account_routing(home, allow_missing=True)
    previous = dict(routing.get("selection", {}))
    selection = dict(previous)
    policy = state["providers"][provider]
    if policy["accounts"] and (policy.get("rebalance") == "reset" or policy.get("override")):
        selection[provider] = "priority"
    else:
        selection.pop(provider, None)
    if selection:
        routing["selection"] = selection
    else:
        routing.pop("selection", None)
    state_rollback = write_state_transaction(home, state, expected)
    routing_rollback = None
    try:
        if selection != previous:
            routing_rollback = write_account_routing_transaction(home, routing, routing_snapshot)
    except BaseException:
        state_rollback()
        raise

    def rollback():
        try:
            if routing_rollback:
                routing_rollback()
        finally:
            state_rollback()

    return rollback


class ManagementClient:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):
            return None

    def __init__(self, opener=None, home=None):
        self.url = validate_proxy_url(os.environ.get("FLEET_CLIPROXY_URL", "http://127.0.0.1:8317"))
        self.open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), self.NoRedirect()).open
        self.key = os.environ.get("FLEET_CLIPROXY_MANAGEMENT_KEY", "")
        if not self.key:
            try:
                platform_name = "darwin" if sys.platform == "darwin" else "linux"
                paths = setup_paths(account_home() if home is None else Path(home), platform_name)
                self.key = managed_key(paths, "hub_management_key" if is_spoke(paths["hub"].parents[3]) else "management_key")
            except fleet.FleetError as error:
                if os.path.lexists(setup_paths(account_home() if home is None else Path(home), platform_name)["management_key"]):
                    raise error
                self.key = ""
        if not self.key:
            raise fleet.FleetError(MANAGEMENT_HELP)
        if self.key != self.key.strip() or not self.key.isprintable():
            raise fleet.FleetError("FLEET_CLIPROXY_MANAGEMENT_KEY must be one non-empty printable line.")

    def request(self, method, path, value=None, raw=None):
        data = raw if raw is not None else None if value is None else json.dumps(value, separators=(",", ":")).encode()
        try:
            request = urllib.request.Request(
                self.url + "/v0/management/" + path,
                data=data,
                method=method,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            )
            with self.open(request, timeout=5) as response:
                if not supported_setup_version(response.headers.get("X-CPA-VERSION")):
                    raise fleet.FleetError(f"Fleet account management requires CLIProxyAPI {SETUP_VERSION}.")
                raw = response.read(4 * 1024 * 1024 + 1)
        except fleet.FleetError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError):
            raise fleet.FleetError(MANAGEMENT_HELP) from None
        if len(raw) > 4 * 1024 * 1024:
            raise fleet.FleetError("CLIProxyAPI management response is too large.")
        try:
            result = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            raise fleet.FleetError("Invalid CLIProxyAPI management response.") from None
        if not isinstance(result, dict):
            raise fleet.FleetError("Invalid CLIProxyAPI management response.")
        return result

    def config(self):
        return self.request("GET", "config")

    def accounts(self):
        result = self.request("GET", "auth-files")
        entries = result.get("files")
        if not isinstance(entries, list):
            raise fleet.FleetError("Invalid CLIProxyAPI account list.")
        accounts = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise fleet.FleetError("Invalid CLIProxyAPI account list.")
            name = entry.get("name")
            provider = entry.get("provider")
            if provider not in PROVIDERS or not isinstance(name, str):
                continue
            match = ACCOUNT_FILE.fullmatch(name)
            if not match or match.group(1) != provider or type(entry.get("disabled")) is not bool:
                raise fleet.FleetError("Invalid CLIProxyAPI account list.")
            accounts.append(entry)
        return sorted(accounts, key=lambda item: item["name"].casefold())

    def priority(self, account, value):
        self.request("PATCH", "auth-files/fields", {"name": account["name"], "priority": value})

    def websockets(self, account, value):
        self.request("PATCH", "auth-files/fields", {"name": account["name"], "websockets": value})

    def api_call(self, account, url, headers):
        result = self.request("POST", "api-call", {"authIndex": account["auth_index"], "method": "GET", "url": url, "header": headers})
        body = result.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (UnicodeError, json.JSONDecodeError):
                body = None
        if result.get("status_code") != 200 or not isinstance(body, dict):
            raise fleet.FleetError(f"Provider usage lookup failed for {account['name']}.")
        return body

    def disabled(self, account, value):
        body = {"name": account["name"], "disabled": value}
        if isinstance(account.get("auth_index"), str) and account["auth_index"]:
            body["auth_index"] = account["auth_index"]
        self.request("PATCH", "auth-files/status", body)

    def delete(self, account):
        result = self.request("DELETE", "auth-files", {"names": [account["name"]]})
        if result.get("status") != "ok":
            raise fleet.FleetError("CLIProxyAPI did not remove the account.")

    def upload(self, name, contents):
        result = self.request("POST", "auth-files?name=" + urllib.parse.quote(name), raw=contents)
        if result.get("status") != "ok":
            raise fleet.FleetError("CLIProxyAPI did not save the native account.")

    def error_rules(self, provider, rules):
        if rules is None:
            self.request("DELETE", "oauth-request-scoped-errors?provider=" + urllib.parse.quote(provider))
        else:
            self.request("PATCH", "oauth-request-scoped-errors", {"provider": provider, "rules": rules})


def accounts_for(entries, provider):
    return [entry for entry in entries if entry["provider"] == provider]


def enrolled_accounts(policy, entries):
    enrolled = [entry for entry in entries if entry["name"] in policy["accounts"]]
    if "order" in policy:
        rank = {name: index for index, name in enumerate(policy["order"])}
        enrolled.sort(key=lambda entry: rank[entry["name"]])
    return enrolled


def require_enrolled_provider(policy, provider, entries):
    missing = sorted(entry["name"] for entry in accounts_for(entries, provider) if entry["name"] not in policy["accounts"])
    if missing:
        raise fleet.FleetError(
            f"Unenrolled {provider} accounts: {', '.join(missing)}. "
            f"Run fleet account enroll {provider} <account> for each account."
        )


def account_label(state, entry):
    saved = state["providers"][entry["provider"]]["accounts"].get(entry["name"])
    if saved:
        return saved
    for key in ("label", "email"):
        if valid_label(entry.get(key)):
            return entry[key]
    return ACCOUNT_FILE.fullmatch(entry["name"]).group(2)


def provider_error_rules(config, provider):
    configured = config.get("oauth-request-scoped-errors")
    if configured is None:
        return None
    if not isinstance(configured, dict):
        raise fleet.FleetError("Invalid CLIProxyAPI account error policy.")
    rules = configured.get(provider)
    if rules is not None and not isinstance(rules, list):
        raise fleet.FleetError("Invalid CLIProxyAPI account error policy.")
    return rules


def set_provider_error_rules(client, provider, previous, desired):
    if previous == desired:
        return lambda: None
    if provider_error_rules(client.config(), provider) != previous:
        raise fleet.FleetError("CLIProxyAPI account error policy changed during update.")
    client.error_rules(provider, desired)
    if provider_error_rules(client.config(), provider) != desired:
        raise fleet.FleetError("CLIProxyAPI account error policy update failed.")

    def rollback():
        current = provider_error_rules(client.config(), provider)
        if current == previous:
            return
        if current != desired:
            raise fleet.FleetError("CLIProxyAPI account error policy changed during rollback.")
        client.error_rules(provider, previous)
        if provider_error_rules(client.config(), provider) != previous:
            raise fleet.FleetError("CLIProxyAPI account error policy rollback failed.")

    return rollback


def latest_successful_accounts(entries):
    latest = None
    current = []
    for entry in entries:
        recent = entry.get("recent_requests")
        if not isinstance(recent, list):
            continue
        for distance, bucket in enumerate(reversed(recent)):
            if not isinstance(bucket, dict) or type(bucket.get("success")) is not int or bucket["success"] <= 0 or not isinstance(bucket.get("time"), str):
                continue
            if latest is None or distance < latest:
                latest = distance
                current = [(entry, bucket["time"])]
            elif distance == latest:
                current.append((entry, bucket["time"]))
            break
    return current


def list_accounts(home, provider=None):
    if provider is not None and provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    state = load_state(home)
    client = ManagementClient()
    entries = client.accounts()
    routing = load_account_routing(home, allow_missing=True)
    ranks = {name: rank for policy in state["providers"].values() for rank, name in enumerate(policy.get("order", []), 1)}
    entries.sort(key=lambda entry: (
        PROVIDERS.index(entry["provider"]),
        ranks.get(entry["name"], float("inf")),
        entry["name"] != state["providers"][entry["provider"]]["primary"],
        account_label(state, entry).casefold(),
    ))
    current = {
        entry["name"]
        for selected_provider in PROVIDERS
        for entry, _time in latest_successful_accounts(accounts_for(entries, selected_provider))
    }
    rows = []
    for entry in entries:
        if provider is not None and entry["provider"] != provider:
            continue
        policy = state["providers"][entry["provider"]]
        if policy["primary"] is None:
            order = "—"
        else:
            role = "Main" if policy["primary"] == entry["name"] else "Fallback"
            order = f"{ranks.get(entry['name'], '—')} · {role}"
        auto = "unconfigured" if policy["auto"] is None else (
            ("limits" if routing["providers"][entry["provider"]] == "limits" else "limits+errors")
            if policy["auto"] else "off"
        )
        if policy.get("rebalance") == "reset":
            auto += " · reset order"
        elif policy.get("rebalance") == "reset-fallbacks":
            auto += " · fallback reset"
        if policy.get("override") == entry["name"]:
            order += " · Override"
        if policy.get("preferred") == entry["name"] and policy["preferred"] != policy["primary"]:
            order += " · Preferred"
        try:
            cells = {label: f"{used:.0f}% · {format_reset(reset)}" for label, used, reset in account_usage(client, entry)}
        except fleet.FleetError:
            cells = {}
        rows.append((entry["provider"], account_label(state, entry), order, auto, cells.get("5-hour", "—"), cells.get("weekly", "—"), "✓" if entry["name"] in current else "—"))
    table = [("PROVIDER", "ACCOUNT", "ORDER", "AUTO", "5H USED · RESET", "WEEK USED · RESET", "CURRENT"), *rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(table[0]) - 1)]
    for row in table:
        print("  ".join(value.ljust(widths[index]) if index < len(widths) else value for index, value in enumerate(row)))


def safe_auth_dir(home, config):
    configured = config.get("auth-dir")
    if not isinstance(configured, str) or not configured.strip():
        raise fleet.FleetError("CLIProxyAPI management config does not expose auth-dir.")
    raw = configured.strip()
    if raw == "~":
        directory = home
    elif raw.startswith("~/"):
        directory = home / raw[2:]
    else:
        directory = Path(raw)
    if not directory.is_absolute():
        raise fleet.FleetError("CLIProxyAPI auth-dir must be absolute or start with ~.")
    try:
        resolved_home = home.resolve(strict=True)
        details = os.lstat(directory)
        resolved = directory.resolve(strict=True)
    except OSError:
        raise fleet.FleetError("CLIProxyAPI auth directory is missing or unsafe.") from None
    if resolved == resolved_home or resolved_home not in resolved.parents:
        raise fleet.FleetError("CLIProxyAPI auth-dir must be inside the account home.")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise fleet.FleetError("CLIProxyAPI auth directory is missing or unsafe.")
    return resolved


def secure_credentials(home, config, entries):
    if is_spoke(home):
        return
    directory = safe_auth_dir(home, config)
    try:
        directory.chmod(0o700)
        for entry in entries:
            if entry.get("runtime_only") or entry.get("source") != "file":
                continue
            path = directory / entry["name"]
            details = os.lstat(path)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.getuid()
                or path.resolve(strict=True).parent != directory
            ):
                raise fleet.FleetError("Unsafe CLIProxyAPI credential file.")
            path.chmod(0o600)
    except fleet.FleetError:
        raise
    except OSError:
        raise fleet.FleetError("Could not secure CLIProxyAPI credentials.") from None


def require_safe_failover_config(config, client_key):
    routing = config.get("routing")
    streaming = config.get("streaming")
    management = config.get("remote-management")
    checks = (
        (config.get("host") in {"127.0.0.1", "::1"}, "host: 127.0.0.1 or ::1"),
        (config.get("api-keys") == [client_key], "api-keys: managed client key"),
        (isinstance(management, dict) and management.get("allow-remote") is False, "remote-management.allow-remote: false"),
        (isinstance(routing, dict) and routing.get("strategy") == "fill-first", "routing.strategy: fill-first"),
        (isinstance(routing, dict) and routing.get("session-affinity") is True, "routing.session-affinity: true"),
        (config.get("max-retry-credentials") == 0, "max-retry-credentials: 0"),
        (config.get("request-retry") == 0, "request-retry: 0"),
        (isinstance(streaming, dict) and streaming.get("bootstrap-retries", 0) == 0, "streaming.bootstrap-retries: 0"),
        (
            isinstance(config.get("codex"), dict) and config["codex"].get("stream-bootstrap-buffering") is True,
            "codex.stream-bootstrap-buffering: true",
        ),
    )
    missing = [setting for valid, setting in checks if not valid]
    if missing:
        raise fleet.FleetError("Safe account changes require " + ", ".join(missing) + " in CLIProxyAPI config.")


def live_primary(policy, entries, provider):
    primary = policy["primary"]
    if primary is None:
        raise fleet.FleetError(f"Select a primary {provider} account first.")
    for entry in entries:
        if entry["name"] == primary:
            return entry
    raise fleet.FleetError(f"The selected primary is no longer available for {provider}.")


def apply_transaction(client, operations, save):
    attempted = []
    try:
        for kind, account, value, previous in operations:
            if value == previous:
                continue
            verify_operations(
                client,
                [(kind, account, previous, None)],
                "CLIProxyAPI account changed during update.",
            )
            attempted.append((kind, account, value, previous))
            getattr(client, kind)(account, value)
        verify_operations(client, operations)
        save()
    except BaseException as error:
        rollback_failed = False
        rollback = []
        for kind, account, value, previous in reversed(attempted):
            try:
                try:
                    verify_operations(client, [(kind, account, value, None)])
                except fleet.FleetError:
                    verify_operations(
                        client,
                        [(kind, account, previous, None)],
                        "CLIProxyAPI account changed during rollback.",
                    )
                    continue
                getattr(client, kind)(account, previous)
                rollback.append((kind, account, previous, None))
            except BaseException:
                rollback_failed = True
        try:
            verify_operations(client, rollback)
        except BaseException:
            rollback_failed = True
        if rollback_failed:
            raise fleet.FleetError("Account update failed and rollback failed; inspect account status in the CLIProxyAPI management panel.") from None
        if isinstance(error, fleet.FleetError):
            raise fleet.FleetError(f"{error} CLIProxyAPI changes were rolled back.") from None
        raise


def verify_operations(client, operations, message="CLIProxyAPI account update postcondition failed."):
    expected = {(account["name"], kind): value for kind, account, value, _ in operations}
    if not expected:
        return
    live = {account["name"]: account for account in client.accounts()}
    for (name, kind), value in expected.items():
        if name not in live or live[name].get(kind) != value:
            raise fleet.FleetError(message)


def priority_operations(entries, primary, order=None):
    priorities = {name: len(order) - index for index, name in enumerate(order)} if order is not None else {primary: 100}
    ordered = sorted(entries, key=lambda entry: -priorities.get(entry["name"], 0))
    return [("priority", entry, priorities.get(entry["name"], 0), entry.get("priority")) for entry in ordered] + websocket_operations(entries)


def websocket_operations(entries):
    return [("websockets", entry, True, entry.get("websockets")) for entry in entries if entry["provider"] == "codex"]


def resolve_account(state, provider, entries, wanted):
    matches = [
        entry
        for entry in entries
        if wanted.casefold() in {entry["name"].casefold(), account_label(state, entry).casefold()}
    ]
    if len(matches) != 1:
        raise fleet.FleetError("Account was not found or is ambiguous.")
    return matches[0]


def runtime_for_mutation(home):
    client = ManagementClient()
    config = client.config()
    platform_name = "darwin" if sys.platform == "darwin" else "linux"
    state = setup_state_path(home)
    if os.path.lexists(state):
        paths = setup_paths(home, platform_name, managed_setup_version(home))
        client_key = managed_runtime_config(paths, config)
    else:
        paths = setup_paths(home, platform_name)
        client_key = managed_key(paths, "client_key")
    require_safe_failover_config(config, client_key)
    if not is_spoke(home):
        safe_auth_dir(home, config)
    entries = client.accounts()
    return client, config, entries


def use_account(home, provider, wanted, *, order=None, keep_rebalance=False, override=None, rebalance_mode=None):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    state_before = client_config_snapshot(state_path(home))
    state = load_state(home)
    client, config, all_entries = runtime_for_mutation(home)
    policy = state["providers"][provider]
    require_enrolled_provider(policy, provider, all_entries)
    entries = enrolled_accounts(policy, accounts_for(all_entries, provider))
    try:
        selected = resolve_account(state, provider, entries, wanted)
    except fleet.FleetError:
        raise fleet.FleetError("Account is not enrolled. Run fleet account enroll first.") from None
    previous_primary = policy["primary"]
    if keep_rebalance or override is not None:
        policy.setdefault("preferred", previous_primary or selected["name"])
    else:
        policy["preferred"] = selected["name"]
    if override is True:
        policy["override"] = selected["name"]
    elif override is False or (order is not None and not keep_rebalance):
        policy.pop("override", None)
    elif not keep_rebalance and "override" in policy:
        policy["override"] = selected["name"]
    if rebalance_mode is not None:
        policy["rebalance"] = rebalance_mode
    if order is not None:
        names = [resolve_account(state, provider, entries, name)["name"] for name in order]
        if len(names) != len(policy["accounts"]) or set(names) != set(policy["accounts"]):
            raise fleet.FleetError("Include every enrolled account exactly once, starting with the primary.")
        policy["order"] = names
        if not keep_rebalance:
            policy.pop("rebalance", None)
    elif "order" in policy:
        policy["order"] = [selected["name"], *[name for name in policy["order"] if name != selected["name"]]]
    policy["primary"] = selected["name"]
    if policy["auto"] is None:
        policy["auto"] = False
    operations = [("disabled", selected, False, selected["disabled"])]
    operations.extend(priority_operations(entries, selected["name"], policy.get("order")))
    if policy["auto"] is False:
        operations.extend(("disabled", entry, True, entry["disabled"]) for entry in entries if entry["name"] != selected["name"])
    reset_affinity = policy["auto"] and (
        not keep_rebalance or override is not None
        or (policy.get("rebalance") == "reset" and previous_primary != selected["name"])
    )
    if reset_affinity and config.get("fleet-session-affinity-supported") is not True:
        raise fleet.FleetError("Update the Fleet proxy before selecting an account with session affinity.")
    if (policy.get("rebalance") == "reset" or policy.get("override")) and config.get("fleet-priority-selection-supported") is not True:
        raise fleet.FleetError("Update the Fleet proxy before using reset order or an account override.")
    secure_credentials(home, config, entries)

    def save():
        rollback = write_policy_transaction(home, provider, state, state_before)
        try:
            if reset_affinity:
                client.request("POST", "routing/session-affinity/reset", {"provider": provider})
        except BaseException:
            rollback()
            raise

    apply_transaction(client, operations, save)
    if order is not None:
        show_account_order(home, provider)
    if policy["auto"]:
        sessions = "existing sessions rebind on their next request" if reset_affinity else "existing sessions keep their available account"
        print(f"Primary {provider.title()} account: {account_label(state, selected)}. Auto switching is on; new sessions try it first and {sessions}.")
    else:
        print(
            f"Primary {provider.title()} account: {account_label(state, selected)}. Auto switching is off; other accounts are unavailable, "
            f"so an existing {provider.title()} session can rebind on its next request."
        )


def pick_account(home, provider):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    state = load_state(home)
    policy = state["providers"][provider]
    entries = enrolled_accounts(policy, accounts_for(ManagementClient().accounts(), provider))
    if not entries:
        raise fleet.FleetError(f"No enrolled {provider} accounts found.")
    for index, entry in enumerate(entries, 1):
        print(f"{index}) {account_label(state, entry)}")
    try:
        selected = int(input(f"Select {provider} account: "))
        if selected < 1:
            raise ValueError
        account = entries[selected - 1]
    except (EOFError, ValueError, IndexError):
        raise fleet.FleetError("Invalid account selection.") from None
    use_account(home, provider, account["name"])


def show_account_order(home, provider):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    policy = load_state(home)["providers"][provider]
    if not policy.get("order"):
        print(f"No fixed {provider} account order. Set one with fleet account order {provider} <primary> <fallback> ...")
    else:
        suffix = {"reset": " (auto: soonest reset first)", "reset-fallbacks": " (auto: primary first, fallbacks by soonest reset)"}.get(policy.get("rebalance"), "")
        print(f"{provider.title()} account order: " + " -> ".join(policy["accounts"][name] for name in policy["order"]) + suffix)
    if policy.get("preferred"):
        print(f"Preferred {provider.title()} account: {policy['accounts'][policy['preferred']]}.")
    if policy.get("override"):
        print(f"{provider.title()} override: {policy['accounts'][policy['override']]}. Clear with fleet account override {provider} off.")


def override_account(home, provider, wanted):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    policy = load_state(home)["providers"][provider]
    if wanted == "off":
        if "override" not in policy:
            print(f"{provider.title()} override is already off.")
            return
        client = ManagementClient()
        entries = enrolled_accounts(policy, accounts_for(client.accounts(), provider))
        if {entry["name"] for entry in entries} != set(policy["accounts"]):
            raise fleet.FleetError(f"Enrolled {provider} accounts changed; run fleet account list.")
        order = reset_policy_order(policy | {"override": None}, entries, client)
        use_account(home, provider, order[0], order=order, keep_rebalance=True, override=False)
    else:
        use_account(home, provider, wanted, keep_rebalance=True, override=True)
    print(f"{provider.title()} override: {wanted}.")



def credit_account(home, provider, mode):
    if provider not in PROVIDERS or mode not in CREDIT_MODES:
        raise fleet.FleetError("Usage: fleet account credits <claude|codex> <never|last|same-account>")
    if is_spoke(home):
        raise fleet.FleetError("Run credit changes on the Fleet hub.")
    if ManagementClient().config().get("fleet-credit-routing-supported") is not True:
        raise fleet.FleetError("The account router needs the Fleet credit routing patch before credits can be configured.")
    snapshot = client_config_snapshot(account_routing_path(home))
    routing = load_account_routing(home, allow_missing=True)
    routing.setdefault("credits", {})[provider] = mode
    write_account_routing_transaction(home, routing, snapshot)
    descriptions = {
        "never": "switch at either subscription limit; stop when all allowances are used",
        "last": "use all account allowances first, then available credits",
        "same-account": "use allowance and credits on each account before switching",
    }
    print(f"{provider.title()} credits: {mode}; {descriptions[mode]}. Auto switching and account order are unchanged.")


def auto_account(home, provider, enabled, mode=None):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    if mode is not None and mode not in ROUTING_MODES:
        raise fleet.FleetError("Valid --when options: limits, any.")
    state_snapshot = client_config_snapshot(state_path(home))
    routing_snapshot = client_config_snapshot(account_routing_path(home))
    state = load_state(home)
    routing = load_account_routing(home, allow_missing=True)
    client, config, all_entries = runtime_for_mutation(home)
    policy = state["providers"][provider]
    require_enrolled_provider(policy, provider, all_entries)
    entries = enrolled_accounts(policy, accounts_for(all_entries, provider))
    primary = live_primary(policy, entries, provider)
    if enabled:
        operations = []
        for entry in sorted(entries, key=lambda item: item["name"] != primary["name"]):
            operations.append(("disabled", entry, False, entry["disabled"]))
        operations.extend(priority_operations(entries, primary["name"], policy.get("order")))
    else:
        operations = [("disabled", primary, False, primary["disabled"])]
        operations.extend(priority_operations(entries, primary["name"], policy.get("order")))
        operations.extend(("disabled", entry, True, entry["disabled"]) for entry in entries if entry["name"] != primary["name"])
    secure_credentials(home, config, entries)
    policy["auto"] = enabled
    if enabled:
        routing["providers"][provider] = mode or "limits"
    previous_rules = provider_error_rules(config, provider)
    desired_rules = limit_error_rules(provider) if routing["providers"][provider] == "limits" else None
    rules_rollback = set_provider_error_rules(client, provider, previous_rules, desired_rules)

    def save():
        state_rollback = write_state_transaction(home, state, state_snapshot)
        try:
            write_account_routing_transaction(home, routing, routing_snapshot)
        except BaseException:
            state_rollback()
            raise

    try:
        apply_transaction(client, operations, save)
    except BaseException as error:
        try:
            rules_rollback()
        except BaseException:
            raise fleet.FleetError(f"{error} Account error policy rollback failed.") from None
        raise
    status = "on (limits only)" if enabled and routing["providers"][provider] == "limits" else "on (limits + temporary errors)" if enabled else "off"
    if enabled:
        message = f"{provider.title()} auto switching: {status}. New sessions try the primary first; existing sessions keep their available account and can fall back before output."
        if routing["providers"][provider] == "any":
            message += " Temporary provider errors can spend fallback quota."
        print(message)
    else:
        print(f"{provider.title()} auto switching: {status}; only the primary account is available.")


USAGE_URLS = {
    "claude": ("https://api.anthropic.com/api/oauth/usage", {"Authorization": "Bearer $TOKEN$", "anthropic-beta": "oauth-2025-04-20"}),
    "codex": ("https://chatgpt.com/backend-api/wham/usage", {"Authorization": "Bearer $TOKEN$"}),
}


def account_usage(client, entry):
    url, headers = USAGE_URLS[entry["provider"]]
    body = client.api_call(entry, url, headers)
    windows = []
    if entry["provider"] == "codex":
        limit = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
        for label, key in (("weekly", "primary_window"), ("5-hour", "secondary_window")):
            window = limit.get(key)
            if isinstance(window, dict) and type(window.get("reset_at")) in {int, float} and type(window.get("used_percent")) in {int, float}:
                duration = window.get("limit_window_seconds")
                if type(duration) in {int, float}:
                    label = "5-hour" if duration <= 18000 else "weekly"
                windows.append((label, float(window["used_percent"]), float(window["reset_at"])))
    else:
        for label, key in (("5-hour", "five_hour"), ("weekly", "seven_day"), ("Fable weekly", "seven_day_fable")):
            window = body.get(key)
            if not isinstance(window, dict) or type(window.get("utilization")) not in {int, float} or not isinstance(window.get("resets_at"), str):
                continue
            try:
                reset = datetime.datetime.fromisoformat(window["resets_at"]).timestamp()
            except ValueError:
                continue
            windows.append((label, float(window["utilization"]), reset))
        limits = body.get("limits")
        for window in limits if isinstance(limits, list) else []:
            if not isinstance(window, dict):
                continue
            label = None
            if window.get("kind") == "session":
                label = "5-hour"
            elif window.get("kind") == "weekly_all":
                label = "weekly"
            elif window.get("kind") == "weekly_scoped":
                scope = window.get("scope")
                model = scope.get("model") if isinstance(scope, dict) else None
                if isinstance(model, dict) and (model.get("display_name") == "Fable" or "fable" in str(model.get("id", "")).lower()):
                    label = "Fable weekly"
            if label is None or type(window.get("percent")) not in {int, float} or not isinstance(window.get("resets_at"), str):
                continue
            try:
                reset = datetime.datetime.fromisoformat(window["resets_at"]).timestamp()
            except ValueError:
                continue
            windows = [item for item in windows if item[0] != label]
            windows.append((label, float(window["percent"]), reset))
    if not windows:
        raise fleet.FleetError(f"Provider usage has no limit windows for {entry['name']}.")
    return windows


def reset_order(entries, usage, preferred=None):
    def soonest(entry):
        resets = [reset for label, _used, reset in usage[entry["name"]] if label in {"weekly", "Fable weekly"}]
        return min(resets, default=float("inf")), entry["name"] != preferred

    return [entry["name"] for entry in sorted(entries, key=soonest)]


def reset_policy_order(policy, entries, client):
    usage = {entry["name"]: account_usage(client, entry) for entry in entries}
    preferred = policy.get("preferred", policy["primary"])
    order = reset_order(entries, usage, preferred)
    first = policy.get("override") or (preferred if policy.get("rebalance") == "reset-fallbacks" else None)
    return [first, *[name for name in order if name != first]] if first else order


def rebalance_provider(home, provider):
    state = load_state(home)
    policy = state["providers"][provider]
    if policy.get("rebalance") not in {"reset", "reset-fallbacks"} or len(policy["accounts"]) < 2:
        return False
    client = ManagementClient()
    entries = enrolled_accounts(policy, accounts_for(client.accounts(), provider))
    if {entry["name"] for entry in entries} != set(policy["accounts"]):
        raise fleet.FleetError(f"Enrolled {provider} accounts changed; run fleet account list.")
    order = reset_policy_order(policy, entries, client)
    selection = load_account_routing(home, allow_missing=True).get("selection", {}).get(provider)
    expected_selection = "priority" if policy.get("override") or policy["rebalance"] == "reset" else None
    if order == policy["order"] and selection == expected_selection and all(value == previous for _, _, value, previous in priority_operations(entries, order[0], order)):
        return False
    use_account(home, provider, order[0], order=order, keep_rebalance=True)
    print(f"{provider.title()} order rebalanced by soonest reset: " + " -> ".join(policy["accounts"][name] for name in order))
    return True


def rebalance(home, provider=None):
    changed = False
    for selected in (provider,) if provider else PROVIDERS:
        if selected not in PROVIDERS:
            raise fleet.FleetError("Account provider must be claude or codex.")
        with fleet.operation_lock(home):
            changed = rebalance_provider(home, selected) or changed
    if not changed:
        print("Account order is already balanced.")


def set_rebalance(home, provider, mode):
    if provider not in PROVIDERS or mode not in {"reset", "reset-fallbacks", "manual"}:
        raise fleet.FleetError("Usage: fleet account order <claude|codex> <reset|reset-fallbacks|manual>")
    with fleet.operation_lock(home):
        state_before = client_config_snapshot(state_path(home))
        state = load_state(home)
        policy = state["providers"][provider]
        if mode != "manual":
            if len(policy["accounts"]) < 2:
                raise fleet.FleetError(f"Reset ordering needs at least two enrolled {provider} accounts.")
            client = ManagementClient()
            entries = enrolled_accounts(policy, accounts_for(client.accounts(), provider))
            if {entry["name"] for entry in entries} != set(policy["accounts"]):
                raise fleet.FleetError(f"Enrolled {provider} accounts changed; run fleet account list.")
            order = reset_policy_order(policy | {"rebalance": mode}, entries, client)
            use_account(home, provider, order[0], order=order, keep_rebalance=True, rebalance_mode=mode)
        else:
            policy.pop("rebalance", None)
            write_policy_transaction(home, provider, state, state_before)
    if mode != "manual":
        order = "primary first, then the soonest limit reset" if mode == "reset-fallbacks" else "the soonest limit reset"
        print(f"{provider.title()} order now follows {order}. Fleet rebalances after each login and every hour.")
    else:
        print(f"{provider.title()} order is manual again. Set it with fleet account order {provider} <primary> <fallback> ...")


def format_reset(seconds):
    remaining = max(0, int(seconds - time.time()))
    if remaining >= 86400:
        return f"{remaining // 86400}d {remaining % 86400 // 3600}h"
    if remaining >= 3600:
        return f"{remaining // 3600}h {remaining % 3600 // 60}m"
    return f"{remaining // 60}m"


def tailscale_binary():
    for candidate in ("/usr/bin/tailscale", "/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.access(candidate, os.X_OK):
            return candidate
    raise fleet.FleetError("Tailscale is not installed. Install it and log in first.")


def tailscale_identity():
    try:
        result = subprocess.run([tailscale_binary(), "status", "--json"], capture_output=True, text=True, timeout=15)
        status = json.loads(result.stdout)
        name = status["Self"]["DNSName"].rstrip(".")
        login = status["User"][str(status["Self"]["UserID"])]["LoginName"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        raise fleet.FleetError("Tailscale is not running or not logged in.") from None
    if not name.endswith(".ts.net") or not login:
        raise fleet.FleetError("Tailscale MagicDNS is required for the Fleet hub.")
    return name, login


def tailscale_serve(enabled):
    binary = tailscale_binary()
    command = [binary, "serve", "--bg", "--http=8317", "http://127.0.0.1:8317"] if enabled else [binary, "serve", "--http=8317", "off"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise fleet.FleetError("Tailscale serve could not be configured.") from None
    if result.returncode and not (not enabled and "does not exist" in result.stderr):
        raise fleet.FleetError("Tailscale serve could not be configured.")


def hub_setup(home):
    platform_name = "darwin" if sys.platform == "darwin" else "linux"
    paths = setup_paths(home, platform_name, managed_setup_version(home))
    verify_managed_config(paths)
    if is_spoke(home):
        raise fleet.FleetError("This host is a spoke. Run fleet account hub leave first.")
    name, login = tailscale_identity()
    url = f"http://{name}:8317"
    snapshot = artifact_snapshot(paths["hub"])
    replacement = (hub_state_bytes("hub", login, url), 0o600)
    replace_snapshot(home, paths["hub"], snapshot, replacement, artifact_snapshot, "Fleet hub state changed during setup.")
    try:
        if snapshot != replacement:
            restart_proxy_service(home, platform_name, paths["service"])
            wait_for_proxy(home)
        tailscale_serve(True)
    except BaseException as error:
        try:
            replace_snapshot(home, paths["hub"], artifact_snapshot(paths["hub"]), snapshot, artifact_snapshot, "Fleet hub state changed during restore.")
            restart_proxy_service(home, platform_name, paths["service"])
        except BaseException:
            raise fleet.FleetError(f"{error} Hub restore also failed.") from None
        raise
    print(f"Fleet hub is serving {url} to {login} on your tailnet.")
    print("On another device run:")
    print(f"  fleet account hub join {name} --ssh <user>@{name.split('.')[0]}")


def read_remote_keys(target):
    command = ["/usr/bin/ssh", "-o", "BatchMode=yes", target, "cat ~/.local/state/fleet/cliproxyapi-client-key ~/.local/state/fleet/cliproxyapi-management-key"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise fleet.FleetError("Could not read the hub keys over SSH.") from None
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 2 or any(not line or not line.isprintable() or line != line.strip() for line in lines):
        raise fleet.FleetError("Could not read the hub keys over SSH.")
    return lines[0], lines[1]


def hub_join(home, hub_name, ssh_target):
    platform_name = "darwin" if sys.platform == "darwin" else "linux"
    url = f"http://{hub_name}:8317"
    if not valid_hub_url(url):
        raise fleet.FleetError("Hub name must be a tailnet MagicDNS name like hub.tail123abc.ts.net.")
    paths = setup_paths(home, platform_name, managed_setup_version(home))
    verify_managed_config(paths)
    hub = load_hub_state(home)
    if hub is not None and hub["role"] == "hub":
        raise fleet.FleetError("This host is the hub. It cannot join itself.")
    _name, login = tailscale_identity()
    client_key, management_key = read_remote_keys(ssh_target)
    snapshots = {path: artifact_snapshot(path) for path in (paths["hub"], paths["hub_client_key"], paths["hub_management_key"], paths["service"])}
    expected = {
        paths["hub_client_key"]: ((client_key + "\n").encode(), 0o600),
        paths["hub_management_key"]: ((management_key + "\n").encode(), 0o600),
        paths["hub"]: (hub_state_bytes("spoke", login, url), 0o600),
        paths["service"]: (setup_service(home, platform_name, managed_setup_version(home), url), 0o600),
    }
    written = {}
    try:
        for path, replacement in expected.items():
            replace_snapshot(home, path, snapshots[path], replacement, artifact_snapshot, "Fleet hub file changed during join.")
            written[path] = artifact_snapshot(path)
        restart_proxy_service(home, platform_name, paths["service"])
        wait_for_proxy(home)
    except BaseException as error:
        try:
            restore_artifacts(home, snapshots, written)
            restart_proxy_service(home, platform_name, paths["service"])
        except BaseException:
            raise fleet.FleetError(f"{error} Hub join restore also failed.") from None
        raise
    print(f"This host now routes Claude and Codex through the Fleet hub at {url}.")
    print("Logins still work here: codex login and claude auth login add the account to the hub.")


def hub_leave(home):
    platform_name = "darwin" if sys.platform == "darwin" else "linux"
    hub = load_hub_state(home)
    if hub is None:
        raise fleet.FleetError("This host is not part of a Fleet hub.")
    paths = setup_paths(home, platform_name, managed_setup_version(home))
    if hub["role"] == "hub":
        tailscale_serve(False)
        replace_snapshot(home, paths["hub"], artifact_snapshot(paths["hub"]), None, artifact_snapshot, "Fleet hub state changed during leave.")
        restart_proxy_service(home, platform_name, paths["service"])
        wait_for_proxy(home)
        print("Fleet hub is off. Other devices must run fleet account hub leave.")
        return
    snapshots = {path: artifact_snapshot(path) for path in (paths["hub"], paths["hub_client_key"], paths["hub_management_key"], paths["service"])}
    expected = {
        paths["service"]: (setup_service(home, platform_name, managed_setup_version(home)), 0o600),
        paths["hub"]: None,
        paths["hub_client_key"]: None,
        paths["hub_management_key"]: None,
    }
    written = {}
    try:
        for path, replacement in expected.items():
            replace_snapshot(home, path, snapshots[path], replacement, artifact_snapshot, "Fleet hub file changed during leave.")
            written[path] = artifact_snapshot(path)
        restart_proxy_service(home, platform_name, paths["service"])
        wait_for_proxy(home)
    except BaseException as error:
        try:
            restore_artifacts(home, snapshots, written)
            restart_proxy_service(home, platform_name, paths["service"])
        except BaseException:
            raise fleet.FleetError(f"{error} Hub leave restore also failed.") from None
        raise
    print("This host uses its own local CLIProxyAPI again.")


def hub_status(home):
    hub = load_hub_state(home)
    if hub is None:
        print("hub: none (this host uses its own local CLIProxyAPI)")
        return
    print(f"hub: {hub['role']}\nurl: {hub['url']}\nowner: {hub['login']}")
    if hub["role"] == "spoke":
        paths = setup_paths(home, "darwin" if sys.platform == "darwin" else "linux", managed_setup_version(home))
        client_key = managed_key(paths, "hub_client_key")
        started = time.monotonic()
        try:
            probe_inference_endpoint("http://127.0.0.1:8317", client_key)
            print(f"reach: ok ({(time.monotonic() - started) * 1000:.0f} ms through the local relay)")
        except fleet.FleetError:
            print("reach: FAILED (hub offline or tailnet down; run fleet account hub leave to use the local proxy)")


def config_path(home):
    configured = os.environ.get("FLEET_CLIPROXY_CONFIG")
    candidates = [Path(configured).expanduser()] if configured else [
        home / ".config/fleet/cliproxyapi.yaml",
        Path("/opt/homebrew/etc/cliproxyapi.conf"),
        Path("/usr/local/etc/cliproxyapi.conf"),
        home / ".cli-proxy-api/config.yaml",
    ]
    for path in candidates:
        if not path.is_absolute():
            raise fleet.FleetError("CLIProxyAPI config path must be absolute.")
        try:
            resolved = path.resolve(strict=True)
            if stat.S_ISREG(os.stat(resolved).st_mode):
                return resolved
        except OSError:
            continue
    raise fleet.FleetError("CLIProxyAPI config is missing. Set FLEET_CLIPROXY_CONFIG to its absolute path.")


def login_auth_dir(home, path):
    try:
        with path.open(encoding="utf-8") as handle:
            raw = handle.read(1024 * 1024 + 1)
    except (OSError, UnicodeError):
        raise fleet.FleetError("Could not read the CLIProxyAPI login config.") from None
    if len(raw) > 1024 * 1024:
        raise fleet.FleetError("CLIProxyAPI login config is too large.")
    values = []
    pattern = re.compile(r'''auth-dir\s*:\s*(?:"([^"\n]+)"|'([^'\n]+)'|([^#\s]+))\s*''')
    for line in raw.splitlines():
        if line.startswith((" ", "\t")):
            continue
        match = pattern.fullmatch(line)
        if match:
            values.append(next(value for value in match.groups() if value is not None))
    if len(values) != 1:
        raise fleet.FleetError("CLIProxyAPI login config must contain one simple top-level auth-dir.")
    return safe_auth_dir(home, {"auth-dir": values[0]})


def proxy_binary():
    configured = os.environ.get("FLEET_CLIPROXYAPI")
    home = account_home()
    try:
        managed_version = managed_setup_version(home)
    except fleet.FleetError:
        managed_version = SETUP_VERSION
    managed = home / f".local/share/fleet/cliproxyapi/{managed_version}/cli-proxy-api"
    candidates = [configured] if configured else [managed, shutil.which("cliproxyapi"), shutil.which("cli-proxy-api"), "/opt/homebrew/bin/cliproxyapi", "/usr/local/bin/cliproxyapi"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            try:
                version = subprocess.run([candidate, "-h"], capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            found = re.findall(r"CLIProxyAPI Version: ([0-9.]+)", version.stdout + version.stderr)
            if not any(supported_setup_version(value) for value in found):
                raise fleet.FleetError(f"Fleet account management requires CLIProxyAPI {SETUP_VERSION}.")
            return candidate
    raise fleet.FleetError(f"CLIProxyAPI {SETUP_VERSION} is not installed.")


def disable_new_logins(client, provider, before):
    previous = {entry["name"] for entry in before}
    try:
        added = [entry for entry in accounts_for(client.accounts(), provider) if entry["name"] not in previous]
        operations = [("disabled", entry, True, entry["disabled"]) for entry in added]
        for _, entry, disabled, _ in operations:
            client.disabled(entry, disabled)
        verify_operations(client, operations)
        after = client.accounts()
        names = {entry["name"] for entry in added}
        return after, [entry for entry in after if entry["name"] in names]
    except BaseException:
        raise fleet.FleetError("The new subscription login could not be disabled. Inspect its status in the CLIProxyAPI management panel before use.") from None


def read_native_json(path, label):
    try:
        details = os.lstat(path)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.getuid():
            raise OSError
        if details.st_size > 1024 * 1024:
            raise OSError
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError(f"Could not read the native {label} credential safely.") from None
    if not isinstance(value, dict):
        raise fleet.FleetError(f"Invalid native {label} credential.")
    return value


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, UnicodeError, ValueError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid native OAuth identity token.") from None
    if not isinstance(value, dict):
        raise fleet.FleetError("Invalid native OAuth identity token.")
    return value


def iso_expiry(value):
    if type(value) not in {int, float} or value <= 0:
        raise fleet.FleetError("Invalid native OAuth expiry.")
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        raise fleet.FleetError("Invalid native OAuth expiry.") from None


def required_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise fleet.FleetError(f"Native OAuth credential is missing {label}.")
    return value.strip()


def safe_account_component(value):
    value = re.sub(r"[^A-Za-z0-9_.@+-]+", "-", value.strip()).strip("-.")
    if not value:
        raise fleet.FleetError("Native OAuth account identity is invalid.")
    return value


def native_codex_credential(home):
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    if not codex_home.is_absolute():
        raise fleet.FleetError("CODEX_HOME must be absolute.")
    native = read_native_json(codex_home / "auth.json", "Codex")
    if native.get("auth_mode") != "chatgpt" or not isinstance(native.get("tokens"), dict):
        raise fleet.FleetError("Native Codex login is not a ChatGPT subscription.")
    tokens = native["tokens"]
    id_token = required_string(tokens.get("id_token"), "id_token")
    claims = jwt_claims(id_token)
    auth = claims.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        raise fleet.FleetError("Native Codex credential has no account identity.")
    email = required_string(claims.get("email"), "email")
    account_id = required_string(tokens.get("account_id") or auth.get("chatgpt_account_id"), "account_id")
    if auth.get("chatgpt_account_id") not in {None, account_id}:
        raise fleet.FleetError("Native Codex account identity does not match its token.")
    plan = safe_account_component(str(auth.get("chatgpt_plan_type") or "").lower()) if auth.get("chatgpt_plan_type") else ""
    stem = f"{hashlib.sha256(account_id.encode()).hexdigest()[:8]}-{safe_account_component(email)}"
    name = f"codex-{stem + '-' + plan if plan else stem}.json"
    payload = {
        "id_token": id_token,
        "access_token": required_string(tokens.get("access_token"), "access_token"),
        "refresh_token": required_string(tokens.get("refresh_token"), "refresh_token"),
        "account_id": account_id,
        "last_refresh": native.get("last_refresh") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "email": email,
        "type": "codex",
        "expired": iso_expiry(claims.get("exp")),
    }
    return name, email, account_id, payload


def claude_keychain_credential(home):
    if sys.platform != "darwin":
        return None
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    suffix = ""
    if configured is not None:
        directory = str(Path(configured).expanduser())
        suffix = "-" + hashlib.sha256(directory.encode()).hexdigest()[:8]
    user = os.environ.get("USER", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        user = "claude-code-user"
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", user, "-w", "-s", f"Claude Code-credentials{suffix}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError):
        raise fleet.FleetError("Invalid native Claude credential in Keychain.") from None
    if not isinstance(value, dict):
        raise fleet.FleetError("Invalid native Claude credential in Keychain.")
    return value


def native_claude_credential(home):
    config_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser()
    if not config_home.is_absolute():
        raise fleet.FleetError("CLAUDE_CONFIG_DIR must be absolute.")
    path = config_home / ".credentials.json"
    native = read_native_json(path, "Claude") if path.exists() else None
    if native is None or not isinstance(native.get("claudeAiOauth"), dict):
        native = claude_keychain_credential(home)
    if native is None or not isinstance(native.get("claudeAiOauth"), dict):
        raise fleet.FleetError("Native Claude login is not a Claude subscription.")
    oauth = native["claudeAiOauth"]
    profile = {}
    for candidate in (config_home / ".claude.json", home / ".claude.json"):
        if candidate.exists():
            profile = read_native_json(candidate, "Claude profile").get("oauthAccount") or {}
            break
    if not isinstance(profile, dict):
        raise fleet.FleetError("Invalid native Claude account profile.")
    email = required_string(profile.get("emailAddress"), "email")
    payload = {
        "access_token": required_string(oauth.get("accessToken"), "access_token"),
        "refresh_token": required_string(oauth.get("refreshToken"), "refresh_token"),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "email": email,
        "type": "claude",
        "expired": iso_expiry(oauth.get("expiresAt")),
    }
    for source, target in (
        ("accountUuid", "account_uuid"),
        ("organizationUuid", "organization_uuid"),
        ("organizationName", "organization_name"),
    ):
        if isinstance(profile.get(source), str) and profile[source].strip():
            payload[target] = profile[source].strip()
    return f"claude-{safe_account_component(email)}.json", email, email.casefold(), payload


def native_credential(home, provider):
    return native_claude_credential(home) if provider == "claude" else native_codex_credential(home)


def account_identity(entry, provider):
    if provider == "claude":
        value = entry.get("email") or entry.get("account")
        return value.casefold() if isinstance(value, str) else None
    claims = entry.get("id_token")
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return value if isinstance(value, str) else None


def save_login(home, provider, label, state_before, state, client, config, after, entry, new_login):
    policy = state["providers"][provider]
    policy["accounts"][entry["name"]] = label or account_label(state, entry)
    if "order" in policy and entry["name"] not in policy["order"]:
        policy["order"].append(entry["name"])
    require_enrolled_provider(policy, provider, after)
    entries = enrolled_accounts(policy, accounts_for(after, provider))
    if not new_login:
        operations = []
    elif policy["primary"] is None:
        policy["primary"] = entry["name"]
        policy["auto"] = False
        operations = [("disabled", entry, False, True)]
        operations.extend(("disabled", account, True, account["disabled"]) for account in entries if account["name"] != entry["name"])
    elif policy["auto"] is True:
        operations = [("disabled", entry, False, True)]
    else:
        operations = []
    if new_login and ("order" in policy or policy["primary"] == entry["name"]):
        operations.extend(priority_operations(entries, policy["primary"], policy.get("order")))
    else:
        operations.extend(websocket_operations([entry]))
    secure_credentials(home, config, [entry])
    if new_login:
        apply_transaction(
            client,
            operations,
            lambda: configure_provider_routing(
                home,
                provider,
                client.url,
                action=lambda: write_state_transaction(home, state, state_before),
            ),
        )
    else:
        write_state_transaction(home, state, state_before)
    return policy


def add_account(home, provider, label=None, *, consent=False):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    if label is not None and not valid_label(label):
        raise fleet.FleetError("Account label must be one printable line of 1 to 80 characters.")
    if not consent and os.environ.get("FLEET_ENABLE_SUBSCRIPTION_ROUTING") != "1":
        raise fleet.FleetError("Subscription OAuth account add is off. For private use, set FLEET_ENABLE_SUBSCRIPTION_ROUTING=1.")
    if is_spoke(home):
        raise fleet.FleetError("This host uses a Fleet hub. Log in with the native codex login or claude auth login command.")
    state_before = client_config_snapshot(state_path(home))
    state = load_state(home)
    if client_config_snapshot(state_path(home)) != state_before:
        raise fleet.FleetError("Fleet account state changed during update.")
    client, config, before = runtime_for_mutation(home)
    policy = state["providers"][provider]
    require_enrolled_provider(policy, provider, before)
    directory = safe_auth_dir(home, config)
    before_files = {
        entry["name"]: credential_identity(directory / entry["name"])
        for entry in accounts_for(before, provider)
        if not entry.get("runtime_only") and entry.get("source") == "file"
    }
    path = config_path(home)
    if login_auth_dir(home, path) != safe_auth_dir(home, config):
        raise fleet.FleetError("CLIProxyAPI login config does not match the running CLIProxyAPI auth-dir.")
    flag = "-claude-login" if provider == "claude" else "-codex-login"
    try:
        login = subprocess.run([proxy_binary(), "-config", str(path), flag])
    except OSError:
        raise fleet.FleetError("Could not start CLIProxyAPI.") from None
    if login.returncode:
        raise fleet.FleetError(f"{provider} login failed.")
    new_login = False
    try:
        after, added = disable_new_logins(client, provider, before)
        if len(added) > 1:
            raise fleet.FleetError("Login changed more than one account credential.")
        if added:
            entry = added[0]
            new_login = True
        else:
            changed = [
                entry
                for entry in accounts_for(after, provider)
                if entry["name"] in before_files
                and credential_identity(directory / entry["name"]) != before_files[entry["name"]]
            ]
            if len(changed) != 1:
                raise fleet.FleetError("Login did not add or refresh exactly one account credential.")
            entry = changed[0]
        if entry.get("runtime_only") or entry.get("source") != "file":
            raise fleet.FleetError("The new login did not create a file credential.")
        policy = save_login(home, provider, label, state_before, state, client, config, after, entry, new_login)
    except BaseException as error:
        _, added = disable_new_logins(client, provider, before)
        suffix = " The new login remains disabled." if added else ""
        raise fleet.FleetError(f"{error}{suffix}") from None
    auto = "on" if policy["auto"] is True else "off"
    action = "Added" if new_login else "Refreshed"
    print(f"{action} {provider} account {policy['accounts'][entry['name']]}. Auto switching is {auto}.")


def sync_native_account(home, provider):
    state_before = client_config_snapshot(state_path(home))
    state = load_state(home)
    if client_config_snapshot(state_path(home)) != state_before:
        raise fleet.FleetError("Fleet account state changed during update.")
    client, config, before = runtime_for_mutation(home)
    policy = state["providers"][provider]
    require_enrolled_provider(policy, provider, before)
    generated_name, label, identity, payload = native_credential(home, provider)
    matches = [entry for entry in accounts_for(before, provider) if account_identity(entry, provider) == identity]
    if len(matches) > 1:
        raise fleet.FleetError("Native account matches more than one proxy credential.")
    existing = matches[0] if matches else None
    name = existing["name"] if existing else generated_name
    payload["disabled"] = existing["disabled"] if existing else True
    if existing and "priority" in existing:
        payload["priority"] = existing["priority"]
    client.upload(name, (json.dumps(payload, separators=(",", ":")) + "\n").encode())
    after = client.accounts()
    entries = [entry for entry in accounts_for(after, provider) if entry["name"] == name]
    if len(entries) != 1 or entries[0].get("runtime_only") or entries[0].get("source") != "file":
        raise fleet.FleetError("Native login did not create one proxy file credential.")
    entry = entries[0]
    policy = save_login(home, provider, None, state_before, state, client, config, after, entry, existing is None)
    auto = "on" if policy["auto"] is True else "off"
    action = "Added" if existing is None else "Refreshed"
    print(f"{action} {provider} account {policy['accounts'][name]} from native login. Auto switching is {auto}.")
    try:
        rebalance_provider(home, provider)
    except fleet.FleetError as error:
        print(f"Reset ordering was not refreshed: {error}", file=sys.stderr)


def run_native_auth(home, provider, binary, arguments):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    path = Path(binary)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise fleet.FleetError(f"Native {provider} executable is missing or unsafe.")
    login = provider == "codex" and arguments[:1] == ["login"] or provider == "claude" and arguments[:2] == ["auth", "login"]
    logout = provider == "codex" and arguments[:1] == ["logout"] or provider == "claude" and arguments[:2] == ["auth", "logout"]
    if not login and not logout:
        raise fleet.FleetError("Invalid native auth command.")
    identity = None
    if logout:
        try:
            identity = native_credential(home, provider)[2]
        except fleet.FleetError:
            pass
    command = [str(path), *arguments]
    if provider == "claude":
        command[1:1] = ["--setting-sources", ""]
    environment = dict(os.environ)
    environment["PATH"] = environment.pop("FLEET_NATIVE_PATH", environment.get("PATH", os.defpath))
    try:
        result = subprocess.run(command, env=environment)
    except OSError:
        raise fleet.FleetError(f"Could not start native {provider}.") from None
    if result.returncode:
        raise SystemExit(result.returncode)
    with fleet.operation_lock(home):
        if login:
            sync_native_account(home, provider)
        elif identity is not None:
            state = load_state(home)
            enrolled = state["providers"][provider]["accounts"]
            matches = [
                entry
                for entry in accounts_for(ManagementClient(home=home).accounts(), provider)
                if entry["name"] in enrolled and account_identity(entry, provider) == identity
            ]
            if len(matches) > 1:
                raise fleet.FleetError("Native account matches more than one proxy credential.")
            if matches:
                remove_account(home, provider, matches[0]["name"])


def remove_account(home, provider, wanted=None):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    state_before = client_config_snapshot(state_path(home))
    state = load_state(home)
    if client_config_snapshot(state_path(home)) != state_before:
        raise fleet.FleetError("Fleet account state changed during update.")
    client, config, all_entries = runtime_for_mutation(home)
    policy = state["providers"][provider]
    require_enrolled_provider(policy, provider, all_entries)
    entries = enrolled_accounts(policy, accounts_for(all_entries, provider))
    if not entries:
        raise fleet.FleetError(f"No enrolled {provider} accounts found.")
    selected = live_primary(policy, entries, provider) if wanted is None else resolve_account(state, provider, entries, wanted)
    label = account_label(state, selected)
    remaining = [entry for entry in entries if entry["name"] != selected["name"]]
    was_primary = policy["primary"] == selected["name"]
    del policy["accounts"][selected["name"]]
    if policy.get("preferred") == selected["name"]:
        policy.pop("preferred")
    if policy.get("override") == selected["name"]:
        policy.pop("override")
    if "order" in policy:
        policy["order"].remove(selected["name"])
    if was_primary and policy.get("order") and not any(entry["name"] == policy["order"][0] for entry in remaining):
        raise fleet.FleetError("The next ordered account is no longer available; restore it before removing the primary.")
    operations = []
    if was_primary:
        if remaining:
            policy["primary"] = remaining[0]["name"]
            operations = [("disabled", remaining[0], False, remaining[0]["disabled"])]
            operations.extend(priority_operations(remaining, policy["primary"], policy.get("order")))
            if policy["auto"] is False:
                operations.extend(
                    ("disabled", entry, True, entry["disabled"])
                    for entry in remaining
                    if entry["name"] != policy["primary"]
                )
        else:
            policy["primary"] = None
            policy["auto"] = None
    if not was_primary and "order" in policy:
        operations.extend(priority_operations(remaining, policy["primary"], policy["order"]))
    secure_credentials(home, config, entries)

    def save_and_delete():
        rollback = write_policy_transaction(home, provider, state, state_before)
        try:
            client.delete(selected)
        except BaseException:
            rollback()
            raise

    apply_transaction(client, operations, save_and_delete)
    message = f"Removed {provider} account {label}."
    if was_primary and policy["primary"] is not None:
        message += f" Primary is now {policy['accounts'][policy['primary']]}."
    print(message)


def enroll_account(home, provider, wanted):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    state_before = client_config_snapshot(state_path(home))
    state = load_state(home)
    if client_config_snapshot(state_path(home)) != state_before:
        raise fleet.FleetError("Fleet account state changed during update.")
    client, config, all_entries = runtime_for_mutation(home)
    entries = accounts_for(all_entries, provider)
    selected = resolve_account(state, provider, entries, wanted)
    policy = state["providers"][provider]
    policy["accounts"][selected["name"]] = account_label(state, selected)
    if "order" in policy and selected["name"] not in policy["order"]:
        policy["order"].append(selected["name"])
    if policy["primary"] is None:
        policy["primary"] = selected["name"]
    secure_credentials(home, config, [selected])
    if all(entry["name"] in policy["accounts"] for entry in entries):
        policy["auto"] = False
        primary = live_primary(policy, entries, provider)
        operations = [("disabled", primary, False, primary["disabled"])]
        operations.extend(priority_operations(entries, primary["name"], policy.get("order")))
        operations.extend(("disabled", entry, True, entry["disabled"]) for entry in entries if entry["name"] != primary["name"])
        secure_credentials(home, config, entries)
        apply_transaction(
            client,
            operations,
            lambda: configure_provider_routing(
                home,
                provider,
                client.url,
                action=lambda: write_state_transaction(home, state, state_before),
            ),
        )
        print(f"Enrolled {provider} account {policy['accounts'][selected['name']]}. {provider.title()} now uses CLIProxyAPI.")
    else:
        write_state(home, state)
        print(f"Enrolled {provider} account {policy['accounts'][selected['name']]}. Enroll the remaining {provider} accounts before routing is enabled.")


def rename_account(home, provider, wanted, label):
    if provider not in PROVIDERS:
        raise fleet.FleetError("Account provider must be claude or codex.")
    if not valid_label(label):
        raise fleet.FleetError("Account label must be one printable line of 1 to 80 characters.")
    state = load_state(home)
    policy = state["providers"][provider]
    entries = [{"name": name, "provider": provider} for name in policy["accounts"]]
    selected = resolve_account(state, provider, entries, wanted)
    policy["accounts"][selected["name"]] = label
    write_state(home, state)
    print(f"Renamed {provider} account to {label}.")


USAGE = """Usage: fleet account <command>

  list [claude|codex]                    accounts, order, auto, usage, and reset
  use <p> [account]                      pick the primary account
  override <p> <account|off>             hold an account first until cleared
  order <p> [accounts... | reset | reset-fallbacks | manual | rebalance]
  auto <p> on|off [--when limits|any]    quota failover
  credits <p> never|last|same-account    credit use and account switching
  login <p> [label] | logout <p> [account] | rename <p> <account> <label>
  login <p> --existing <account>         enroll a credential already in CLIProxyAPI
  setup [--check | --restore-clients]    local CLIProxyAPI router
  hub [setup | join <name> --ssh <target> | leave]   shared router"""


def main(arguments):
    home = account_home()
    try:
        command = arguments[0] if arguments else "list"
        rest = arguments[1:]
        if command == "native-auth" and len(rest) >= 3:
            run_native_auth(home, rest[0], rest[1], rest[2:])
            return
        if command == "hub":
            if rest == ["setup"]:
                with fleet.operation_lock(home):
                    hub_setup(home)
            elif len(rest) == 4 and rest[0] == "join" and rest[2] == "--ssh":
                with fleet.operation_lock(home):
                    hub_join(home, rest[1], rest[3])
            elif rest == ["leave"]:
                with fleet.operation_lock(home):
                    hub_leave(home)
            elif rest in ([], ["status"]):
                hub_status(home)
            else:
                raise fleet.FleetError(USAGE)
            return
        if command == "setup":
            shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
            if rest == []:
                shell_startup_snapshot(shell_startup_path(home, shell))
                with fleet.operation_lock(home):
                    setup_proxy(home)
                    install_shell_hook(home, shell)
                    for provider in load_client_routing_state(home)["providers"]:
                        configure_provider_routing(home, provider, "http://127.0.0.1:8317")
                if not check_setup(home, shell=shell):
                    raise SystemExit(1)
                return
            if rest == ["--check"]:
                raise SystemExit(0 if check_setup(home, shell=shell) else 1)
            if rest == ["--restore-clients"]:
                with fleet.operation_lock(home):
                    restore_client_routing(home)
                print("Fleet client routing restored. Accounts, auth, sessions, and the proxy setup were not changed.")
                return
            raise fleet.FleetError(USAGE)
        if command in {"list", "quota", "current"} and len(rest) <= 1:
            list_accounts(home, rest[0] if rest else None)
            return
        if command == "credits" and rest == ["--help"]:
            print("Usage: fleet account credits <claude|codex> <never|last|same-account>\n\nnever         Skip accounts at either subscription limit. Stop before credits.\nlast          Use all account allowances before available credits.\nsame-account  Use allowance and credits on each account before switching.\n\nEach provider has its own choice. Auto switching must be on to use fallback accounts.\nUsage checks can fail or lag. Provider billing controls are required for a strict zero-credit guarantee.")
            return
        if command == "credits" and len(rest) == 2:
            with fleet.operation_lock(home):
                credit_account(home, rest[0], rest[1])
            return
        if command == "auto" and rest == ["--help"]:
            print("Usage: fleet account auto <claude|codex> <on|off> [--when <limits|any>]\n\n--when limits  Switch only when subscription limits are reached (default).\n--when any     Also switch on temporary provider errors; this can spend fallback quota.")
            return
        if command in {"login", "add"} and len(rest) == 3 and rest[1] == "--existing":
            with fleet.operation_lock(home):
                enroll_account(home, rest[0], rest[2])
            return
        if command == "enroll" and len(rest) == 2:
            with fleet.operation_lock(home):
                enroll_account(home, rest[0], rest[1])
            return
        if command in {"login", "add"} and 1 <= len(rest) <= 2:
            with fleet.operation_lock(home):
                add_account(home, rest[0], rest[1] if len(rest) == 2 else None, consent=command == "login")
            return
        if command in {"logout", "remove"} and 1 <= len(rest) <= 2:
            with fleet.operation_lock(home):
                remove_account(home, rest[0], rest[1] if len(rest) == 2 else None)
            return
        if command == "rename" and len(rest) == 3:
            with fleet.operation_lock(home):
                rename_account(home, rest[0], rest[1], rest[2])
            return
        if command == "use" and len(rest) == 2:
            with fleet.operation_lock(home):
                use_account(home, rest[0], rest[1])
            return
        if command == "override" and len(rest) == 2:
            with fleet.operation_lock(home):
                override_account(home, rest[0], rest[1])
            return
        if command == "use" and len(rest) == 1:
            with fleet.operation_lock(home):
                pick_account(home, rest[0])
            return
        if command == "rebalance" and len(rest) <= 1:
            rebalance(home, rest[0] if rest else None)
            return
        if command == "order" and rest[:1] == ["--auto"] and len(rest) == 3 and rest[2] in {"reset", "reset-fallbacks", "manual"}:
            set_rebalance(home, rest[1], rest[2])
            return
        if command == "order" and rest == ["rebalance"]:
            rebalance(home)
            return
        if command == "order" and len(rest) == 1:
            show_account_order(home, rest[0])
            return
        if command == "order" and len(rest) == 2 and rest[1] == "rebalance":
            rebalance(home, rest[0])
            return
        if command == "order" and len(rest) == 2 and rest[1] in {"reset", "reset-fallbacks", "manual"}:
            set_rebalance(home, rest[0], rest[1])
            return
        if command == "order" and len(rest) >= 2:
            with fleet.operation_lock(home):
                use_account(home, rest[0], rest[1], order=rest[1:])
            return
        if command == "auto" and len(rest) >= 2 and rest[1] == "on":
            if rest[2:] == []:
                mode = "limits"
            elif len(rest[2:]) == 2 and rest[2] == "--when" and rest[3] in ROUTING_MODES:
                mode = rest[3]
            else:
                raise fleet.FleetError("Valid --when options: limits, any.")
            with fleet.operation_lock(home):
                auto_account(home, rest[0], True, mode)
            return
        if command == "auto" and len(rest) == 2 and rest[1] == "off":
            with fleet.operation_lock(home):
                auto_account(home, rest[0], False)
            return
        raise fleet.FleetError(USAGE)
    except fleet.FleetError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[2:])
