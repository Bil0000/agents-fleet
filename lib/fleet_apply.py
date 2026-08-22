#!/usr/bin/env python3
import hashlib
import importlib.util
import io
import json
import os
import fcntl
import errno
import plistlib
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_mcp", Path(__file__).with_name("fleet_mcp.py"))
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
FleetError = mcp.FleetError
MissingCredential = mcp.MissingCredential

HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
BACKUP_NAME = re.compile(r"^[0-9]{8}T[0-9]{12}Z-(?:none|[0-9a-f]{40}(?:[0-9a-f]{24})?)-[0-9a-f]{8}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9-]*$")
GITHUB_SOURCE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")
CODEX_SCALARS = ("model", "model_reasoning_effort", "personality", "web_search", "service_tier")
CODEX_FEATURES = ("multi_agent", "memories", "chronicle")
CODEX_REMOVED_FEATURES = ("plugin_hooks", "js_repl")
CODEX_HOOK_EVENTS = {
    "PermissionRequest", "PostCompact", "PostToolUse", "PreCompact", "PreToolUse",
    "SessionEnd", "SessionStart", "Stop", "SubagentStart", "SubagentStop", "UserPromptSubmit",
}
CLAUDE_STRING_LEAVES = {
    "model",
    "effortLevel",
    "theme",
    "tui",
    "preferredNotifChannel",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
}
CLAUDE_BOOLEAN_LEAVES = {
    "agentPushNotifEnabled",
    "remoteControlAtStartup",
    "skipAutoPermissionPrompt",
    "skipDangerousModePermissionPrompt",
    "skipWorkflowUsageWarning",
    "switchModelsOnFlag",
}
CLAUDE_OBJECT_LEAVES = {"hooks", "permissions", "statusLine"}
CLAUDE_LEAVES = CLAUDE_STRING_LEAVES | CLAUDE_BOOLEAN_LEAVES | CLAUDE_OBJECT_LEAVES
CLAUDE_NESTED = {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY"}, "attribution": {"commit", "pr"}}
MAC_UPDATER_LABEL = "dev.agents-fleet.update"


def fail(message):
    raise FleetError(message)


def fixed_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def validate_hooks(hooks, relative):
    if not isinstance(hooks, dict) or set(hooks) - CODEX_HOOK_EVENTS:
        fail(f"Invalid managed hooks: {relative}")
    for groups in hooks.values():
        if not isinstance(groups, list) or not groups:
            fail(f"Invalid managed hooks: {relative}")
        for group in groups:
            if not isinstance(group, dict) or set(group) - {"matcher", "hooks"}:
                fail(f"Invalid managed hooks: {relative}")
            if not isinstance(group.get("hooks"), list) or not group["hooks"]:
                fail(f"Invalid managed hooks: {relative}")
            if "matcher" in group and not isinstance(group["matcher"], str):
                fail(f"Invalid managed hooks: {relative}")
            for hook in group["hooks"]:
                allowed = {"type", "command", "timeout", "async", "statusMessage"}
                if not isinstance(hook, dict) or set(hook) - allowed:
                    fail(f"Invalid managed hooks: {relative}")
                if hook.get("type") != "command" or not isinstance(hook.get("command"), str) or not hook["command"]:
                    fail(f"Invalid managed hooks: {relative}")
                if "timeout" in hook and (type(hook["timeout"]) is not int or not 1 <= hook["timeout"] <= 600):
                    fail(f"Invalid managed hooks: {relative}")
                if "async" in hook and type(hook["async"]) is not bool:
                    fail(f"Invalid managed hooks: {relative}")
                if "statusMessage" in hook and not isinstance(hook["statusMessage"], str):
                    fail(f"Invalid managed hooks: {relative}")


def host_id(root, explicit=None):
    value = explicit or socket.gethostname().split(".", 1)[0].lower().replace("_", "-")
    if not HOST_NAME.fullmatch(value):
        fail("Invalid Fleet host name.")
    path = root / "hosts" / value / "host.toml"
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        fail(f"Unknown Fleet host: {value}")
    if data.get("id") != value:
        fail(f"Unknown Fleet host: {value}")
    return value


def git(root, *arguments, check=True):
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fixed_home()), "LANG": "C", "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        fail("Fleet Git operation failed.")
    return result


def repo_sha(root):
    value = git(root, "rev-parse", "HEAD^{commit}").stdout.strip()
    if not GIT_SHA.fullmatch(value):
        fail("Invalid Fleet Git revision.")
    return value


def repo_dirty(root):
    return bool(git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout)


def source_repository(root, home):
    root = Path(root).resolve()
    if (root / ".git").exists():
        return root
    state_path = safe_target(home, ".local/state/fleet/last-applied.json")
    try:
        details = os.lstat(state_path)
        state = json.loads(state_path.read_text())
        repository = Path(state["repository"])
        repo_details = os.lstat(repository)
        git_details = os.lstat(repository / ".git")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        fail("Fleet source repository is unavailable.")
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or not repository.is_absolute()
        or stat.S_ISLNK(repo_details.st_mode)
        or not stat.S_ISDIR(repo_details.st_mode)
        or stat.S_ISLNK(git_details.st_mode)
        or not (stat.S_ISDIR(git_details.st_mode) or stat.S_ISREG(git_details.st_mode))
    ):
        fail("Fleet source repository is unavailable.")
    return repository.resolve()


