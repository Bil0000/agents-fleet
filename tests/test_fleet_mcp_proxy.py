import base64
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
PROXY = ROOT / "lib/fleet_mcp_proxy.py"
MODERN = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER = r'''
import base64,json,os,pathlib,signal,sys
log=pathlib.Path(os.environ["FLEET_TEST_LOG"])
def record(event):
 with log.open("a") as output:output.write(json.dumps(event)+"\n")
def emit(message):
 data=(json.dumps(message,indent=None,separators=(", ", ": "))+"\n").encode()
 sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
 return data
def result(identity,value):
 emit({"jsonrpc":"2.0","id":identity,"result":value})
record({"event":"start","pid":os.getpid(),"cwd":os.getcwd(),"home":os.environ.get("HOME"),"sentinel":os.environ.get("FLEET_TEST_SENTINEL"),"eager":os.environ.get("FLEET_MCP_EAGER")})
if os.environ.get("FLEET_TEST_READY"):emit({"ready":True})
pending=None
state={}
count=0
for raw in sys.stdin.buffer:
 record({"event":"wire","raw":base64.b64encode(raw).decode()})
 if len(raw)>1024*1024:
  sys.stdout.buffer.write(raw);sys.stdout.buffer.flush();continue
 try:message=json.loads(raw)
 except (ValueError,UnicodeDecodeError):message=None
 if not isinstance(message,dict):
  sys.stdout.buffer.write(raw);sys.stdout.buffer.flush();continue
 method=message.get("method");params=message.get("params",{});identity=message.get("id")
 if method=="initialize":
  value={"protocolVersion":params["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"},"instructions":"Retain instructions","_meta":{"private":"initialization"}}
  if os.environ.get("FLEET_TEST_ROOTS"):
   pending=(identity,value)
   emit({"jsonrpc":"2.0","id":"server-roots","method":"roots/list","params":{"_meta":{"keep":True}}})
  else:result(identity,value)
  continue
 if "method" not in message:
  if identity=="server-roots" and pending:
   result(*pending);pending=None
  continue
 if method in ("notifications/cancelled","notifications/progress"):
  sys.stdout.buffer.write(raw);sys.stdout.buffer.flush();continue
 if "id" not in message:continue
 meta=params.get("_meta",{})
 if method=="server/discover":
  value={"resultType":"complete","supportedVersions":["2026-07-28"],"capabilities":{"tools":{},"resources":{"subscribe":True}},"instructions":"Retain instructions","ttlMs":60000,"cacheScope":"public","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"fixture","version":"1"}}}
 elif method=="tools/list":
  if meta.get("fixtureMode")=="error":
   emit({"jsonrpc":"2.0","id":identity,"error":{"code":-32000,"message":"fixture error","data":{"keep":True}}});continue
  value={"resultType":"complete","tools":[{"name":"state","description":"Session state","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":False}}],"cacheScope":"private" if meta.get("fixtureMode")=="private" else "public","ttlMs":60000,"_meta":{"kept":"metadata"}}
  if meta.get("fixtureMode")=="input":
   value={"resultType":"input_required","requestState":"private-list-state","inputRequests":{"roots":{"method":"roots/list","params":{}}},"_meta":{"kept":True}}
 elif method=="tools/call":
  count+=1
  arguments=params.get("arguments",{})
  if arguments.get("exit"):sys.exit(23)
  if arguments.get("input"):
   value={"resultType":"input_required","requestState":"private-call-state","inputRequests":{"roots":{"method":"roots/list","params":{}}},"_meta":{"kept":True}}
  else:
   state.update(arguments.get("set",{}))
   value={"resultType":"complete","content":[{"type":"text","text":"state"}],"structuredContent":{"count":count,"state":state,"params":params},"_meta":{"private":"retained"}}
 elif method=="resources/read":
  value={"resultType":"complete","contents":[{"uri":params["uri"],"text":"private contents"}],"ttlMs":60000,"cacheScope":"public"}
 elif method=="subscriptions/listen":
  emit({"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged","params":{"notifications":params["notifications"],"_meta":{"io.modelcontextprotocol/subscriptionId":identity}}})
  emit({"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":params["notifications"]["resourceSubscriptions"][0],"_meta":{"io.modelcontextprotocol/subscriptionId":identity,"keep":True}}});continue
 else:value={"echo":message}
 result(identity,value)
if os.environ.get("FLEET_TEST_HOLD"):
 while True:signal.pause()
'''


def wire(message):
    return (json.dumps(message, separators=(", ", ": ")) + "\n").encode()


