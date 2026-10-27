#!/usr/bin/env python3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import gzip
import hashlib
import http.client
import importlib.util
import json
import os
import plistlib
import pwd
import re
import selectors
import socket
import socketserver
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid


db_spec = importlib.util.spec_from_file_location("fleet_memory_db", Path(__file__).with_name("fleet_memory_db.py"))
db = importlib.util.module_from_spec(db_spec)
db_spec.loader.exec_module(db)


class FleetMemoryError(RuntimeError):
    pass


VULNERABLE_WORKER = b"e.memorySessionId&&(this.dbManager.getSessionStore().updateMemorySessionId(e.sessionDbId,null),e.memorySessionId=null)"
HARDENED_WORKER = b"e.memorySessionId&&(e.memorySessionId=null)"
STALE_WORKER_HEALTH_VERSION = b'QZ="13.23.1"'
PINNED_WORKER_HEALTH_VERSION = b'QZ="13.24.0"'
WORKER_VERSION_ALIASES = {"13.24.0": {"13.23.1"}}
RUNTIME_BEGIN = b'"fleet-memory-runtime-start";\n'
RUNTIME_END = b'\n"fleet-memory-runtime-end";\n'
RUNTIME_JS = RUNTIME_BEGIN + Path(__file__).with_name("fleet_memory_runtime.cjs").read_bytes() + RUNTIME_END
RUNTIME_PATCHES = {
    b';Io();il();var Zw=class{': b';Yw=fleetDurableBuffer(Yw);Io();il();var Zw=class{',
    b'Kme((r,n)=>e.ensureGeneratorRunning(r,n)),': b'Kme((r,n)=>e.ensureGeneratorRunning(r,n)),setInterval(()=>fleetRecoverPending(this,e),30000).unref(),',
    b'let a=(e??"").split(":")[0];': b'if(n.getMessageBuffer().getPendingCount(i)>0){await n.resetProcessingToPending(i);return}let a=(e??"").split(":")[0];',
    b'let F=mQ(t),ee=sy(t);': b'if(!n.getClaimedMessages(e.sessionDbId).length)return;await n.resetProcessingToPending(e.sessionDbId);e.abortReason="auth:invalid-output";e.abortController.abort();return;let F=mQ(t),ee=sy(t);',
    b'let i=zme(),o=i.provider;if(!s.generatorPromise)': b'if(s.fleetRetryAt>Date.now()||!fleetMemoryCanRun(this.sessionManager,s,n))return;let i={provider:"openrouter",gatewayProbeClaimId:null},o=i.provider;if(!s.generatorPromise)',
    b',l=qo(e);e.conversationHistory.push({role:"user",content:c});try{e.lastPromptSentAt=Date.now(),e.lastGeneratorSource="init";let d=await this.query(e.conversationHistory,n);await this.handleInitResponse(d,e,r,i,l)': b';e.conversationHistory.push({role:"user",content:c});try{e.lastGeneratorSource="init-context"',
    b'let m=f instanceof Error?f.message:String(f);if(n==="claude"&&Vs(f)': b'if(Vs(f)&&f.kind==="rate_limit"&&Number.isFinite(f.retryAfterMs)){r.fleetRetryAt=Date.now()+Math.max(60000,f.retryAfterMs);v.warn("SESSION","Memory request rate-limited; queued work retained",{sessionId:r.sessionDbId,retryAt:new Date(r.fleetRetryAt).toISOString()})}let m=f instanceof Error?f.message:String(f);if(n==="claude"&&Vs(f)',
    b'getConfig(){return jme()}missingApiKeyError()': b'getConfig(){return fleetMemoryConfig()}missingApiKeyError()',
    b'prepareSessionExtras(e,r){e.endpointClass=': b'prepareSessionExtras(e,r){r.fleetSession=e;e.endpointClass=',
    b'async query(e,r){return this.queryOpenRouterMultiTurn(e,r.apiKey,r.model,r.apiUrl,r.siteUrl,r.appName)}': b'async query(e,r){if(r.fleetSession&&!(r.fleetSession.fleetActivityUntil>Date.now())){r.fleetSession.abortReason="idle";r.fleetSession.abortController.abort();throw new DOMException("Memory paused until new session activity","AbortError")}return this.queryOpenRouterMultiTurn(e,r.apiKey,r.model,r.apiUrl,r.siteUrl,r.appName)}',
    b'{label:`OpenRouter ${n}`}': b'{label:`OpenRouter ${n}`,perAttemptTimeoutMs:120000,maxRetries:0}',
    b'if(n=c,!oIt(c)||s===r.maxRetries)throw c;let l;': b'if(n=c,!oIt(c)||s===r.maxRetries||Vs(c)&&c.kind==="rate_limit"&&c.retryAfterMs>r.maxDelayMs)throw c;let l;',
    b'fetchChatCompletion(e,r,n,s,i,o,a,c){return fetch(e,{method:"POST",headers:{Authorization:`Bearer ${r}`,...Pme(i,o),"Content-Type":"application/json",...a?{"x-claude-mem-prior-request-id":a}:{}},body:JSON.stringify({model:n,messages:s,temperature:.3,max_tokens:4096,...e.includes("openrouter.ai")?{usage:{include:!0}}:{}}),signal:c})}': b'fetchChatCompletion(e,r,n,s,i,o,a,c){return fleetMemoryFetch(e,r,n,s,i,o,a,c)}',
}
MEMORY_GATEWAY_URL = "http://127.0.0.1:8317"
OBSERVATION_ENTRY = b'async function PPt(t,e){'
ENQUEUE_MEMORY = (
    'const r=require("child_process").spawnSync('
    'require("path").join(require("os").homedir(),".local/bin/fleet"),'
    '["memory","enqueue"],{input:JSON.stringify(p),encoding:"utf8",timeout:2000});'
    'if(r.error||r.status!==0){console.error("Fleet memory enqueue failed; observation was not acknowledged");process.exit(1);}'
    'return {continue:true,suppressOutput:true};'
)
OBSERVATION_ENQUEUE = OBSERVATION_ENTRY + (
    'if(e==="codex"){'
    'const p={contentSessionId:t.sessionId,platformSource:e,tool_name:t.toolName,'
    'tool_input:t.toolInput,tool_response:t.toolResponse,cwd:t.cwd,agentId:t.agentId,'
    'agentType:t.agentType,tool_use_id:t.toolUseId};' + ENQUEUE_MEMORY + '}'
).encode()
SUMMARY_REQUEST = b'let c=await fs("/api/sessions/summarize","POST",{contentSessionId:e,last_assistant_message:n,platformSource:i,observedModel:s,observedBilling:o});'
SUMMARY_ENQUEUE = (
    'if(i==="codex"){const p={contentSessionId:e,last_assistant_message:n,'
    'platformSource:i,observedModel:s,observedBilling:o,queue_kind:"summarize"};'
    + ENQUEUE_MEMORY + '}'
).encode() + SUMMARY_REQUEST
PROMPT_REQUEST = b'let u=await su.executeWithWorkerFallback("/api/sessions/init","POST",{contentSessionId:e,project:i,prompt:s,platformSource:o},o==="codex"?{workerStartupTimeoutMs:jr.POST_SPAWN_WAIT,timeoutMs:2e3}:void 0);'
PROMPT_ORDER = (
    'let u;if(o==="codex"){const r=require("child_process").spawnSync('
    'require("path").join(require("os").homedir(),".local/bin/fleet"),'
    '["memory","init"],{input:JSON.stringify({contentSessionId:e,project:i,prompt:s,'
    'platformSource:o}),encoding:"utf8",timeout:7000});'
    'if(r.error||r.status!==0){console.error("Fleet memory prompt was not acknowledged");process.exit(1);}'
    'u=JSON.parse(r.stdout);if(u.status==="pending")return {continue:true,suppressOutput:true};'
    '}else{' + PROMPT_REQUEST.decode().replace('let u=', 'u=', 1) + '}'
).encode()
CODEX_TOOL_ID = b'toolResponse:e.tool_response,transcriptPath:au(e.transcript_path)'
CODEX_TOOL_ID_CAPTURE = b'toolResponse:e.tool_response,toolUseId:au(e.tool_use_id),transcriptPath:au(e.transcript_path)'
WORKER_DRAIN = (
    'if(process.argv.includes("--daemon")){let busy=false;'
    'setInterval(()=>{if(busy)return;busy=true;require("child_process").execFile('
    'require("path").join(require("os").homedir(),".local/bin/fleet"),["memory","drain"],'
    '{timeout:10000},e=>{busy=false;if(e)console.error("Fleet memory queue drain failed; run fleet memory status");});'
    '},5000).unref();}\n'
).encode()
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
MEMORY_SKIP_TOOLS = tuple(
    prefix + tool
    for prefix in ("", "mcp__mcp_search__", "mcp__mcp-search__", "mcp__plugin_claude-mem_mcp-search__", "mcp__claude-mem__")
    for tool in ("search", "get_observations", "timeline", "save_memory", "update_memory")
)
MEMORY_PRIVATE = re.compile(r'<(private|claude-mem-context|system_instruction|system-instruction|persisted-output|system-reminder)\b[^>]*>[\s\S]*?</\1>')
MANUAL_BODY_JS = ('''
function fleetMemoryBody(body, update=false) {
  const validate = value => {
    const allowed = update ? ["id","text","title","metadata"] : ["text","title","project","metadata"];
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Object.keys(value).some(key => !allowed.includes(key)) ||
        typeof value.text !== "string" || !value.text.trim() ||
        ["title","project"].some(key => key in value && typeof value[key] !== "string") ||
        "metadata" in value && (!value.metadata || typeof value.metadata !== "object" || Array.isArray(value.metadata)) ||
        update && (!Number.isSafeInteger(value.id) || value.id <= 0)) throw Error("Invalid Fleet memory payload.");
  };
  validate(body);
  let encoded = JSON.stringify(body);
  if (Buffer.byteLength(encoded) > ''' + str(MAX_OBSERVATION_BYTES) + ''') throw Error("Fleet memory payload exceeds the size limit.");
  const privateTags = new RegExp(''' + json.dumps(MEMORY_PRIVATE.pattern) + ''', "g");
  for (let i=0; i<100; i++) {
    const filtered = encoded.replace(privateTags, "");
    if (filtered === encoded) {
      const value = JSON.parse(filtered);
      validate(value);
      value.text = value.text.trim();
      return value;
    }
    encoded = filtered;
  }
  throw Error("Invalid Fleet memory payload.");
}
''').encode()
MANUAL_WORKER_ROUTE = b'setupRoutes(r){r.post("/api/memory/save",Un(XIt),this.handleSaveMemory.bind(this))}'
MANUAL_WORKER_ROUTES = b'''setupRoutes(r){
  r.post("/api/memory/save",this.fleetMemoryRequest(false),Un(XIt),this.handleSaveMemory.bind(this));
  r.post("/api/memory/update",this.fleetMemoryRequest(true),this.fleetUpdateMemory);
}
fleetMemoryRequest(update){return (r,n,next)=>{
  try {r.body=fleetMemoryBody(r.body,update);next();}
  catch {n.status(400).json({error:"Invalid Fleet memory payload."});}
};}
fleetSyncObservation(id){
  const store=this.dbManager.getSessionStore(), row=store.getObservationById(id);
  ln.markPending(row.project,"observations",[id]);
  this.fleetMemorySync=(this.fleetMemorySync||Promise.resolve()).catch(()=>{}).then(async()=>{
    const sync=this.dbManager.getChromaSync();
    if(!sync)return;
    const current=store.db.prepare("SELECT o.*,s.platform_source FROM observations o LEFT JOIN sdk_sessions s ON s.memory_session_id=o.memory_session_id WHERE o.id=?").get(id);
    const docs=sync.formatObservationDocs(current), written=await sync.addDocuments(docs);
    if(written===docs.length && current.sync_rev===store.getObservationById(id).sync_rev)ln.bump(current.project,"observations",id);
  });
  return this.fleetMemorySync;
}
fleetUpdateMemory=this.wrapHandler(async(r,n)=>{
  const body=r.body, store=this.dbManager.getSessionStore(), row=store.getObservationById(body.id);
  if(!row){n.status(404).json({error:"Memory ID was not found."});return;}
  if(row.origin_device_id!==null){n.status(409).json({error:"Update this memory on its origin device."});return;}
  const title=body.title===undefined?row.title:body.title;
  const metadata=body.metadata===undefined?row.metadata:JSON.stringify(body.metadata);
  const hash=YJ(row.memory_session_id,title,body.text);
  if(store.db.prepare("SELECT id FROM observations WHERE memory_session_id=? AND content_hash=? AND id<>?").get(row.memory_session_id,hash,body.id)){
    n.status(409).json({error:"Another memory already has this content."});return;
  }
  store.db.transaction(()=>{
    ln.markPending(row.project,"observations",[body.id]);
    store.db.prepare("UPDATE observations SET title=?,narrative=?,metadata=?,content_hash=?,sync_rev=?,synced_at=NULL WHERE id=?")
      .run(title,body.text,metadata,hash,Gn(row.sync_rev),body.id);
  })();
  this.dbManager.getCloudSync()?.notify();
  this.fleetSyncObservation(body.id).catch(()=>{v.error("CHROMA","Fleet memory index sync failed; native backfill will retry",{id:body.id});});
  n.json({success:true,id:body.id,title,project:row.project,message:`Memory updated as observation #${body.id}`});
});'''
MANUAL_SAVE_SYNC = b'p.syncObservation(h.id,f,u,m,0,h.createdAtEpoch)'
MANUAL_SAVE_SYNC_PATCH = b'this.fleetSyncObservation(h.id)'
MANUAL_WORKER_PINS = {
    b'getObservationById(e,r){': 1, b'function YJ(t,e,r){': 1, b'ensureSyncOriginColumns(){': 1,
    b'CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN': 2,
    b'getPending(t,e){': 1, b'async addDocuments(e){': 1,
}
MANUAL_MCP_ENTRY = b'var kS=[{name:"important_workflow",'
MANUAL_MCP_TOOLS = []
for _operation in ("save", "update"):
    _properties = {"text": {"type": "string", "minLength": 1}, "title": {"type": "string"},
                   "metadata": {"type": "object", "additionalProperties": True}}
    _properties.update({"id": {"type": "integer", "minimum": 1}} if _operation == "update" else {"project": {"type": "string"}})
    _schema = {"type": "object", "properties": _properties, "required": ["text", "id"] if _operation == "update" else ["text"], "additionalProperties": False}
    MANUAL_MCP_TOOLS.append(
        '{name:' + json.dumps(_operation + "_memory") + ',description:'
        + json.dumps("Save memory directly without an LLM. Worker runtime only. Returns the observation ID." if _operation == "save" else "Update memory by observation ID. Keeps its project and session. Worker runtime only. Imported rows must be updated on their origin device.")
        + ',inputSchema:' + json.dumps(_schema) + ',handler:async body=>{'
        'if(Ci()!=="worker")throw Error("Fleet memory writes require worker runtime.");'
        'return await vt("/api/memory/' + _operation + '",{body:fleetMemoryBody(body,' + ('true' if _operation == "update" else 'false') + ')});}}'
    )
