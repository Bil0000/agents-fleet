#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_apply", Path(__file__).with_name("fleet_apply.py"))
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)
FleetError = fleet.FleetError

STATE_VERSION = 1
SCAN_INTERVAL = 3600


@dataclass(frozen=True)
class AdoptionRequest:
    root: Path
    home: Path
    host: str
    kind: str
    name: str
    scope: str


def fail(message):
    raise FleetError(message)


def item_id(item):
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def discovery_path(home):
    return fleet.safe_target(Path(home), ".local/state/fleet/discoveries.json")


def valid_item(item):
    if not isinstance(item, dict) or item.get("kind") not in {"skill", "hook", "plugin", "mcp", "drift"}:
        return False
    if not isinstance(item.get("name"), str):
        return False
    allowed = {
        "skill": {"kind", "name", "client", "path"},
        "hook": {"kind", "name", "client", "path"},
        "plugin": {"kind", "name", "client", "marketplace", "source"},
        "mcp": {"kind", "name", "client"},
        "drift": {"kind", "name"},
    }[item["kind"]]
    if set(item) != allowed:
        return False
    if "client" in item and item["client"] not in {"agents", "codex", "claude"}:
        return False
    if "path" in item and (not isinstance(item["path"], str) or not fleet.safe_relative_parts(item["path"])):
        return False
    if item["kind"] == "plugin":
        if not fleet.PLUGIN_ID.fullmatch(item["name"]):
            return False
        if not fleet.SAFE_NAME.fullmatch(item["marketplace"]):
            return False
        source = item["source"]
        if not isinstance(source, str) or not (source.startswith("github:") or source.startswith("$HOME/")):
            return False
        if item["client"] not in {"codex", "claude"}:
            return False
    if item["kind"] == "mcp" and not fleet.mcp.SERVER.fullmatch(item["name"]):
        return False
    if item["kind"] == "mcp" and item["client"] not in {"codex", "claude"}:
        return False
    if item["kind"] not in {"plugin", "mcp"} and not fleet.SAFE_NAME.fullmatch(item["name"]):
        return False
    return True


def load_state(home):
    path = discovery_path(home)
    try:
        details = os.lstat(path)
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("Invalid Fleet discovery state.")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
        fail("Invalid Fleet discovery state.")
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "host", "scanned_at", "known", "discoveries"}
        or value["version"] != STATE_VERSION
        or not isinstance(value["host"], str)
        or type(value["scanned_at"]) is not int
        or not isinstance(value["known"], list)
        or any(not isinstance(entry, str) or not valid_item_id(entry) for entry in value["known"])
        or value["known"] != sorted(set(value["known"]))
        or not isinstance(value["discoveries"], list)
        or any(not valid_item(item) for item in value["discoveries"])
    ):
        fail("Invalid Fleet discovery state.")
    return value


def valid_item_id(serialized_item):
    try:
        parsed_item = json.loads(serialized_item)
    except json.JSONDecodeError:
        return False
    return valid_item(parsed_item) and item_id(parsed_item) == serialized_item


