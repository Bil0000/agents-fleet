import json
import os
import selectors
import signal
import subprocess
import sys


def valid(catalog, command):
    if not isinstance(catalog, dict) or catalog.get("command") != command:
        return False
    snapshot = catalog.get("snapshot")
    return (
        isinstance(catalog.get("protocols"), list) and bool(catalog["protocols"])
        and all(isinstance(item, str) for item in catalog["protocols"])
        and isinstance(snapshot, dict)
        and set(snapshot) == {"initialize", "tools/list", "resources/list", "resources/templates/list", "prompts/list"}
        and all(isinstance(item, dict) for item in snapshot.values())
        and isinstance(snapshot["initialize"].get("capabilities"), dict)
        and isinstance(snapshot["initialize"].get("serverInfo"), dict)
        and isinstance(snapshot["tools/list"].get("tools"), list)
        and all(not item.get("nextCursor") for item in snapshot.values())
    )


def run(catalog, command, environment):
    if not valid(catalog, command):
        os.execvpe(command[0], command, environment)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.buffer, selectors.EVENT_READ, "client")
    buffers = {"client": b"", "server": b""}
    child = None
    phase = "discovery"
    snapshot = None
    initialization = None
    pending = []
    replies = {}
    fresh = {}
    prefix = "fleet-" + os.urandom(16).hex()

    def send(stream, message):
        stream.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        stream.flush()

    def reply(identity, result):
        send(sys.stdout.buffer, {"jsonrpc": "2.0", "id": identity, "result": result})

    def start():
        nonlocal child
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 env=environment, start_new_session=True)
        selector.register(child.stdout, selectors.EVENT_READ, "server")

    def receive_client(message):
        nonlocal phase, snapshot, initialization
        method = message.get("method")
        if method == "server/discover" and initialization is None:
            send(sys.stdout.buffer, {"jsonrpc": "2.0", "id": message["id"],
                                     "error": {"code": -32601, "message": "Method not found"}})
            return
        if phase == "boot":
            if "method" not in message:
                send(child.stdin, message)
            elif method == "notifications/cancelled":
                identity = message.get("params", {}).get("requestId")
                pending[:] = [item for item in pending if item.get("id") != identity]
            else:
                pending.append(message)
            return
        if method == "initialize":
            initialization = message
            protocol = message.get("params", {}).get("protocolVersion")
            snapshot = None
            if protocol in catalog["protocols"]:
                snapshot = {**catalog["snapshot"], "initialize": {
                    **catalog["snapshot"]["initialize"], "protocolVersion": protocol}}
            if snapshot is None:
                start()
                phase = "pass"
                send(child.stdin, message)
            else:
                reply(message["id"], snapshot["initialize"])
            return
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return
        if method == "ping":
            reply(message["id"], {})
            return
        discovery = message.get("params") or {}
        if isinstance(discovery, dict):
            metadata = discovery.get("_meta") or {}
            if isinstance(metadata, dict) and set(metadata) <= {"progressToken"}:
                discovery = {key: value for key, value in discovery.items() if key != "_meta"}
        if snapshot is not None and method in snapshot and discovery in ({}, {"cursor": None}):
            reply(message["id"], snapshot[method])
            return
        if initialization is None:
            start()
            phase = "pass"
            send(child.stdin, message)
            return
        pending.append(message)
        start()
        phase = "boot"
        send(child.stdin, {**initialization, "id": prefix + "-initialize"})

    def receive_server(message):
        nonlocal phase
        identity = message.get("id")
        if identity == prefix + "-initialize":
            if "result" not in message:
                raise RuntimeError("MCP initialization failed before any tool call")
            fresh["initialize"] = message["result"]
            send(child.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            for index, method in enumerate(snapshot):
                if method != "initialize":
                    key = prefix + "-" + str(index)
                    replies[key] = method
                    send(child.stdin, {"jsonrpc": "2.0", "id": key, "method": method, "params": {}})
            return
        if identity not in replies:
            send(sys.stdout.buffer, message)
            return
        method = replies.pop(identity)
        if "result" not in message:
            raise RuntimeError("MCP discovery failed before any tool call")
        fresh[method] = message["result"]
        if replies:
            return
        if fresh != snapshot:
            for request in pending:
                if "id" in request:
                    send(sys.stdout.buffer, {"jsonrpc": "2.0", "id": request["id"], "error": {
                        "code": -32000, "message": "MCP discovery changed. Refresh tools before retrying. No operation was run."}})
                else:
                    send(child.stdin, request)
            for category in ("tools", "resources", "prompts"):
                send(sys.stdout.buffer, {"jsonrpc": "2.0", "method": "notifications/" + category + "/list_changed"})
        else:
            for request in pending:
                send(child.stdin, request)
        pending.clear()
        phase = "pass"

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        while True:
            for key, _ in selector.select():
                side = key.data
                data = os.read(key.fd, 65536)
                if not data:
                    return
                if phase == "pass":
                    target = child.stdin if side == "client" else sys.stdout.buffer
                    target.write(buffers[side] + data)
                    target.flush()
                    buffers[side] = b""
                    continue
                buffers[side] += data
                while b"\n" in buffers[side]:
                    line, buffers[side] = buffers[side].split(b"\n", 1)
                    if line.strip():
                        message = json.loads(line)
                        (receive_client if side == "client" else receive_server)(message)
                    if phase == "pass":
                        for source, remaining in buffers.items():
                            if remaining:
                                target = child.stdin if source == "client" else sys.stdout.buffer
                                target.write(remaining)
                                target.flush()
                                buffers[source] = b""
                        break
    finally:
        selector.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=3)
            except ProcessLookupError:
                child.wait()
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == "__main__":
    try:
        with open(sys.argv[1]) as source:
            catalog = json.load(source)
    except (OSError, ValueError):
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    run(catalog, sys.argv[2:], os.environ)
