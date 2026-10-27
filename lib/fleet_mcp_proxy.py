import importlib.util
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import subprocess
import sys
import time


LIMIT = 1024 * 1024
OUTSTANDING = 128
GRACE = 3
METADATA = {"server/discover", "tools/list", "prompts/list", "resources/list", "resources/templates/list"}
CHANGES = {"notifications/tools/list_changed", "notifications/prompts/list_changed",
           "notifications/resources/list_changed"}
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def load_cache(home, command, environment):
    try:
        spec = importlib.util.spec_from_file_location(
            "fleet_mcp_cache", Path(__file__).resolve().with_name("fleet_mcp_cache.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.MetadataCache(home, command, environment, Path.cwd())
    except Exception:
        return None


def execute(command, environment):
    previous = {signum: signal.signal(signum, signal.SIG_DFL)
                for signum in (signal.SIGPIPE, signal.SIGXFSZ)}
    try:
        os.execvpe(command[0], command, environment)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def identity(message):
    value = message.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return json.dumps(value)


def write_all(descriptor, data):
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def stop(child, timeout):
    child.stdin.close()
    child.stdout.close()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
        if signum == signal.SIGTERM:
            time.sleep(0.1)


def run(command, environment=None, home=None, managed=False):
    environment = dict(os.environ if environment is None else environment)
    child_environment = {**environment, "FLEET_MCP_EAGER": "1"} if managed else environment
    cache = None
    if environment.get("FLEET_MCP_EAGER") != "1":
        if home is None:
            home = environment.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
        cache = load_cache(home, command, environment)
    if cache is None or not cache.enabled or cache.prefer_native():
        execute(command, child_environment)

    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    selector = selectors.DefaultSelector()
    child = None
    child_in = None
    pending = b""
    outgoing = b""
    reading = True
    deadline = None
    tracking = True
    watching = True
    outstanding = {}
    lines = {"client": [b"", False], "server": [b"", False]}

    def want(descriptor, data, events, on):
        registered = descriptor in selector.get_map()
        if on and not registered:
            selector.register(descriptor, events, data)
        elif not on and registered:
            selector.unregister(descriptor)

    def refresh():
        want(stdin, "client", selectors.EVENT_READ, reading and (child is None or not outgoing))
        if child_in is not None:
            want(child_in, "write", selectors.EVENT_WRITE, bool(outgoing))

    def close_child_in():
        nonlocal child_in, outgoing
        want(child_in, "write", selectors.EVENT_WRITE, False)
        child.stdin.close()
        child_in = None
        outgoing = b""

    def flush():
        nonlocal outgoing
        if outgoing:
            try:
                outgoing = outgoing[os.write(child_in, outgoing):]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                close_child_in()
        if not reading and not outgoing and child_in is not None:
            close_child_in()
        refresh()

    def start():
        nonlocal child, child_in
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 env=child_environment, start_new_session=True)
        child_in = child.stdin.fileno()
        os.set_blocking(child_in, False)
        selector.register(child.stdout.fileno(), selectors.EVENT_READ, "server")

    def inspect(side, line):
        nonlocal tracking, watching
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            message = None
        if not isinstance(message, dict):
            if side == "server":
                cache.invalidate()
                watching = False
            tracking = False
            outstanding.clear()
            return
        method = message.get("method")
        if side == "client":
            if method == "initialize":
                cache.mark_native()
                tracking = False
                outstanding.clear()
            elif isinstance(method, str) and method in METADATA and identity(message) is not None and len(outstanding) < OUTSTANDING:
                outstanding[identity(message)] = message
            return
        if isinstance(method, str) and method in CHANGES:
            cache.invalidate()
            return
        if method is not None:
            return
        request = outstanding.pop(identity(message), None)
        if request is None:
            return
        result = message.get("result")
        if request["method"] == "server/discover" and not (
                isinstance(result, dict) and isinstance(result.get("supportedVersions"), list)):
            cache.mark_native()
        elif isinstance(result, dict):
            cache.put(request, result)

    def observe(side, data):
        state = lines[side]
        position = 0
        while tracking if side == "client" else watching:
            end = data.find(b"\n", position)
            chunk = data[position:] if end < 0 else data[position:end]
            if not state[1]:
                if len(state[0]) + len(chunk) > LIMIT:
                    state[:] = [b"", True]
                else:
                    state[0] += chunk
            if end < 0:
                return
            line, skipped = state
            state[:] = [b"", False]
            position = end + 1
            if skipped or not line.strip():
                continue
            if side == "server" and not outstanding and b"list_changed" not in line:
                continue
            inspect(side, line)

    def send(data):
        nonlocal outgoing
        if child_in is None:
            return
        outgoing += data
        observe("client", data)
        flush()

    def serve(line):
        if not line.strip():
            return True
        if len(line) > LIMIT:
            return False
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return False
        if not isinstance(message, dict):
            return False
        if message.get("method") == "notifications/cancelled" and "id" not in message:
            return True
        if identity(message) is None or "method" not in message:
            return False
        result = cache.get(message)
        if result is None:
            return False
        write_all(stdout, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result},
                                     separators=(",", ":")).encode() + b"\n")
        return True

    def from_client(data):
        nonlocal pending
        if child is not None:
            send(data)
            return
        pending += data
        while True:
            end = pending.find(b"\n")
            if end < 0:
                if len(pending) > LIMIT or (not reading and pending.strip()):
                    break
                return
            if not serve(pending[:end]):
                break
            pending = pending[end + 1:]
        start()
        data, pending = pending, b""
        send(data)

    def stop_signal(signum, frame):
        raise SystemExit(128 + signum)

    previous = {signum: signal.signal(signum, stop_signal) for signum in SIGNALS}
    wait = 0
    try:
        refresh()
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            events = selector.select(None if deadline is None else max(0, deadline - time.monotonic()))
            ended = False
            for key, _ in events:
                if key.data == "write":
                    flush()
                    continue
                data = os.read(key.fd, 65536)
                if key.data == "client":
                    if not data:
                        reading = False
                        deadline = time.monotonic() + GRACE
                    from_client(data)
                    if child is None and not reading:
                        return 0
                    if child is not None:
                        flush()
                elif data:
                    write_all(stdout, data)
                    observe("server", data)
                else:
                    wait = GRACE if deadline is None else max(0, deadline - time.monotonic())
                    ended = True
                    break
            if ended:
                break
    except BrokenPipeError:
        wait = GRACE
    finally:
        for signum in SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        selector.close()
        try:
            if child is not None:
                stop(child, wait)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    code = 0 if child is None else child.returncode
    return code if code is None or code >= 0 else 128 - code


if __name__ == "__main__":
    arguments = sys.argv[1:]
    managed = arguments[:1] == ["--managed"]
    if managed:
        arguments = arguments[1:]
    if len(arguments) < 2 or arguments[0] != "--":
        print("Usage: fleet_mcp_proxy.py [--managed] -- COMMAND...", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(arguments[1:], managed=managed))