def write_state(home, state):
    fleet.atomic_write(
        Path(home),
        discovery_path(home),
        (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )


def managed_names(root, host):
    shared_skills = {path.name for path in (root / "skills").iterdir() if path.is_dir()}
    host_skills = root / "hosts" / host / "skills"
    if host_skills.is_dir():
        shared_skills.update(path.name for path in host_skills.iterdir() if path.is_dir())
    hooks = {path.name for path in (root / "hooks").glob("*.sh")}
    host_hooks = root / "hosts" / host / "hooks"
    if host_hooks.is_dir():
        hooks.update(path.name for path in host_hooks.glob("*.sh"))
    plugins = fleet.load_plugin_manifest(root, host)
    plugin_names = {client: set(plugins[client]["plugins"]) for client in ("codex", "claude")}
    mcp_names = set()
    for directory in (root / "mcp", root / "hosts" / host / "mcp"):
        codex = directory / "codex.toml"
        if codex.is_file():
            mcp_names.update(tomllib.loads(codex.read_text()).get("mcp_servers", {}))
    return shared_skills, hooks, plugin_names, mcp_names


def safe_plugin_source(source, home):
    source = fleet.canonical_plugin_source(source, home)
    if source.startswith("github:"):
        value = source[7:]
        return source if fleet.GITHUB_SOURCE.fullmatch(value) else None
    try:
        relative = Path(source).resolve().relative_to(Path(home).resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return f"$HOME/{relative.as_posix()}" if fleet.safe_relative_parts(relative) else None


def installed_plugin_items(root, home, host):
    _, _, managed, _ = managed_names(root, host)
    output = []
    for client in ("codex", "claude"):
        try:
            binary = fleet.plugin_client(client, home)
            marketplaces = fleet.plugin_marketplaces(client, binary, home, fleet.run_plugin_command)
            installed = fleet.installed_plugins(client, binary, home, fleet.run_plugin_command)
        except FleetError:
            continue
        for name, enabled in installed.items():
            if not enabled or name in managed[client] or not fleet.PLUGIN_ID.fullmatch(name):
                continue
            marketplace = name.rsplit("@", 1)[1]
            source = safe_plugin_source(marketplaces.get(marketplace, ""), home)
            if source:
                output.append(
                    {"kind": "plugin", "name": name, "client": client, "marketplace": marketplace, "source": source}
                )
    return output


def regular_children(home, relative, suffix=None):
    directory = fleet.safe_target(home, relative)
    try:
        details = os.lstat(directory)
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        return []
    output = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        try:
            mode = os.lstat(child).st_mode
        except OSError:
            continue
        wanted = stat.S_ISREG(mode) if suffix else stat.S_ISDIR(mode)
        if wanted and not stat.S_ISLNK(mode) and fleet.SAFE_NAME.fullmatch(child.name) and (suffix is None or child.suffix == suffix):
            output.append(child)
    return output


def client_mcp_servers(home, client):
    relative, key = (".codex/config.toml", "mcp_servers") if client == "codex" else (".claude.json", "mcpServers")
    path = fleet.safe_target(home, relative)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        fail("Fleet could not scan client MCP configuration.")
    try:
        client_config = tomllib.loads(path.read_text()) if client == "codex" else json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        fail("Fleet could not scan client MCP configuration.")
    servers = client_config.get(key, {}) if isinstance(client_config, dict) else None
    if not isinstance(servers, dict):
        fail("Fleet could not scan client MCP configuration.")
    return servers


def live_mcp_items(root, home, managed):
    output = []
    for client in ("codex", "claude"):
        servers = client_mcp_servers(home, client)
        for name in sorted(servers):
            if name not in managed and fleet.mcp.SERVER.fullmatch(name):
                output.append({"kind": "mcp", "name": name, "client": client})
    return output


def has_managed_drift(home):
    path = fleet.safe_target(home, ".local/state/fleet/last-applied.json")
    try:
        state = json.loads(path.read_text())
        roots = state["roots"]
        fingerprints = state["fingerprints"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    try:
        return not isinstance(roots, list) or not roots or fleet.live_fingerprints(home, roots, state.get("managed_mcp")) != fingerprints
    except FleetError:
        return True


def inventory(root, home, host, plugin_provider=None):
    root, home = Path(root), Path(home)
    skills, hooks, _, mcp_names = managed_names(root, host)
    output = []
    for client, relative in (("agents", ".agents/skills"), ("codex", ".codex/skills"), ("claude", ".claude/skills")):
        for path in regular_children(home, relative):
            if path.name not in skills:
                output.append({"kind": "skill", "name": path.name, "client": client, "path": str(path.relative_to(home))})
    for client, relative in (("codex", ".codex/hooks"), ("claude", ".claude/hooks")):
        for path in regular_children(home, relative, ".sh"):
            if path.name not in hooks:
                output.append({"kind": "hook", "name": path.name, "client": client, "path": str(path.relative_to(home))})
    provider = installed_plugin_items if plugin_provider is None else plugin_provider
    for item in provider(root, home, host):
        if valid_item(item):
            output.append(item)
    output.extend(live_mcp_items(root, home, mcp_names))
    if has_managed_drift(home):
        output.append({"kind": "drift", "name": "managed-files"})
    unique = {item_id(item): item for item in output}
    return [unique[key] for key in sorted(unique)]


def scan(root, home, host, plugin_provider=None):
    root, home = Path(root).resolve(), Path(home)
    fleet.host_id(root, host)
    current = inventory(root, home, host, plugin_provider)
    state = load_state(home)
    if state is None or state["host"] != host:
        known = sorted(item_id(item) for item in current if item["kind"] != "drift")
        discoveries = [item for item in current if item["kind"] == "drift"]
    else:
        known = [entry for entry in state["known"] if json.loads(entry)["kind"] != "drift"]
        discoveries = [item for item in current if item_id(item) not in set(known)]
    value = {
        "version": STATE_VERSION,
        "host": host,
        "scanned_at": int(time.time()),
        "known": known,
        "discoveries": discoveries,
    }
    write_state(home, value)
    return discoveries


def scan_if_due(root, home, host, plugin_provider=None):
    state = load_state(home)
    stale_drift = state is not None and any(item["kind"] == "drift" for item in state["discoveries"])
    due = state is None or state["host"] != host or stale_drift or int(time.time()) - state["scanned_at"] >= SCAN_INTERVAL
    drift = has_managed_drift(home)
    items = scan(root, home, host, plugin_provider) if due or drift else state["discoveries"]
    return items, drift


def git_run(cwd, *arguments, check=True):
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fleet.fixed_home()), "LANG": "C", "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        fail("Fleet Git operation failed.")
    return result


def safe_tree(path):
    total = 0
    for node in [path, *path.rglob("*")]:
        mode = os.lstat(node).st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            fail("Adopted files must be regular files and directories.")
        if stat.S_ISREG(mode):
            total += node.stat().st_size
            if total > 64 * 1024 * 1024:
                fail("Adopted files are too large.")


def tree_digest(path):
    digest = hashlib.sha256()
    for node in [path, *sorted(path.rglob("*"))]:
        relative = node.relative_to(path).as_posix()
        mode = os.lstat(node).st_mode
        digest.update(relative.encode())
        digest.update(b"d" if stat.S_ISDIR(mode) else b"f")
        if stat.S_ISREG(mode):
            digest.update(node.read_bytes())
    return digest.hexdigest()


def discovered_paths(home, discoveries):
    paths = []
    for discovery in discoveries:
        path = fleet.safe_target(home, discovery["path"], allow_missing=False)
        details = os.lstat(path)
        if stat.S_ISLNK(details.st_mode):
            fail("Discovered item is no longer safe.")
        paths.append(path)
    return paths


def selected_skill_source(home, discoveries):
    paths = discovered_paths(home, discoveries)
    for path in paths:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            fail("Discovered skill is no longer safe.")
        safe_tree(path)
    signatures = {tree_digest(path) for path in paths}
    if len(signatures) != 1:
        fail("Discovered item is ambiguous across clients.")
    return paths[0]


def selected_hook_source(home, discoveries):
    paths = discovered_paths(home, discoveries)
    for path in paths:
        if not stat.S_ISREG(os.lstat(path).st_mode) or path.stat().st_size > 1024 * 1024:
            fail("Discovered hook is no longer safe.")
    signatures = {hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    if len(signatures) != 1:
        fail("Discovered item is ambiguous across clients.")
    return paths[0]


def empty_plugin_manifest():
    return {client: {"marketplaces": {}, "plugins": []} for client in ("codex", "claude")}


def adopt_plugin(candidate, request, discoveries):
    path = candidate / ("plugins.json" if request.scope == "shared" else f"hosts/{request.host}/plugins.json")
    manifest = json.loads(path.read_text()) if path.exists() else empty_plugin_manifest()
    for item in discoveries:
        source = item["source"]
        if source.startswith("github:"):
            source = source[7:]
        elif request.scope == "shared":
            fail("A local plugin marketplace can only be host-specific.")
        current = manifest[item["client"]]["marketplaces"].get(item["marketplace"])
        if current is not None and current != source:
            fail("Plugin marketplace source conflicts with Fleet.")
        manifest[item["client"]]["marketplaces"][item["marketplace"]] = source
        manifest[item["client"]]["plugins"] = sorted(set(manifest[item["client"]]["plugins"]) | {item["name"]})
    fleet.validate_plugin_manifest(manifest, path.relative_to(candidate), allow_local=request.scope == "host")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def live_mcp_definition(home, name):
    definitions = []
    for client in ("codex", "claude"):
        definition = client_mcp_servers(home, client).get(name)
        if definition is not None:
            definitions.append(definition)
    normalized = []
    for value in definitions:
        if not isinstance(value, dict):
            fail("MCP adoption needs a secret-free command or URL.")
        try:
            fleet.mcp.reject_static_credentials(value)
            fleet.mcp.reject_unsafe_urls(value)
            fleet.mcp.reject_credential_args(value)
        except FleetError:
            fail("MCP adoption needs a secret-free command or URL.")
        allowed = {"type", "command", "args", "url"}
        if set(value) - allowed:
            fail("MCP adoption needs a secret-free command or URL.")
        if "url" in value:
            if set(value) - {"type", "url"} or value.get("type", "http") != "http" or not isinstance(value["url"], str):
                fail("MCP adoption needs a secret-free command or URL.")
            normalized.append(("url", value["url"]))
        else:
            command, arguments = value.get("command"), value.get("args", [])
            if value.get("type", "stdio") != "stdio" or not isinstance(command, str) or not command or not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
                fail("MCP adoption needs a secret-free command or URL.")
            if Path(command).name == "fleet":
                fail("MCP adoption needs a direct server command.")
            normalized.append(("stdio", tuple([command, *arguments])))
    if not normalized or len(set(normalized)) != 1:
        fail("MCP definitions differ across clients.")
    return normalized[0]


def toml_array(values):
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def append_block(path, lines):
    text = path.read_text().rstrip()
    path.write_text(text + "\n\n" + "\n".join(lines) + "\n")


def adopt_mcp(candidate, request):
    kind, value = live_mcp_definition(request.home, request.name)
    directory = candidate / ("mcp" if request.scope == "shared" else f"hosts/{request.host}/mcp")
    codex_path, claude_path, runner_path = directory / "codex.toml", directory / "claude.json", directory / "runners.toml"
    directory.mkdir(parents=True, exist_ok=True)
    if not codex_path.exists():
        codex_path.write_text("[mcp_servers]\n")
        claude_path.write_text('{"mcpServers": {}}\n')
        runner_path.write_text("[servers]\n")
    codex = tomllib.loads(codex_path.read_text())["mcp_servers"]
    claude = json.loads(claude_path.read_text())
    runners = tomllib.loads(runner_path.read_text())["servers"]
    if request.name in codex or request.name in claude["mcpServers"] or request.name in runners:
        fail("MCP server is already managed by Fleet.")
    if kind == "url":
        append_block(codex_path, [f"[mcp_servers.{request.name}]", f"url = {json.dumps(value)}"])
        claude["mcpServers"][request.name] = {"type": "http", "url": value}
    else:
        command = list(value)
        wrapper = ["mcp", "run", request.name, "--", *command]
        append_block(codex_path, [f"[mcp_servers.{request.name}]", 'command = "fleet"', f"args = {toml_array(wrapper)}"])
        claude["mcpServers"][request.name] = {"type": "stdio", "command": "fleet", "args": wrapper}
        append_block(runner_path, [f"[servers.{request.name}]", "secrets = []", f"command = {toml_array(command)}"])
    claude_path.write_text(json.dumps(claude, indent=2, sort_keys=True) + "\n")


def mutate_candidate(candidate, request, discoveries):
    if request.kind == "skill":
        source = selected_skill_source(request.home, discoveries)
        target = candidate / (f"skills/{request.name}" if request.scope == "shared" else f"hosts/{request.host}/skills/{request.name}")
        if target.exists():
            fail("Skill is already managed by Fleet.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True)
    elif request.kind == "hook":
        source = selected_hook_source(request.home, discoveries)
        target = candidate / (f"hooks/{request.name}" if request.scope == "shared" else f"hosts/{request.host}/hooks/{request.name}")
        if target.exists():
            fail("Hook is already managed by Fleet.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    elif request.kind == "plugin":
        adopt_plugin(candidate, request, discoveries)
    elif request.kind == "mcp":
        adopt_mcp(candidate, request)
    else:
        fail("Managed rule or setting drift must be edited in the Fleet repository.")


def adopt(request):
    if request.kind not in {"skill", "hook", "plugin", "mcp", "drift"} or request.scope not in {"shared", "host"}:
        fail("Invalid Fleet adoption request.")
    state = load_state(request.home)
    discoveries = [] if state is None else [item for item in state["discoveries"] if item["kind"] == request.kind and item["name"] == request.name]
    if not discoveries:
        fail("Run fleet scan; that item is not a current discovery.")
    with fleet.operation_lock(request.home):
        if git_run(request.root, "branch", "--show-current").stdout.strip() != "main" or fleet.repo_dirty(request.root):
            fail("Fleet adopt needs a clean main checkout.")
        git_run(request.root, "fetch", "--prune", "origin", "refs/heads/main")
        if fleet.repo_sha(request.root) != git_run(request.root, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip():
            fail("Run fleet update before adopting another item.")
        origin = git_run(request.root, "config", "--get", "remote.origin.url").stdout.strip()
        if not origin:
            fail("Fleet origin is unavailable.")
        with tempfile.TemporaryDirectory(prefix="fleet-adopt-") as temporary:
            candidate = Path(temporary) / "repo"
            git_run(Path(temporary), "clone", "--quiet", "--no-tags", "--single-branch", "--branch", "main", origin, str(candidate))
            name_value = git_run(request.root, "config", "user.name", check=False).stdout.strip() or "Fleet"
            email_value = git_run(request.root, "config", "user.email", check=False).stdout.strip() or "fleet@localhost"
            git_run(candidate, "config", "user.name", name_value)
            git_run(candidate, "config", "user.email", email_value)
            mutate_candidate(candidate, request, discoveries)
            fleet.check_repository(candidate)
            git_run(candidate, "add", "--all")
            if not git_run(candidate, "diff", "--cached", "--quiet", check=False).returncode:
                fail("Fleet adoption made no change.")
            git_run(candidate, "commit", "--quiet", "-m", f"fleet: adopt {request.kind} {request.name} from {request.host}")
            sha = fleet.repo_sha(candidate)
            git_run(candidate, "push", "origin", "HEAD:refs/heads/main")
    return {"sha": sha, "kind": request.kind, "name": request.name, "scope": request.scope}


def print_items(items, as_json=False):
    if as_json:
        print(json.dumps(items, indent=2, sort_keys=True))
        return
    print(f"discoveries: {len(items)}")
    for item in items:
        client = f" [{item['client']}]" if "client" in item else ""
        print(f"{item['kind']}: {item['name']}{client}")


def memory_status_line(root, home, host, **kwargs):
    if not (Path(root) / "memory.toml").is_file():
        return "memory: local (plugin-managed)", True
    try:
        memory_spec = importlib.util.spec_from_file_location("fleet_memory", Path(root) / "lib/fleet_memory.py")
        memory = importlib.util.module_from_spec(memory_spec)
        memory_spec.loader.exec_module(memory)
        return memory.status_line(root, home, host, **kwargs)
    except (AttributeError, ImportError, OSError):
        return "memory: error", False


def main():
    if len(sys.argv) < 3:
        fail("Missing Fleet command.")
    root = Path(sys.argv[1]).resolve()
    command = sys.argv[2]
    home = fleet.fixed_home()
    if command == "adopt":
        arguments = sys.argv[3:]
        scope = "shared" if "--shared" in arguments else ("host" if "--host" in arguments else None)
        arguments = [argument for argument in arguments if argument not in {"--shared", "--host"}]
        if scope is None or len(arguments) not in {2, 4}:
            fail("Usage: fleet adopt KIND NAME (--shared|--host) [--device HOST]")
        host = None
        if len(arguments) == 4 and arguments[2] == "--device":
            host = arguments[3]
            arguments = arguments[:2]
        if len(arguments) != 2:
            fail("Usage: fleet adopt KIND NAME (--shared|--host) [--device HOST]")
        root = fleet.source_repository(root, home)
        host = fleet.host_id(root, host)
        result = adopt(AdoptionRequest(root, home, host, arguments[0], arguments[1], scope))
        print(f"pushed: {result['sha']}")
        return 0
    host, arguments = fleet.parse_host(sys.argv[3:])
    root = fleet.source_repository(root, home)
    host = fleet.host_id(root, host)
    if command == "scan":
        as_json = "--json" in arguments
        arguments = [argument for argument in arguments if argument != "--json"]
        if arguments:
            fail("Usage: fleet scan [--json] [--host HOST]")
        with fleet.operation_lock(home):
            items = scan(root, home, host)
        print_items(items, as_json)
        return 0
    if command == "update":
        if arguments:
            fail("Usage: fleet update [--host HOST]")
        items, drift = scan_if_due(root, home, host)
        if drift:
            fail("Local managed changes need review. Run fleet scan, then edit Fleet or run fleet apply to discard them.")
        fleet.update(root, home, host=host)
        return 0
    if command == "status":
        if arguments:
            fail("Usage: fleet status [--host HOST]")
        code = fleet.status(root, home, host=host)
        state = load_state(home)
        count = len(state["discoveries"]) if state else 0
        print(f"discoveries: {count}")
        memory_line, memory_ok = memory_status_line(root, home, host)
        print(memory_line)
        return 1 if count or not memory_ok else code
    fail("Unknown Fleet discovery command.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
