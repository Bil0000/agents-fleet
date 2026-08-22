#!/usr/bin/env python3
import getpass
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


class FleetError(Exception):
    pass


class MissingCredential(FleetError):
    pass


NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
SERVER = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STATIC_CREDENTIAL_WORDS = ("key", "authorization", "header", "token", "password", "secret")
MAC_KEYCHAIN_WRITE = r"""
log_user 0
set timeout 15
if {[gets stdin account] < 0 || $account eq ""} { exit 64 }
if {[gets stdin secret] < 0 || $secret eq ""} { exit 64 }
spawn /usr/bin/security add-generic-password -U -a $account -s dev.agents-fleet -w
expect {
    -nocase -re "password" { send -- "$secret\r" }
    timeout { exit 124 }
    eof { exit 1 }
}
expect {
    -nocase -re "password" { send -- "$secret\r" }
    timeout { exit 124 }
    eof { exit 1 }
}
expect {
    eof {}
    timeout { exit 124 }
}
set result [wait]
exit [lindex $result 3]
"""


def fail(message):
    raise FleetError(message)


def required_names(root):
    path = root / "mcp" / "required-secrets.txt"
    names = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if names != sorted(set(names)) or any(not NAME.fullmatch(name) for name in names):
        fail("Invalid Fleet secret registry.")
    return names


def host_id():
    value = socket.gethostname().split(".", 1)[0].lower().replace("_", "-")
    if not SERVER.fullmatch(value):
        fail("Invalid Fleet host name.")
    return value


def load_runner_files(root, files):
    servers = {}
    for path in files:
        data = tomllib.loads(path.read_text())
        for server, entry in data["servers"].items():
            if server in servers:
                fail(f"Invalid MCP runner: {server}")
            servers[server] = entry
    return servers


def load_runners(root, host):
    files = [root / "mcp" / "runners.toml"]
    host_file = root / "hosts" / host / "mcp" / "runners.toml"
    if host_file.exists():
        files.append(host_file)
    return load_runner_files(root, files)


def reject_static_credentials(value, allow_oauth=False):
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        lowered = key.lower()
        if allow_oauth and lowered == "auth" and child == "oauth":
            continue
        if "auth" in lowered or any(word in lowered for word in STATIC_CREDENTIAL_WORDS):
            fail(f"Static MCP credential field is not allowed: {key}")
        reject_static_credentials(child)


def reject_unsafe_urls(value):
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if isinstance(child, str) and ("url" in key.lower() or child.startswith(("http://", "https://"))):
            parsed = urlsplit(child)
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                fail(f"Unsafe managed URL: {key}")
        reject_unsafe_urls(child)


def reject_credential_args(value):
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if key in {"args", "command"} and isinstance(child, list):
            for argument in child:
                lowered = argument.lower() if isinstance(argument, str) else ""
                if isinstance(argument, str) and (
                    argument in {"-H", "--header"}
                    or lowered.startswith(("authorization:", "proxy-authorization:"))
                    or re.search(r"(?i)(?:^|[-_])(key|auth|header|token|password|secret)(?:$|[-_=])", argument)
                ):
                    fail(f"Credential-like managed argument is not allowed: {argument}")
        reject_credential_args(child)


def validate_mcp_file(root, path, client):
    data = tomllib.loads(path.read_text()) if path.suffix == ".toml" else json.loads(path.read_text())
    key = "mcp_servers" if client == "codex" else "mcpServers"
    if not isinstance(data, dict) or set(data) != {key} or not isinstance(data[key], dict):
        fail(f"Invalid managed MCP manifest: {path.relative_to(root)}")
    for entry in data[key].values():
        reject_static_credentials(entry, allow_oauth=client == "codex")
    reject_unsafe_urls(data)
    reject_credential_args(data)
    if any(not SERVER.fullmatch(name) or not isinstance(entry, dict) for name, entry in data[key].items()):
        fail(f"Invalid managed MCP manifest: {path.relative_to(root)}")
    return data[key]


def validate_runner_file(root, path):
    data = tomllib.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"servers"} or not isinstance(data["servers"], dict):
        fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
    for name, entry in data["servers"].items():
        if not SERVER.fullmatch(name) or not isinstance(entry, dict):
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        allowed = {"secrets", "command", "cwd", "env"}
        valid_shape = {"secrets", "command"}.issubset(entry) and not set(entry) - allowed
        if not valid_shape:
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        secrets = entry.get("secrets")
        if not isinstance(secrets, list) or secrets != sorted(set(secrets)) or any(not NAME.fullmatch(secret) for secret in secrets):
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        arguments = entry.get("command")
        if not isinstance(arguments, list) or not arguments or any(not isinstance(value, str) or not value for value in arguments):
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        if "cwd" in entry and (not isinstance(entry["cwd"], str) or not Path(entry["cwd"]).is_absolute()):
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        if "env" in entry and (not isinstance(entry["env"], dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in entry["env"].items())):
            fail(f"Invalid managed MCP runner manifest: {path.relative_to(root)}")
        reject_static_credentials(entry.get("env", {}))
    reject_unsafe_urls(data)
    reject_credential_args(data)
    return data["servers"]