def request(method, identity, params=None, modern=True):
    params = dict(params or {})
    if modern:
        params["_meta"] = {VERSION_KEY: MODERN, **params.get("_meta", {})}
    return {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}


class Session:
    def __init__(self, command, environment, cwd, stderr):
        self.errors = stderr
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=stderr, env=environment, cwd=cwd)
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(message if isinstance(message, bytes) else wire(message))
        self.process.stdin.flush()

    def receive(self):
        try:
            return self.messages.get(timeout=5)
        except queue.Empty:
            self.errors.seek(0)
            raise AssertionError(f"No proxy output; stderr={self.errors.read()!r}") from None

    def exchange(self, message):
        self.send(message)
        response = self.receive()
        if response is None:
            self.errors.seek(0)
            raise AssertionError(f"Proxy closed stdout; stderr={self.errors.read()!r}")
        return json.loads(response)

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdout.close()


class McpProxyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.backend = self.root / "server.py"
        self.backend.write_text(SERVER)
        self.log = self.root / "backend.jsonl"
        self.environment = {**os.environ, "HOME": str(self.home),
                            "FLEET_TEST_LOG": str(self.log),
                            "XDG_CACHE_HOME": str(self.home / ".cache"),
                            "XDG_CONFIG_HOME": str(self.home / ".config"),
                            "FLEET_TEST_SENTINEL": "caller-kept"}
        self.environment.pop("FLEET_MCP_EAGER", None)
        self.addCleanup(self.clean_children)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def starts(self):
        return [item for item in self.events() if item["event"] == "start"]

    def received(self):
        return [base64.b64decode(item["raw"]) for item in self.events() if item["event"] == "wire"]

    def clean_children(self):
        for event in self.starts():
            try:
                os.kill(event["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start(self, managed=False, environment=None):
        errors = tempfile.TemporaryFile()
        self.addCleanup(errors.close)
        command = [sys.executable, "-B", "-I", str(PROXY)]
        if managed:
            command.append("--managed")
        command.extend(["--", sys.executable, "-B", "-I", str(self.backend)])
        session = Session(command, {**self.environment, **(environment or {})}, self.root, errors)
        self.addCleanup(session.close)
        return session

    def learn(self, messages):
        session = self.start()
        responses = []
        for message in messages:
            session.send(message)
            raw = session.receive()
            self.assertIsNotNone(raw)
            response = json.loads(raw)
            self.assertEqual(raw, wire(response))
            responses.append(response)
        self.assertEqual(self.received(), [wire(message) for message in messages])
        session.close()
        self.assertEqual(session.process.returncode, 0)
        return responses

    def assert_cached(self, actual, expected):
        self.assertGreater(actual["result"]["ttlMs"], 0)
        self.assertLessEqual(actual["result"]["ttlMs"], expected["result"]["ttlMs"])
        self.assertEqual(actual, {**expected, "result": {**expected["result"], "ttlMs": actual["result"]["ttlMs"]}})

    def test_warm_metadata_only_exits_without_starting_any_backend(self):
        messages = [request("server/discover", 1), request("tools/list", 2)]
        expected = self.learn(messages)
        session = self.start()
        for message, response in zip(messages, expected):
            self.assert_cached(session.exchange(message), response)
        session.close()
        self.assertEqual(session.process.returncode, 0)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.received(), [wire(message) for message in messages])

    def test_cold_learning_warm_metadata_and_concurrent_operations(self):
        discovery = request("server/discover", "cold-discover")
        listing = request("tools/list", "cold-list", {"_meta": {"progressToken": "cold"}})
        learned = self.learn([discovery, listing])
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(learned[0]["result"]["supportedVersions"], [MODERN])
        self.assertEqual(learned[1]["result"]["cacheScope"], "public")
        self.assertEqual(learned[1]["result"]["resultType"], "complete")
        self.assertGreater(learned[1]["result"]["ttlMs"], 0)
        warm = self.start()
        for identity, message, expected in zip((101, 102), (discovery, listing), learned):
            message = {**message, "id": identity}
            if message["method"] == "tools/list":
                message["params"] = {"_meta": {VERSION_KEY: MODERN, "progressToken": "warm"}}
            self.assert_cached(warm.exchange(message), {**expected, "id": identity})
        self.assertEqual(len(self.starts()), 1)
        calls = [request("tools/call", identity, {"name": "state", "arguments": arguments})
                 for identity, arguments in ((201, {"set": {"secret": "kept"}}), (202, {}), (203, {}))]
        warm.send(b"".join(wire(message) for message in calls[:2]))
        responses = [json.loads(warm.receive()), json.loads(warm.receive())]
        responses.append(warm.exchange(calls[2]))
        for count, response in enumerate(responses, 1):
            self.assertEqual(response["id"], 200 + count)
            self.assertEqual(response["result"]["structuredContent"]["count"], count)
            self.assertEqual(response["result"]["structuredContent"]["state"], {"secret": "kept"})
            self.assertEqual(response["result"]["_meta"], {"private": "retained"})
        active_listing = {**listing, "id": 204}
        warm.send(active_listing)
        self.assertEqual(warm.receive(), wire({**learned[1], "id": 204}))
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.received(), [wire(message) for message in [discovery, listing, *calls, active_listing]])

    def test_changed_parameters_and_protocol_metadata_miss_persisted_cache(self):
        listing = request("tools/list", 1, {"cursor": "page-one", "_meta": {"tenant": "one"}})
        expected = self.learn([listing])[0]
        cached = self.start()
        self.assert_cached(cached.exchange(listing), expected)
        cached.close()
        self.assertEqual(len(self.starts()), 1)
        variants = (
            {"cursor": "page-two", "_meta": {"tenant": "one"}},
            {"cursor": "page-one", "_meta": {"tenant": "two"}},
            {"cursor": "page-one", "_meta": {"tenant": "one", VERSION_KEY: "2099-01-01"}},
        )
        for params in variants:
            with self.subTest(params=params):
                session = self.start()
                message = request("tools/list", 2, params)
                before = len(self.starts())
                self.assertEqual(session.exchange(message)["id"], 2)
                self.assertEqual(len(self.starts()), before + 1)
                self.assertEqual(self.received()[-1], wire(message))
                session.close()

    def test_private_errors_continuations_resources_and_operations_are_not_cached(self):
        messages = [
            request("tools/list", "private", {"_meta": {"fixtureMode": "private"}}),
            request("tools/list", "error", {"_meta": {"fixtureMode": "error"}}),
            request("tools/list", "input", {"_meta": {"fixtureMode": "input"}}),
            request("tools/list", "state", {"requestState": "private-state"}),
            request("tools/list", "responses", {"inputResponses": {"roots": {"roots": []}}}),
            request("resources/read", "resource", {"uri": "test:///private"}),
            request("tools/call", "operation", {"name": "state", "arguments": {"input": True}}),
        ]
        expected = self.learn(messages)
        for message, response in zip(messages, expected):
            with self.subTest(identity=message["id"]):
                session = self.start()
                before = len(self.starts())
                self.assertEqual(session.exchange(message), response)
                self.assertEqual(len(self.starts()), before + 1)
                self.assertEqual(self.received()[-1], wire(message))
                session.close()

    def test_legacy_initialize_roots_responses_cancel_and_progress_relay_exactly(self):
        session = self.start(environment={"FLEET_TEST_ROOTS": "1"})
        initialize = request("initialize", 1, {"protocolVersion": "2025-11-25", "capabilities": {"roots": {}},
                                              "clientInfo": {"name": "fixture-client", "version": "1"}}, modern=False)
        session.send(initialize)
        roots = {"jsonrpc": "2.0", "id": "server-roots", "method": "roots/list", "params": {"_meta": {"keep": True}}}
        self.assertEqual(session.receive(), wire(roots))
        answer = b'{ "jsonrpc": "2.0", "id": "server-roots", "result": {"roots": [{"uri": "file:///private"}]} }\n'
        session.send(answer)
        response = json.loads(session.receive())
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["_meta"], {"private": "initialization"})
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        session.send(initialized)
        notifications = [
            b'{"jsonrpc":"2.0", "method":"notifications/cancelled", "params":{"requestId":9,"reason":"keep"}}\n',
            b'{"jsonrpc":"2.0", "method":"notifications/progress", "params":{"progressToken":"token","progress":1,"total":2}}\n',
        ]
        for raw in notifications:
            session.send(raw)
            self.assertEqual(session.receive(), raw)
        self.assertEqual(self.received(), [wire(initialize), answer, wire(initialized), *notifications])
        session.close()
        second = self.start(environment={"FLEET_TEST_ROOTS": "1"})
        second.send(initialize)
        self.assertEqual(second.receive(), wire(roots))
        second.send(answer)
        self.assertEqual(json.loads(second.receive()), response)
        self.assertEqual(len(self.starts()), 2)

    def test_modern_input_round_and_subscription_are_unchanged(self):
        session = self.start()
        first = request("tools/call", 1, {"name": "state", "arguments": {"input": True}})
        session.send(first)
        self.assertEqual(session.receive(), wire({"jsonrpc": "2.0", "id": 1, "result": {
            "resultType": "input_required", "requestState": "private-call-state",
            "inputRequests": {"roots": {"method": "roots/list", "params": {}}}, "_meta": {"kept": True}}}))
        continuation = request("tools/call", 2, {"name": "state", "requestState": "private-call-state",
                                                 "inputResponses": {"roots": {"roots": [{"uri": "file:///private"}]}}})
        self.assertEqual(session.exchange(continuation)["result"]["structuredContent"]["params"], continuation["params"])
        subscribe = request("subscriptions/listen", "private-subscription", {"notifications": {"resourceSubscriptions": ["test:///private"]}})
        session.send(subscribe)
        self.assertEqual(session.receive(), wire({"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged", "params": {
            "notifications": subscribe["params"]["notifications"], "_meta": {"io.modelcontextprotocol/subscriptionId": "private-subscription"}}}))
        self.assertEqual(session.receive(), wire({"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {
            "uri": "test:///private", "_meta": {"io.modelcontextprotocol/subscriptionId": "private-subscription", "keep": True}}}))
        self.assertEqual(self.received(), [wire(message) for message in (first, continuation, subscribe)])

    def test_legacy_session_changes_use_native_process_without_a_relay(self):
        first = self.start(environment={"FLEET_TEST_SESSION": "one"})
        initialize = request("initialize", 1, {"protocolVersion": "2025-06-18"}, modern=False)
        self.assertEqual(first.exchange(initialize)["id"], 1)
        first.close()
        second = self.start(environment={"FLEET_TEST_SESSION": "two", "FLEET_TEST_READY": "1"})
        self.assertEqual(json.loads(second.receive()), {"ready": True})
        self.assertEqual(self.starts()[-1]["pid"], second.process.pid)
        self.assertEqual(second.exchange(request("ping", 2))["id"], 2)

    def test_non_json_batch_and_oversize_lines_are_byte_exact(self):
        payloads = (b"not-json \xff\r\n", b' [ {"jsonrpc":"2.0","id":1,"method":"ping"} ] \n',
                    b'{"padding":"' + b"x" * (2 * 1024 * 1024) + b'"}\n')
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                session = self.start()
                session.send(payload)
                self.assertEqual(session.receive(), payload)
                marker = request("ping", "still-alive")
                self.assertEqual(session.exchange(marker)["id"], "still-alive")
                self.assertEqual(self.received()[-2:], [payload, wire(marker)])
                session.close()
                self.assertEqual(session.process.returncode, 0)

    def test_backend_failure_does_not_restart_or_replay(self):
        metadata = request("tools/list", 1)
        self.learn([metadata])
        session = self.start()
        self.assertEqual(session.exchange(metadata)["id"], 1)
        failure = request("tools/call", 2, {"name": "state", "arguments": {"exit": True}})
        session.send(failure)
        self.assertIsNone(session.receive())
        session.process.wait(timeout=5)
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.received(), [wire(metadata), wire(failure)])

    def test_client_eof_keeps_the_final_response(self):
        session = self.start()
        message = request("tools/call", 1, {"name": "state", "arguments": {"set": {"last": True}}})
        session.send(message)
        session.process.stdin.close()
        response = json.loads(session.receive())
        self.assertEqual(response["result"]["structuredContent"]["state"], {"last": True})
        self.assertIsNone(session.receive())
        session.process.wait(timeout=5)
        self.assertEqual(session.process.returncode, 0)

    def test_unrecognized_method_types_relay_without_crashing(self):
        for method in ([], {}, 1):
            with self.subTest(method=method):
                session = self.start()
                message = {"jsonrpc": "2.0", "id": 1, "method": method}
                self.assertEqual(session.exchange(message)["result"], {"echo": message})
                self.assertEqual(self.received()[-1], wire(message))
                session.close()

    def test_eof_reaps_owned_backend_even_when_it_ignores_stdin_eof(self):
        session = self.start(environment={"FLEET_TEST_HOLD": "1"})
        self.assertEqual(session.exchange(request("ping", 1))["id"], 1)
        child = self.starts()[0]["pid"]
        session.process.stdin.close()
        session.process.wait(timeout=5)
        self.assertIsNone(session.receive())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_managed_environment_and_caller_cwd_are_preserved(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                session = self.start(managed, {"FLEET_MCP_EAGER": "caller-value"})
                self.assertEqual(session.exchange(request("ping", 1))["id"], 1)
                event = self.starts()[-1]
                self.assertEqual(event["cwd"], str(self.root.resolve()))
                self.assertEqual(event["home"], str(self.home))
                self.assertEqual(event["sentinel"], "caller-kept")
                self.assertEqual(event["eager"], "1" if managed else "caller-value")
                session.close()


if __name__ == "__main__":
    unittest.main()
