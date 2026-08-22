#!/usr/bin/env python3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import json
import os
import plistlib
import pwd
import re
import selectors
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.error
import urllib.request


db_spec = importlib.util.spec_from_file_location("fleet_memory_db", Path(__file__).with_name("fleet_memory_db.py"))
db = importlib.util.module_from_spec(db_spec)
db_spec.loader.exec_module(db)


class FleetMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryConfig:
    version: str
    server_host: str
    client_host: str
    ssh_target: str
    worker_host: str
    worker_port: int
    tunnel_port: int
    backup_retention_days: int


def load_config(root):
    path = Path(root) / "memory.toml"
    if not path.is_file():
        raise FleetMemoryError(
            "Shared memory is not configured. Copy memory.example.toml to memory.toml."
        )
    try:
        values = tomllib.loads(path.read_text())
        config = MemoryConfig(**values)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError) as error:
        raise FleetMemoryError("Invalid Fleet memory config.") from error
    if config.worker_host != "127.0.0.1":
        raise FleetMemoryError("Fleet memory worker must use loopback.")
    host = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
    target = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+")
    version = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
    if not all(isinstance(value, str) and host.fullmatch(value) for value in (config.server_host, config.client_host)):
        raise FleetMemoryError("Invalid Fleet memory host.")
    if not isinstance(config.ssh_target, str) or not target.fullmatch(config.ssh_target):
        raise FleetMemoryError("Invalid Fleet memory SSH target.")
    if not isinstance(config.version, str) or not version.fullmatch(config.version):
        raise FleetMemoryError("Invalid Fleet memory version.")
    ports = (config.worker_port, config.tunnel_port)
    if any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in ports):
        raise FleetMemoryError("Invalid Fleet memory port.")
    if config.worker_port == config.tunnel_port:
        raise FleetMemoryError("Fleet memory ports must differ.")
    retention = config.backup_retention_days
    if isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 365:
        raise FleetMemoryError("Invalid Fleet memory backup retention.")
    return config