def validate_mcp_entry(name, entries, paths):
    codex_entry, claude_entry, runner = entries
    codex_path, runner_path = paths
    if not SERVER.fullmatch(name) or not isinstance(codex_entry, dict) or not isinstance(claude_entry, dict):
        fail(f"Invalid managed MCP manifest: {codex_path}")
    if "url" in codex_entry or "url" in claude_entry:
        codex_keys = {"url"} if "auth" not in codex_entry else {"url", "auth"}
        if (
            set(codex_entry) != codex_keys
            or set(claude_entry) != {"type", "url"}
            or claude_entry.get("type") != "http"
            or codex_entry["url"] != claude_entry.get("url")
            or ("auth" in codex_entry and codex_entry["auth"] != "oauth")
            or runner is not None
        ):
            fail(f"Invalid managed MCP manifest: {codex_path}")
        return
    if not isinstance(runner, dict):
        fail(f"Invalid managed MCP runner manifest: {runner_path}")
    wrapper = ["mcp", "run", name, "--", *runner["command"]]
    if codex_entry != {"command": "fleet", "args": wrapper} or claude_entry != {
        "type": "stdio",
        "command": "fleet",
        "args": wrapper,
    }:
        fail(f"Invalid managed MCP manifest: {codex_path}")


def validate_mcp_bundle(root, directory):
    codex_path = directory / "codex.toml"
    claude_path = directory / "claude.json"
    runner_path = directory / "runners.toml"
    codex = validate_mcp_file(root, codex_path, "codex")
    claude = validate_mcp_file(root, claude_path, "claude")
    runners = validate_runner_file(root, runner_path)
    if set(codex) != set(claude):
        fail("MCP servers need matching Codex and Claude entries.")
    stdio = {name for name, entry in codex.items() if "url" not in entry}
    if set(runners) != stdio:
        fail(f"Invalid managed MCP runner manifest: {runner_path.relative_to(root)}")
    paths = (codex_path.relative_to(root), runner_path.relative_to(root))
    for name in codex:
        validate_mcp_entry(name, (codex[name], claude[name], runners.get(name)), paths)
    return runners


def validate_all(root):
    mcp_entries = sorted(path.relative_to(root).as_posix() for path in (root / "mcp").iterdir())
    if mcp_entries != [
        "mcp/README.md",
        "mcp/claude.json",
        "mcp/codex.toml",
        "mcp/required-secrets.txt",
        "mcp/runners.toml",
    ]:
        fail("Invalid managed MCP files.")
    registered = set(required_names(root))
    runners = validate_mcp_bundle(root, root / "mcp")
    for directory in sorted((root / "hosts").glob("*/mcp")):
        entries = sorted(path.name for path in directory.iterdir())
        if entries != ["claude.json", "codex.toml", "runners.toml"]:
            fail("Invalid managed MCP host overlay.")
        runners.update(validate_mcp_bundle(root, directory))
    used = {secret for entry in runners.values() for secret in entry["secrets"]}
    missing = sorted(used - registered)
    if missing:
        fail(f"Unregistered MCP secret: {missing[0]}")


