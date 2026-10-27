import json
import shutil
from pathlib import Path


def copy_fixture(source, target):
    source, target = Path(source), Path(target)
    config = {"rules", "settings", "skills", "commands", "hooks", "hosts", "mcp", "plugins.json", "memory.toml"}
    shutil.copytree(source, target, ignore=lambda directory, names: set(names) & (
        {".git", "__pycache__", ".worktrees"} | (config if Path(directory) == source else set())
    ))
    for directory in ("rules", "settings", "skills", "commands", "hooks", "hosts", "mcp"):
        (target / directory).mkdir()
    files = {
        "rules/common.md": "", "rules/claude.md": "", "rules/codex.md": "",
        "settings/claude.json": "{}\n", "settings/codex.toml": "",
        "mcp/codex.toml": "[mcp_servers]\n", "mcp/claude.json": '{"mcpServers": {}}\n',
        "mcp/runners.toml": "[servers]\n", "mcp/required-secrets.txt": "",
        "mcp/README.md": "MCP configuration.\n",
        "plugins.json": json.dumps({
            client: {"marketplaces": {}, "plugins": []} for client in ("claude", "codex")
        }) + "\n",
    }
    for path, contents in files.items():
        (target / path).write_text(contents)