MANUAL_MCP_PATCH = MANUAL_BODY_JS + ('var kS=[' + ','.join(MANUAL_MCP_TOOLS) + ',{name:"important_workflow",').encode()


@dataclass(frozen=True)
class MemoryConfig:
    version: str
    server_host: str
    client_hosts: tuple[str, ...]
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
        client_host = values.pop("client_host", None)
        client_hosts = values.pop("client_hosts", None)
        if (client_host is None) == (client_hosts is None) or (
            client_hosts is not None and not isinstance(client_hosts, list)
        ):
            raise TypeError
        values["client_hosts"] = (client_host,) if client_host is not None else tuple(client_hosts)
        config = MemoryConfig(**values)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError) as error:
        raise FleetMemoryError("Invalid Fleet memory config.") from error
    if config.worker_host != "127.0.0.1":
        raise FleetMemoryError("Fleet memory worker must use loopback.")
    host = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
    target = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+")
    version = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
    if (
        not isinstance(config.server_host, str)
        or not host.fullmatch(config.server_host)
        or not config.client_hosts
        or not all(isinstance(value, str) and host.fullmatch(value) for value in config.client_hosts)
        or len(config.client_hosts) != len(set(config.client_hosts))
        or config.server_host in config.client_hosts
    ):
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


def harden_plugin(home, config):
    script = resolve_plugin(home, config.version)
    contents = safe_regular(script).read_bytes()
    memory_ok = contents.count(HARDENED_WORKER) == 1 and VULNERABLE_WORKER not in contents
    version_ok = contents.count(PINNED_WORKER_HEALTH_VERSION) == 1 and STALE_WORKER_HEALTH_VERSION not in contents
    if not memory_ok and (contents.count(VULNERABLE_WORKER) != 1 or HARDENED_WORKER in contents):
        raise FleetMemoryError("Claude-mem pinned worker does not match the reviewed bundle.")
    if not version_ok and (contents.count(STALE_WORKER_HEALTH_VERSION) != 1 or PINNED_WORKER_HEALTH_VERSION in contents):
        raise FleetMemoryError("Claude-mem pinned worker does not match the reviewed bundle.")
    contents = contents.replace(VULNERABLE_WORKER, HARDENED_WORKER)
    contents = contents.replace(STALE_WORKER_HEALTH_VERSION, PINNED_WORKER_HEALTH_VERSION)
    primary_contents = contents
    updates = {}
    retired_skills = {}
    guard = client_worker_guard(config)
    for candidate in (script, Path(home) / ".codex/plugins/cache/claude-mem-local/claude-mem" / config.version / "scripts/worker-service.cjs"):
        if not candidate.exists():
            continue
        contents = primary_contents if candidate == script else safe_regular(candidate).read_bytes()
        contents = contents.replace(
            b'if(s.fleetRetryAt>Date.now())return;let i={provider:"openrouter",gatewayProbeClaimId:null},o=i.provider;if(!s.generatorPromise)',
            b'let i=zme(),o=i.provider;if(!s.generatorPromise)',
        )
        contents = contents.replace(
            b'if(t&&t.trim()){if(!n.getClaimedMessages(e.sessionDbId).length)return;await n.resetProcessingToPending(e.sessionDbId);e.abortReason="auth:invalid-output";e.abortController.abort();return}let F=mQ(t),ee=sy(t);',
            b'let F=mQ(t),ee=sy(t);',
        )
        contents = contents.replace(b'{label:`OpenRouter ${n}`,perAttemptTimeoutMs:120000,maxRetries:1}', b'{label:`OpenRouter ${n}`}')
        if any(contents.count(fragment) != count for fragment, count in MANUAL_WORKER_PINS.items()):
            raise FleetMemoryError("Claude-mem memory store does not match the reviewed bundle.")
        for original, replacement in (*RUNTIME_PATCHES.items(), (OBSERVATION_ENTRY, OBSERVATION_ENQUEUE), (CODEX_TOOL_ID, CODEX_TOOL_ID_CAPTURE), (SUMMARY_REQUEST, SUMMARY_ENQUEUE), (PROMPT_REQUEST, PROMPT_ORDER), (MANUAL_WORKER_ROUTE, MANUAL_WORKER_ROUTES), (MANUAL_SAVE_SYNC, MANUAL_SAVE_SYNC_PATCH)):
            if contents.count(replacement) == 1:
                continue
            if contents.count(original) != 1 or replacement in contents:
                raise FleetMemoryError("Claude-mem observation hook does not match the reviewed bundle.")
            contents = contents.replace(original, replacement)
        if MANUAL_BODY_JS not in contents:
            first, separator, remaining = contents.partition(b"\n")
            contents = first + separator + MANUAL_BODY_JS + remaining if first.startswith(b"#!") else MANUAL_BODY_JS + contents
        if RUNTIME_JS not in contents:
            if RUNTIME_BEGIN in contents or RUNTIME_END in contents:
                if contents.count(RUNTIME_BEGIN) != 1 or contents.count(RUNTIME_END) != 1:
                    raise FleetMemoryError("Claude-mem runtime patch is incomplete.")
                start = contents.index(RUNTIME_BEGIN)
                end = contents.index(RUNTIME_END) + len(RUNTIME_END)
                if end <= start:
                    raise FleetMemoryError("Claude-mem runtime patch is incomplete.")
                contents = contents[:start] + RUNTIME_JS + contents[end:]
            else:
                first, separator, remaining = contents.partition(b"\n")
                contents = first + separator + RUNTIME_JS + b"\n" + remaining if first.startswith(b"#!") else RUNTIME_JS + b"\n" + contents
        if guard not in contents:
            first, separator, remaining = contents.partition(b"\n")
            contents = first + separator + guard + remaining if first.startswith(b"#!") else guard + contents
        if WORKER_DRAIN not in contents:
            contents += b"\n" + WORKER_DRAIN
        updates[candidate] = contents
        mcp = candidate.with_name("mcp-server.cjs")
        contents = safe_regular(mcp).read_bytes()
        if contents.count(MANUAL_MCP_PATCH) != 1:
            if contents.count(MANUAL_MCP_ENTRY) != 1 or MANUAL_MCP_PATCH in contents:
                raise FleetMemoryError("Claude-mem MCP does not match the reviewed bundle.")
            contents = contents.replace(MANUAL_MCP_ENTRY, MANUAL_MCP_PATCH)
        updates[mcp] = contents
        skills = candidate.parent.parent / "skills"
        learning = skills / "learn-codebase/SKILL.md"
        if learning.exists():
            header, separator, body = safe_regular(learning).read_bytes().partition(b"\n---\n")
            if not header.startswith(b"---\nname: learn-codebase\n") or not separator:
                raise FleetMemoryError("Claude-mem learning skill does not match the reviewed bundle.")
            updates[learning] = (
                b'---\nname: learn-codebase\ndescription: Read every source file in full only when the user explicitly invokes learn-codebase.\n'
                b'disable-model-invocation: true\n---\n' + body
            )
            policy = learning.parent / "agents/openai.yaml"
            contents = b"policy:\n  allow_implicit_invocation: false\n"
            if policy.exists() and safe_regular(policy).read_bytes() != contents:
                raise FleetMemoryError("Claude-mem learning skill has an unmanaged invocation policy.")
            updates[policy] = contents
        babysit = skills / "babysit/SKILL.md"
        if babysit.exists():
            contents = safe_regular(babysit).read_bytes()
            retired = babysit.with_name("RETIRED.md")
            if retired.exists() and safe_regular(retired).read_bytes() != contents:
                raise FleetMemoryError("Claude-mem retired babysit recovery file differs from the active skill.")
            updates[retired] = contents
            retired_skills[babysit] = retired
    for candidate, contents in updates.items():
        if not candidate.exists() or contents != candidate.read_bytes():
            atomic_bytes(candidate, contents, stat.S_IMODE(os.stat(candidate).st_mode) if candidate.exists() else 0o600)
    for candidate, retired in retired_skills.items():
        if safe_regular(candidate).read_bytes() != safe_regular(retired).read_bytes():
            raise FleetMemoryError("Claude-mem babysit recovery verification failed.")
        candidate.unlink()