def fixed_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def runtime_path(home):
    return os.pathsep.join(
        [
            str(home / ".local" / "bin"),
            str(home / ".npm-global" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    )


def child_environment(ambient=None, home=None):
    ambient = os.environ if ambient is None else ambient
    home = fixed_home() if home is None else Path(home)
    environment = {"HOME": str(home), "PATH": runtime_path(home)}
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
        value = ambient.get(name)
        if isinstance(value, str) and value and "\0" not in value:
            environment[name] = value
    return environment


def resolve_runner_root(root, entry):
    return Path(entry["cwd"]) if "cwd" in entry else root.resolve()


def platform_name():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    fail("Fleet credentials support only macOS and Linux.")


def credential_directory(home, create):
    home = Path(home)
    try:
        if stat.S_ISLNK(os.lstat(home).st_mode):
            fail("Unsafe Fleet credential path.")
    except FileNotFoundError:
        fail("Fleet home directory is unavailable.")
    current = home
    for index, part in enumerate((".config", "fleet", "credentials")):
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if not create:
                raise MissingCredential(str(current))
            os.mkdir(current, 0o700)
            mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            fail("Unsafe Fleet credential path.")
        if create and index > 0:
            os.chmod(current, 0o700)
    return current


def credential_path(name, home, create=False):
    return credential_directory(home, create) / f"{name}.cred"


def read_secret():
    if sys.stdin.isatty():
        value = getpass.getpass("Secret: ").encode()
    else:
        value = sys.stdin.buffer.read()
        if value.endswith(b"\n"):
            value = value[:-1]
        if value.endswith(b"\r"):
            value = value[:-1]
    if not value or b"\0" in value or b"\n" in value or b"\r" in value:
        fail("Secret must be one non-empty line.")
    return value


def run_native(arguments, stdin=None):
    return subprocess.run(
        arguments,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment(),
    )


def credential_set(name, secret, *, platform=None, runner=run_native, home=None):
    platform = platform_name() if platform is None else platform
    home = fixed_home() if home is None else Path(home)
    if platform == "macos":
        result = runner(
            [
                "/usr/bin/expect",
                "-c",
                MAC_KEYCHAIN_WRITE,
            ],
            name.encode() + b"\n" + secret + b"\n",
        )
        if result.returncode:
            fail(f"Could not store Fleet credential: {name}")
        return

    if platform != "linux":
        fail("Fleet credentials support only macOS and Linux.")
    path = credential_path(name, home, create=True)
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            fail(f"Unsafe Fleet credential file: {name}")
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    os.fchmod(descriptor, 0o600)
    try:
        result = runner(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/systemd-creds",
                f"--name={name}",
                "--with-key=host",
                "encrypt",
                "-",
                "-",
            ],
            secret + b"\n",
        )
        if result.returncode or not result.stdout:
            fail(f"Could not store Fleet credential: {name}")
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(result.stdout)
            output.flush()
            os.fsync(output.fileno())
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                fail(f"Unsafe Fleet credential file: {name}")
        except FileNotFoundError:
            pass
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def credential_get(name, *, platform=None, runner=run_native, home=None):
    platform = platform_name() if platform is None else platform
    home = fixed_home() if home is None else Path(home)
    if platform == "macos":
        result = runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                name,
                "-s",
                "dev.agents-fleet",
                "-w",
            ],
            None,
        )
    elif platform == "linux":
        path = credential_path(name, home)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            fail(f"Unsafe or missing Fleet credential file: {name}")
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
                fail(f"Unsafe Fleet credential permissions: {name}")
            ciphertext = os.read(descriptor, 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        if not ciphertext or len(ciphertext) > 1024 * 1024:
            fail(f"Unsafe Fleet credential permissions: {name}")
        result = runner(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/systemd-creds",
                f"--name={name}",
                "decrypt",
                "-",
                "-",
            ],
            ciphertext,
        )
    else:
        fail("Fleet credentials support only macOS and Linux.")
    if result.returncode:
        raise MissingCredential(name)
    value = result.stdout.rstrip(b"\r\n")
    if not value or b"\0" in value or b"\n" in value or b"\r" in value:
        raise MissingCredential(name)
    try:
        return value.decode()
    except UnicodeDecodeError:
        raise MissingCredential(name) from None


def auth(root, arguments):
    if not arguments or arguments[0] not in {"set", "status"}:
        fail("Usage: fleet auth set NAME | fleet auth status [NAME]")
    required = required_names(root)
    action = arguments[0]
    names = arguments[1:]
    if action == "set" and len(names) != 1:
        fail("Usage: fleet auth set NAME")
    if action == "status" and len(names) > 1:
        fail("Usage: fleet auth status [NAME]")
    if names and names[0] not in required:
        fail(f"Unknown Fleet secret: {names[0]}")

    if action == "set":
        name = names[0]
        credential_set(name, read_secret())
        print(f"{name} is set.")
        return 0

    failed = False
    for name in names or required:
        try:
            credential_get(name)
            state = "set"
        except MissingCredential:
            state = "missing"
            failed = True
        print(f"{name}: {state}")
    return 1 if failed else 0


def mcp(
    root,
    arguments,
    *,
    host=None,
    credential_reader=credential_get,
    executor=os.execvpe,
    ambient=None,
    home=None,
    trusted_root_resolver=resolve_runner_root,
):
    validate_all(root)
    if len(arguments) < 2 or arguments[0] != "run":
        fail("Usage: fleet mcp run SERVER [-- COMMAND...]")
    server = arguments[1]
    servers = load_runners(root, host_id() if host is None else host)
    if server not in servers:
        fail(f"Unknown Fleet MCP server: {server}")
    entry = servers[server]
    home = fixed_home() if home is None else Path(home)
    expected = ["run", server, "--", *entry["command"]]
    if arguments != expected:
        fail(f"Command does not match Fleet manifest for: {server}")
    command = entry["command"]
    trusted_root = Path(trusted_root_resolver(root, entry))
    if not trusted_root.is_absolute() or not trusted_root.is_dir() or trusted_root.is_symlink():
        fail(f"Trusted Fleet MCP root is unavailable: {server}")

    environment = child_environment(ambient, home)
    environment.update(entry.get("env", {}))
    for name in entry["secrets"]:
        try:
            environment[name] = credential_reader(name)
        except MissingCredential:
            fail(f"Missing Fleet credential: {name}")
    previous = os.open(".", os.O_RDONLY)
    try:
        os.chdir(trusted_root)
        executor(command[0], command, environment)
    except OSError:
        fail(f"Could not start Fleet MCP server: {server}")
    finally:
        os.fchdir(previous)
        os.close(previous)


def main():
    if len(sys.argv) < 3:
        fail("Missing Fleet command.")
    root = Path(sys.argv[1]).resolve()
    command = sys.argv[2]
    arguments = sys.argv[3:]
    if command == "validate":
        validate_all(root)
        return 0
    if command == "auth":
        return auth(root, arguments)
    if command == "mcp":
        return mcp(root, arguments)
    fail("Unknown Fleet command.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        print("Invalid or unavailable Fleet files.", file=sys.stderr)
        raise SystemExit(1)
