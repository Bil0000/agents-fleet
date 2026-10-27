#!/usr/bin/env python3
import importlib.util
import gzip
import http.server
import io
import json
import os
import selectors
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1] if len(sys.argv) < 2 else Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("fleet_memory", ROOT / "lib" / "fleet_memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


VALID_CONFIG = """\
version = "13.24.0"
server_host = "test-server"
client_host = "test-mac"
ssh_target = "user@memory.example.com"
worker_host = "127.0.0.1"
worker_port = 37702
tunnel_port = 37703
backup_retention_days = 14
"""

OBSERVATION_WORKER = (
    'async function PPt(t,e){return await fs("/api/sessions/observations","POST",t)}'
    'function normalize(e){return {toolResponse:e.tool_response,transcriptPath:au(e.transcript_path)}}'
    'async function summarize(e,n,i,s,o){' + memory.SUMMARY_REQUEST.decode() + 'return c;}'
    'async function init(e,i,s,o){' + memory.PROMPT_REQUEST.decode() + 'return u;}'
)
MANUAL_WORKER = (
    'var Rr=class{wrapHandler(f){return f}},Un=()=>{},XIt={},v={info(){},debug(){},error(){}};'
    'var yA=class extends Rr{constructor(r,n){super();this.dbManager=r;this.defaultProject=n}'
    'setupRoutes(r){r.post("/api/memory/save",Un(XIt),this.handleSaveMemory.bind(this))}'
    'handleSaveMemory=async()=>{p.syncObservation(h.id,f,u,m,0,h.createdAtEpoch).catch(()=>{});}};'
    '"getObservationById(e,r){";"function YJ(t,e,r){";"ensureSyncOriginColumns(){";'
    '"CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN";'
    '"CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN";'
    '"getPending(t,e){";"async addDocuments(e){";'
)
MANUAL_MCP = 'var kS=[{name:"important_workflow",handler:async()=>({})}];'
RUNTIME_WORKER = "".join("'" + value.decode() + "';" for value in memory.RUNTIME_PATCHES)


class FleetMemoryConfigTest(unittest.TestCase):
    def assert_invalid(self, contents, message=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.toml").write_text(contents)
            if message is None:
                with self.assertRaises(memory.FleetMemoryError):
                    memory.load_config(root)
            else:
                with self.assertRaisesRegex(memory.FleetMemoryError, message):
                    memory.load_config(root)

    def test_load_config_accepts_fleet_memory_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.toml").write_text(VALID_CONFIG)
            config = memory.load_config(root)

        self.assertEqual(config.version, "13.24.0")
        self.assertEqual(config.server_host, "test-server")
        self.assertEqual(config.client_hosts, ("test-mac",))
        self.assertEqual(config.ssh_target, "user@memory.example.com")
        self.assertEqual(config.worker_host, "127.0.0.1")
        self.assertEqual(config.worker_port, 37702)
        self.assertEqual(config.tunnel_port, 37703)
        self.assertEqual(config.backup_retention_days, 14)

    def test_load_config_accepts_multiple_client_hosts(self):
        contents = VALID_CONFIG.replace(
            'client_host = "test-mac"',
            'client_hosts = ["test-mac", "test-linux"]',
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.toml").write_text(contents)

            config = memory.load_config(root)

        self.assertEqual(config.client_hosts, ("test-mac", "test-linux"))

    def test_load_config_rejects_public_worker_bind(self):
        self.assert_invalid(VALID_CONFIG.replace('"127.0.0.1"', '"0.0.0.0"'), "loopback")

    def test_load_config_rejects_unsafe_values(self):
        cases = {
            "unknown key": VALID_CONFIG + 'extra = "no"\n',
            "same ports": VALID_CONFIG.replace("tunnel_port = 37703", "tunnel_port = 37702"),
            "low port": VALID_CONFIG.replace("worker_port = 37702", "worker_port = 0"),
            "bad host": VALID_CONFIG.replace('server_host = "test-server"', 'server_host = "../server"'),
            "bad target": VALID_CONFIG.replace('ssh_target = "user@memory.example.com"', 'ssh_target = "192.168.50.10"'),
            "bad version": VALID_CONFIG.replace('version = "13.24.0"', 'version = "latest"'),
            "zero retention": VALID_CONFIG.replace("backup_retention_days = 14", "backup_retention_days = 0"),
            "large retention": VALID_CONFIG.replace("backup_retention_days = 14", "backup_retention_days = 366"),
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(contents)

    def test_load_config_rejects_invalid_client_hosts(self):
        for clients in ('["mac", "mac"]', '["test-server"]', '[]', '"mac"', '[{ name = "mac" }]'):
            with self.subTest(clients=clients):
                self.assert_invalid(
                    VALID_CONFIG.replace(
                        'client_host = "test-mac"',
                        f"client_hosts = {clients}",
                    )
                )


class FleetMemoryCliTest(unittest.TestCase):
    def test_memory_command_has_scoped_usage(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "fleet"), "memory", "unknown"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: fleet memory", result.stderr)


class FleetMemoryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.client_root = Path(self.temporary.name) / "client-root"
        self.client_root.mkdir()
        (self.client_root / "memory.toml").write_text(
            VALID_CONFIG.replace(
                'client_host = "test-mac"',
                'client_hosts = ["test-mac", "test-linux"]',
            )
        )
        shutil.copytree(ROOT / "system", self.client_root / "system")
        self.config = memory.load_config(self.client_root)

    def tearDown(self):
        self.temporary.cleanup()

    def install_plugin_fixture(self, version="13.24.0", vulnerable=False, stale_health=False):
        plugin = self.home / ".claude/plugins/cache/thedotmack/claude-mem" / version
        script = plugin / "scripts/worker-service.cjs"
        script.parent.mkdir(parents=True)
        health_version = "13.23.1" if stale_health else "13.24.0"
        if vulnerable:
            script.write_text(
                f'worker;QZ="{health_version}";e.memorySessionId&&(this.dbManager.getSessionStore().updateMemorySessionId(e.sessionDbId,null),e.memorySessionId=null);'
            )
        else:
            script.write_text(f'worker;QZ="{health_version}";e.memorySessionId&&(e.memorySessionId=null);')
        script.write_text(script.read_text() + OBSERVATION_WORKER)
        script.write_text(script.read_text() + MANUAL_WORKER)
        script.write_text(script.read_text() + RUNTIME_WORKER)
        (script.parent / "mcp-server.cjs").write_text(MANUAL_MCP)
        registry = self.home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "claude-mem@thedotmack": [
                            {"scope": "user", "installPath": str(plugin), "version": version}
                        ]
                    },
                }
            )
        )
        return script

    def test_resolve_plugin_requires_exact_regular_pinned_worker(self):
        expected = self.install_plugin_fixture()

        self.assertEqual(memory.resolve_plugin(self.home, "13.24.0"), expected.resolve())
        with self.assertRaisesRegex(memory.FleetMemoryError, "13.24.1"):
            memory.resolve_plugin(self.home, "13.24.1")

    def test_harden_plugin_corrects_stale_health_version(self):
        worker = self.install_plugin_fixture(stale_health=True)

        memory.harden_plugin(self.home, self.config)

        self.assertIn('QZ="13.24.0"', worker.read_text())
        self.assertTrue(memory.plugin_hardened(self.home, self.config))

    def test_harden_plugin_blocks_client_daemon_but_allows_server(self):
        script = self.install_plugin_fixture()
        script.write_text('let e={memorySessionId:null};' + script.read_text().replace('worker;', 'console.log("worker");'))
        memory.harden_plugin(self.home, self.config)
        node = shutil.which("node")
        self.assertIsNotNone(node)
        for host, expected in ((self.config.client_hosts[0], ""), (self.config.server_host, "worker\n")):
            with self.subTest(host=host):
                result = subprocess.run(
                    [node, "-e", 'require("os").hostname=()=>process.argv[1];process.argv.push("--daemon");require(process.argv[2]);', host, str(script)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_harden_plugin_guards_codex_when_claude_is_already_hardened(self):
        script = self.install_plugin_fixture()
        original = script.read_bytes()
        memory.harden_plugin(self.home, self.config)
        codex = self.home / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts/worker-service.cjs"
        codex.parent.mkdir(parents=True)
        codex.write_bytes(original)
        codex.with_name("mcp-server.cjs").write_text(MANUAL_MCP)

        memory.harden_plugin(self.home, self.config)

        self.assertEqual(codex.read_bytes().count(memory.client_worker_guard(self.config)), 1)

    def test_harden_plugin_makes_learning_explicit_and_retires_duplicate_babysit(self):
        script = self.install_plugin_fixture()
        codex = self.home / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts"
        shutil.copytree(script.parent, codex)
        roots = (script.parent.parent, codex.parent)
        original = '---\nname: learn-codebase\ndescription: Read the entire codebase.\n---\n\nKeep the original learning workflow.\n'
        for root in roots:
            learning = root / "skills/learn-codebase/SKILL.md"
            learning.parent.mkdir(parents=True)
            learning.write_text(original)
            babysit = root / "skills/babysit/SKILL.md"
            babysit.parent.mkdir()
            babysit.write_text("Original legacy watcher workflow.\n")
            other = root / "skills/mem-search/SKILL.md"
            other.parent.mkdir()
            other.write_text("Shared memory search stays available.\n")

        memory.harden_plugin(self.home, self.config)
        first = {path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}
        memory.harden_plugin(self.home, self.config)
        self.assertEqual(first, {path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()})
        for root in roots:
            learning = root / "skills/learn-codebase"
            self.assertIn("disable-model-invocation: true\n", (learning / "SKILL.md").read_text())
            self.assertTrue((learning / "SKILL.md").read_text().endswith("\n\nKeep the original learning workflow.\n"))
            self.assertEqual((learning / "agents/openai.yaml").read_text(), "policy:\n  allow_implicit_invocation: false\n")
            self.assertFalse((root / "skills/babysit/SKILL.md").exists())
            self.assertEqual((root / "skills/babysit/RETIRED.md").read_text(), "Original legacy watcher workflow.\n")
            self.assertEqual((root / "skills/mem-search/SKILL.md").read_text(), "Shared memory search stays available.\n")
        policy = roots[1] / "skills/learn-codebase/agents/openai.yaml"
        policy.write_text("interface:\n  display_name: Custom learning skill\n")
        with self.assertRaisesRegex(memory.FleetMemoryError, "unmanaged invocation policy"):
            memory.harden_plugin(self.home, self.config)
        self.assertEqual(policy.read_text(), "interface:\n  display_name: Custom learning skill\n")
        policy.write_text("policy:\n  allow_implicit_invocation: false\n")
        babysit = roots[1] / "skills/babysit/SKILL.md"
        babysit.write_text("A different local workflow.\n")
        with self.assertRaisesRegex(memory.FleetMemoryError, "recovery file differs"):
            memory.harden_plugin(self.home, self.config)
        self.assertEqual(babysit.read_text(), "A different local workflow.\n")
        self.assertEqual(babysit.with_name("RETIRED.md").read_text(), "Original legacy watcher workflow.\n")

    def test_observation_patch_enqueues_codex_without_calling_worker(self):
        script = self.install_plugin_fixture()
        script.write_text('let e={};' + script.read_text().replace('worker;', ''))
        memory.harden_plugin(self.home, self.config)
        first = script.read_bytes()
        memory.harden_plugin(self.home, self.config)
        self.assertEqual(first, script.read_bytes())
        probe = (
            'require("child_process").spawnSync=(exe,args,opts)=>{'
            'console.log(JSON.stringify({exe,args,body:JSON.parse(opts.input),timeout:opts.timeout}));'
            'return {status:0};};'
            'function fs(){throw Error("remote call");}'
            + script.read_text()
            + ';PPt({sessionId:"s",toolName:"Bash",toolUseId:"call-1",cwd:"/repo"},"codex");'
        )
        result = subprocess.run([shutil.which("node"), "-e", probe], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)
        self.assertEqual(captured["args"], ["memory", "enqueue"])
        self.assertEqual(captured["body"]["tool_use_id"], "call-1")
        self.assertLessEqual(captured["timeout"], 2000)
        summary = probe.replace(';PPt({sessionId:"s",toolName:"Bash",toolUseId:"call-1",cwd:"/repo"},"codex");', ';summarize("s","Finished","codex");')
        result = subprocess.run([shutil.which("node"), "-e", summary], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["body"]["queue_kind"], "summarize")
        prompt_probe = (
            'require("child_process").spawnSync=(exe,args,opts)=>{'
            'console.log(JSON.stringify({args,body:opts.input?JSON.parse(opts.input):null}));'
            'return {status:0,stdout:JSON.stringify({status:"pending"})};};'
            + script.read_text()
            + ';init("s","repo","<private>Next task</private>","codex");'
        )
        result = subprocess.run([shutil.which("node"), "-e", prompt_probe], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([call["args"] for call in calls], [["memory", "init"]])
        self.assertEqual(calls[-1]["body"]["prompt"], "<private>Next task</private>")
        failed = subprocess.run(
            [shutil.which("node"), "-e", probe.replace('return {status:0};', 'return {status:1};')],
            capture_output=True, text=True,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("not acknowledged", failed.stderr)

    def test_harden_rejects_changed_hook_before_writing_either_worker(self):
        script = self.install_plugin_fixture(vulnerable=True)
        codex = self.home / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts/worker-service.cjs"
        codex.parent.mkdir(parents=True)
        codex.write_text("unreviewed worker")
        before = script.read_bytes()
        with self.assertRaisesRegex(memory.FleetMemoryError, "reviewed bundle"):
            memory.harden_plugin(self.home, self.config)
        self.assertEqual(script.read_bytes(), before)
        self.assertEqual(codex.read_text(), "unreviewed worker")

    def test_harden_adds_worker_memory_tools_to_both_mcp_caches(self):
        script = self.install_plugin_fixture()
        codex = self.home / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts"
        codex.mkdir(parents=True)
        (codex / "worker-service.cjs").write_bytes(script.read_bytes())
        (codex / "mcp-server.cjs").write_text(MANUAL_MCP)
        memory.harden_plugin(self.home, self.config)
        for directory in (script.parent, codex):
            probe = (
                'let runtime="worker",calls=0;function Ci(){return runtime}async function vt(endpoint,{body}){calls++;return {endpoint,body}}'
                + (directory / "mcp-server.cjs").read_text()
                + ';(async()=>{let names=kS.map(t=>t.name);'
                'let saved=await kS.find(t=>t.name==="save_memory").handler({text:"public <private>secret</private>",project:"fleet"});'
                'let updated=await kS.find(t=>t.name==="update_memory").handler({id:1,text:"new"});'
                'for(const [name,body] of [["save_memory",{text:"<private>secret</private>"}],["save_memory",{text:"ok",extra:true}],["update_memory",{id:true,text:"ok"}]]){'
                'let rejected=false;try{await kS.find(t=>t.name===name).handler(body)}catch{rejected=true}if(!rejected)throw Error("invalid payload accepted");}'
                'runtime="server";try{await kS.find(t=>t.name==="save_memory").handler({text:"ok"});throw Error("wrong runtime")}catch(e){if(!e.message.includes("worker runtime"))throw e}'
                'if(calls!==2)throw Error("invalid payload reached network");'
                'console.log(JSON.stringify({names,saved,updated}));})();'
            )
            result = subprocess.run([shutil.which("node"), "-e", probe], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertIn("save_memory", output["names"])
            self.assertIn("update_memory", output["names"])
            self.assertEqual(output["saved"], {"endpoint": "/api/memory/save", "body": {"text": "public", "project": "fleet"}})
            self.assertEqual(output["updated"], {"endpoint": "/api/memory/update", "body": {"id": 1, "text": "new"}})
        first = {path: path.read_bytes() for path in (script, script.parent / "mcp-server.cjs", codex / "worker-service.cjs", codex / "mcp-server.cjs")}
        memory.harden_plugin(self.home, self.config)
        self.assertEqual(first, {path: path.read_bytes() for path in first})

    def test_harden_rejects_changed_mcp_before_writing_any_bundle(self):
        script = self.install_plugin_fixture(vulnerable=True)
        mcp = script.parent / "mcp-server.cjs"
        mcp.write_text("unreviewed mcp")
        before = script.read_bytes()
        with self.assertRaisesRegex(memory.FleetMemoryError, "reviewed bundle"):
            memory.harden_plugin(self.home, self.config)
        self.assertEqual(script.read_bytes(), before)
        self.assertEqual(mcp.read_text(), "unreviewed mcp")

    def test_manual_cli_validates_and_filters_before_network_or_disk(self):
        invalid = [None, {}, {"text": " "}, {"text": "<private>secret</private>"},
                   {"text": "ok", "metadata": []}, {"text": "ok", "other": "x"}]
        for command, payload in [("save", value) for value in invalid] + [
            ("update", {"id": value, "text": "ok"}) for value in (True, 0, -1, "1")
        ] + [("update", {"id": 1, "text": "ok", "project": "different"})]:
            with self.subTest(command=command, payload=payload), mock.patch.object(
                memory.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
            ), mock.patch.object(memory.http.client, "HTTPConnection", side_effect=AssertionError("network")):
                with self.assertRaises(memory.FleetMemoryError):
                    memory.main([str(self.client_root), command])
        client = mock.MagicMock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = b'{"success":true,"id":7}'
        stdout = io.StringIO()
        payload = {"text": "public <private>secret</private>", "metadata": {"nested": ["<private>hidden</private>"]}}
        with mock.patch.object(memory.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), mock.patch.object(
            memory.http.client, "HTTPConnection", return_value=client
        ), mock.patch.object(memory.sys, "stdout", stdout):
            self.assertEqual(memory.main([str(self.client_root), "save"]), 0)
        self.assertEqual(json.loads(client.request.call_args.args[2]), {"text": "public", "metadata": {"nested": [""]}})
        self.assertEqual(json.loads(stdout.getvalue())["id"], 7)
        self.assertFalse((self.home / ".local").exists())

    def test_manual_cli_reports_uncertain_writes_without_queue_or_retry(self):
        for response in (OSError("private detail"), b'{"status":"pending"}'):
            with self.subTest(response=response):
                client = mock.MagicMock()
                client.getresponse.return_value.status = 200
                client.getresponse.return_value.read.return_value = response
                failure = {"side_effect": response} if isinstance(response, OSError) else {"return_value": client}
                with mock.patch.object(memory.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"text":"public"}'))), mock.patch.object(
                    memory.http.client, "HTTPConnection", **failure
                ) as connect, mock.patch.object(memory.subprocess, "Popen", side_effect=AssertionError("background process")):
                    with self.assertRaisesRegex(memory.FleetMemoryError, "search before retry"):
                        memory.main([str(self.client_root), "save"])
                self.assertEqual(connect.call_count, 1)
                self.assertFalse((self.home / ".local").exists())

    def test_observation_queue_survives_outage_and_deduplicates_drain(self):
        payload = {"contentSessionId": "s", "tool_name": "Bash", "tool_input": {"command": "pwd"},
                   "tool_response": "repo", "cwd": "/repo", "platformSource": "codex", "tool_use_id": "call-1"}
        started = time.monotonic()
        with mock.patch.object(memory.http.client, "HTTPConnection", side_effect=AssertionError("foreground network")):
            memory.enqueue_observation(self.home, payload)
            memory.enqueue_observation(self.home, payload)
        self.assertLess(time.monotonic() - started, 1)
        with mock.patch.object(memory.http.client, "HTTPConnection", side_effect=OSError("private detail")):
            self.assertEqual(memory.drain_observations(self.home, self.config), 0)
        self.assertEqual(memory.observation_queue_status(self.home), (1, 1))
        path = self.home / ".local/state/fleet/memory/outbox/observations.db"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(inner_self):
                received.append(json.loads(inner_self.rfile.read(int(inner_self.headers["Content-Length"]))))
                inner_self.send_response(200)
                inner_self.end_headers()
                inner_self.wfile.write(b'{"status":"queued"}')

            def log_message(inner_self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = replace(self.config, worker_port=server.server_port)
            self.assertEqual(memory.drain_observations(self.home, config), 1)
            memory.enqueue_observation(self.home, payload)
            self.assertEqual(memory.drain_observations(self.home, config), 0)
            self.assertEqual(received, [payload])
            self.assertEqual(memory.observation_queue_status(self.home), (0, 0))
            memory.enqueue_observation(self.home, dict(payload, tool_use_id="call-2"))
            self.assertEqual(memory.drain_observations(self.home, config), 1)
            self.assertEqual(len(received), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_observation_queue_rejects_symlink_and_malformed_payload(self):
        path = self.home / ".local/state/fleet/memory/outbox"
        path.parent.mkdir(parents=True)
        path.symlink_to(self.home, target_is_directory=True)
        payload = {"contentSessionId": "s", "tool_name": "Bash", "cwd": "/repo", "platformSource": "codex"}
        with self.assertRaises(memory.FleetMemoryError):
            memory.enqueue_observation(self.home, payload)
        with self.assertRaises(memory.FleetMemoryError):
            memory.enqueue_observation(self.home, {"tool_name": "Bash"})

    def test_summary_waits_behind_observations_during_an_outage(self):
        observation = {"contentSessionId": "s", "tool_name": "Bash", "cwd": "/repo", "platformSource": "codex", "tool_use_id": "call-1"}
        summary = {"contentSessionId": "s", "platformSource": "codex", "queue_kind": "summarize", "last_assistant_message": "Finished"}
        prompt = {"contentSessionId": "s", "platformSource": "codex", "queue_kind": "init", "project": "repo", "prompt": "<private>Next task</private>"}
        memory.enqueue_observation(self.home, observation)
        memory.enqueue_observation(self.home, summary)
        memory.enqueue_observation(self.home, prompt)
        client = mock.MagicMock()
        client.getresponse.return_value.status = 503
        with mock.patch.object(memory.http.client, "HTTPConnection", return_value=client):
            self.assertEqual(memory.drain_observations(self.home, self.config), 0)
        client.request.assert_called_once()
        self.assertEqual(memory.observation_queue_status(self.home), (3, 1))
        client.reset_mock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.side_effect = [b'{"status":"queued"}', b'{"status":"queued"}', b'{"sessionDbId":1}']
        with mock.patch.object(memory.http.client, "HTTPConnection", return_value=client):
            self.assertEqual(memory.drain_observations(self.home, self.config), 3)
        self.assertEqual([call.args[1] for call in client.request.call_args_list], ["/api/sessions/observations", "/api/sessions/summarize", "/api/sessions/init"])
        self.assertEqual(memory.observation_queue_status(self.home), (0, 0))

    def test_private_content_is_filtered_before_local_storage(self):
        memory.enqueue_observation(self.home, {"contentSessionId": "s", "platformSource": "codex", "tool_name": "Bash", "cwd": "/repo",
            "tool_input": {"nested": ["public <private>hidden-input</private>"]},
            "tool_response": ["<private>hidden-cross", "entry</private>", "<private>hidden-result</private>"]})
        memory.enqueue_observation(self.home, {"contentSessionId": "s", "platformSource": "codex", "queue_kind": "init", "project": "repo", "prompt": "<private>hidden-prompt</private>"})
        path = memory.observation_queue_path(self.home)
        with sqlite3.connect(path) as connection:
            payloads = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM observations")]
        self.assertEqual(payloads[0]["tool_input"]["nested"], ["public "])
        self.assertEqual(payloads[0]["tool_response"], ["", ""])
        self.assertEqual(payloads[1]["prompt"], "<private></private>")
        self.assertNotIn(b"hidden-", path.read_bytes())

    def test_prompt_is_retained_when_empty_queue_worker_is_unavailable(self):
        payload = {"contentSessionId": "s", "platformSource": "codex", "project": "repo", "prompt": "Next task"}
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        stdout = io.StringIO()
        with mock.patch.object(memory.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))), mock.patch.object(
            memory.sys, "stdin", stdin
        ), mock.patch.object(memory.sys, "stdout", stdout), mock.patch.object(memory.subprocess, "Popen"), mock.patch.object(memory.http.client, "HTTPConnection", side_effect=OSError):
            self.assertEqual(memory.main([str(self.client_root), "init"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "pending"})
        self.assertEqual(memory.observation_queue_status(self.home), (1, 1))
        client = mock.MagicMock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = b'{"sessionDbId":1,"promptNumber":2}'
        with mock.patch.object(memory.http.client, "HTTPConnection", return_value=client):
            self.assertEqual(memory.drain_observations(self.home, self.config), 1)
        self.assertEqual(client.request.call_args.args[1], "/api/sessions/init")
        with sqlite3.connect(memory.observation_queue_path(self.home)) as connection:
            result = json.loads(connection.execute("SELECT result FROM observations").fetchone()[0])
        self.assertEqual(result, {"sessionDbId": 1, "promptNumber": 2})

    def test_capture_starts_background_delivery_without_restarting_worker(self):
        payload = {"contentSessionId": "s", "platformSource": "codex", "tool_name": "Bash", "cwd": "/repo"}
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        with mock.patch.object(memory.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))), mock.patch.object(
            memory.sys, "stdin", stdin
        ), mock.patch.object(memory.subprocess, "Popen") as start, mock.patch.object(memory.http.client, "HTTPConnection", side_effect=AssertionError("foreground network")):
            self.assertEqual(memory.main([str(self.client_root), "enqueue"]), 0)
        self.assertEqual(memory.observation_queue_status(self.home), (1, 0))
        self.assertIsNotNone(start.call_args, "Background delivery was not started")
        self.assertEqual(start.call_args.args[0], [str(self.home / ".local/bin/fleet"), "memory", "deliver"])
        self.assertTrue(start.call_args.kwargs["start_new_session"])

    def test_background_delivery_retries_outage_and_exits_when_empty(self):
        memory.enqueue_observation(self.home, {"contentSessionId": "s", "platformSource": "codex", "tool_name": "Bash", "cwd": "/repo"})
        client = mock.MagicMock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = b'{"status":"queued"}'
        with mock.patch.object(memory.http.client, "HTTPConnection", side_effect=[OSError(), client]), mock.patch.object(memory.time, "sleep"):
            memory.deliver_observations(self.home, self.config)
        self.assertEqual(memory.observation_queue_status(self.home), (0, 0))

    def test_observation_queue_retains_unacknowledged_delivery(self):
        memory.enqueue_observation(self.home, {"contentSessionId": "s", "tool_name": "Bash", "cwd": "/repo", "platformSource": "codex"})
        client = mock.MagicMock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = b'{"stored":false}'
        with mock.patch.object(memory.http.client, "HTTPConnection", return_value=client):
            self.assertEqual(memory.drain_observations(self.home, self.config), 0)
        self.assertEqual(memory.observation_queue_status(self.home), (1, 1))

    def test_observation_queue_drain_does_not_block_enqueue_or_another_drainer(self):
        payload = {"contentSessionId": "s", "tool_name": "Bash", "cwd": "/repo", "platformSource": "codex"}
        memory.enqueue_observation(self.home, payload)
        client = mock.MagicMock()
        client.getresponse.return_value.status = 200

        def read(size):
            memory.enqueue_observation(self.home, payload)
            self.assertEqual(memory.drain_observations(self.home, self.config), 0)
            return b'{"status":"queued"}'

        client.getresponse.return_value.read.side_effect = read
        with mock.patch.object(memory.http.client, "HTTPConnection", return_value=client):
            self.assertEqual(memory.drain_observations(self.home, self.config), 1)
        self.assertEqual(memory.observation_queue_status(self.home), (1, 0))

    def test_configure_client_preserves_unmanaged_settings_and_freezes_only_claude_mem(self):
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"PRIVATE": "keep", "CLAUDE_MEM_WORKER_PORT": "1", "CLAUDE_MEM_SKIP_TOOLS": "CustomTool,AskUserQuestion"}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True)
        marketplaces.write_text(
            json.dumps(
                {
                    "thedotmack": {"source": {"repo": "thedotmack/claude-mem"}, "autoUpdate": True},
                    "other": {"source": {"repo": "owner/other"}, "autoUpdate": True},
                }
            )
        )

        memory.configure_client(self.home, self.config)

        applied = json.loads(settings.read_text())
        self.assertEqual(applied["PRIVATE"], "keep")
        self.assertEqual(applied["CLAUDE_MEM_RUNTIME"], "worker")
        self.assertEqual(applied["CLAUDE_MEM_WORKER_HOST"], "127.0.0.1")
        self.assertEqual(applied["CLAUDE_MEM_WORKER_PORT"], "37702")
        self.assertEqual(applied["CLAUDE_MEM_PROVIDER"], "claude")
        self.assertEqual(applied["CLAUDE_MEM_CLAUDE_AUTH_METHOD"], "subscription")
        skipped = set(applied["CLAUDE_MEM_SKIP_TOOLS"].split(","))
        self.assertIn("CustomTool", skipped)
        self.assertIn("AskUserQuestion", skipped)
        for tool in ("search", "get_observations", "timeline", "save_memory", "update_memory"):
            self.assertIn(f"mcp__plugin_claude-mem_mcp-search__{tool}", skipped)
            self.assertIn(f"mcp__mcp_search__{tool}", skipped)
        self.assertNotIn("Bash", skipped)
        self.assertNotIn("Read", skipped)
        self.assertNotIn("mcp__github__search", skipped)
        memory.configure_client(self.home, self.config)
        self.assertEqual(json.loads(settings.read_text()), applied)
        self.assertEqual(os.stat(settings).st_mode & 0o777, 0o600)
        frozen = json.loads(marketplaces.read_text())
        self.assertFalse(frozen["thedotmack"]["autoUpdate"])
        self.assertTrue(frozen["other"]["autoUpdate"])

    def test_invalid_memory_tool_exclusions_leave_settings_unchanged(self):
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"CLAUDE_MEM_SKIP_TOOLS": []}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True)
        marketplaces.write_text(json.dumps({"thedotmack": {}}))
        before = settings.read_bytes()
        with self.assertRaisesRegex(memory.FleetMemoryError, "Invalid Claude-mem tool exclusions"):
            memory.configure_client(self.home, self.config)
        self.assertEqual(settings.read_bytes(), before)

    def test_ssh_command_uses_batch_loopback_forwarding(self):
        self.assertEqual(
            memory.ssh_command(self.config),
            [
                "/usr/bin/ssh", "-N", "-T", "-o", "BatchMode=yes", "-o",
                "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o",
                "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-L",
                "127.0.0.1:37703:127.0.0.1:37702", "user@memory.example.com",
            ],
        )

    def test_guard_forwards_when_tunnel_is_up_and_keeps_port_when_down(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(inner_self):
                data = inner_self.request.recv(1024)
                inner_self.request.sendall(data)

        echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        echo.daemon_threads = True
        echo_thread = threading.Thread(target=echo.serve_forever, daemon=True)
        echo_thread.start()
        listen_socket = socket.socket()
        listen_socket.bind(("127.0.0.1", 0))
        listen_port = listen_socket.getsockname()[1]
        listen_socket.close()
        stop = threading.Event()
        ready = threading.Event()
        guard_thread = threading.Thread(
            target=memory.serve_guard,
            args=(listen_port, echo.server_address[1], stop, ready),
            daemon=True,
        )
        guard_thread.start()
        self.assertTrue(ready.wait(2))

        with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
            client.sendall(b"shared-memory")
            self.assertEqual(client.recv(1024), b"shared-memory")

        echo.shutdown()
        echo.server_close()
        with socket.create_connection(("127.0.0.1", listen_port), timeout=2) as client:
            client.sendall(b"offline")
            try:
                received = client.recv(1024)
            except ConnectionResetError:
                received = b""
            self.assertEqual(received, b"")
        probe = socket.socket()
        with self.assertRaises(OSError):
            probe.bind(("127.0.0.1", listen_port))
        probe.close()
        self.assertFalse((self.home / ".claude-mem/claude-mem.db").exists())

        stop.set()
        guard_thread.join(2)
        self.assertFalse(guard_thread.is_alive())

    def test_enable_server_installs_owned_services_and_preserves_settings(self):
        self.install_plugin_fixture()
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key_12345678901234567890123456789012\n")
        client_key.chmod(0o600)
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"PRIVATE": "keep"}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        memory.enable_memory(
            self.client_root,
            self.home,
            "test-server",
            platform_name="linux",
            native_runner=runner,
        )

        units = self.home / ".config/systemd/user"
        self.assertEqual(
            (units / "fleet-memory.service").read_text(),
            (ROOT / "system/linux/fleet-memory.service").read_text(),
        )
        self.assertTrue((units / "fleet-memory-backup.service").is_file())
        self.assertTrue((units / "fleet-memory-backup.timer").is_file())
        gateway = self.home / ".claude-mem/.env"
        self.assertEqual(
            gateway.read_text(),
            "ANTHROPIC_BASE_URL=http://127.0.0.1:8317\n"
            "ANTHROPIC_AUTH_TOKEN=client-key_12345678901234567890123456789012\n",
        )
        self.assertEqual(os.stat(gateway).st_mode & 0o777, 0o600)
        self.assertIn(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory.service"],
            commands,
        )
        applied_settings = json.loads(settings.read_text())
        self.assertEqual(applied_settings["PRIVATE"], "keep")
        self.assertEqual(applied_settings["CLAUDE_MEM_CLAUDE_AUTH_METHOD"], "gateway")

    def test_enable_server_refuses_unmanaged_gateway_env(self):
        self.install_plugin_fixture()
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key_12345678901234567890123456789012\n")
        client_key.chmod(0o600)
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        gateway = self.home / ".claude-mem/.env"
        gateway.write_text("KEEP=private\n")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))

        with self.assertRaisesRegex(memory.FleetMemoryError, "gateway"):
            memory.enable_memory(
                self.client_root,
                self.home,
                "test-server",
                platform_name="linux",
                native_runner=lambda *_: subprocess.CompletedProcess([], 0, "active\n", ""),
            )

        self.assertEqual(gateway.read_text(), "KEEP=private\n")

    def test_enable_server_restores_files_when_native_start_fails(self):
        self.install_plugin_fixture()
        client_key = self.home / ".local/state/fleet/cliproxyapi-client-key"
        client_key.parent.mkdir(parents=True)
        client_key.write_text("client-key_12345678901234567890123456789012\n")
        client_key.chmod(0o600)
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"PRIVATE": "original"}))
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        before_settings = settings.read_bytes()
        before_marketplaces = marketplaces.read_bytes()

        def runner(command, home, platform):
            code = 1 if command[-3:] == ["enable", "--now", "fleet-memory.service"] else 0
            return subprocess.CompletedProcess(command, code, "", "failed" if code else "")

        with self.assertRaisesRegex(memory.FleetMemoryError, "enable"):
            memory.enable_memory(
                self.client_root,
                self.home,
                "test-server",
                platform_name="linux",
                native_runner=runner,
            )

        self.assertEqual(settings.read_bytes(), before_settings)
        self.assertEqual(marketplaces.read_bytes(), before_marketplaces)
        self.assertFalse((self.home / ".config/systemd/user/fleet-memory.service").exists())

    def test_enable_client_installs_guard_launch_agent(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "", "")

        memory.enable_memory(
            self.client_root,
            self.home,
            "test-mac",
            platform_name="darwin",
            uid=501,
            native_runner=runner,
        )

        target = self.home / "Library/LaunchAgents/dev.agents-fleet.memory.plist"
        plist = __import__("plistlib").loads(target.read_bytes())
        self.assertEqual(plist["Label"], "dev.agents-fleet.memory")
        self.assertEqual(
            plist["ProgramArguments"],
            [str(self.home / ".local/bin/fleet"), "memory", "proxy"],
        )
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["SoftResourceLimits"]["NumberOfFiles"], 4096)

    def test_enable_linux_client_installs_only_proxy_service(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        memory.enable_memory(
            self.client_root,
            self.home,
            "test-linux",
            platform_name="linux",
            native_runner=runner,
        )

        service = self.home / ".config/systemd/user/fleet-memory-client.service"
        self.assertIn("ExecStart=%h/.local/bin/fleet memory proxy", service.read_text())
        self.assertIn(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-client.service"],
            commands,
        )
        self.assertFalse((self.home / ".config/systemd/user/fleet-memory.service").exists())
        self.assertFalse((self.home / ".config/systemd/user/fleet-memory-backup.timer").exists())
        self.assertFalse((self.home / ".claude-mem/.env").exists())

    def test_enable_linux_client_stops_owned_old_server_first(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        for name in memory.LINUX_UNITS:
            shutil.copy2(ROOT / "system/linux" / name, units / name)
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        memory.enable_memory(
            self.client_root,
            self.home,
            "test-linux",
            platform_name="linux",
            native_runner=runner,
        )

        stop = [
            "/usr/bin/systemctl", "--user", "disable", "--now",
            "fleet-memory-backup.timer", "fleet-memory.service",
        ]
        start = ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-client.service"]
        self.assertLess(commands.index(stop), commands.index(start))

    def test_failed_linux_client_transition_restarts_old_server(self):
        self.install_plugin_fixture()
        settings = self.home / ".claude-mem/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        marketplaces = self.home / ".claude/plugins/known_marketplaces.json"
        marketplaces.parent.mkdir(parents=True, exist_ok=True)
        marketplaces.write_text(json.dumps({"thedotmack": {"autoUpdate": True}}))
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        for name in memory.LINUX_UNITS:
            shutil.copy2(ROOT / "system/linux" / name, units / name)
        commands = []
        settings_at_restart = []

        def runner(command, home, platform):
            commands.append(command)
            if command[-3:] == ["enable", "--now", "fleet-memory.service"]:
                settings_at_restart.append(settings.read_text())
            failed = command[-3:] == ["enable", "--now", "fleet-memory-client.service"]
            return subprocess.CompletedProcess(command, int(failed), "active\n", "failed" if failed else "")

        with self.assertRaisesRegex(memory.FleetMemoryError, "guard"):
            memory.enable_memory(
                self.client_root,
                self.home,
                "test-linux",
                platform_name="linux",
                native_runner=runner,
            )

        self.assertIn(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory.service"],
            commands,
        )
        self.assertIn(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-backup.timer"],
            commands,
        )
        self.assertEqual(settings_at_restart, ["{}"])

    def test_worker_exec_uses_pinned_bundle_and_safe_environment(self):
        worker = self.install_plugin_fixture(vulnerable=True)
        bun = self.home / ".bun/bin/bun"
        bun.parent.mkdir(parents=True)
        bun.write_text("bun")
        bun.chmod(0o700)
        captured = {}

        def execute(path, arguments, environment):
            captured.update(path=path, arguments=arguments, environment=environment)

        memory.run_worker(self.home, self.config, executor=execute)

        self.assertEqual(captured["path"], str(bun))
        self.assertEqual(captured["arguments"], [str(bun), str(worker.resolve()), "--daemon"])
        self.assertEqual(captured["environment"]["CLAUDE_MEM_WORKER_HOST"], "127.0.0.1")
        self.assertEqual(captured["environment"]["CLAUDE_MEM_WORKER_PORT"], "37702")
        self.assertEqual(captured["environment"]["CLAUDE_MEM_CLAUDE_AUTH_METHOD"], "gateway")
        self.assertNotIn("updateMemorySessionId(e.sessionDbId,null)", worker.read_text())

    def test_disable_server_stops_services_without_deleting_memory(self):
        database = self.home / ".claude-mem/claude-mem.db"
        database.parent.mkdir()
        database.write_bytes(b"memory")
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        memory.disable_memory(
            self.client_root,
            self.home,
            "test-server",
            platform_name="linux",
            native_runner=runner,
        )

        self.assertIn(
            [
                "/usr/bin/systemctl", "--user", "disable", "--now",
                "fleet-memory-backup.timer", "fleet-memory.service",
            ],
            commands,
        )
        self.assertEqual(database.read_bytes(), b"memory")

    def test_disable_linux_client_stops_only_proxy_service(self):
        commands = []

        def runner(command, home, platform):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        memory.disable_memory(
            self.client_root,
            self.home,
            "test-linux",
            platform_name="linux",
            native_runner=runner,
        )

        self.assertIn(
            ["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-memory-client.service"],
            commands,
        )
        self.assertFalse(any("fleet-memory.service" in command for command in commands))

    def test_run_proxy_keeps_guard_until_stop_and_terminates_ssh(self):
        listen_socket = socket.socket()
        listen_socket.bind(("127.0.0.1", 0))
        listen_port = listen_socket.getsockname()[1]
        listen_socket.close()
        tunnel_socket = socket.socket()
        tunnel_socket.bind(("127.0.0.1", 0))
        tunnel_port = tunnel_socket.getsockname()[1]
        tunnel_socket.close()
        config = replace(self.config, worker_port=listen_port, tunnel_port=tunnel_port)
        stop = threading.Event()

        class Process:
            def __init__(inner_self):
                inner_self.terminated = False

            def poll(inner_self):
                return None

            def terminate(inner_self):
                inner_self.terminated = True

            def wait(inner_self, timeout=None):
                return 0

        process = Process()
        commands = []

        def factory(command):
            commands.append(command)
            threading.Timer(0.2, stop.set).start()
            return process

        with mock.patch.object(memory, "drain_observations", return_value=0) as drain:
            memory.run_proxy(config, home=self.home, stop=stop, process_factory=factory)
        drain.assert_called_once_with(self.home, config)

        self.assertEqual(commands, [memory.ssh_command(config)])
        self.assertTrue(process.terminated)

    def test_backup_creates_private_dated_snapshot_and_prunes(self):
        source = self.home / ".claude-mem/claude-mem.db"
        source.parent.mkdir()
        connection = sqlite3.connect(source)
        connection.executescript(
            """
            CREATE TABLE schema_versions(version INTEGER);
            INSERT INTO schema_versions VALUES (49);
            CREATE TABLE sdk_sessions(id INTEGER);
            CREATE TABLE observations(id INTEGER);
            CREATE TABLE session_summaries(id INTEGER);
            CREATE TABLE user_prompts(id INTEGER);
            CREATE TABLE pending_messages(status TEXT);
            """
        )
        connection.close()
        source_before = source.read_bytes()

        path, database_report = memory.backup_memory(
            self.home,
            self.config,
            now=datetime(2026, 8, 22, 10, 11, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            path,
            self.home / ".local/state/fleet/memory/backups/daily-20260822T101112Z.db.gz",
        )
        self.assertEqual(database_report.schema_version, 49)
        self.assertEqual(database_report.quick_check, "ok")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        restored = self.home / "restored.db"
        with gzip.open(path, "rb") as archive:
            restored.write_bytes(archive.read())
        self.assertEqual(memory.db.check_database(restored), database_report)
        self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(memory.latest_backup(self.home), path)
        self.assertFalse(path.with_suffix("").exists())

    def test_compress_file_round_trips_bytes_and_preserves_private_backup_age(self):
        source = self.home / "cold.log"
        payload = b"Preserve every original byte.\n" * 4096
        source.write_bytes(payload)
        os.utime(source, (1000000000, 1000000000))

        archive = memory.compress_file(source)

        with gzip.open(archive, "rb") as restored:
            self.assertEqual(restored.read(), payload)
        self.assertFalse(source.exists())
        self.assertLess(archive.stat().st_size, len(payload))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.stat().st_mtime, 1000000000)
        self.assertFalse(list(self.home.glob(".fleet-memory-gzip-*")))

    def test_compress_file_keeps_sources_on_corruption_change_or_existing_archive(self):
        payload = b"Original recovery data.\n" * 4096
        original_open = gzip.open
        for failure in ("corruption", "change", "conflict"):
            with self.subTest(failure=failure):
                source = self.home / f"{failure}.log"
                source.write_bytes(payload)
                archive = source.with_name(source.name + ".gz")
                def read_archive(*args, **kwargs):
                    if failure == "corruption":
                        return io.BytesIO(b"Corrupt restored bytes")
                    if failure == "change":
                        source.write_bytes(payload + b"New data")
                    return original_open(*args, **kwargs)
                if failure == "conflict":
                    archive.write_bytes(b"Existing recovery data")
                with mock.patch.object(memory.gzip, "open", side_effect=read_archive):
                    with self.assertRaises((memory.FleetMemoryError, FileExistsError)):
                        memory.compress_file(source)
                self.assertEqual(source.read_bytes(), payload + (b"New data" if failure == "change" else b""))
                if failure == "conflict":
                    self.assertEqual(archive.read_bytes(), b"Existing recovery data")
                else:
                    self.assertFalse(archive.exists())
                self.assertFalse(list(self.home.glob(".fleet-memory-gzip-*")))

    def test_server_status_is_ok_only_with_pinned_worker_clean_db_and_backup(self):
        self.install_plugin_fixture()
        memory.harden_plugin(self.home, self.config)
        backup = self.home / ".local/state/fleet/memory/backups/daily-20260822T101112Z.db"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"backup")
        backup.chmod(0o600)
        database = self.home / ".claude-mem/claude-mem.db"
        database.parent.mkdir()
        database.write_bytes(b"database")
        report = SimpleNamespace(
            quick_check="ok",
            foreign_key_errors=(),
            schema_version=50,
            counts={"pending": 0, "processing": 0},
        )

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        with mock.patch.object(memory.db, "check_database", return_value=report), mock.patch.object(
            memory, "worker_health", side_effect=lambda port, endpoint="health": {"version": "13.23.1"} if endpoint == "health" else {"queueDepth": 0}
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
            line, ok = memory.status_line(self.client_root, self.home, "test-server")

        self.assertTrue(ok)
        self.assertEqual(line, "memory: ok")

        os.utime(backup, (time.time() - 3 * 86400,) * 2)
        with mock.patch.object(memory.db, "check_database", return_value=report), mock.patch.object(
            memory, "worker_health", side_effect=lambda port, endpoint="health": {"version": "13.24.0"} if endpoint == "health" else {"queueDepth": 0}
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
            lines, ok = memory.status_report(self.client_root, self.home, "test-server")
        self.assertFalse(ok)
        self.assertIn("backup: error", lines)
        os.utime(backup, None)

        for depth in (4351, None, True):
            with self.subTest(depth=depth), mock.patch.object(memory.db, "check_database", return_value=report), mock.patch.object(
                memory, "worker_health", side_effect=lambda port, endpoint="health": {"version": "13.24.0"} if endpoint == "health" else {"queueDepth": depth}
            ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
                lines, ok = memory.status_report(self.client_root, self.home, "test-server")
            self.assertFalse(ok)
            self.assertIn(f"worker queue: {depth} pending" if type(depth) is int else "worker queue: unknown", lines)

    def test_server_status_reports_observer_failures(self):
        self.install_plugin_fixture()
        memory.harden_plugin(self.home, self.config)
        backup = self.home / ".local/state/fleet/memory/backups/daily-20260822T101112Z.db"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"backup")
        backup.chmod(0o600)
        observer = self.home / ".claude-mem/observer-health.json"
        observer.parent.mkdir(parents=True)
        observer.write_text(json.dumps({"consecutiveFailures": 3}))
        report = SimpleNamespace(
            quick_check="ok",
            foreign_key_errors=(),
            schema_version=50,
            counts={"pending": 0, "processing": 0},
        )

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "active\n", "")

        with mock.patch.object(memory.db, "check_database", return_value=report), mock.patch.object(
            memory, "worker_health", return_value={"version": "13.24.0", "queueDepth": 0}
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
            lines, ok = memory.status_report(self.client_root, self.home, "test-server")

        self.assertFalse(ok)
        self.assertIn("observer: error", lines)

    def test_server_status_limits_database_check_work(self):
        database = self.home / ".claude-mem/claude-mem.db"
        database.parent.mkdir()
        with sqlite3.connect(database) as connection:
            connection.executescript("CREATE TABLE schema_versions(version INTEGER); INSERT INTO schema_versions VALUES(50); CREATE TABLE pending_messages(status TEXT);")
            for table in memory.db.DURABLE_TABLES:
                connection.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO observations VALUES(?)", ((i,) for i in range(5000)))
        with mock.patch("time.monotonic", side_effect=[0] + [4] * 100):
            self.assertEqual(memory.server_database_status(self.home), (False, False))

        original = memory.db.readonly_connection

        def without_deep_scans(path):
            connection = original(path)
            connection.set_authorizer(lambda action, argument, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and argument in {"quick_check", "foreign_key_check"} else sqlite3.SQLITE_OK)
            return connection

        with mock.patch.object(memory.db, "readonly_connection", side_effect=without_deep_scans):
            self.assertEqual(memory.server_database_status(self.home), (True, True))
            with self.assertRaises(memory.db.FleetMemoryDatabaseError):
                memory.db.check_database(database)

    def test_client_status_requires_guard_and_remote_worker(self):
        self.install_plugin_fixture()
        memory.harden_plugin(self.home, self.config)

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "state = running\n", "")

        with mock.patch.object(memory, "worker_health", return_value={"version": "13.24.0", "queueDepth": 0}), mock.patch.object(
            memory, "port_is_bound", return_value=True
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "darwin"):
            line, ok = memory.status_line(self.client_root, self.home, "test-mac")

        self.assertTrue(ok)
        self.assertEqual(line, "memory: ok")

        with mock.patch.object(memory, "worker_health", return_value={"version": "13.24.0", "queueDepth": 4351}), mock.patch.object(
            memory, "port_is_bound", return_value=True
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "darwin"):
            lines, ok = memory.status_report(self.client_root, self.home, "test-mac")
        self.assertFalse(ok)
        self.assertIn("worker queue: 4351 pending", lines)

    def test_client_status_rejects_failed_launch_agent(self):
        self.install_plugin_fixture()
        memory.harden_plugin(self.home, self.config)

        def runner(command, home, platform):
            return subprocess.CompletedProcess(command, 0, "state = spawn scheduled\nlast exit code = 1\n", "")

        with mock.patch.object(memory, "worker_health", return_value={"version": "13.24.0", "queueDepth": 0}), mock.patch.object(
            memory, "port_is_bound", return_value=True
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "darwin"):
            line, ok = memory.status_line(self.client_root, self.home, "test-mac")

        self.assertFalse(ok)
        self.assertEqual(line, "memory: error")

    def test_linux_client_status_requires_proxy_and_remote_worker(self):
        self.install_plugin_fixture()
        memory.harden_plugin(self.home, self.config)

        def runner(command, home, platform):
            code = 0 if command == [
                "/usr/bin/systemctl", "--user", "is-active", "fleet-memory-client.service"
            ] else 1
            return subprocess.CompletedProcess(command, code, "active\n" if not code else "", "")

        with mock.patch.object(memory, "worker_health", return_value={"version": "13.24.0", "queueDepth": 0}), mock.patch.object(
            memory, "port_is_bound", return_value=True
        ), mock.patch.object(memory, "run_native", side_effect=runner), mock.patch.object(memory.sys, "platform", "linux"):
            line, ok = memory.status_line(self.client_root, self.home, "test-linux")

        self.assertTrue(ok)
        self.assertEqual(line, "memory: ok")


class FleetManualMemoryNativeTest(unittest.TestCase):
    def runtime_probe(self, source):
        worker = self.mcp.with_name("worker-service.cjs")
        with worker.open("a") as output:
            output.write("\nmodule.exports={Yw,jO,ud,dA,fleetRecoverPending,Ut};\n")
        source = "(async()=>{" + source + "})().then(()=>console.log('probe passed'),error=>{console.error(error);process.exit(1)});"
        result = subprocess.run([shutil.which("bun"), "-e", source, str(worker)],
                                env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe passed", result.stdout)

    def test_native_queue_survives_restart_and_dispose_until_confirmation(self):
        self.runtime_probe('''
const {Yw}=require(process.argv[1]),assert=require('node:assert/strict');
let queue=new Yw();
const id=queue.enqueue(1,{type:'observation',tool_name:'Read',toolUseId:'same-tool',prompt_number:7,agentId:'child',tool_response:['<private>secret','rest</private>','kept']});
const original=queue.buffers.get(1)[0].enqueuedAt;
queue.claimNext(1);queue.dispose(1);queue.clear(1);queue.fleetDb?.close();
queue=new Yw();
assert.equal(queue.getPendingCount(1),1,'Unconfirmed work must survive a restart');
const row=queue.buffers.get(1)[0];assert.equal(row.claimed,false);assert.equal(row.enqueuedAt,original);assert.equal(row.message.prompt_number,7);assert.equal(row.message.agentId,'child');
assert.ok(!JSON.stringify(row.message).includes('secret'));
assert.equal(queue.enqueue(1,{toolUseId:'same-tool'}),0);
queue.confirm(id);queue.fleetDb.close();queue=new Yw();assert.equal(queue.getTotalDepth(),0);queue.fleetDb.close();
''')

    def test_native_memory_falls_back_to_terra_high_only_for_allowance_errors(self):
        (self.home / ".claude-mem/.env").write_text("ANTHROPIC_BASE_URL=http://127.0.0.1:8317\nANTHROPIC_AUTH_TOKEN=fixture-key\n")
        self.runtime_probe('''
const {jO}=require(process.argv[1]),assert=require('node:assert/strict'),calls=[];
let status=500;
global.fetch=async(url,options)=>{const body=JSON.parse(options.body);calls.push(body);return new Response('{}',{status:body.model==='gpt-6.1-terra'?200:status})};
const agent=new jO({},{}),send=()=>agent.fetchChatCompletion('http://127.0.0.1/v1/chat/completions','fixture','claude-haiku-4-5-20251001',[],null,null,null,null);
const config=agent.getConfig();assert.equal(config.apiUrl,'http://127.0.0.1:8317/v1/chat/completions');assert.equal(config.apiKey,'fixture-key');
assert.equal((await send()).status,500);assert.equal(calls.length,1);
status=429;assert.equal((await send()).status,200);assert.equal(calls.at(-1).model,'gpt-6.1-terra');assert.equal(calls.at(-1).reasoning_effort,'high');
calls.length=0;await send();assert.equal(calls.length,1);assert.equal(calls[0].model,'gpt-6.1-terra');
let active=0,maximum=0;
global.fetch=async()=>{active++;maximum=Math.max(maximum,active);return new Response(new ReadableStream({start(controller){setTimeout(()=>{active--;controller.enqueue(new TextEncoder().encode('{}'));controller.close()},20)}}))};
await Promise.all(Array.from({length:5},send));assert.equal(maximum,2,'The limit must cover the full response, not only headers');
const timer=setTimeout;let requests=0;
global.setTimeout=(callback,ms,...args)=>timer(callback,ms/1000,...args);
global.fetch=async(url,options)=>{requests++;return new Response(new ReadableStream({start(controller){const pending=timer(()=>{controller.enqueue(new TextEncoder().encode(JSON.stringify({choices:[{message:{content:'fixture'}}]})));controller.close()},45);options.signal.addEventListener('abort',()=>{clearTimeout(pending);controller.error(new DOMException('Model deadline exceeded','AbortError'))},{once:true})}}))};
try{const response=await agent.query([{role:'user',content:'fixture'}],{apiUrl:'http://127.0.0.1/v1/chat/completions',apiKey:'fixture',model:'claude-haiku-4-5-20251001'});assert.equal(response.content,'fixture');assert.equal(requests,1)}finally{global.setTimeout=timer}
requests=0;
global.setTimeout=(callback,ms,...args)=>{if(ms>120000)throw Error('Unexpected long retry wait');return timer(callback,ms/1000,...args)};
global.fetch=async()=>{requests++;return new Response('{"error":{"message":"rate limited"}}',{status:429,headers:{'Retry-After':'7200'}})};
try{await assert.rejects(()=>agent.query([{role:'user',content:'fixture'}],{apiUrl:'http://127.0.0.1/v1/chat/completions',apiKey:'fixture',model:'claude-haiku-4-5-20251001'}),error=>error.kind==='rate_limit');assert.equal(requests,1)}finally{global.setTimeout=timer}
requests=0;
global.setTimeout=(callback,ms,...args)=>timer(callback,ms/1000,...args);
global.fetch=async()=>{requests++;throw new TypeError('connection lost after submission')};
try{await assert.rejects(()=>agent.query([{role:'user',content:'fixture'}],{apiUrl:'http://127.0.0.1/v1/chat/completions',apiKey:'fixture',model:'claude-haiku-4-5-20251001'}));assert.equal(requests,1,'An uncertain model submission must not be replayed automatically')}finally{global.setTimeout=timer}
''')

    def test_native_initialization_keeps_context_without_a_model_call(self):
        self.environment["CLAUDE_MEM_MODES_DIR"] = str(Path.home() / ".claude/plugins/cache/thedotmack/claude-mem/13.24.0/modes")
        self.runtime_probe('''
const {jO,Ut}=require(process.argv[1]),assert=require('node:assert/strict');
Ut.getInstance().loadMode('code');
let queries=0,registered=0;
const agent=new jO({getSessionStore:()=>({updateMemorySessionId:()=>registered++})},{});
agent.getConfig=()=>({apiKey:'fixture',model:'fixture',apiUrl:'http://127.0.0.1:1'});
agent.query=async()=>{queries++;return {content:''}};
agent.runMessageLoop=async()=>{};
const state={sessionDbId:1,contentSessionId:'fixture',project:'fixture',userPrompt:'Keep this task context',lastPromptNumber:1,conversationHistory:[],startTime:Date.now()};
await agent.startSession(state,{});
assert.equal(queries,0,'Initialization must not spend inference before processing an event');
assert.equal(registered,1);assert.equal(state.conversationHistory.length,1);
assert.ok(state.conversationHistory[0].content.includes('Keep this task context'));
''')

    def test_running_memory_generator_pauses_each_new_request_when_idle(self):
        self.runtime_probe('''
const {jO}=require(process.argv[1]),assert=require('node:assert/strict');
let requests=0;
global.fetch=async()=>{requests++;return new Response(JSON.stringify({choices:[{message:{content:'fixture'}}]}))};
const agent=new jO({},{}),state={fleetActivityUntil:Date.now()-1,abortController:new AbortController()};
const config={apiKey:'fixture',model:'claude-haiku-4-5-20251001',apiUrl:'http://127.0.0.1/v1/chat/completions'};
agent.prepareSessionExtras(state,config);
await assert.rejects(()=>agent.query([{role:'user',content:'queued event'}],config),error=>error.name==='AbortError' && /new session activity/.test(error.message));
assert.equal(requests,0,'An existing generator must not drain new model requests while idle');
assert.ok(state.abortController.signal.aborted);
state.fleetActivityUntil=Date.now()+60000;state.abortController=new AbortController();
assert.equal((await agent.query([{role:'user',content:'queued event'}],config)).content,'fixture');assert.equal(requests,1);
''')

    def test_native_empty_and_idle_sessions_do_not_start_inference(self):
        self.runtime_probe('''
const {dA,fleetRecoverPending}=require(process.argv[1]),assert=require('node:assert/strict');
let pending=0,starts=0;
const state={sessionDbId:1,conversationHistory:[]},items=[{id:1}];
const buffer={buffers:new Map([[1,items]]),getPendingCount:()=>pending};
const manager={getSession:()=>state,getMessageBuffer:()=>buffer,sessions:new Map([[1,state]])};
const routes=new dA(manager);routes.applyTierRouting=async()=>{};routes.startGeneratorWithProvider=async()=>starts++;
await routes.ensureGeneratorRunning(1,'init');assert.equal(starts,0,'An empty prompt must not start inference');
pending=1;await routes.ensureGeneratorRunning(1,'observation');assert.equal(starts,1);
const worker={initializationCompleteFlag:true,sessionManager:manager};
fleetRecoverPending(worker,routes);await Promise.resolve();assert.equal(starts,1,'Unchanged queued work must not trigger repeated model attempts');
state.fleetActivityUntil=Date.now()-1;items.push({id:2});
fleetRecoverPending(worker,routes);await Promise.resolve();assert.equal(starts,1,'Idle sessions must retain work without inference');
assert.equal(buffer.getPendingCount(1),1);
''')

    def test_native_error_response_keeps_claimed_items(self):
        self.runtime_probe('''
const {ud}=require(process.argv[1]),assert=require('node:assert/strict');
let confirmed=0,resets=0;
const state={sessionDbId:1,contentSessionId:'fixture',conversationHistory:[],abortController:new AbortController()};
await ud('API Error: 503 fleet_credit_usage_unavailable',state,{},
 {getClaimedMessages:()=>[{}],confirmClaimedMessages:async()=>confirmed++,resetProcessingToPending:async()=>resets++},null,0,null,'OpenRouter');
assert.equal(confirmed,0,'An error is not a saved memory');assert.equal(resets,1);assert.ok(state.abortController.signal.aborted);
const empty={...state,abortController:new AbortController(),conversationHistory:[]};
await ud('',empty,{}, {getClaimedMessages:()=>[{}],confirmClaimedMessages:async()=>confirmed++,resetProcessingToPending:async()=>resets++},null,0,null,'OpenRouter');
assert.equal(confirmed,0,'An empty response must not delete queued work');assert.equal(resets,2);assert.ok(empty.abortController.signal.aborted);
const initialization={...state,sessionDbId:2,abortController:new AbortController(),conversationHistory:[]};
await ud('Ready to observe',initialization,{}, {getClaimedMessages:()=>[],resetProcessingToPending:async()=>{throw Error('Initialization has no claimed items')}},null,0,null,'OpenRouter');
assert.equal(initialization.abortController.signal.aborted,false,'Initialization prose must not stop later observations');
const {dA}=require(process.argv[1]);let starts=0;
const routes=new dA({getSession:()=>({fleetRetryAt:Date.now()+7200000})});
routes.applyTierRouting=async()=>{};routes.startGeneratorWithProvider=async()=>starts++;
await routes.ensureGeneratorRunning(1,'new-observation');assert.equal(starts,0,'New captures must honor the provider retry deadline');
''')

    def setUp(self):
        native = Path.home() / ".claude/plugins/cache/thedotmack/claude-mem/13.24.0"
        bun = shutil.which("bun")
        if not bun or not (native / "scripts/worker-service.cjs").exists():
            self.skipTest("Native integration requires the installed pinned Claude-mem bundle and Bun")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        plugin = self.home / ".claude/plugins/cache/thedotmack/claude-mem/13.24.0"
        scripts = plugin / "scripts"
        scripts.mkdir(parents=True)
        for name in ("worker-service.cjs", "mcp-server.cjs"):
            shutil.copyfile(native / "scripts" / name, scripts / name)
        native_codex = Path.home() / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts"
        if native_codex.exists():
            codex = self.home / ".codex/plugins/cache/claude-mem-local/claude-mem/13.24.0/scripts"
            codex.mkdir(parents=True)
            for name in ("worker-service.cjs", "mcp-server.cjs"):
                shutil.copyfile(native_codex / name, codex / name)
        original_worker = (scripts / "worker-service.cjs").read_text()
        (self.home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({
            "plugins": {"claude-mem@thedotmack": [{"version": "13.24.0", "installPath": str(plugin)}]}
        }))
        self.config_root = self.home / "config"
        self.config_root.mkdir()
        (self.config_root / "memory.toml").write_text(VALID_CONFIG)
        self.config = memory.load_config(self.config_root)
        memory.harden_plugin(self.home, self.config)
        worker = scripts / "worker-service.cjs"
        if self._testMethodName == "test_cli_save_without_worker_reload":
            worker.write_text(original_worker.replace(memory.MANUAL_WORKER_ROUTES.decode(), memory.MANUAL_WORKER_ROUTE.decode())
                              .replace(memory.MANUAL_SAVE_SYNC_PATCH.decode(), memory.MANUAL_SAVE_SYNC.decode()))
        worker.write_text(worker.read_text() + '\nvP=require(' + json.dumps(str(native / "sqlite/observations/files.js"))
                          + ');module.exports={qw,yA,ww,ml,ln,pA,mA,zO,zw,WO,GO,Bi};')
        self.mcp = scripts / "mcp-server.cjs"
        source = self.mcp.read_text()
        self.assertEqual(source.count('II().catch(e=>{'), 1)
        self.mcp.write_text(source.replace('II().catch(e=>{', 'false&&II().catch(e=>{') + '\nmodule.exports={Fl};')
        self.environment = {
            "HOME": str(self.home), "PATH": os.environ["PATH"], "NODE_PATH": str(native / "node_modules"),
            "CLAUDE_MEM_DATA_DIR": str(self.home / ".claude-mem"), "CLAUDE_MEM_RUNTIME": "worker",
            "CLAUDE_MEM_LOG_LEVEL": "SILENT", "DO_NOT_TRACK": "1", "DISABLE_TELEMETRY": "true",
        }
        runner = self.home / "native-worker.cjs"
        runner.write_text('''
global.fetch=()=>{throw Error("Unexpected external request")};
const {qw,yA,ww,ml,ln,pA,mA,zO,zw,WO,GO,Bi}=require(process.argv[2]);
const store=new qw(process.argv[3]), vectors=new Map(), indexCalls=[];
let failIndex=false, notifications=0;
const sync=new ml("fleet-test");
Bi.getInstance=()=>({callTool:async(name,args)=>{
  indexCalls.push(name);
  await new Promise(resolve=>setTimeout(resolve,10));
  if(failIndex)throw Error("index outage");
  if(name==="chroma_create_collection")return {};
  if(name==="chroma_get_documents")return {ids:args.ids.filter(id=>vectors.has(id))};
  if(name==="chroma_add_documents" && args.ids.some(id=>vectors.has(id)))throw Error("IDs already exist");
  if(!["chroma_add_documents","chroma_update_documents"].includes(name))throw Error("Unexpected index tool");
  args.ids.forEach((id,i)=>vectors.set(id,{id,document:args.documents[i],metadata:args.metadatas[i]}));
  return {};
}});
const manager={getSessionStore:()=>store,getChromaSync:()=>sync,getCloudSync:()=>({notify(){notifications++}})};
const express=ww(),app=express();app.use(express.json({limit:"4mb"}));
const routes=new yA(manager,"fleet-test");routes.setupRoutes(app);
new pA(null,manager,null,null,null,Date.now()).setupRoutes(app);
new mA(new zO(new zw(store.db),store,null,new WO(),new GO())).setupRoutes(app);
app.post("/test/index",(r,n)=>{failIndex=r.body.fail;n.json({});});
app.get("/test/state",async(r,n)=>{
  await routes.fleetMemorySync?.catch(()=>{});
  if(r.query.retry)await sync.backfillObservations(store,"fleet-test",ln.get("fleet-test").observations);
  n.json({vectors:[...vectors.values()],pending:ln.getPending("fleet-test","observations"),notifications,indexCalls});
});
const server=app.listen(0,"127.0.0.1",()=>process.stdout.write(JSON.stringify({port:server.address().port})+"\\n"));
process.stdin.once("data",()=>server.close(()=>{store.close();process.exit(0)}));
''')
        self.database = self.home / "native.db"
        self.worker = subprocess.Popen([bun, str(runner), str(worker), str(self.database)],
                                       env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_worker)
        with selectors.DefaultSelector() as selector:
            selector.register(self.worker.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), "Native worker fixture did not start")
        line = self.worker.stdout.readline()
        if not line:
            self.fail(self.worker.stderr.read().decode())
        self.port = json.loads(line)["port"]
        self.environment["CLAUDE_MEM_WORKER_PORT"] = str(self.port)
        root = self.home / "root"
        root.mkdir()
        (root / "memory.toml").write_text(VALID_CONFIG.replace("worker_port = 37702", f"worker_port = {self.port}")
                                         .replace("tunnel_port = 37703", f"tunnel_port = {self.port % 65535 + 1}"))
        self.cli_root = root

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.communicate(b"stop", timeout=10)
        for stream in (self.worker.stdin, self.worker.stdout, self.worker.stderr):
            stream.close()

    def request(self, path, body=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request("POST" if body is not None else "GET", path,
                           json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def cli(self, command, body):
        result = subprocess.run([sys.executable, "-B", str(Path(memory.__file__)), str(self.cli_root), command],
                                input=json.dumps(body), capture_output=True, text=True, env=self.environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def mcp_calls(self, calls):
        probe = self.home / "mcp-probe.cjs"
        probe.write_text('''
const originalFetch=fetch;
global.fetch=(url,options)=>{
  if(new URL(url).hostname!=="127.0.0.1")throw Error("Unexpected external request");
  return originalFetch(url,options);
};
const {Fl}=require(process.argv[2]);
(async()=>{
  const calls=JSON.parse(require("fs").readFileSync(0,"utf8")), results=[];
  for(const call of calls)results.push(await Fl._requestHandlers.get(call.method)(call,{}));
  process.stdout.write(JSON.stringify(results));process.exit(0);
})().catch(error=>{process.stderr.write(String(error));process.exit(1)});
''')
        result = subprocess.run([shutil.which("node"), str(probe), str(self.mcp)], input=json.dumps(calls),
                                env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_mcp_and_cli_save_retrieve_update_search(self):
        listed, saved = self.mcp_calls([
            {"method": "tools/list"},
            {"method": "tools/call", "params": {"name": "save_memory", "arguments": {
                "text": "olduniqueterm <private>hidden-secret</private>", "title": "Original", "project": "fleet-test",
                "metadata": {"nested": ["public <private>hidden-metadata</private>"]},
            }}},
        ])
        names = {tool["name"] for tool in listed["tools"]}
        self.assertTrue({"save_memory", "update_memory"} <= names)
        self.assertNotIn("observation_add", names)
        identity = json.loads(saved["content"][0]["text"])["id"]
        status, rows = self.request("/api/observations/batch", {"ids": [identity]})
        self.assertEqual(status, 200)
        before = rows[0]
        self.assertEqual(before["narrative"], "olduniqueterm")
        self.assertEqual(json.loads(before["metadata"]), {"nested": ["public "]})
        self.assertEqual(self.request("/api/search?query=olduniqueterm&format=json")[1]["totalResults"], 1)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE observations SET synced_at=1 WHERE id=?", (identity,))
        self.cli("update", {"id": identity, "text": "newuniqueterm", "title": "Changed"})
        after = self.request("/api/observations/batch", {"ids": [identity]})[1][0]
        self.assertEqual(after["narrative"], "newuniqueterm")
        for key in ("id", "project", "memory_session_id", "created_at_epoch", "metadata"):
            self.assertEqual(before[key], after[key], key)
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(after["sync_rev"], "2")
        self.assertIsNone(after["synced_at"])
        self.assertEqual(self.request("/api/search?query=olduniqueterm&format=json")[1]["totalResults"], 0)
        self.assertEqual(self.request("/api/search?query=newuniqueterm&format=json")[1]["totalResults"], 1)
        second = self.cli("save", {"text": "anotherterm", "project": "fleet-test"})
        updated, retrieved = self.mcp_calls([
            {"method": "tools/call", "params": {"name": "update_memory", "arguments": {"id": second["id"], "text": "finalterm", "metadata": {"changed": True}}}},
            {"method": "tools/call", "params": {"name": "get_observations", "arguments": {"ids": [second["id"]]}}},
        ])
        self.assertEqual(json.loads(updated["content"][0]["text"])["id"], second["id"])
        self.assertEqual(json.loads(retrieved["content"][0]["text"])[0]["narrative"], "finalterm")
        self.assertEqual(json.loads(json.loads(retrieved["content"][0]["text"])[0]["metadata"]), {"changed": True})
        self.assertEqual(self.cli("save", {"text": "finalterm", "title": second["title"], "project": "fleet-test"})["id"], second["id"])
        state = self.request("/test/state")[1]
        self.assertEqual(state["pending"], [])
        self.assertEqual({doc["document"] for doc in state["vectors"]}, {"newuniqueterm", "finalterm"})
        self.assertIn("chroma_update_documents", state["indexCalls"])
        self.assertGreaterEqual(state["notifications"], 4)
        self.assertNotIn(b"hidden-", self.database.read_bytes())

    def test_native_update_rejects_invalid_ids_imports_and_conflicts_and_retries_index(self):
        first = self.cli("save", {"text": "before", "title": "First", "project": "fleet-test"})
        other = self.cli("save", {"text": "other", "title": "Other", "project": "fleet-test"})
        self.assertEqual(self.request("/api/memory/update", {"id": 99999, "text": "missing"})[0], 404)
        self.assertEqual(self.request("/api/memory/update", {"id": first["id"], "text": "other", "title": "Other"})[0], 409)
        for body in ({"id": 0, "text": "bad"}, {"id": first["id"], "text": "<private>hidden</private>"},
                     {"id": first["id"], "text": "bad", "project": "different"}):
            self.assertEqual(self.request("/api/memory/update", body)[0], 400)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE observations SET origin_device_id='remote',origin_local_id='1' WHERE id=?", (other["id"],))
        self.assertEqual(self.request("/api/memory/update", {"id": other["id"], "text": "changed"})[0], 409)
        self.request("/test/state")
        self.request("/test/index", {"fail": True})
        self.cli("update", {"id": first["id"], "text": "afteroutage"})
        self.assertEqual(self.request("/test/state")[1]["pending"], [first["id"]])
        self.request("/test/index", {"fail": False})
        state = self.request("/test/state?retry=1")[1]
        self.assertEqual(state["pending"], [])
        self.assertIn("afteroutage", {doc["document"] for doc in state["vectors"]})
        self.assertEqual(self.request("/api/search?query=afteroutage&format=json")[1]["totalResults"], 1)

    def test_cli_save_without_worker_reload(self):
        saved = self.cli("save", {"text": "existingworkerterm <private>hidden-secret</private>", "project": "fleet-test"})
        rows = self.request("/api/observations/batch", {"ids": [saved["id"]]})[1]
        self.assertEqual(rows[0]["narrative"], "existingworkerterm")
        self.assertEqual(self.request("/api/search?query=existingworkerterm&format=json")[1]["totalResults"], 1)
        mcp_saved = self.mcp_calls([{"method": "tools/call", "params": {"name": "save_memory", "arguments": {
            "text": "existingworkermcpterm", "project": "fleet-test",
        }}}])[0]
        self.assertTrue(json.loads(mcp_saved["content"][0]["text"])["success"])
        result = subprocess.run([sys.executable, "-B", str(Path(memory.__file__)), str(self.cli_root), "update"],
                                input=json.dumps({"id": saved["id"], "text": "changed"}), capture_output=True, text=True,
                                env=self.environment, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("update support is not loaded", result.stderr)
        self.assertIn("Keep the running worker", result.stderr)
        self.assertEqual(self.request("/api/observations/batch", {"ids": [saved["id"]]})[1][0]["narrative"], "existingworkerterm")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
