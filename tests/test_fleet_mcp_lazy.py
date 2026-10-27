import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
SERVER = '''import json,os,pathlib,sys
log=pathlib.Path(sys.argv[1])
def record(event):
 with log.open("a") as output:output.write(json.dumps(event)+"\\n")
record({"event":"start","pid":os.getpid()})
for line in sys.stdin:
 message=json.loads(line);method=message.get("method");record({"event":method})
 if "id" not in message:continue
 if method=="initialize":
  if os.environ.get("FLEET_TEST_ROOTS"):
   print(json.dumps({"jsonrpc":"2.0","id":"server-roots","method":"roots/list"}),flush=True)
   answer=json.loads(sys.stdin.readline());assert answer["id"]=="server-roots"
  result={"protocolVersion":message["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"test","version":os.environ.get("FLEET_TEST_VERSION","1")},"instructions":"Keep all metadata"}
 elif method=="tools/list":result={"tools":[{"name":"echo","description":"Read-only test","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}]}
 elif method=="tools/call":
  result={"content":[{"type":"text","text":message["params"]["arguments"]["text"]}],"structuredContent":{"kept":True},"_meta":{"kept":"metadata"}}
  if message["params"]["arguments"].get("exit"):sys.exit(1)
 else:result={}
 print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
'''


class LazyMcpTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.server = self.root / "server.py"
        self.server.write_text(SERVER)
        self.log = self.root / "calls.jsonl"
        self.command = [sys.executable, "-B", str(self.server), str(self.log)]
        self.snapshot = {
            "initialize": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "test", "version": "1"}, "instructions": "Keep all metadata"},
            "tools/list": {"tools": [{"name": "echo", "description": "Read-only test",
                                       "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]},
            "resources/list": {}, "resources/templates/list": {}, "prompts/list": {},
        }
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"command": self.command, "protocols": ["2025-11-25"], "snapshot": self.snapshot}))

    def start(self, env=None, catalog=None):
        self.process = subprocess.Popen([sys.executable, "-B", str(ROOT / "lib/fleet_mcp_lazy.py"),
                                         str(catalog or self.catalog), *self.command],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, env={**os.environ, **(env or {})})
        self.addCleanup(self.stop)
        self.messages = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        if not self.process.stdin.closed:
            self.process.stdin.close()

    def send(self, method, identity=None, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if identity is not None:
            message["id"] = identity
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self):
        return self.messages.get(timeout=8)

    def initialize(self, protocol="2025-11-25"):
        self.send("initialize", 1, {"protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "proof", "version": "1"}})
        response = self.receive()
        self.send("notifications/initialized")
        return response

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_discovery_starts_no_child_and_first_concurrent_calls_share_one_child(self):
        self.start()
        self.send("server/discover", 0, {})
        self.assertEqual(self.receive()["error"]["code"], -32601)
        self.assertEqual(self.initialize()["result"], self.snapshot["initialize"])
        for identity, method in enumerate(self.snapshot, 2):
            if method != "initialize":
                self.send(method, identity, {"cursor": None} if method == "tools/list" else {})
                self.assertEqual(self.receive()["result"], self.snapshot[method])
        self.send("tools/list", 7, {"_meta": {"progressToken": 0}})
        self.assertEqual(self.receive()["result"], self.snapshot["tools/list"])
        self.send("ping", 8)
        self.assertEqual(self.receive()["result"], {})
        self.assertEqual(self.events(), [])
        for identity in (10, 11):
            self.send("tools/call", identity, {"name": "echo", "arguments": {"text": str(identity)}})
        results = [self.receive(), self.receive()]
        self.assertEqual({item["id"] for item in results}, {10, 11})
        self.assertTrue(all(item["result"]["_meta"] == {"kept": "metadata"} for item in results))
        self.assertTrue(all(item["result"]["structuredContent"] == {"kept": True} for item in results))
        self.assertEqual(sum(item["event"] == "start" for item in self.events()), 1)
        pid = next(item["pid"] for item in self.events() if item["event"] == "start")
        self.stop()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_changed_metadata_blocks_operation_before_execution(self):
        self.start({"FLEET_TEST_VERSION": "2", "FLEET_TEST_ROOTS": "1"})
        self.initialize()
        self.send("tools/call", 9, {"name": "echo", "arguments": {"text": "must not run"}})
        self.assertEqual(self.receive()["method"], "roots/list")
        self.send("notifications/roots/list_changed")
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "server-roots", "result": {"roots": []}}) + "\n")
        self.process.stdin.flush()
        self.assertIn("No operation was run", self.receive()["error"]["message"])
        for _ in range(3):
            self.assertIn("list_changed", self.receive()["method"])
        self.send("tools/list", 10)
        self.assertEqual(self.receive()["result"], self.snapshot["tools/list"])
        self.assertFalse(any(item["event"] == "tools/call" for item in self.events()))
        self.assertTrue(any(item["event"] == "notifications/roots/list_changed" for item in self.events()))

    def test_missing_catalog_and_unknown_protocol_keep_eager_behavior(self):
        invalid = self.root / "invalid.json"
        invalid.write_text("[]")
        for protocol, catalog in (("2025-11-25", self.root / "missing.json"),
                                  ("future-protocol", self.catalog), ("2025-11-25", invalid)):
            with self.subTest(protocol=protocol):
                self.start(catalog=catalog)
                self.assertEqual(self.initialize(protocol)["result"]["protocolVersion"], protocol)
                self.send("tools/call", 3, {"name": "echo", "arguments": {"text": "works"}})
                self.assertEqual(self.receive()["result"]["content"][0]["text"], "works")
                self.stop()

    def test_server_requests_during_startup_and_cancellation_are_preserved(self):
        self.start({"FLEET_TEST_ROOTS": "1"})
        self.initialize()
        self.send("tools/call", 9, {"name": "echo", "arguments": {"text": "cancel"}})
        self.assertEqual(self.receive()["method"], "roots/list")
        self.send("notifications/cancelled", params={"requestId": 9})
        self.send("tools/call", 10, {"name": "echo", "arguments": {"text": "keep"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "server-roots", "result": {"roots": []}}) + "\n")
        self.process.stdin.flush()
        self.assertEqual(self.receive()["id"], 10)
        self.assertEqual(sum(item["event"] == "tools/call" for item in self.events()), 1)

    def test_backend_failure_does_not_replay_operation(self):
        self.start()
        self.initialize()
        self.send("tools/call", 9, {"name": "echo", "arguments": {"text": "fail", "exit": True}})
        self.assertIsNone(self.receive())
        self.assertEqual(sum(item["event"] == "tools/call" for item in self.events()), 1)


if __name__ == "__main__":
    unittest.main()