def client_worker_guard(config):
    return (
        'if (process.argv.includes("--daemon") && '
        f'{json.dumps(config.client_hosts)}.includes(require("os").hostname().split(".")[0])) process.exit(0);\n'
    ).encode()


def plugin_hardened(home, config):
    try:
        script = resolve_plugin(home, config.version)
        contents = safe_regular(script).read_bytes()
        for candidate in (script, Path(home) / ".codex/plugins/cache/claude-mem-local/claude-mem" / config.version / "scripts/worker-service.cjs"):
            if not candidate.exists():
                continue
            worker = contents if candidate == script else safe_regular(candidate).read_bytes()
            if not (
                all(worker.count(fragment) == 1 for fragment in (MANUAL_WORKER_ROUTES, MANUAL_BODY_JS, MANUAL_SAVE_SYNC_PATCH))
                and RUNTIME_JS in worker
                and all(worker.count(fragment) == 1 for fragment in RUNTIME_PATCHES.values())
                and safe_regular(candidate.with_name("mcp-server.cjs")).read_bytes().count(MANUAL_MCP_PATCH) == 1
            ):
                return False
    except (FleetMemoryError, OSError):
        return False
    return (
        contents.count(HARDENED_WORKER) == 1
        and VULNERABLE_WORKER not in contents
        and contents.count(PINNED_WORKER_HEALTH_VERSION) == 1
        and STALE_WORKER_HEALTH_VERSION not in contents
        and OBSERVATION_ENQUEUE in contents
        and CODEX_TOOL_ID_CAPTURE in contents
        and SUMMARY_ENQUEUE in contents
        and PROMPT_ORDER in contents
        and WORKER_DRAIN in contents
    )


