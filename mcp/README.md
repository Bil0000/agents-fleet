# MCP configuration

Fleet uses one logical MCP list for both clients. Every server name must exist in both `codex.toml` and `claude.json`.

## Remote HTTP server

Add the same URL to both manifests. No runner entry is needed.

```toml
[mcp_servers.docs]
url = "https://example.com/mcp"
```

```json
{
  "mcpServers": {
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp"
    }
  }
}
```

## Local stdio server

Both clients call Fleet. Fleet checks the exact command and adds only the declared secrets.

```toml
[mcp_servers.docs]
command = "fleet"
args = ["mcp", "run", "docs", "--", "npx", "-y", "docs-mcp@1.0.0"]
```

```json
{
  "mcpServers": {
    "docs": {
      "type": "stdio",
      "command": "fleet",
      "args": ["mcp", "run", "docs", "--", "npx", "-y", "docs-mcp@1.0.0"]
    }
  }
}
```

Declare the runner:

```toml
[servers.docs]
secrets = ["DOCS_API_KEY"]
command = ["npx", "-y", "docs-mcp@1.0.0"]
```

Add the name, not the value, to `required-secrets.txt`:

```text
DOCS_API_KEY
```

Store the value on each device:

```bash
./bin/fleet auth set DOCS_API_KEY
```

Use a host overlay under `hosts/HOST/mcp/` for a server that exists on only one device. A host overlay has the same three manifest files.

Run `./bin/fleet check` after every change. Static credentials, credential-like command arguments, unsafe URLs, mismatched client entries, and undeclared secrets are rejected.

## Start tools on first use

Managed stdio servers use Fleet's shared launcher. It reuses safe startup data across sessions and starts the server when a call needs it. Set `FLEET_MCP_EAGER=1` to use normal startup. Fleet still checks the exact command and declared secrets.

For a native server registration, run:

```bash
fleet mcp command -- SERVER-COMMAND ARGUMENTS...
```

This prints a JSON registration. Review it before adding it to a client. It preserves the server's working directory, arguments, and environment. It uses the original command if the Fleet helper is unavailable.

A host can also add non-secret environment values to an existing shared runner without changing its command. Put only the override in `hosts/HOST/mcp/runners.toml`; leave its client manifests empty if it has no host-only servers.

```toml
[servers.docs.env]
DOCS_MODE = "local"
```

The shared runner must already exist. Secret-like values, invalid environment names, and command replacements are rejected.