def check_repository(root):
    root = Path(root)
    try:
        if root.is_symlink() or not root.is_dir():
            fail("Invalid Fleet repository.")
        ignored = {".git", ".worktrees", "__pycache__"}
        paths = [path for path in root.rglob("*") if not ignored.intersection(path.relative_to(root).parts)]
        if any(path.is_symlink() for path in paths):
            fail("Fleet repository contains a symlink.")
        files = [path for path in paths if path.is_file()]
        for path in files:
            if path.suffix == ".json":
                json.loads(path.read_text())
            elif path.suffix == ".toml":
                data = tomllib.loads(path.read_text())
                relative = path.relative_to(root)
                if path.name == "host.toml" and path.parent.parent == root / "hosts":
                    if set(data) != {"id", "os", "role"} or data["id"] != path.parent.name or data["os"] not in {"macos", "linux"} or not isinstance(data["role"], str) or not data["role"].strip():
                        fail(f"Invalid host manifest: {relative}")
                if path.name == "codex.toml" and (path.parent == root / "settings" or path.parent.parent == root / "hosts"):
                    allowed = set(CODEX_SCALARS) | {"features"}
                    if not isinstance(data, dict) or set(data) - allowed:
                        fail(f"Invalid managed Codex settings: {relative}")
                    if any(key in data and not isinstance(data[key], str) for key in CODEX_SCALARS):
                        fail(f"Invalid managed Codex setting type: {relative}")
                    features = data.get("features", {})
                    if not isinstance(features, dict) or set(features) - set(CODEX_FEATURES) or any(type(value) is not bool for value in features.values()):
                        fail(f"Invalid managed Codex features: {relative}")
            if path.name == "claude.json" and (path.parent == root / "settings" or path.parent.parent == root / "hosts"):
                data = json.loads(path.read_text())
                allowed = CLAUDE_LEAVES | set(CLAUDE_NESTED)
                if not isinstance(data, dict) or set(data) - allowed:
                    fail(f"Invalid managed Claude settings: {path.relative_to(root)}")
                if any(key in data and not isinstance(data[key], str) for key in CLAUDE_STRING_LEAVES):
                    fail(f"Invalid managed Claude setting type: {path.relative_to(root)}")
                if any(key in data and type(data[key]) is not bool for key in CLAUDE_BOOLEAN_LEAVES):
                    fail(f"Invalid managed Claude setting type: {path.relative_to(root)}")
                if any(key in data and not isinstance(data[key], dict) for key in CLAUDE_OBJECT_LEAVES):
                    fail(f"Invalid managed Claude setting type: {path.relative_to(root)}")
                if "hooks" in data:
                    validate_hooks(data["hooks"], path.relative_to(root))
                if "permissions" in data:
                    permissions = data["permissions"]
                    if set(permissions) - {"allow", "deny", "defaultMode"}:
                        fail(f"Invalid managed Claude permissions: {path.relative_to(root)}")
                    lists = (permissions[key] for key in ("allow", "deny") if key in permissions)
                    if any(not isinstance(names, list) or any(not isinstance(name, str) for name in names) for names in lists):
                        fail(f"Invalid managed Claude permissions: {path.relative_to(root)}")
                    if "defaultMode" in permissions and not isinstance(permissions["defaultMode"], str):
                        fail(f"Invalid managed Claude permissions: {path.relative_to(root)}")
                if "statusLine" in data:
                    status_line = data["statusLine"]
                    if set(status_line) != {"type", "command"} or status_line.get("type") != "command" or not isinstance(status_line.get("command"), str):
                        fail(f"Invalid managed Claude status line: {path.relative_to(root)}")
                for parent, leaves in CLAUDE_NESTED.items():
                    child = data.get(parent, {})
                    if not isinstance(child, dict) or set(child) - leaves or any(not isinstance(value, str) for value in child.values()):
                        fail(f"Invalid managed Claude nested settings: {path.relative_to(root)}")
            if path.name == "codex-hooks.json":
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or set(data) != {"hooks"} or not isinstance(data["hooks"], dict):
                    fail(f"Invalid managed Codex hooks: {path.relative_to(root)}")
                validate_hooks(data["hooks"], path.relative_to(root))
        for path in files:
            if path.suffix == ".sh" or path.parent.name == "bin":
                result = subprocess.run(["/bin/bash", "-n", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode:
                    fail(f"Invalid shell file: {path.relative_to(root)}")
        secret_patterns = (
            re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
            re.compile(rb"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),
        )
        credential_pair = re.compile(rb"(?i)\b(?:pg|postgres(?:ql)?|minio)\b[^\r\n()]{0,64}\([A-Za-z0-9_.@-]{2,}/[A-Za-z0-9_.@-]{2,}(?:/[A-Za-z0-9_.-]+)?\)")
        for path in files:
            contents = path.read_bytes()
            if any(pattern.search(contents) for pattern in secret_patterns) or credential_pair.search(contents):
                fail(f"Possible secret: {path.relative_to(root)}")
            relative = path.relative_to(root)
            if relative.parts[0] != "hosts" and re.search(
                rb"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)", contents
            ):
                fail(f"Device path outside host overlay: {relative}")
        mcp.validate_all(root)
        if (root / "plugins.json").exists():
            for directory in sorted((root / "hosts").iterdir()):
                if directory.is_dir():
                    load_plugin_manifest(root, directory.name)
        validate_scheduler_sources(root)
    except FleetError:
        raise
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise FleetError("Fleet repository validation failed.") from error


def validate_repo(root):
    check_repository(root)


def safe_target(home, relative, *, allow_missing=True):
    home = Path(home)
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        fail("Unsafe Fleet target path.")
    try:
        mode = os.lstat(home).st_mode
    except OSError:
        fail("Fleet home is unavailable.")
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        fail("Unsafe Fleet home path.")
    current = home
    for part in relative.parts[:-1]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if allow_missing:
                break
            fail("Unsafe Fleet target path.")
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            fail("Unsafe Fleet target path.")
    return home / relative


def ensure_parent(home, target):
    relative = target.relative_to(home)
    current = home
    for part in relative.parts[:-1]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            fail("Unsafe Fleet target path.")


def atomic_write(home, target, data, mode=0o600):
    ensure_parent(home, target)
    try:
        old_mode = os.lstat(target).st_mode
        if stat.S_ISREG(old_mode):
            mode = stat.S_IMODE(old_mode)
        elif not stat.S_ISLNK(old_mode):
            fail("Unsafe Fleet file target.")
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def native_environment(home, platform):
    value = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C"}
    if platform == "linux":
        value["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return value


def run_native(command, home, platform, native_runner=None):
    runner = subprocess.run if native_runner is None else native_runner
    try:
        return runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=native_environment(home, platform),
        )
    except OSError as error:
        raise FleetError("Fleet updater native command could not start.") from error


def scheduler_platform(platform_name=None):
    value = sys.platform if platform_name is None else platform_name
    if value == "darwin":
        return "darwin"
    if value.startswith("linux"):
        return "linux"
    fail("Fleet updates are supported only on macOS and Linux.")


def scheduler_source_contents(root):
    root = Path(root)
    paths = [root / "system/linux/fleet-update.service", root / "system/linux/fleet-update.timer"]
    try:
        if any(path.is_symlink() or not path.is_file() for path in paths):
            fail("Invalid Fleet updater unit files.")
        service, timer = (path.read_text() for path in paths)
    except OSError as error:
        raise FleetError("Invalid Fleet updater unit files.") from error
    service_lines = tuple(line.strip() for line in service.splitlines() if line.strip())
    timer_lines = tuple(line.strip() for line in timer.splitlines() if line.strip())
    if service_lines != (
        "[Unit]", "Description=Update and apply Fleet configuration",
        "[Service]", "Type=oneshot", "ExecStart=%h/.local/bin/fleet update",
    ) or timer_lines != (
        "[Unit]", "Description=Check for validated Fleet updates every minute",
        "[Timer]", "OnActiveSec=1m", "OnUnitActiveSec=1m", "AccuracySec=1us",
        "Persistent=true", "Unit=fleet-update.service",
        "[Install]", "WantedBy=timers.target",
    ):
        fail("Invalid Fleet updater unit files.")
    return service, timer


def validate_scheduler_sources(root, *, native_runner=None, platform_name=None, home=None):
    service_text, timer_text = scheduler_source_contents(root)
    platform = scheduler_platform(platform_name)
    analyzer = Path("/usr/bin/systemd-analyze")
    if platform == "linux" and analyzer.is_file() and os.access(analyzer, os.X_OK):
        with tempfile.TemporaryDirectory(prefix="fleet-systemd-verify-") as temporary:
            directory = Path(temporary)
            service = directory / "fleet-update.service"
            timer = directory / "fleet-update.timer"
            service.write_text(service_text.replace("ExecStart=%h/.local/bin/fleet update", "ExecStart=/bin/true"))
            timer.write_text(timer_text)
            result = run_native(
                [str(analyzer), "--user", "verify", str(service), str(timer)],
                fixed_home() if home is None else Path(home),
                "linux",
                native_runner,
            )
        if result.returncode:
            fail("Fleet updater unit validation failed.")


def render_macos_plist(home, interval=60):
    home = Path(home)
    logs = home / ".local/state/fleet/logs"
    return plistlib.dumps(
        {
            "Label": MAC_UPDATER_LABEL,
            "ProgramArguments": [str(home / ".local/bin/fleet"), "update"],
            "RunAtLoad": True,
            "StandardErrorPath": str(logs / "update.error.log"),
            "StandardOutPath": str(logs / "update.log"),
            "StartInterval": interval,
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def updater_paths(root, home, platform):
    if platform == "darwin":
        return {safe_target(home, "Library/LaunchAgents/dev.agents-fleet.update.plist"): render_macos_plist(home)}
    service, timer = scheduler_source_contents(root)
    return {
        safe_target(home, ".config/systemd/user/fleet-update.service"): service.encode(),
        safe_target(home, ".config/systemd/user/fleet-update.timer"): timer.encode(),
    }


def legacy_updater_paths(root, home, platform):
    if platform == "darwin":
        target = safe_target(home, "Library/LaunchAgents/dev.agents-fleet.update.plist")
        return [{target: render_macos_plist(home, 3600)}]
    service, _ = scheduler_source_contents(root)
    hourly = (
        "[Unit]\nDescription=Check for Fleet updates hourly\n\n"
        "[Timer]\nOnBootSec=5m\nOnUnitActiveSec=1h\nRandomizedDelaySec=10m\n"
        "Persistent=true\nUnit=fleet-update.service\n\n[Install]\nWantedBy=timers.target\n"
    ).encode()
    first_minute = (
        "[Unit]\nDescription=Check for validated Fleet updates every minute\n\n"
        "[Timer]\nOnBootSec=1m\nOnUnitActiveSec=1m\nPersistent=true\n"
        "Unit=fleet-update.service\n\n[Install]\nWantedBy=timers.target\n"
    ).encode()
    service_path = safe_target(home, ".config/systemd/user/fleet-update.service")
    timer_path = safe_target(home, ".config/systemd/user/fleet-update.timer")
    return [
        {service_path: service.encode(), timer_path: hourly},
        {service_path: service.encode(), timer_path: first_minute},
    ]


def updater_files_state(paths, legacy=None):
    present = {}
    for path in paths:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return "invalid"
        present[path] = path.read_bytes()
    if not present:
        return "not-installed"
    if len(present) != len(paths):
        return "invalid"
    if all(present[path] == expected for path, expected in paths.items()):
        return "installed"
    if legacy is not None and any(all(present[path] == expected for path, expected in version.items()) for version in legacy):
        return "outdated"
    return "invalid"


def mac_job_state(home, uid, native_runner):
    result = run_native(
        ["/bin/launchctl", "print", f"gui/{uid}/{MAC_UPDATER_LABEL}"], home, "darwin", native_runner
    )
    if result.returncode:
        if result.returncode == 113 and "Could not find service" in result.stderr:
            return "inactive"
        return "error"
    executable = str(Path(home) / ".local/bin/fleet")
    lines = [line.strip().strip('"') for line in result.stdout.splitlines()]
    program = f"program = {executable}" in lines or f"path = {executable}" in lines
    try:
        start = lines.index("arguments = {")
        end = lines.index("}", start + 1)
        arguments = lines[start + 1:end]
    except ValueError:
        arguments = []
    return "active" if program and arguments == [executable, "update"] else "invalid"


def systemctl_boolean(result, true_value, false_values):
    value = result.stdout.strip()
    if result.returncode == 0 and value == true_value:
        return True
    if result.returncode != 0 and value in false_values:
        return False
    return None


def linux_job_state(home, native_runner):
    enabled = systemctl_boolean(
        run_native(["/usr/bin/systemctl", "--user", "is-enabled", "fleet-update.timer"], home, "linux", native_runner),
        "enabled",
        {"disabled", "static", "indirect", "masked", "not-found"},
    )
    active = systemctl_boolean(
        run_native(["/usr/bin/systemctl", "--user", "is-active", "fleet-update.timer"], home, "linux", native_runner),
        "active",
        {"inactive", "failed", "unknown"},
    )
    if enabled is None or active is None:
        return {"kind": "error", "enabled": False, "active": False}
    if enabled or active:
        timer = run_native(
            ["/usr/bin/systemctl", "--user", "show", "--property=FragmentPath", "fleet-update.timer"],
            home, "linux", native_runner,
        )
        service = run_native(
            ["/usr/bin/systemctl", "--user", "show", "--property=FragmentPath", "--property=ExecStart", "fleet-update.service"],
            home, "linux", native_runner,
        )
        executable = str(Path(home) / ".local/bin/fleet")
        expected_timer = f"FragmentPath={home}/.config/systemd/user/fleet-update.timer"
        expected_service = f"FragmentPath={home}/.config/systemd/user/fleet-update.service"
        if timer.returncode or service.returncode:
            return {"kind": "error", "enabled": enabled, "active": active}
        if expected_timer not in timer.stdout.splitlines() or expected_service not in service.stdout.splitlines():
            return {"kind": "invalid", "enabled": enabled, "active": active}
        exec_line = next((line for line in service.stdout.splitlines() if line.startswith("ExecStart=")), "")
        if f"path={executable} ;" not in exec_line or f"argv[]={executable} update ;" not in exec_line:
            return {"kind": "invalid", "enabled": enabled, "active": active}
    return {"kind": "ok", "enabled": enabled, "active": active}


def updater_status(root, home=None, *, platform_name=None, uid=None, native_runner=None):
    root = Path(root).resolve()
    home = fixed_home() if home is None else Path(home)
    platform = scheduler_platform(platform_name)
    uid = os.getuid() if uid is None else uid
    try:
        paths = updater_paths(root, home, platform)
        files = updater_files_state(paths, legacy_updater_paths(root, home, platform))
        if files != "installed":
            return files
        if platform == "darwin":
            return mac_job_state(home, uid, native_runner)
        state = linux_job_state(home, native_runner)
        if state["kind"] != "ok":
            return state["kind"]
        return "active" if state["enabled"] and state["active"] else "inactive"
    except (FleetError, OSError):
        return "error"


def require_installed_fleet(home):
    installed = safe_target(home, ".local/bin/fleet", allow_missing=False)
    current = safe_target(home, ".local/share/fleet/current", allow_missing=False)
    try:
        if not installed.is_symlink() or os.readlink(installed) != "../share/fleet/current/bin/fleet":
            fail("Run fleet apply before enabling updates.")
        if not current.is_symlink():
            fail("Run fleet apply before enabling updates.")
        link = os.readlink(current)
        parts = Path(link).parts
        if len(parts) != 2 or parts[0] != "releases" or not GIT_SHA.fullmatch(parts[1]):
            fail("Run fleet apply before enabling updates.")
        release = safe_target(home, Path(".local/share/fleet") / link, allow_missing=False)
        details = os.lstat(release)
        bin_details = os.lstat(release / "bin")
        executable = release / "bin/fleet"
        executable_details = os.lstat(executable)
        resolved = installed.resolve(strict=True)
    except OSError:
        fail("Run fleet apply before enabling updates.")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        fail("Run fleet apply before enabling updates.")
    if stat.S_ISLNK(bin_details.st_mode) or not stat.S_ISDIR(bin_details.st_mode):
        fail("Run fleet apply before enabling updates.")
    if stat.S_ISLNK(executable_details.st_mode) or not stat.S_ISREG(executable_details.st_mode) or not os.access(executable, os.X_OK):
        fail("Run fleet apply before enabling updates.")
    real_release = release.resolve(strict=True)
    if resolved != (real_release / "bin/fleet") or not resolved.is_relative_to(real_release):
        fail("Run fleet apply before enabling updates.")


def validate_macos_plist(home, contents, native_runner):
    target = safe_target(home, "Library/LaunchAgents/dev.agents-fleet.update.plist")
    ensure_parent(home, target)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(contents)
        result = run_native(["/usr/bin/plutil", "-lint", temporary], home, "darwin", native_runner)
        if result.returncode:
            fail("Fleet updater plist validation failed.")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def prepare_macos_logs(home):
    logs = safe_target(home, ".local/state/fleet/logs")
    ensure_parent(home, logs / "placeholder")
    try:
        details = os.lstat(logs)
    except FileNotFoundError:
        os.mkdir(logs, 0o700)
        details = os.lstat(logs)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        fail("Unsafe Fleet updater log directory.")
    os.chmod(logs, 0o700)


def snapshot_native_state(home, platform, uid, native_runner):
    if platform == "darwin":
        state = mac_job_state(home, uid, native_runner)
        if state == "invalid":
            fail("Loaded updater job is not owned by Fleet.")
        if state == "error":
            fail("Loaded updater job could not be verified.")
        return {"enabled": state == "active", "active": state == "active"}
    state = linux_job_state(home, native_runner)
    if state["kind"] == "invalid":
        fail("Loaded updater units are not owned by Fleet.")
    if state["kind"] == "error":
        fail("Loaded updater units could not be verified.")
    return {"enabled": state["enabled"], "active": state["active"]}


def restore_updater_files(home, previous):
    for path, contents in previous.items():
        if contents is None:
            if os.path.lexists(path):
                os.unlink(path)
        else:
            atomic_write(home, path, contents, 0o644)


def restore_native_state(home, platform, uid, paths, previous, native_runner):
    errors = []
    if platform == "darwin":
        current = mac_job_state(home, uid, native_runner)
        if current == "active":
            result = run_native(["/bin/launchctl", "bootout", f"gui/{uid}", str(next(iter(paths)))], home, "darwin", native_runner)
            if result.returncode:
                errors.append("bootout")
        elif current != "inactive":
            errors.append("launchctl state")
        try:
            restore_updater_files(home, {path: contents for path, contents in previous.items() if path != "native"})
        except OSError:
            errors.append("files")
        if previous["native"]["active"]:
            result = run_native(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(next(iter(paths)))], home, "darwin", native_runner)
            if result.returncode:
                errors.append("bootstrap")
        return errors
    current = linux_job_state(home, native_runner)
    if current["kind"] == "ok" and (current["enabled"] or current["active"]):
        result = run_native(["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-update.timer"], home, "linux", native_runner)
        if result.returncode:
            errors.append("disable")
    elif current["kind"] != "ok":
        errors.append("systemd state")
    try:
        restore_updater_files(home, {path: contents for path, contents in previous.items() if path != "native"})
    except OSError:
        errors.append("files")
    if run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
        errors.append("daemon-reload")
    native = previous["native"]
    if native["enabled"]:
        command = ["/usr/bin/systemctl", "--user", "enable"]
        if native["active"]:
            command.append("--now")
        command.append("fleet-update.timer")
        if run_native(command, home, "linux", native_runner).returncode:
            errors.append("enable")
    elif native["active"]:
        if run_native(["/usr/bin/systemctl", "--user", "start", "fleet-update.timer"], home, "linux", native_runner).returncode:
            errors.append("start")
    return errors


def enable_updates(root, home=None, *, platform_name=None, uid=None, native_runner=None):
    root = Path(root).resolve()
    home = fixed_home() if home is None else Path(home)
    platform = scheduler_platform(platform_name)
    uid = os.getuid() if uid is None else uid
    require_installed_fleet(home)
    validate_scheduler_sources(root, native_runner=native_runner, platform_name=platform, home=home)
    paths = updater_paths(root, home, platform)
    files = updater_files_state(paths, legacy_updater_paths(root, home, platform))
    if files == "invalid":
        fail("Fleet updater file exists but is not owned by Fleet.")
    native = snapshot_native_state(home, platform, uid, native_runner)
    if files == "not-installed" and (native["enabled"] or native["active"]):
        fail("Fleet updater native state has no matching owned files.")
    if files == "installed" and native["enabled"] and native["active"]:
        return
    previous = {path: path.read_bytes() if path.exists() else None for path in paths}
    previous["native"] = native
    if platform == "darwin":
        validate_macos_plist(home, next(iter(paths.values())), native_runner)
        prepare_macos_logs(home)
    try:
        if files == "outdated":
            if platform == "darwin" and native["active"]:
                result = run_native(["/bin/launchctl", "bootout", f"gui/{uid}", str(next(iter(paths)))], home, "darwin", native_runner)
                if result.returncode:
                    fail("Could not migrate Fleet updates.")
            elif platform == "linux" and (native["enabled"] or native["active"]):
                result = run_native(["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-update.timer"], home, "linux", native_runner)
                if result.returncode:
                    fail("Could not migrate Fleet updates.")
        for path, contents in paths.items():
            if previous[path] is None or files == "outdated":
                atomic_write(home, path, contents, 0o644)
        if platform == "darwin":
            result = run_native(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(next(iter(paths)))], home, "darwin", native_runner)
        else:
            if run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
                fail("Could not enable Fleet updates.")
            result = run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-update.timer"], home, "linux", native_runner)
        if result.returncode:
            fail("Could not enable Fleet updates.")
    except FleetError as error:
        try:
            cleanup_errors = restore_native_state(home, platform, uid, paths, previous, native_runner)
        except FleetError as cleanup_error:
            raise FleetError(f"{error} Cleanup also failed: {cleanup_error}") from error
        if cleanup_errors:
            raise FleetError(f"{error} Cleanup also failed: {', '.join(cleanup_errors)}.") from error
        raise


def disable_updates(root, home=None, *, platform_name=None, uid=None, native_runner=None):
    root = Path(root).resolve()
    home = fixed_home() if home is None else Path(home)
    platform = scheduler_platform(platform_name)
    uid = os.getuid() if uid is None else uid
    validate_scheduler_sources(root, native_runner=native_runner, platform_name=platform, home=home)
    paths = updater_paths(root, home, platform)
    files = updater_files_state(paths, legacy_updater_paths(root, home, platform))
    if files == "not-installed":
        return
    if files == "invalid":
        fail("Fleet updater file exists but is not owned by Fleet.")
    native = snapshot_native_state(home, platform, uid, native_runner)
    if platform == "darwin":
        if native["active"]:
            result = run_native(["/bin/launchctl", "bootout", f"gui/{uid}", str(next(iter(paths)))], home, "darwin", native_runner)
            if result.returncode:
                fail("Could not disable Fleet updates.")
        next(iter(paths)).unlink()
        return
    if native["enabled"] or native["active"]:
        result = run_native(["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-update.timer"], home, "linux", native_runner)
        if result.returncode:
            fail("Could not disable Fleet updates.")
    for path in paths:
        path.unlink()
    if run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, "linux", native_runner).returncode:
        for path, contents in paths.items():
            atomic_write(home, path, contents, 0o644)
        fail("Could not reload user services after disabling Fleet updates.")


def remove_node(path):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def atomic_symlink(home, target, link):
    ensure_parent(home, target)
    temporary = target.parent / f".{target.name}.fleet-{os.getpid()}"
    if os.path.lexists(temporary):
        remove_node(temporary)
    os.symlink(link, temporary)
    try:
        try:
            mode = os.lstat(target).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            remove_node(target)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            remove_node(temporary)


def compose_rules(root, host, tool):
    parts = [root / "rules" / "common.md", root / "rules" / f"{tool}.md"]
    parts.append(root / "hosts" / host / "rules.md")
    contents = [path.read_text().rstrip() for path in parts if path.exists()]
    return "\n\n".join(content for content in contents if content) + "\n"


def merged_mapping(common, overlay):
    result = deepcopy(common)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def validate_plugin_manifest(value, relative, *, allow_local=False):
    if not isinstance(value, dict) or set(value) != {"codex", "claude"}:
        fail(f"Invalid plugin manifest: {relative}")
    for client in ("codex", "claude"):
        entry = value[client]
        if not isinstance(entry, dict) or set(entry) != {"marketplaces", "plugins"}:
            fail(f"Invalid plugin manifest: {relative}")
        marketplaces = entry["marketplaces"]
        plugin_names = entry["plugins"]
        if not isinstance(marketplaces, dict) or not isinstance(plugin_names, list):
            fail(f"Invalid plugin manifest: {relative}")
        for name, source in marketplaces.items():
            if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or not isinstance(source, str):
                fail(f"Invalid plugin manifest: {relative}")
            if source.startswith("$HOME/"):
                if not allow_local or not safe_relative_parts(source[6:]):
                    fail(f"Invalid plugin marketplace source: {relative}")
            elif not GITHUB_SOURCE.fullmatch(source):
                fail(f"Invalid plugin marketplace source: {relative}")
        if plugin_names != sorted(set(plugin_names)) or any(not isinstance(name, str) or not PLUGIN_ID.fullmatch(name) for name in plugin_names):
            fail(f"Invalid plugin list: {relative}")


def load_plugin_manifest(root, host):
    root = Path(root)
    common_path = root / "plugins.json"
    try:
        common = json.loads(common_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("Invalid plugin manifest: plugins.json")
    validate_plugin_manifest(common, "plugins.json")
    result = deepcopy(common)
    host_path = root / "hosts" / host / "plugins.json"
    if host_path.exists():
        try:
            overlay = json.loads(host_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            fail(f"Invalid plugin manifest: {host_path.relative_to(root)}")
        validate_plugin_manifest(overlay, host_path.relative_to(root), allow_local=True)
        for client in ("codex", "claude"):
            for name, source in overlay[client]["marketplaces"].items():
                current = result[client]["marketplaces"].get(name)
                if current is not None and current != source:
                    fail(f"Conflicting plugin marketplace: {name}")
                result[client]["marketplaces"][name] = source
            result[client]["plugins"] = sorted(set(result[client]["plugins"]) | set(overlay[client]["plugins"]))
    return result


def plugin_client(name, home):
    home = Path(home)
    candidates = [home / ".local" / "bin" / name, home / ".npm-global" / "bin" / name]
    candidates.extend(sorted((home / ".nvm" / "versions" / "node").glob(f"*/bin/{name}"), reverse=True))
    candidates.extend(Path(path) for path in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    fail(f"Fleet could not find {name}.")


def run_plugin_command(command, home):
    environment = {
        "HOME": str(home),
        "PATH": f"{Path(command[0]).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C"),
        "NO_COLOR": "1",
    }
    for name in ("LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP"):
        if name in os.environ:
            environment[name] = os.environ[name]
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    except OSError as error:
        raise FleetError("Fleet plugin command could not start.") from error


def plugin_json(command, home, runner):
    result = runner(command, home)
    if result.returncode:
        fail(f"{Path(command[0]).name.capitalize()} plugin command failed.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"{Path(command[0]).name.capitalize()} returned invalid plugin data.")


def canonical_plugin_source(value, home):
    if value.startswith("$HOME/"):
        return str(Path(home) / value[6:])
    match = GITHUB_SOURCE.fullmatch(value)
    return f"github:{match.group(1).lower()}/{match.group(2).lower()}" if match else value


def codex_marketplace_git_source(entry, home):
    root = entry.get("root")
    if not isinstance(root, str):
        return None
    try:
        path = Path(root)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return None
        path = path.resolve(strict=True)
        path.relative_to((Path(home) / ".codex/.tmp/marketplaces").resolve())
        config = path / ".git/config"
        if not stat.S_ISREG(os.lstat(config).st_mode):
            return None
        lines = config.read_text().splitlines()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    in_origin = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped == '[remote "origin"]'
        elif in_origin:
            key, separator, value = stripped.partition("=")
            if separator and key.strip().lower() == "url":
                source = canonical_plugin_source(value.strip(), home)
                return source if source.startswith("github:") else None
    return None


def plugin_marketplaces(client, binary, home, runner):
    data = plugin_json([str(binary), "plugin", "marketplace", "list", "--json"], home, runner)
    entries = data.get("marketplaces", []) if client == "codex" and isinstance(data, dict) else data
    if not isinstance(entries, list):
        fail(f"{client.capitalize()} returned invalid marketplace data.")
    output = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            fail(f"{client.capitalize()} returned invalid marketplace data.")
        if client == "codex":
            details = entry.get("marketplaceSource", {})
            source = details.get("source") if isinstance(details, dict) else None
            if not isinstance(source, str):
                source = codex_marketplace_git_source(entry, home)
        else:
            source = entry.get("repo") if entry.get("source") == "github" else entry.get("path")
        if isinstance(source, str):
            output[entry["name"]] = canonical_plugin_source(source, home)
    return output


def installed_plugins(client, binary, home, runner):
    data = plugin_json([str(binary), "plugin", "list", "--json"], home, runner)
    entries = data.get("installed", []) if client == "codex" and isinstance(data, dict) else data
    if not isinstance(entries, list):
        fail(f"{client.capitalize()} returned invalid plugin data.")
    output = {}
    for entry in entries:
        if not isinstance(entry, dict):
            fail(f"{client.capitalize()} returned invalid plugin data.")
        name = entry.get("pluginId") if client == "codex" else entry.get("id")
        if isinstance(name, str) and (client == "codex" or entry.get("scope") == "user"):
            output[name] = entry.get("enabled") is True
    return output


def sync_plugins(root, home, host, clients=None):
    client_finder, runner = clients or (plugin_client, run_plugin_command)
    manifest = load_plugin_manifest(root, host)
    synced = {}
    for client in ("claude", "codex"):
        if not manifest[client]["marketplaces"] and not manifest[client]["plugins"]:
            synced[client] = []
            continue
        binary = client_finder(client, home)
        marketplaces = plugin_marketplaces(client, binary, home, runner)
        for name, source in manifest[client]["marketplaces"].items():
            wanted = canonical_plugin_source(source, home)
            if name in marketplaces and marketplaces[name] != wanted:
                fail(f"{client.capitalize()} marketplace source differs: {name}")
            if name not in marketplaces:
                resolved = str(Path(home) / source[6:]) if source.startswith("$HOME/") else source
                command = [str(binary), "plugin", "marketplace", "add", resolved]
                if client == "codex":
                    command.append("--json")
                else:
                    command.extend(["--scope", "user"])
                result = runner(command, home)
                if result.returncode:
                    fail(f"Could not add {client.capitalize()} marketplace: {name}")
                marketplaces = plugin_marketplaces(client, binary, home, runner)
                if marketplaces.get(name) != wanted:
                    fail(f"{client.capitalize()} marketplace name differs: {name}")
        installed = installed_plugins(client, binary, home, runner)
        for name in manifest[client]["plugins"]:
            if installed.get(name) is True:
                continue
            if client == "codex":
                command = [str(binary), "plugin", "add", name, "--json"]
            elif name in installed:
                command = [str(binary), "plugin", "enable", name, "--scope", "user"]
            else:
                command = [str(binary), "plugin", "install", name, "--scope", "user", "--yes"]
            result = runner(command, home)
            if result.returncode:
                fail(f"Could not install or enable plugin: {name}")
        installed = installed_plugins(client, binary, home, runner)
        missing = [name for name in manifest[client]["plugins"] if installed.get(name) is not True]
        if missing:
            fail(f"{client.capitalize()} plugin is not enabled: {missing[0]}")
        synced[client] = list(manifest[client]["plugins"])
    return synced


def load_settings(root, host):
    codex = tomllib.loads((root / "settings" / "codex.toml").read_text())
    host_codex = root / "hosts" / host / "codex.toml"
    if host_codex.exists():
        codex = merged_mapping(codex, tomllib.loads(host_codex.read_text()))
    claude = json.loads((root / "settings" / "claude.json").read_text())
    host_claude = root / "hosts" / host / "claude.json"
    if host_claude.exists():
        claude = merged_mapping(claude, json.loads(host_claude.read_text()))
    return codex, claude


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    fail("Unsupported managed TOML value.")


def strip_codex_managed(text, managed_servers, settings):
    if '\"\"\"' in text or "'''" in text or re.search(r"(?m)^\s*features\.[A-Za-z0-9_-]+\s*=", text):
        fail("Unsupported or ambiguous Codex TOML form.")
    lines = text.splitlines()
    output = []
    section = ()
    skip_table = False
    table = re.compile(r"^\s*\[\[?(.+?)\]\]?\s*(?:#.*)?$")
    assignment = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    for line in lines:
        match = table.match(line)
        if match:
            raw = match.group(1)
            section = tuple(part.strip().strip('"') for part in raw.split("."))
            skip_table = (
                len(section) >= 2
                and section[0] == "mcp_servers"
                and section[1] in managed_servers
            )
            if skip_table:
                continue
        if skip_table:
            continue
        match = assignment.match(line)
        if match and (
            (not section and match.group(1) in settings)
            or (section == ("features",) and match.group(1) in settings.get("features", {}))
        ):
            continue
        output.append(line)
    return "\n".join(output).rstrip()


def codex_unmanaged(data, managed_servers, settings):
    value = deepcopy(data)
    for key in settings:
        if key != "features":
            value.pop(key, None)
    features = value.get("features")
    if isinstance(features, dict):
        for key in settings.get("features", {}):
            features.pop(key, None)
        if not features:
            value.pop("features", None)
    servers = value.get("mcp_servers")
    if isinstance(servers, dict):
        for name in managed_servers:
            servers.pop(name, None)
        if not servers:
            value.pop("mcp_servers", None)
    return value


def render_codex(current, settings, servers, source_blocks):
    try:
        before = tomllib.loads(current) if current.strip() else {}
    except tomllib.TOMLDecodeError:
        fail("Invalid existing Codex configuration.")
    managed_servers = set(source_blocks)
    base = strip_codex_managed(current, managed_servers, settings)
    lines = base.splitlines() if base else []
    scalars = [f"{key} = {toml_value(settings[key])}" for key in CODEX_SCALARS if key in settings]
    if scalars:
        first_table = next((index for index, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
        while first_table and not lines[first_table - 1].strip():
            lines.pop(first_table - 1)
            first_table -= 1
        lines[first_table:first_table] = scalars + ([""] if first_table < len(lines) else [])
    features = settings.get("features", {})
    if features:
        feature_lines = [f"{key} = {toml_value(features[key])}" for key in CODEX_FEATURES if key in features]
        header = next((index for index, line in enumerate(lines) if re.match(r"^\s*\[features\]\s*(?:#.*)?$", line)), None)
        if header is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[features]", *feature_lines])
        else:
            lines[header + 1:header + 1] = feature_lines
    chunks = ["\n".join(lines).rstrip()] if lines else []
    for name in sorted(servers):
        chunks.append(source_blocks[name].rstrip())
    rendered = "\n\n".join(chunk for chunk in chunks if chunk).rstrip() + "\n"
    try:
        after = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError:
        fail("Fleet produced invalid Codex configuration.")
    if codex_unmanaged(before, managed_servers, settings) != codex_unmanaged(after, managed_servers, settings):
        fail("Fleet would change unmanaged Codex configuration.")
    return rendered


def merge_claude_settings(current, managed):
    try:
        before = json.loads(current) if current.strip() else {}
    except json.JSONDecodeError:
        fail("Invalid existing Claude settings.")
    if not isinstance(before, dict):
        fail("Invalid existing Claude settings.")
    result = deepcopy(before)
    for key in CLAUDE_LEAVES:
        if key in managed:
            result[key] = deepcopy(managed[key])
    for parent, leaves in CLAUDE_NESTED.items():
        if parent not in managed:
            continue
        existing = result.get(parent, {})
        if not isinstance(existing, dict):
            fail("Ambiguous existing Claude settings.")
        existing = deepcopy(existing)
        for key in leaves:
            if key in managed[parent]:
                existing[key] = managed[parent][key]
        result[parent] = existing
    return json.dumps(result, indent=2, sort_keys=True) + "\n"


def claude_unmanaged(value, managed_servers):
    value = deepcopy(value)
    servers = value.get("mcpServers")
    if isinstance(servers, dict):
        for name in managed_servers:
            servers.pop(name, None)
        if not servers:
            value.pop("mcpServers", None)
    return value


def merge_claude_mcp(current, servers, managed_names):
    try:
        before = json.loads(current) if current.strip() else {}
    except json.JSONDecodeError:
        fail("Invalid existing Claude state.")
    if not isinstance(before, dict):
        fail("Invalid existing Claude state.")
    result = deepcopy(before)
    current_servers = result.get("mcpServers", {})
    if not isinstance(current_servers, dict):
        fail("Ambiguous existing Claude MCP state.")
    current_servers = deepcopy(current_servers)
    for name in managed_names:
        current_servers.pop(name, None)
    current_servers.update(servers)
    result["mcpServers"] = current_servers
    if claude_unmanaged(before, managed_names) != claude_unmanaged(result, managed_names):
        fail("Fleet would change unmanaged Claude state.")
    return json.dumps(result, indent=2, sort_keys=True) + "\n"


def codex_blocks(path):
    text = path.read_text()
    starts = list(re.finditer(r"(?m)^\[mcp_servers\.([a-z0-9-]+)\]\s*$", text))
    return {match.group(1): text[match.start() : (starts[index + 1].start() if index + 1 < len(starts) else len(text))].strip() for index, match in enumerate(starts)}


def managed_mcp(root, host, credential_reader):
    codex = tomllib.loads((root / "mcp" / "codex.toml").read_text())["mcp_servers"]
    claude = json.loads((root / "mcp" / "claude.json").read_text())["mcpServers"]
    blocks = codex_blocks(root / "mcp" / "codex.toml")
    runners = mcp.load_runners(root, host)
    host_dir = root / "hosts" / host / "mcp"
    if (host_dir / "codex.toml").exists():
        codex.update(tomllib.loads((host_dir / "codex.toml").read_text())["mcp_servers"])
        claude.update(json.loads((host_dir / "claude.json").read_text())["mcpServers"])
        blocks.update(codex_blocks(host_dir / "codex.toml"))
    all_names = set(codex)
    selected = {}
    skipped = {}
    for name in sorted(all_names):
        secrets = runners.get(name, {}).get("secrets", [])
        missing = []
        for secret in secrets:
            try:
                credential_reader(secret)
            except (MissingCredential, FleetError):
                missing.append(secret)
        if missing:
            skipped[name] = missing
        else:
            selected[name] = True
    return (
        {name: codex[name] for name in selected},
        {name: claude[name] for name in selected},
        {name: blocks[name] for name in all_names},
        all_names,
        skipped,
    )


def read_existing(path, default):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return default
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        fail("Unsafe Fleet configuration target.")
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        fail("Invalid existing Fleet configuration.")


def path_operation(home, relative, *, data=None, link=None, source=None, mode=0o600):
    target = safe_target(home, relative)
    return {"relative": str(Path(relative)), "target": target, "data": data, "link": link, "source": source, "mode": mode}


def git_archive(root, sha):
    if not GIT_SHA.fullmatch(sha):
        fail("Invalid Fleet Git revision.")
    result = subprocess.run(
        ["/usr/bin/git", "archive", "--format=tar", sha],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fixed_home()), "LANG": "C", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        fail("Fleet could not read the Git revision.")
    return result.stdout


def tree_signature(root):
    output = {}
    for path in [root, *sorted(root.rglob("*"))]:
        details = os.lstat(path)
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(details.st_mode)
        if stat.S_ISDIR(details.st_mode):
            output[relative] = ["directory", mode]
        elif stat.S_ISREG(details.st_mode):
            output[relative] = ["file", mode, hashlib.sha256(path.read_bytes()).hexdigest()]
        elif stat.S_ISLNK(details.st_mode):
            output[relative] = ["symlink", mode, os.readlink(path)]
        else:
            fail("Invalid Fleet release file type.")
    return output


def build_plan(root, home, host, temporary, credential_reader, *, sha=None, archive_data=None):
    validate_repo(root)
    sha = repo_sha(root) if sha is None else sha
    if not GIT_SHA.fullmatch(sha):
        fail("Invalid Fleet Git revision.")
    if repo_dirty(root):
        fail("Fleet apply needs a clean checkout.")
    archive_data = git_archive(root, sha) if archive_data is None else archive_data
    release_stage = Path(temporary) / "release"
    release_stage.mkdir()
    extract_git_archive(archive_data, release_stage)
    check_repository(release_stage)
    source_root = release_stage
    codex_settings, claude_settings = load_settings(source_root, host)
    codex_servers, claude_servers, blocks, managed_names, skipped = managed_mcp(source_root, host, credential_reader)
    fleet_command = str(home / ".local/bin/fleet")
    for name, server in codex_servers.items():
        if server.get("command") == "fleet":
            line = 'command = "fleet"'
            if blocks[name].count(line) != 1:
                fail("Invalid Fleet MCP command block.")
            blocks[name] = blocks[name].replace(line, f"command = {toml_value(fleet_command)}")
    for server in claude_servers.values():
        if server.get("command") == "fleet":
            server["command"] = fleet_command

    codex_path = safe_target(home, ".codex/config.toml")
    claude_path = safe_target(home, ".claude/settings.json")
    state_path = safe_target(home, ".claude.json")
    codex_text = render_codex(read_existing(codex_path, ""), codex_settings, codex_servers, blocks)
    claude_text = merge_claude_settings(read_existing(claude_path, "{}"), claude_settings)
    claude_state = merge_claude_mcp(read_existing(state_path, "{}"), claude_servers, managed_names)

    operations = []
    release_relative = Path(".local/share/fleet/releases") / sha
    release = safe_target(home, release_relative)
    if os.path.lexists(release):
        details = os.lstat(release)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            fail("Unsafe existing Fleet release.")
        if tree_signature(release) != tree_signature(release_stage):
            fail("Existing Fleet release does not match Git.")
    else:
        operations.append(path_operation(home, release.relative_to(home), source=release_stage, mode=0o700))
    operations.extend(
        [
            path_operation(home, ".local/share/fleet/current", link=f"releases/{sha}"),
            path_operation(home, ".local/bin/fleet", link="../share/fleet/current/bin/fleet"),
            path_operation(home, ".codex/AGENTS.md", data=compose_rules(source_root, host, "codex").encode(), mode=0o644),
            path_operation(home, ".claude/CLAUDE.md", data=compose_rules(source_root, host, "claude").encode(), mode=0o644),
        ]
    )
    statusline = source_root / "hooks" / "statusline.sh"
    if statusline.exists():
        target = home / ".claude" / "statusline.sh"
        operations.append(path_operation(home, ".claude/statusline.sh", link=os.path.relpath(home / ".local/share/fleet/current/hooks/statusline.sh", target.parent)))
    for hook in sorted((source_root / "hooks").glob("*.sh")):
        if hook.name == "statusline.sh":
            continue
        source = home / ".local/share/fleet/current" / hook.relative_to(source_root)
        for client in (".codex", ".claude"):
            relative = Path(client) / "hooks" / hook.name
            operations.append(path_operation(home, relative, link=os.path.relpath(source, (home / relative).parent)))
    host_hooks = source_root / "hosts" / host / "hooks"
    if host_hooks.exists():
        for hook in sorted(host_hooks.glob("*.sh")):
            source = home / ".local/share/fleet/current" / "hosts" / host / "hooks" / hook.name
            for client in (".codex", ".claude"):
                relative = Path(client) / "hooks" / hook.name
                operations.append(path_operation(home, relative, link=os.path.relpath(source, (home / relative).parent)))
    codex_hooks = source_root / "hosts" / host / "codex-hooks.json"
    if codex_hooks.exists():
        operations.append(path_operation(home, ".codex/hooks.json", data=codex_hooks.read_bytes(), mode=0o600))
    skill_sources = {}
    for directory in (source_root / "skills", source_root / "hosts" / host / "skills"):
        if not directory.exists():
            continue
        for skill in sorted(directory.iterdir()):
            if skill.name.startswith("."):
                continue
            if not skill.is_dir() or skill.is_symlink() or skill.name in skill_sources:
                fail("Invalid or conflicting Fleet skill source.")
            skill_sources[skill.name] = skill
    for name, skill in sorted(skill_sources.items()):
        central = Path(".agents/skills") / skill.name
        central_target = home / central
        source = home / ".local/share/fleet/current" / skill.relative_to(source_root)
        operations.append(path_operation(home, central, link=os.path.relpath(source, central_target.parent)))
        for client in (".codex", ".claude"):
            relative = Path(client) / "skills" / skill.name
            operations.append(path_operation(home, relative, link=os.path.relpath(central_target, (home / relative).parent)))
    for command in sorted((source_root / "commands").glob("*.md")):
        for directory in (".codex/prompts", ".claude/commands"):
            relative = Path(directory) / command.name
            operations.append(path_operation(home, relative, link=os.path.relpath(home / ".local/share/fleet/current/commands" / command.name, (home / relative).parent)))
    operations.extend(
        [
            path_operation(home, ".codex/config.toml", data=codex_text.encode(), mode=0o600),
            path_operation(home, ".claude/settings.json", data=claude_text.encode(), mode=0o600),
            path_operation(home, ".claude.json", data=claude_state.encode(), mode=0o600),
        ]
    )
    applied_credentials = sorted({secret for name, entry in mcp.load_runners(source_root, host).items() if name in codex_servers for secret in entry.get("secrets", [])})
    return {
        "sha": sha,
        "host": host,
        "operations": operations,
        "state_roots": sorted({str(release_relative), *(operation["relative"] for operation in operations)}),
        "managed_mcp": sorted(managed_names),
        "skipped": skipped,
        "applied_credentials": applied_credentials,
    }


def fingerprint_node(path, base, output, managed_mcp):
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return
    relative = str(path.relative_to(base))
    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISLNK(details.st_mode):
        output[relative] = ["symlink", mode, os.readlink(path)]
    elif stat.S_ISREG(details.st_mode):
        contents = path.read_bytes()
        if relative == ".codex/config.toml":
            try:
                config = tomllib.loads(contents.decode())
                server_names = set(managed_mcp)
                managed = {key: config[key] for key in CODEX_SCALARS if key in config}
                features = config.get("features", {})
                if not isinstance(features, dict):
                    raise TypeError
                managed["features"] = {key: features[key] for key in CODEX_FEATURES if key in features}
                servers = config.get("mcp_servers", {})
                hooks = config.get("hooks", {})
                if not isinstance(servers, dict) or not isinstance(hooks, dict):
                    raise TypeError
                managed["mcp_servers"] = {name: servers[name] for name in sorted(server_names) if name in servers}
                managed["hooks"] = {name: hooks[name] for name in sorted(CODEX_HOOK_EVENTS) if name in hooks}
                contents = json.dumps(managed, sort_keys=True, separators=(",", ":")).encode()
            except (AttributeError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
                fail("Invalid Codex configuration file.")
        elif relative == ".claude.json":
            try:
                servers = json.loads(contents).get("mcpServers", {})
                names = set(managed_mcp)
                if not isinstance(servers, dict):
                    raise TypeError
                managed = {name: servers[name] for name in sorted(names) if name in servers}
                contents = json.dumps(managed, sort_keys=True, separators=(",", ":")).encode()
            except (AttributeError, TypeError, UnicodeError, json.JSONDecodeError):
                fail("Invalid Claude state file.")
        output[relative] = ["file", mode, hashlib.sha256(contents).hexdigest()]
    elif stat.S_ISDIR(details.st_mode):
        output[relative] = ["directory", mode]
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            fingerprint_node(child, base, output, managed_mcp)
    else:
        output[relative] = ["other", mode]


def live_fingerprints(home, relatives=None, managed_mcp=None):
    home = Path(home)
    managed_mcp = [] if managed_mcp is None else managed_mcp
    output = {}
    roots = relatives or [".agents", ".codex", ".claude", ".claude.json", ".local/bin/fleet", ".local/share/fleet/current", ".local/share/fleet/tools"]
    for relative in roots:
        target = safe_target(home, relative)
        fingerprint_node(target, home, output, managed_mcp)
    return output


def snapshot_node(path, home, blob_dir, entries):
    details = os.lstat(path)
    entry = {"path": str(path.relative_to(home)), "mode": stat.S_IMODE(details.st_mode)}
    if stat.S_ISLNK(details.st_mode):
        entry.update(type="symlink", link=os.readlink(path))
    elif stat.S_ISREG(details.st_mode):
        blob = str(len(list(blob_dir.iterdir())))
        contents = path.read_bytes()
        (blob_dir / blob).write_bytes(contents)
        (blob_dir / blob).chmod(0o600)
        entry.update(type="file", blob=blob, size=len(contents), sha256=hashlib.sha256(contents).hexdigest())
    elif stat.S_ISDIR(details.st_mode):
        entry["type"] = "directory"
    else:
        fail("Fleet cannot back up a special file.")
    entries.append(entry)
    if entry["type"] == "directory":
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            snapshot_node(child, home, blob_dir, entries)


def backup_id(home, old_sha):
    if old_sha is not None and not GIT_SHA.fullmatch(old_sha):
        fail("Invalid previous Fleet revision.")
    parent = backup_parent(home, create=True)
    for _ in range(10):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{stamp}-{old_sha or 'none'}-{secrets.token_hex(4)}"
        directory = parent / name
        if not os.path.lexists(directory):
            return directory
    fail("Could not allocate Fleet backup ID.")


def allowed_backup_root(relative):
    parts = Path(relative).parts
    if relative in {
        ".codex/AGENTS.md", ".claude/CLAUDE.md", ".codex/config.toml",
        ".codex/hooks.json", ".claude/settings.json", ".claude/statusline.sh",
        ".claude.json", ".local/bin/fleet",
        ".local/share/fleet/current", ".local/state/fleet/last-applied.json",
    }:
        return True
    if len(parts) == 5 and parts[:4] == (".local", "share", "fleet", "releases") and GIT_SHA.fullmatch(parts[4]):
        return True
    if len(parts) == 3 and parts[:2] == (".agents", "skills") and SAFE_NAME.fullmatch(parts[2]):
        return True
    if len(parts) == 3 and parts[0] in {".codex", ".claude"} and parts[1] == "skills" and SAFE_NAME.fullmatch(parts[2]):
        return True
    if len(parts) == 3 and parts[0] in {".codex", ".claude"} and parts[1] == "hooks" and SAFE_NAME.fullmatch(parts[2]):
        return True
    if len(parts) == 3 and ((parts[:2] == (".codex", "prompts")) or (parts[:2] == (".claude", "commands"))):
        return parts[2].endswith(".md") and SAFE_NAME.fullmatch(parts[2]) is not None
    return False


def safe_relative_parts(value):
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    parts = value.split("/")
    return not value.startswith("/") and all(part not in {"", ".", ".."} for part in parts)


def normalized_roots(relatives):
    roots = []
    for relative in relatives:
        if not isinstance(relative, str):
            fail("Invalid Fleet backup root.")
        path = Path(relative)
        if not safe_relative_parts(relative) or path.is_absolute() or str(path) != relative or not allowed_backup_root(relative):
            fail("Invalid Fleet backup root.")
        roots.append(relative)
    if len(roots) != len(set(roots)):
        fail("Invalid Fleet backup roots.")
    for left in roots:
        for right in roots:
            if left != right and Path(right).is_relative_to(Path(left)):
                fail("Overlapping Fleet backup roots.")
    return sorted(roots)


def backup_parent(home, create=False):
    parent = safe_target(home, ".local/state/fleet/backups", allow_missing=create)
    if create:
        ensure_parent(home, parent)
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError:
            pass
    try:
        details = os.lstat(parent)
    except OSError:
        fail("Fleet backup directory is unavailable.")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        fail("Unsafe Fleet backup directory.")
    return parent


def missing_parent_directories(home, roots):
    missing = set()
    for relative in roots:
        parent = Path(relative).parent
        while str(parent) != ".":
            target = home / parent
            if os.path.lexists(target):
                details = os.lstat(target)
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                    fail("Unsafe Fleet target parent.")
                break
            missing.add(str(parent))
            parent = parent.parent
    return sorted(missing)


def validate_created_parents(values, roots):
    if not isinstance(values, list) or values != sorted(set(values)):
        fail("Invalid Fleet created parent metadata.")
    for relative in values:
        if not safe_relative_parts(relative):
            fail("Invalid Fleet created parent path.")
        parent = Path(relative)
        if not any(Path(root) != parent and Path(root).is_relative_to(parent) for root in roots):
            fail("Fleet created parent is outside managed roots.")
    return values


def create_backup(home, relatives, old_sha):
    roots = normalized_roots(list(dict.fromkeys(str(Path(relative)) for relative in relatives)))
    created_parents = missing_parent_directories(home, roots)
    directory = backup_id(home, old_sha)
    os.mkdir(directory, 0o700)
    blobs = directory / "blobs"
    blobs.mkdir(mode=0o700)
    entries = []
    for relative in roots:
        target = safe_target(home, relative)
        if os.path.lexists(target):
            snapshot_node(target, home, blobs, entries)
    manifest = {"version": 2, "state": "pending", "roots": roots, "entries": entries, "created_parents": created_parents}
    atomic_write(home, directory / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    return directory


def read_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FleetError("Unsafe Fleet backup file.") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            fail("Unsafe Fleet backup file.")
        contents = os.read(descriptor, limit + 1)
        if len(contents) > limit:
            fail("Fleet backup file is too large.")
        return contents
    finally:
        os.close(descriptor)


def preflight_backup(home, directory):
    parent = backup_parent(home)
    if directory.parent != parent or not BACKUP_NAME.fullmatch(directory.name):
        fail("Unsafe Fleet backup path.")
    try:
        details = os.lstat(directory)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            fail("Unsafe Fleet backup path.")
        value = json.loads(read_regular(directory / "manifest.json", 16 * 1024 * 1024))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("Invalid Fleet backup.")
    if not isinstance(value, dict) or set(value) != {"version", "state", "roots", "entries", "created_parents"} or value["version"] != 2 or value["state"] not in {"pending", "successful"} or not isinstance(value["roots"], list) or not isinstance(value["entries"], list):
        fail("Invalid Fleet backup.")
    roots = normalized_roots(value["roots"])
    if roots != value["roots"]:
        fail("Invalid Fleet backup roots.")
    validate_created_parents(value["created_parents"], roots)
    try:
        blob_details = os.lstat(directory / "blobs")
    except OSError:
        fail("Invalid Fleet backup blobs.")
    if stat.S_ISLNK(blob_details.st_mode) or not stat.S_ISDIR(blob_details.st_mode):
        fail("Invalid Fleet backup blobs.")
    entries = {}
    blob_data = {}
    referenced = set()
    for entry in value["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("mode"), int) or not 0 <= entry["mode"] <= 0o777:
            fail("Invalid Fleet backup entry.")
        path = Path(entry["path"])
        if not safe_relative_parts(entry["path"]) or path.is_absolute() or str(path) != entry["path"] or entry["path"] in entries:
            fail("Invalid Fleet backup entry.")
        if not any(path == Path(root) or path.is_relative_to(Path(root)) for root in roots):
            fail("Fleet backup entry is outside its roots.")
        kind = entry.get("type")
        if kind == "directory":
            if set(entry) != {"path", "mode", "type"}:
                fail("Invalid Fleet directory backup.")
        elif kind == "symlink":
            if set(entry) != {"path", "mode", "type", "link"} or not isinstance(entry["link"], str) or "\0" in entry["link"]:
                fail("Invalid Fleet symlink backup.")
        elif kind == "file":
            if set(entry) != {"path", "mode", "type", "blob", "size", "sha256"} or not safe_relative_parts(entry["blob"]) or not entry["blob"].isdigit() or not isinstance(entry["size"], int) or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                fail("Invalid Fleet file backup.")
            if entry["blob"] in referenced:
                fail("Duplicate Fleet backup blob.")
            referenced.add(entry["blob"])
            contents = read_regular(directory / "blobs" / entry["blob"], max(entry["size"], 1))
            if len(contents) != entry["size"] or hashlib.sha256(contents).hexdigest() != entry["sha256"]:
                fail("Fleet backup blob hash mismatch.")
            blob_data[entry["blob"]] = contents
        else:
            fail("Invalid Fleet backup entry.")
        entries[entry["path"]] = entry
    for path, entry in entries.items():
        parent_path = str(Path(path).parent)
        if path not in roots and (parent_path not in entries or entries[parent_path]["type"] != "directory"):
            fail("Incomplete Fleet backup tree.")
    for root in roots:
        if any(Path(path).is_relative_to(Path(root)) for path in entries) and root not in entries:
            fail("Incomplete Fleet backup root.")
    actual_blobs = set()
    for path in (directory / "blobs").iterdir():
        if path.is_symlink() or not path.is_file() or not path.name.isdigit():
            fail("Invalid Fleet backup blob path.")
        actual_blobs.add(path.name)
    if actual_blobs != referenced:
        fail("Invalid Fleet backup blob set.")
    return value, blob_data


def load_manifest(home, directory):
    return preflight_backup(home, directory)[0]


def restore_backup(home, directory):
    manifest, blobs = preflight_backup(home, directory)
    root_targets = {relative: safe_target(home, relative) for relative in manifest["roots"]}
    parent_targets = {}
    for relative in manifest["created_parents"]:
        target = safe_target(home, relative)
        if os.path.lexists(target):
            details = os.lstat(target)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                fail("Unsafe Fleet created parent path.")
        parent_targets[relative] = target
    for relative in sorted(manifest["roots"], key=lambda value: len(Path(value).parts), reverse=True):
        remove_node(root_targets[relative])
    directories = []
    for entry in sorted(manifest["entries"], key=lambda value: len(Path(value["path"]).parts)):
        target = safe_target(home, entry["path"])
        if entry["type"] == "directory":
            ensure_parent(home, target)
            os.mkdir(target, entry["mode"])
            directories.append((target, entry["mode"]))
        elif entry["type"] == "file":
            atomic_write(home, target, blobs[entry["blob"]], entry["mode"])
        elif entry["type"] == "symlink":
            atomic_symlink(home, target, entry["link"])
        else:
            fail("Invalid Fleet backup entry.")
    for target, mode in reversed(directories):
        os.chmod(target, mode)
    for relative in sorted(manifest["created_parents"], key=lambda value: len(Path(value).parts), reverse=True):
        target = parent_targets[relative]
        try:
            details = os.lstat(target)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            fail("Unsafe Fleet created parent path.")
        try:
            os.rmdir(target)
        except OSError as error:
            if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise


def mark_backup(home, directory, state):
    manifest = load_manifest(home, directory)
    manifest["state"] = state
    atomic_write(home, directory / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def _recover_interrupted(home):
    home = Path(home)
    marker = safe_target(home, ".local/state/fleet/in-progress.json")
    if not os.path.lexists(marker):
        return
    try:
        details = os.lstat(marker)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            fail("Invalid Fleet recovery marker.")
        value = json.loads(marker.read_text())
        name = value["backup"]
    except (OSError, KeyError, json.JSONDecodeError):
        fail("Invalid Fleet recovery marker.")
    if not BACKUP_NAME.fullmatch(name):
        fail("Invalid Fleet recovery marker.")
    directory = safe_target(home, ".local/state/fleet/backups") / name
    restore_backup(home, directory)
    marker.unlink()


@contextmanager
def operation_lock(home):
    home = Path(home)
    lock = safe_target(home, ".local/state/fleet/lock")
    ensure_parent(home, lock)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as error:
        raise FleetError("Unsafe Fleet lock file.") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            fail("Unsafe Fleet lock file.")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FleetError("Fleet is already running.") from error
        _recover_interrupted(home)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def recover_interrupted(home):
    with operation_lock(home):
        return None


def apply_operation(home, operation):
    target = operation["target"]
    if operation["link"] is not None:
        atomic_symlink(home, target, operation["link"])
    elif operation["data"] is not None:
        atomic_write(home, target, operation["data"], operation["mode"])
    elif operation["source"] is not None:
        source = Path(operation["source"])
        ensure_parent(home, target)
        if source.is_dir():
            if os.path.lexists(target):
                fail("Fleet release already exists.")
            temporary = target.parent / f".{target.name}.fleet-{os.getpid()}"
            relative = temporary.relative_to(home)
            if temporary.parent != target.parent or safe_target(home, relative) != temporary or os.path.lexists(temporary):
                fail("Unsafe Fleet release temporary path.")
            try:
                shutil.copytree(source, temporary, symlinks=True)
                os.replace(temporary, target)
            finally:
                cleanup = safe_target(home, relative)
                if cleanup != temporary or cleanup.parent != target.parent:
                    fail("Unsafe Fleet release temporary path.")
                if os.path.lexists(cleanup):
                    remove_node(cleanup)
        else:
            atomic_write(home, target, source.read_bytes(), operation["mode"])
    else:
        fail("Invalid Fleet operation.")


def execute_plan(root, home, host, plan, credential_reader, fault):
    last_path = safe_target(home, ".local/state/fleet/last-applied.json")
    try:
        if os.path.lexists(last_path):
            details = os.lstat(last_path)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                fail("Invalid Fleet state.")
            previous = json.loads(last_path.read_text())
        else:
            previous = {}
    except (OSError, json.JSONDecodeError):
        fail("Invalid Fleet state.")
    relatives = [operation["relative"] for operation in plan["operations"]] + [str(last_path.relative_to(home))]
    backup = create_backup(home, relatives, previous.get("sha"))
    marker = safe_target(home, ".local/state/fleet/in-progress.json")
    atomic_write(home, marker, (json.dumps({"backup": backup.name}) + "\n").encode(), 0o600)
    old_handlers = {}
    def interrupted(signum, frame):
        raise InterruptedError("Fleet interrupted")
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupted)
        for index, operation in enumerate(plan["operations"], 1):
            apply_operation(home, operation)
            if fault:
                fault(index, operation["target"])
        if (Path(root) / "plugins.json").exists():
            plan["plugins"] = sync_plugins(root, home, host)
        roots = plan["state_roots"]
        fingerprints = live_fingerprints(home, roots, plan["managed_mcp"])
        state = {
            "sha": plan["sha"],
            "host": host,
            "repository": str(root),
            "roots": roots,
            "fingerprints": fingerprints,
            "skipped": plan["skipped"],
            "applied_credentials": plan["applied_credentials"],
            "credential_presence": credential_presence(root, host, credential_reader),
            "managed_mcp": plan["managed_mcp"],
        }
        atomic_write(home, last_path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        mark_backup(home, backup, "successful")
        marker.unlink()
    except BaseException as error:
        restore_backup(home, backup)
        if marker.exists():
            marker.unlink()
        if isinstance(error, FleetError):
            raise
        raise FleetError("Fleet apply failed; previous state was restored.") from error
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def apply_locked(root, home, host, credential_reader, fault=None, *, sha=None, archive_data=None):
    with tempfile.TemporaryDirectory(prefix="fleet-build-") as temporary:
        plan = build_plan(root, home, host, temporary, credential_reader, sha=sha, archive_data=archive_data)
        plan["plugins"] = {}
        execute_plan(root, home, host, plan, credential_reader, fault)
        return {**plan, "dry_run": False}


def apply(root, home=None, *, host=None, dry_run=False, credential_reader=mcp.credential_get, fault=None):
    root = Path(root).resolve()
    home = fixed_home() if home is None else Path(home)
    host = host_id(root, host)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="fleet-build-") as temporary:
            plan = build_plan(root, home, host, temporary, credential_reader)
            print(f"host: {host}")
            for operation in plan["operations"]:
                print(f"target: ~/{operation['relative']}")
            for name in plan["managed_mcp"]:
                state = "skipped" if name in plan["skipped"] else "managed"
                print(f"mcp: {name} ({state})")
            return {**plan, "dry_run": True}
    with operation_lock(home):
        return apply_locked(root, home, host, credential_reader, fault)


def credential_presence(root, host, reader):
    runners = mcp.load_runners(root, host)
    names = sorted({secret for entry in runners.values() for secret in entry.get("secrets", [])})
    result = {}
    for name in names:
        try:
            reader(name)
            result[name] = True
        except (MissingCredential, FleetError):
            result[name] = False
    return result


def status(
    root,
    home=None,
    *,
    host=None,
    credential_reader=mcp.credential_get,
    platform_name=None,
    uid=None,
    native_runner=None,
):
    home = fixed_home() if home is None else Path(home)
    root = source_repository(root, home)
    host = host_id(root, host)
    sha = repo_sha(root)
    dirty = repo_dirty(root)
    last = safe_target(home, ".local/state/fleet/last-applied.json")
    try:
        state = json.loads(last.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    applied = state.get("sha", "none")
    roots = state.get("roots", [])
    drift = not roots or live_fingerprints(home, roots, state.get("managed_mcp")) != state.get("fingerprints")
    presence = credential_presence(root, host, credential_reader)
    skipped = state.get("skipped", {})
    missing_applied = any(not presence.get(name, False) for name in state.get("applied_credentials", []))
    print(f"host: {host}")
    print(f"repo: {sha}")
    print(f"applied: {applied}")
    print(f"dirty: {'yes' if dirty else 'no'}")
    print(f"drift: {'yes' if drift else 'no'}")
    for name, present in presence.items():
        print(f"{name}: {'set' if present else 'missing'}")
    for name in sorted(skipped):
        print(f"mcp {name}: skipped")
    updater = updater_status(root, home, platform_name=platform_name, uid=uid, native_runner=native_runner)
    print(f"updater: {updater}")
    return 1 if dirty or drift or applied != sha or bool(skipped) or missing_applied or updater != "active" else 0


def rollback(root, home=None, backup=None):
    root = Path(root).resolve()
    home = fixed_home() if home is None else Path(home)
    backups = safe_target(home, ".local/state/fleet/backups")
    if backup is not None and not BACKUP_NAME.fullmatch(backup):
        fail("Invalid Fleet backup ID.")
    if not os.path.lexists(backups):
        fail("No Fleet backup is available.")
    backups = backup_parent(home)
    candidates = []
    for directory in backups.iterdir():
        if directory.is_dir() and not directory.is_symlink() and (backup is None or directory.name == backup):
            try:
                if load_manifest(home, directory)["state"] == "successful":
                    candidates.append(directory)
            except FleetError:
                continue
    if not candidates:
        fail("No Fleet backup is available.")
    selected = sorted(candidates)[-1]
    with operation_lock(home):
        manifest = load_manifest(home, selected)
        try:
            last = json.loads((safe_target(home, ".local/state/fleet/last-applied.json")).read_text())
        except (OSError, json.JSONDecodeError):
            last = {}
        current = create_backup(home, manifest["roots"], last.get("sha"))
        marker = safe_target(home, ".local/state/fleet/in-progress.json")
        atomic_write(home, marker, (json.dumps({"backup": current.name}) + "\n").encode(), 0o600)
        try:
            restore_backup(home, selected)
            mark_backup(home, current, "successful")
            marker.unlink()
        except BaseException as error:
            restore_backup(home, current)
            if marker.exists():
                marker.unlink()
            raise FleetError("Fleet rollback failed; current state was restored.") from error
    return selected.name


def extract_git_archive(data, destination):
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError:
        fail("Invalid Fleet Git archive.")
    with archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
                fail("Unsafe Fleet Git archive.")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                target.write_bytes(source.read() if source else b"")
                target.chmod(member.mode & 0o777)
            else:
                fail("Unsafe Fleet Git archive.")


def applied_state_matches(home, sha, host):
    last = safe_target(home, ".local/state/fleet/last-applied.json")
    try:
        details = os.lstat(last)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return False
        state = json.loads(last.read_text())
        roots = state.get("roots")
        fingerprints = state.get("fingerprints")
        return (
            state.get("sha") == sha
            and state.get("host") == host
            and isinstance(roots, list)
            and bool(roots)
            and isinstance(fingerprints, dict)
            and live_fingerprints(home, roots, state.get("managed_mcp")) == fingerprints
        )
    except (FleetError, OSError, TypeError, json.JSONDecodeError):
        return False


def update(root, home=None, *, host=None, credential_reader=mcp.credential_get):
    home = fixed_home() if home is None else Path(home)
    root = source_repository(root, home)
    host = host_id(root, host)
    with operation_lock(home):
        branch = git(root, "branch", "--show-current").stdout.strip()
        if branch != "main" or repo_dirty(root):
            fail("Fleet update needs a clean main checkout.")
        git(root, "fetch", "--prune", "origin", "refs/heads/live")
        captured = git(root, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
        if not GIT_SHA.fullmatch(captured):
            fail("Invalid fetched Fleet revision.")
        if git(root, "merge-base", "--is-ancestor", "HEAD", captured, check=False).returncode:
            fail("Fleet update is not a fast-forward.")
        archive = git_archive(root, captured)
        with tempfile.TemporaryDirectory(prefix="fleet-update-") as temporary:
            candidate = Path(temporary)
            extract_git_archive(archive, candidate)
            check_repository(candidate)
        if captured == repo_sha(root) and applied_state_matches(home, captured, host):
            return {"sha": captured, "host": host, "noop": True}
        git(root, "merge", "--ff-only", captured)
        if repo_sha(root) != captured:
            fail("Fleet did not merge the captured revision.")
        return apply_locked(root, home, host, credential_reader, sha=captured, archive_data=archive)


def parse_host(arguments):
    host = None
    output = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--host":
            if host is not None or index + 1 >= len(arguments):
                fail("Invalid --host option.")
            host = arguments[index + 1]
            index += 2
        else:
            output.append(arguments[index])
            index += 1
    return host, output


def main():
    if len(sys.argv) < 3:
        fail("Missing Fleet command.")
    root = Path(sys.argv[1]).resolve()
    command = sys.argv[2]
    host, arguments = parse_host(sys.argv[3:])
    if command == "check":
        if host is not None or arguments:
            fail("Usage: fleet check")
        check_repository(root)
        return 0
    if command == "apply":
        dry_run = False
        if "--dry-run" in arguments:
            arguments.remove("--dry-run")
            dry_run = True
        if arguments:
            fail("Usage: fleet apply [--dry-run] [--host HOST]")
        apply(root, host=host, dry_run=dry_run)
        return 0
    if command == "status":
        if arguments:
            fail("Usage: fleet status [--host HOST]")
        return status(root, host=host)
    if command == "update":
        if arguments:
            fail("Usage: fleet update [--host HOST]")
        update(root, host=host)
        return 0
    if command == "enable-updates":
        if host is not None or arguments:
            fail("Usage: fleet enable-updates")
        enable_updates(root)
        return 0
    if command == "disable-updates":
        if host is not None or arguments:
            fail("Usage: fleet disable-updates")
        disable_updates(root)
        return 0
    if command == "rollback":
        if len(arguments) > 1:
            fail("Usage: fleet rollback [BACKUP-ID]")
        rollback(root, backup=arguments[0] if arguments else None)
        return 0
    fail("Unknown Fleet command.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