def configure_client(home, config, auth_method="subscription"):
    home = Path(home)
    settings_path = home / ".claude-mem/settings.json"
    settings = read_json(settings_path) if settings_path.exists() else {}
    skipped = settings.get("CLAUDE_MEM_SKIP_TOOLS", "ListMcpResourcesTool,SlashCommand,Skill,TodoWrite,AskUserQuestion")
    if not isinstance(skipped, str):
        raise FleetMemoryError("Invalid Claude-mem tool exclusions.")
    skipped = [name.strip() for name in skipped.split(",") if name.strip()]
    settings.update(
        {
            "CLAUDE_MEM_SKIP_TOOLS": ",".join(dict.fromkeys([*skipped, *MEMORY_SKIP_TOOLS])),
            "CLAUDE_MEM_RUNTIME": "worker",
            "CLAUDE_MEM_WORKER_HOST": config.worker_host,
            "CLAUDE_MEM_WORKER_PORT": str(config.worker_port),
            "CLAUDE_MEM_PROVIDER": "claude",
            "CLAUDE_MEM_CLAUDE_AUTH_METHOD": auth_method,
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
            "SoftResourceLimits": {"NumberOfFiles": 4096},
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


def memory_role(config, host, platform):
    if host == config.server_host and platform == "linux":
        return "server"
    if host in config.client_hosts and platform in {"darwin", "linux"}:
        return "client"
    raise FleetMemoryError("Fleet memory host role does not match this platform.")


def render_linux_client_service():
    return (
        "[Unit]\n"
        "Description=Connect to shared Fleet Claude-mem worker\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%h/.local/bin/fleet memory proxy\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def old_server_active(root, home, native_runner):
    directory = Path(home) / ".config/systemd/user"
    paths = [directory / name for name in LINUX_UNITS]
    captured = capture_files(paths)
    if all(value is None for value in captured.values()):
        return False
    sources = linux_sources(root)
    if any(captured[path] is None or captured[path][0] != sources[path.name] for path in paths):
        raise FleetMemoryError("Existing Fleet memory server units do not match this release.")
    active = []
    for unit in ("fleet-memory.service", "fleet-memory-backup.timer"):
        result = run_native(["/usr/bin/systemctl", "--user", "is-active", unit], home, "linux", native_runner)
        active.append(result.returncode == 0 and result.stdout.strip() == "active")
    if any(active) and not all(active):
        raise FleetMemoryError("Existing Fleet memory server units have mixed state.")
    return all(active)


def enable_memory(root, home, host, *, platform_name=None, uid=None, native_runner=None):
    root = Path(root)
    home = Path(home)
    config = load_config(root)
    platform = ("darwin" if sys.platform == "darwin" else "linux") if platform_name is None else platform_name
    if platform not in {"darwin", "linux"}:
        raise FleetMemoryError("Fleet memory supports only macOS and Linux.")
    role = memory_role(config, host, platform)
    harden_plugin(home, config)
    settings = home / ".claude-mem/settings.json"
    marketplaces = home / ".claude/plugins/known_marketplaces.json"
    gateway = None
    gateway_contents = None
    if role == "server":
        client_key = safe_regular(home / ".local/state/fleet/cliproxyapi-client-key").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", client_key):
            raise FleetMemoryError("Invalid Fleet memory gateway key.")
        gateway = home / ".claude-mem/.env"
        gateway_contents = (
            f"ANTHROPIC_BASE_URL={MEMORY_GATEWAY_URL}\n"
            f"ANTHROPIC_AUTH_TOKEN={client_key}\n"
        ).encode()
        unit_directory = home / ".config/systemd/user"
        sources = linux_sources(root)
        managed = {unit_directory / name: contents for name, contents in sources.items()}
    elif platform == "linux":
        managed = {
            home / ".config/systemd/user/fleet-memory-client.service": render_linux_client_service()
        }
    else:
        managed = {home / "Library/LaunchAgents/dev.agents-fleet.memory.plist": render_macos_plist(home)}
    previous = capture_files([settings, marketplaces, *managed, *([gateway] if gateway else [])])
    if gateway and previous[gateway] is not None and previous[gateway][0] != gateway_contents:
        raise FleetMemoryError(f"Fleet memory gateway is not owned by Fleet: {gateway}")
    started = False
    old_server_was_active = False
    try:
        if role == "client" and platform == "linux":
            old_server_was_active = old_server_active(root, home, native_runner)
            if old_server_was_active:
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
                    "stop old server",
                )
        configure_client(home, config, "gateway" if gateway else "subscription")
        if gateway:
            atomic_bytes(gateway, gateway_contents, 0o600)
        for path, contents in managed.items():
            if previous[path] is not None and previous[path][0] != contents:
                raise FleetMemoryError(f"Fleet memory target is not owned by Fleet: {path}")
            atomic_bytes(path, contents, 0o644)
        if role == "server":
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
        elif platform == "linux":
            require_success(run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner), "reload")
            require_success(
                run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-client.service"], home, platform, native_runner),
                "enable guard",
            )
            started = True
            result = run_native(["/usr/bin/systemctl", "--user", "is-active", "fleet-memory-client.service"], home, platform, native_runner)
            if result.returncode or result.stdout.strip() != "active":
                raise FleetMemoryError("Fleet memory unit is not active: fleet-memory-client.service")
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
        if role == "server" and started:
            run_native(
                ["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-memory-backup.timer", "fleet-memory.service"],
                home,
                platform,
                native_runner,
            )
            run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner)
        elif platform == "linux" and started:
            run_native(
                ["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-memory-client.service"],
                home,
                platform,
                native_runner,
            )
            run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner)
        elif platform == "darwin" and started:
            run_native(["/bin/launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], home, platform, native_runner)
        restore_files(previous)
        if old_server_was_active:
            run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory.service"], home, platform, native_runner)
            run_native(["/usr/bin/systemctl", "--user", "enable", "--now", "fleet-memory-backup.timer"], home, platform, native_runner)
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
    harden_plugin(home, config)
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
        "CLAUDE_MEM_PROVIDER": "claude",
        "CLAUDE_MEM_CLAUDE_AUTH_METHOD": "gateway",
    }
    executable = str(bun)
    executor(executable, [executable, str(script), "--daemon"], environment)


def default_process_factory(command):
    return subprocess.Popen(command, stdin=subprocess.DEVNULL)


def observation_queue_path(home, *, create=False):
    path = Path(home)
    for part in (".local", "state", "fleet", "memory", "outbox"):
        path /= part
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        if not os.path.lexists(path):
            return Path(home) / ".local/state/fleet/memory/outbox/observations.db"
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o022:
            raise FleetMemoryError("Unsafe Fleet memory queue directory.")
        if part == "outbox" and stat.S_IMODE(details.st_mode) != 0o700:
            raise FleetMemoryError("Fleet memory queue directory must be private.")
    path /= "observations.db"
    if create and not os.path.lexists(path):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        db.fsync_path(path.parent)
    for candidate in (path, path.with_suffix(".lock"), path.with_suffix(".delivery.lock"), Path(str(path) + "-journal")):
        if not os.path.lexists(candidate):
            continue
        details = candidate.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600:
            raise FleetMemoryError("Unsafe Fleet memory queue file.")
    return path


def private_filtered(value):
    encoded = json.dumps(value)
    for _ in range(100):
        filtered = MEMORY_PRIVATE.sub("", encoded)
        if filtered == encoded:
            try:
                return json.loads(filtered)
            except ValueError:
                return ""
        encoded = filtered
    return ""


def manual_memory(config, command, payload):
    allowed = {"text", "title", "metadata", "id" if command == "update" else "project"}
    for filtered in (False, True):
        if filtered:
            payload = private_filtered(payload)
        if (
            not isinstance(payload, dict) or set(payload) - allowed
            or not isinstance(payload.get("text"), str) or not payload["text"].strip()
            or any(key in payload and not isinstance(payload[key], str) for key in ("title", "project"))
            or "metadata" in payload and not isinstance(payload["metadata"], dict)
            or command == "update" and (type(payload.get("id")) is not int or not 0 < payload["id"] <= 2**53 - 1)
        ):
            raise FleetMemoryError("Invalid Fleet memory payload.")
    payload["text"] = payload["text"].strip()
    try:
        encoded = json.dumps(payload, allow_nan=False).encode()
    except (UnicodeError, ValueError, TypeError) as error:
        raise FleetMemoryError("Invalid Fleet memory payload.") from error
    if len(encoded) > MAX_OBSERVATION_BYTES:
        raise FleetMemoryError("Fleet memory payload exceeds the size limit.")
    try:
        with closing(http.client.HTTPConnection("127.0.0.1", config.worker_port, timeout=2)) as client:
            client.request("POST", f"/api/memory/{command}", encoded, {"Content-Type": "application/json"})
            response = client.getresponse()
            if response.status != 200:
                if command == "update" and response.status == 404:
                    raise FleetMemoryError("Fleet memory update failed (HTTP 404): memory ID was not found or update support is not loaded. Keep the running worker until its queue is safe.")
                raise FleetMemoryError(f"Fleet memory {command} failed (HTTP {response.status}); check the ID and worker hardening.")
            result = json.loads(response.read(65537))
        if (
            not isinstance(result, dict) or result.get("success") is not True
            or type(result.get("id")) is not int or result["id"] <= 0
            or command == "update" and result["id"] != payload["id"]
        ):
            raise ValueError("unacknowledged")
        return result
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise FleetMemoryError(f"Fleet memory {command} was not acknowledged; it may have been saved, search before retrying.") from error


def enqueue_observation(home, payload):
    if not isinstance(payload, dict) or payload.get("platformSource") != "codex" or any(
        not isinstance(payload.get(key), str) or not payload[key].strip()
        for key in ("contentSessionId",)
    ):
        raise FleetMemoryError("Invalid Fleet memory observation.")
    kind = payload.get("queue_kind", "observations")
    keys = {"observations": ("tool_name", "cwd"), "summarize": ("last_assistant_message",), "init": ("project", "prompt")}.get(kind)
    if keys is None or any(
        not isinstance(payload.get(key), str) or not payload[key].strip() for key in keys
    ):
        raise FleetMemoryError("Invalid Fleet memory observation.")
    payload = {private_filtered(key): private_filtered(value) for key, value in payload.items()}
    if kind == "init" and not payload["prompt"].strip():
        payload["prompt"] = "<private></private>"
    tool_id = payload.get("tool_use_id")
    if tool_id is not None and (not isinstance(tool_id, str) or not tool_id):
        raise FleetMemoryError("Invalid Fleet memory tool ID.")
    payload["tool_use_id"] = tool_id or str(uuid.uuid4())
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_OBSERVATION_BYTES:
        raise FleetMemoryError("Fleet memory observation exceeds the queue size limit.")
    identity = hashlib.sha256(json.dumps([payload["contentSessionId"], kind, payload["tool_use_id"]]).encode()).hexdigest()
    try:
        path = observation_queue_path(home, create=True)
        with closing(sqlite3.connect(path, timeout=0.2)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, payload TEXT, error TEXT, result TEXT)")
            connection.execute("INSERT OR IGNORE INTO observations (id, payload, error) VALUES (?, ?, NULL)", (identity, encoded))
    except (OSError, sqlite3.Error) as error:
        raise FleetMemoryError("Could not save Fleet memory observation; retry the hook.") from error
    return identity


def observation_queue_status(home):
    path = observation_queue_path(home)
    if not path.exists():
        return 0, 0
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            return connection.execute(
                "SELECT count(*), count(error) FROM observations WHERE payload IS NOT NULL"
            ).fetchone()
    except sqlite3.Error as error:
        raise FleetMemoryError("Could not read Fleet memory queue.") from error


def drain_observations(home, config):
    path = observation_queue_path(home)
    if not path.exists():
        return 0
    delivered = 0
    deadline = time.monotonic() + 5
    try:
        descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as lock, closing(sqlite3.connect(path, timeout=0.2)) as connection:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            rows = connection.execute("SELECT id, payload FROM observations WHERE payload IS NOT NULL ORDER BY rowid LIMIT 100").fetchall()
            for identity, payload in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    body = json.loads(payload)
                    kind = body.pop("queue_kind", "observations")
                    with closing(http.client.HTTPConnection("127.0.0.1", config.worker_port, timeout=2)) as client:
                        client.request("POST", f"/api/sessions/{kind}", json.dumps(body).encode(), {"Content-Type": "application/json"})
                        response = client.getresponse()
                        if response.status != 200:
                            raise ValueError(f"HTTP {response.status}")
                        result = json.loads(response.read(65537))
                    if not isinstance(result, dict) or not (
                        kind == "init" and type(result.get("sessionDbId")) is int
                        or kind != "init" and result.get("status") in {"queued", "skipped"}
                    ):
                        raise ValueError("unacknowledged")
                except (OSError, http.client.HTTPException, ValueError) as error:
                    with connection:
                        connection.execute("UPDATE observations SET error=? WHERE id=?", (type(error).__name__, identity))
                    break
                with connection:
                    receipt = json.dumps({key: result[key] for key in ("sessionDbId", "promptNumber", "skipped", "reason") if key in result}) if kind == "init" else None
                    connection.execute("UPDATE observations SET payload=NULL, error=NULL, result=? WHERE id=?", (receipt, identity))
                delivered += 1
    except (OSError, sqlite3.Error) as error:
        raise FleetMemoryError("Could not drain Fleet memory queue.") from error
    return delivered


def deliver_observations(home, config):
    path = observation_queue_path(home)
    if not path.exists():
        return
    descriptor = os.open(path.with_suffix(".delivery.lock"), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            drain_observations(home, config)
            with closing(sqlite3.connect(path, timeout=0.2)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT count(*) FROM observations WHERE payload IS NOT NULL").fetchone()[0] == 0:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    return
            time.sleep(5)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_proxy(config, *, home=None, stop=None, process_factory=default_process_factory):
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

    def keep_queue():
        while not stop.is_set():
            try:
                drain_observations(home, config)
            except FleetMemoryError:
                print("Fleet memory queue drain failed; run fleet memory status", file=sys.stderr)
            stop.wait(5)

    drainer = threading.Thread(target=keep_queue, name="fleet-memory-queue", daemon=True) if home is not None else None
    if drainer is not None:
        drainer.start()
    try:
        serve_guard(config.worker_port, config.tunnel_port, stop)
    finally:
        stop.set()
        if drainer is not None:
            drainer.join(7)
        keeper.join(7)
        if keeper.is_alive():
            stop_process(process_holder["process"])
            keeper.join(2)
        if own_stop:
            import signal
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def compress_file(path):
    path = safe_regular(path)
    original = path.stat()
    destination = path.with_name(path.name + ".gz")
    descriptor, temporary = tempfile.mkstemp(prefix=".fleet-memory-gzip-", dir=path.parent)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            with path.open("rb") as source, gzip.GzipFile(filename="", fileobj=output, mode="wb", compresslevel=1, mtime=0) as archive:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    archive.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        with gzip.open(temporary, "rb") as restored:
            if hashlib.file_digest(restored, "sha256").digest() != digest.digest():
                raise FleetMemoryError("Memory archive verification failed; original retained.")
        current = safe_regular(path).stat()
        if (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns) != (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns
        ):
            raise FleetMemoryError("Memory file changed during compression; original retained.")
        if os.stat(temporary).st_size >= original.st_size:
            return path
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.link(temporary, destination)
        db.fsync_path(path.parent)
        path.unlink()
        db.fsync_path(path.parent)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def backup_memory(home, config, *, now=None):
    home = Path(home)
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    directory = home / ".local/state/fleet/memory/backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = directory / f"daily-{now.strftime('%Y%m%dT%H%M%SZ')}.db"
    report = db.snapshot_database(home / ".claude-mem/claude-mem.db", destination)
    destination = compress_file(destination)
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
        and re.fullmatch(r"daily-[0-9]{8}T[0-9]{6}Z\.db(?:\.gz)?", path.name)
    )
    return backups[-1] if backups else None


def worker_health(port, endpoint="health"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/{endpoint}", timeout=2) as response:
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
    return plugin_hardened(home, config)


def observer_ok(home):
    path = Path(home) / ".claude-mem/observer-health.json"
    if not path.exists():
        return True
    try:
        failures = read_json(path).get("consecutiveFailures")
    except FleetMemoryError:
        return False
    return type(failures) is int and failures == 0


def server_database_status(home):
    try:
        database_report = db.check_database(Path(home) / ".claude-mem/claude-mem.db", timeout=3, integrity=False)
        database_ok = not database_report.foreign_key_errors and database_report.schema_version == 50
        queue_ok = database_report.counts["pending"] == 0 and database_report.counts["processing"] == 0
        return database_ok, queue_ok
    except (KeyError, db.FleetMemoryDatabaseError):
        return False, False


def server_status(home, config, plugin_ok, worker_ok):
    service = run_native(["/usr/bin/systemctl", "--user", "is-active", "fleet-memory.service"], home, "linux")
    service_ok = service.returncode == 0 and service.stdout.strip() == "active"
    database_ok, queue_ok = server_database_status(home)
    observer_healthy = observer_ok(home)
    backup = latest_backup(home)
    backup_details = os.stat(backup) if backup is not None else None
    backup_ok = backup_details is not None and stat.S_IMODE(backup_details.st_mode) == 0o600 and 0 <= time.time() - backup_details.st_mtime <= 2 * 86400
    lines = [
        f"service: {'active' if service_ok else 'error'}",
        f"database: {'readable' if database_ok else 'error'}",
        f"queue: {'empty' if queue_ok else 'error'}",
        f"observer: {'ok' if observer_healthy else 'error'}",
        f"backup: {backup.name if backup_ok else 'error'}",
    ]
    return lines, all((plugin_ok, worker_ok, service_ok, database_ok, queue_ok, observer_healthy, backup_ok))


def client_status(home, config, plugin_ok, worker_ok, platform):
    command = (
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{MAC_LABEL}"]
        if platform == "darwin"
        else ["/usr/bin/systemctl", "--user", "is-active", "fleet-memory-client.service"]
    )
    service = run_native(command, home, platform)
    service_ok = service.returncode == 0 and (
        "state = running" in service.stdout if platform == "darwin" else service.stdout.strip() == "active"
    )
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
    role = memory_role(config, host, platform)
    plugin_ok = pinned_plugin_ok(home, config)
    health_payload = worker_health(config.worker_port)
    reported_version = health_payload.get("version") if isinstance(health_payload, dict) else None
    worker_ok = reported_version == config.version or reported_version in WORKER_VERSION_ALIASES.get(config.version, set())
    lines = [f"role: {role}", f"pinned: {config.version}", f"plugin: {'ok' if plugin_ok else 'error'}"]
    lines.append(f"worker: {reported_version if worker_ok else 'error'}")
    role_lines, ok = (
        server_status(home, config, plugin_ok, worker_ok)
        if role == "server"
        else client_status(home, config, plugin_ok, worker_ok, platform)
    )
    processing = worker_health(config.worker_port, "processing-status")
    depth = processing.get("queueDepth") if isinstance(processing, dict) else None
    depth_ok = type(depth) is int and depth >= 0
    lines.append(f"worker queue: {depth} pending" if depth_ok else "worker queue: unknown")
    ok = ok and depth_ok and depth == 0
    try:
        pending, failed = observation_queue_status(home)
        lines.append(f"local queue: {pending} pending, {failed} failed")
        ok = ok and pending == 0
    except FleetMemoryError:
        lines.append("local queue: error")
        ok = False
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
    role = memory_role(config, host, platform)
    if role == "server":
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
    if platform == "linux":
        require_success(
            run_native(
                ["/usr/bin/systemctl", "--user", "disable", "--now", "fleet-memory-client.service"],
                home,
                platform,
                native_runner,
            ),
            "disable guard",
        )
        require_success(run_native(["/usr/bin/systemctl", "--user", "daemon-reload"], home, platform, native_runner), "reload")
        return
    uid = os.getuid() if uid is None else uid
    result = run_native(["/bin/launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], home, platform, native_runner)
    if result.returncode not in {0, 113}:
        raise FleetMemoryError("Could not disable Fleet memory guard.")


USAGE = "Usage: fleet memory status | backup | enable | disable | harden | enqueue | init | drain | deliver | save | update (save/update read JSON from stdin)"


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 2 or arguments[1] not in {"status", "backup", "enable", "disable", "harden", "proxy", "worker", "enqueue", "init", "drain", "deliver", "save", "update"}:
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
    if command in {"save", "update"}:
        try:
            raw = sys.stdin.buffer.read(MAX_OBSERVATION_BYTES + 1)
            if len(raw) > MAX_OBSERVATION_BYTES:
                raise FleetMemoryError("Fleet memory payload exceeds the size limit.")
            payload = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise FleetMemoryError("Invalid Fleet memory JSON.") from error
        print(json.dumps(manual_memory(config, command, payload)))
        return 0
    if command in {"enqueue", "init"}:
        try:
            raw = sys.stdin.buffer.read(MAX_OBSERVATION_BYTES + 1)
            if len(raw) > MAX_OBSERVATION_BYTES:
                raise FleetMemoryError("Fleet memory observation exceeds the queue size limit.")
            payload = json.loads(raw)
            if command == "init":
                if not isinstance(payload, dict):
                    raise FleetMemoryError("Invalid Fleet memory prompt.")
                payload["queue_kind"] = "init"
            identity = enqueue_observation(home, payload)
        except (UnicodeError, ValueError) as error:
            raise FleetMemoryError("Invalid Fleet memory observation JSON.") from error
        if command == "init":
            drain_observations(home, config)
            path = observation_queue_path(home)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
                result = connection.execute("SELECT result FROM observations WHERE id=?", (identity,)).fetchone()[0]
            print(result or '{"status":"pending"}')
        if command == "enqueue" or not result:
            try:
                subprocess.Popen([str(home / ".local/bin/fleet"), "memory", "deliver"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                print("Fleet memory queue saved; delivery will retry with the next hook.", file=sys.stderr)
        return 0
    if command == "deliver":
        deliver_observations(home, config)
        return 0
    if command == "drain":
        drain_observations(home, config)
        return 0 if observation_queue_status(home)[0] == 0 else 1
    if command == "proxy":
        run_proxy(config, home=home)
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
    if command == "harden":
        harden_plugin(home, config)
        print("plugin: hardened")
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