def safe_regular(path):
    path = Path(path)
    try:
        details = os.lstat(path)
    except FileNotFoundError as error:
        raise FleetMemoryError(f"Required file does not exist: {path}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise FleetMemoryError(f"Unsafe Fleet memory file: {path}")
    return path


def read_json(path):
    try:
        value = json.loads(safe_regular(path).read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FleetMemoryError(f"Invalid JSON file: {path}") from error
    if not isinstance(value, dict):
        raise FleetMemoryError(f"Invalid JSON object: {path}")
    return value


def atomic_json(path, value, mode):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as output:
            descriptor = -1
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def atomic_bytes(path, contents, mode):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def resolve_plugin(home, version):
    home = Path(home)
    registry = read_json(home / ".claude/plugins/installed_plugins.json")
    plugins = registry.get("plugins")
    entries = plugins.get("claude-mem@thedotmack") if isinstance(plugins, dict) else None
    if not isinstance(entries, list):
        raise FleetMemoryError(f"Claude-mem {version} is not installed.")
    base = home / ".claude/plugins/cache/thedotmack/claude-mem"
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("version") != version or not isinstance(entry.get("installPath"), str):
            continue
        install = Path(entry["installPath"])
        try:
            if stat.S_ISLNK(os.lstat(install).st_mode) or not install.is_dir():
                continue
            resolved = install.resolve(strict=True)
            resolved.relative_to(base.resolve(strict=True))
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            continue
        if resolved.name != version:
            continue
        script = resolved / "scripts/worker-service.cjs"
        try:
            if stat.S_ISREG(os.lstat(script).st_mode):
                return script
        except OSError:
            pass
    raise FleetMemoryError(f"Claude-mem {version} pinned worker is not installed.")


def configure_client(home, config):
    home = Path(home)
    settings_path = home / ".claude-mem/settings.json"
    settings = read_json(settings_path) if settings_path.exists() else {}
    settings.update(
        {
            "CLAUDE_MEM_RUNTIME": "worker",
            "CLAUDE_MEM_WORKER_HOST": config.worker_host,
            "CLAUDE_MEM_WORKER_PORT": str(config.worker_port),
        }
    )
    atomic_json(settings_path, settings, 0o600)
    marketplaces_path = home / ".claude/plugins/known_marketplaces.json"
    marketplaces = read_json(marketplaces_path)
    claude_mem = marketplaces.get("thedotmack")
    if not isinstance(claude_mem, dict):
        raise FleetMemoryError("Claude-mem marketplace is not installed.")
    claude_mem["autoUpdate"] = False
    atomic_json(marketplaces_path, marketplaces, 0o600)


def ssh_command(config):
    return [
        "/usr/bin/ssh", "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10",
        "-L", f"127.0.0.1:{config.tunnel_port}:127.0.0.1:{config.worker_port}", config.ssh_target,
    ]


def relay(left, right):
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            events = selector.select(timeout=30)
            if not events:
                continue
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    finally:
        selector.close()


def serve_guard(listen_port, tunnel_port, stop, ready=None):
    class GuardHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                upstream = socket.create_connection(("127.0.0.1", tunnel_port), timeout=5)
            except OSError:
                return
            try:
                relay(self.request, upstream)
            except OSError:
                pass
            finally:
                upstream.close()

    class GuardServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = False
        daemon_threads = True

    with GuardServer(("127.0.0.1", listen_port), GuardHandler) as server:
        server.timeout = 0.2
        if ready is not None:
            ready.set()
        while not stop.is_set():
            server.handle_request()


LINUX_UNITS = ("fleet-memory.service", "fleet-memory-backup.service", "fleet-memory-backup.timer")
MAC_LABEL = "dev.agents-fleet.memory"


def linux_sources(root):
    directory = Path(root) / "system/linux"
    output = {}
    for name in LINUX_UNITS:
        path = safe_regular(directory / name)
        output[name] = path.read_bytes()
    return output


def render_macos_plist(home):
    home = Path(home)
    logs = home / ".local/state/fleet/logs"
    return plistlib.dumps(
        {
            "Label": MAC_LABEL,
            "ProgramArguments": [str(home / ".local/bin/fleet"), "memory", "proxy"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardErrorPath": str(logs / "memory.error.log"),
            "StandardOutPath": str(logs / "memory.log"),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def run_native(command, home, platform, native_runner=None):
    if native_runner is not None:
        return native_runner(command, Path(home), platform)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C"}
    if platform == "linux":
        environment["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    try:
        return subprocess.run(command, capture_output=True, text=True, env=environment)
    except OSError as error:
        raise FleetMemoryError("Fleet memory native command could not start.") from error


def capture_files(paths):
    captured = {}
    for path in paths:
        path = Path(path)
        if not os.path.lexists(path):
            captured[path] = None
            continue
        details = os.lstat(path)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise FleetMemoryError(f"Unsafe Fleet memory target: {path}")
        captured[path] = (path.read_bytes(), stat.S_IMODE(details.st_mode))
    return captured


def restore_files(captured):
    for path, previous in captured.items():
        if previous is None:
            if os.path.lexists(path):
                os.unlink(path)
        else:
            atomic_bytes(path, previous[0], previous[1])


def require_success(result, action):
    if result.returncode:
        raise FleetMemoryError(f"Could not {action} Fleet memory.")


def enable_memory(root, home, host, *, platform_name=None, uid=None, native_runner=None):
    root = Path(root)
    home = Path(home)
    config = load_config(root)
    platform = ("darwin" if sys.platform == "darwin" else "linux") if platform_name is None else platform_name
    if platform not in {"darwin", "linux"}:
        raise FleetMemoryError("Fleet memory supports only macOS and Linux.")
    if (host, platform) not in {(config.client_host, "darwin"), (config.server_host, "linux")}:
        raise FleetMemoryError("Fleet memory host role does not match this platform.")
    resolve_plugin(home, config.version)
    settings = home / ".claude-mem/settings.json"
    marketplaces = home / ".claude/plugins/known_marketplaces.json"
    if platform == "linux":
        unit_directory = home / ".config/systemd/user"
        sources = linux_sources(root)
        managed = {unit_directory / name: contents for name, contents in sources.items()}
    else:
        managed = {home / "Library/LaunchAgents/dev.agents-fleet.memory.plist": render_macos_plist(home)}
    previous = capture_files([settings, marketplaces, *managed])
    started = False
    try:
        configure_client(home, config)
        for path, contents in managed.items():
            if previous[path] is not None and previous[path][0] != contents:
                raise FleetMemoryError(f"Fleet memory target is not owned by Fleet: {path}")
            atomic_bytes(path, contents, 0o644)
        if platform == "linux":
            require_success(run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner), "reload")
            require_success(
                run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory.service"], home, platform, native_runner),
                "enable worker",
            )
            started = True
            require_success(
                run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-backup.timer"], home, platform, native_runner),
                "enable backup timer",
            )
            for unit in ("fleet-memory.service", "fleet-memory-backup.timer"):
                result = run_native(["/usr/bin/systemctl", "--user", "is-active", unit], home, platform, native_runner)
                if result.returncode or result.stdout.strip() != "active":
                    raise FleetMemoryError(f"Fleet memory unit is not active: {unit}")
        else:
            logs = home / ".local/state/fleet/logs"
            logs.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(logs, 0o700)
            uid = os.getuid() if uid is None else uid
            target = next(iter(managed))
            require_success(
                run_native(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(target)], home, platform, native_runner),
                "enable guard",
            )
            started = True
    except BaseException:
        if platform == "linux" and started:
            run_native(
                ["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-memory-backup.timer", "fleet-memory.service"],
                home,
                platform,
                native_runner,
            )
            run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner)
        elif platform == "darwin" and started:
            run_native(["/bin/launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], home, platform, native_runner)
        restore_files(previous)
        raise


def resolve_bun(home):
    home = Path(home)
    candidates = (
        home / ".bun/bin/bun",
        home / ".local/bin/bun",
        Path("/opt/homebrew/bin/bun"),
        Path("/usr/local/bin/bun"),
        Path("/usr/bin/bun"),
    )
    for candidate in candidates:
        try:
            details = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode) and os.access(candidate, os.X_OK):
            return candidate
    raise FleetMemoryError("Fleet memory could not find Bun.")


def run_worker(home, config, *, executor=os.execve):
    home = Path(home)
    bun = resolve_bun(home)
    script = resolve_plugin(home, config.version)
    paths = (
        home / ".local/bin",
        home / ".npm-global/bin",
        home / ".bun/bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    )
    environment = {
        "HOME": str(home),
        "PATH": ":".join(str(path) for path in paths),
        "LANG": "C.UTF-8",
        "CLAUDE_MEM_RUNTIME": "worker",
        "CLAUDE_MEM_WORKER_HOST": config.worker_host,
        "CLAUDE_MEM_WORKER_PORT": str(config.worker_port),
    }
    executable = str(bun)
    executor(executable, [executable, str(script), "--daemon"], environment)


def default_process_factory(command):
    return subprocess.Popen(command, stdin=subprocess.DEVNULL)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_proxy(config, *, stop=None, process_factory=default_process_factory):
    own_stop = stop is None
    stop = threading.Event() if stop is None else stop
    previous_handlers = {}
    if own_stop:
        import signal

        def request_stop(signum, frame):
            stop.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
    process_holder = {"process": None}

    def keep_tunnel():
        try:
            while not stop.is_set():
                try:
                    process = process_factory(ssh_command(config))
                except OSError:
                    stop.wait(1)
                    continue
                process_holder["process"] = process
                while not stop.wait(0.2) and process.poll() is None:
                    pass
                if stop.is_set():
                    break
                stop.wait(1)
        finally:
            stop_process(process_holder["process"])

    keeper = threading.Thread(target=keep_tunnel, name="fleet-memory-ssh", daemon=True)
    keeper.start()
    try:
        serve_guard(config.worker_port, config.tunnel_port, stop)
    finally:
        stop.set()
        keeper.join(7)
        if keeper.is_alive():
            stop_process(process_holder["process"])
            keeper.join(2)
        if own_stop:
            import signal
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def backup_memory(home, config, *, now=None):
    home = Path(home)
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    directory = home / ".local/state/fleet/memory/backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = directory / f"daily-{now.strftime('%Y%m%dT%H%M%SZ')}.db"
    report = db.snapshot_database(home / ".claude-mem/claude-mem.db", destination)
    db.prune_daily_backups(directory, config.backup_retention_days)
    return destination, report


def latest_backup(home):
    directory = Path(home) / ".local/state/fleet/memory/backups"
    if not directory.is_dir() or directory.is_symlink():
        return None
    backups = sorted(
        path for path in directory.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and re.fullmatch(r"daily-[0-9]{8}T[0-9]{6}Z\.db", path.name)
    )
    return backups[-1] if backups else None


def worker_health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            body = response.read(65537)
    except (OSError, urllib.error.URLError):
        return None
    if len(body) > 65536:
        return None
    try:
        health_payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    return health_payload if isinstance(health_payload, dict) else None


def port_is_bound(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def pinned_plugin_ok(home, config):
    try:
        resolve_plugin(home, config.version)
        return True
    except FleetMemoryError:
        return False


def server_database_status(home):
    try:
        database_report = db.check_database(Path(home) / ".claude-mem/claude-mem.db")
        database_ok = not database_report.foreign_key_errors and database_report.schema_version == 49
        queue_ok = database_report.counts["pending"] == 0 and database_report.counts["processing"] == 0
        return database_ok, queue_ok
    except (KeyError, db.FleetMemoryDatabaseError):
        return False, False


def server_status(home, config, plugin_ok, worker_ok):
    service = run_native(["/usr/bin/systemctl", "--user", "is-active", "fleet-memory.service"], home, "linux")
    service_ok = service.returncode == 0 and service.stdout.strip() == "active"
    database_ok, queue_ok = server_database_status(home)
    backup = latest_backup(home)
    backup_ok = backup is not None and stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    lines = [
        f"service: {'active' if service_ok else 'error'}",
        f"database: {'ok' if database_ok else 'error'}",
        f"queue: {'empty' if queue_ok else 'error'}",
        f"backup: {backup.name if backup_ok else 'error'}",
    ]
    return lines, all((plugin_ok, worker_ok, service_ok, database_ok, queue_ok, backup_ok))


def client_status(home, config, plugin_ok, worker_ok):
    service = run_native(["/bin/launchctl", "print", f"gui/{os.getuid()}/{MAC_LABEL}"], home, "darwin")
    service_ok = service.returncode == 0
    guard_ok = port_is_bound(config.worker_port)
    lines = [
        f"guard: {'active' if guard_ok else 'error'}",
        f"tunnel: {'ok' if worker_ok else 'error'}",
        f"service: {'active' if service_ok else 'error'}",
    ]
    return lines, all((plugin_ok, worker_ok, service_ok, guard_ok))


def status_report(root, home, host):
    home = Path(home)
    config = load_config(root)
    platform = "darwin" if sys.platform == "darwin" else "linux"
    roles = {(config.client_host, "darwin"): "client", (config.server_host, "linux"): "server"}
    role = roles.get((host, platform))
    if role is None:
        raise FleetMemoryError("Fleet memory host role does not match this platform.")
    plugin_ok = pinned_plugin_ok(home, config)
    health_payload = worker_health(config.worker_port)
    worker_ok = isinstance(health_payload, dict) and health_payload.get("version") == config.version
    lines = [f"role: {role}", f"pinned: {config.version}", f"plugin: {'ok' if plugin_ok else 'error'}"]
    lines.append(f"worker: {config.version if worker_ok else 'error'}")
    role_lines, ok = (
        server_status(home, config, plugin_ok, worker_ok)
        if role == "server"
        else client_status(home, config, plugin_ok, worker_ok)
    )
    return lines + role_lines, ok


def status_line(root, home, host):
    try:
        _, ok = status_report(root, home, host)
    except (FleetMemoryError, OSError):
        ok = False
    return f"memory: {'ok' if ok else 'error'}", ok


def disable_memory(root, home, host, *, platform_name=None, uid=None, native_runner=None):
    home = Path(home)
    config = load_config(root)
    platform = ("darwin" if sys.platform == "darwin" else "linux") if platform_name is None else platform_name
    if (host, platform) not in {(config.client_host, "darwin"), (config.server_host, "linux")}:
        raise FleetMemoryError("Fleet memory host role does not match this platform.")
    if platform == "linux":
        require_success(
            run_native(
                [
                    "/usr/bin/systemctl", "--user", "disable", "--now",
                    "fleet-memory-backup.timer", "fleet-memory.service",
                ],
                home,
                platform,
                native_runner,
            ),
            "disable services",
        )
        require_success(run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner), "reload")
        return
    uid = os.getuid() if uid is None else uid
    result = run_native(["/bin/launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], home, platform, native_runner)
    if result.returncode not in {0, 113}:
        raise FleetMemoryError("Could not disable Fleet memory guard.")


USAGE = "Usage: fleet memory status | backup | enable | disable"


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 2 or arguments[1] not in {"status", "backup", "enable", "disable", "proxy", "worker"}:
        print(USAGE, file=sys.stderr)
        return 2
    root = Path(arguments[0]).resolve()
    command = arguments[1]
    rest = arguments[2:]
    host = None
    if "--host" in rest:
        index = rest.index("--host")
        if index + 1 >= len(rest) or host is not None:
            print(USAGE, file=sys.stderr)
            return 2
        host = rest[index + 1]
        rest = rest[:index] + rest[index + 2:]
    if rest:
        print(USAGE, file=sys.stderr)
        return 2
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    config = load_config(root)
    if command == "proxy":
        run_proxy(config)
        return 0
    if command == "worker":
        run_worker(home, config)
        return 0
    if host is None:
        state_path = home / ".local/state/fleet/last-applied.json"
        if state_path.exists():
            state = read_json(state_path)
            host = state.get("host")
        if not isinstance(host, str):
            raise FleetMemoryError("Fleet memory host is unknown. Use --host HOST.")
    if command == "enable":
        enable_memory(root, home, host)
        return 0
    if command == "disable":
        disable_memory(root, home, host)
        return 0
    if command == "backup":
        if host != config.server_host:
            raise FleetMemoryError("Fleet memory backups run only on the DevBox.")
        path, report = backup_memory(home, config)
        print(f"backup: {path}")
        print(f"schema: {report.schema_version}")
        return 0
    if command == "status":
        lines, ok = status_report(root, home, host)
        print("\n".join(lines))
        return 0 if ok else 1
    raise FleetMemoryError("Fleet memory command is not ready.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetMemoryError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
