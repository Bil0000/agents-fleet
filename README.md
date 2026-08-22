# Agents Fleet

[![Check](https://github.com/Bil0000/agents-fleet/actions/workflows/check.yml/badge.svg)](https://github.com/Bil0000/agents-fleet/actions/workflows/check.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A blank, working source of truth for Claude Code and Codex across macOS and Linux.

Fleet keeps your own rules, settings, skills, commands, hooks, plugins, MCP servers, secrets, updates, and Claude-Mem setup consistent. This repository contains the engine and empty folders. It does not contain the author's configuration.

## Set it up with an agent

Fork this repository or copy it into a private repository that you own. Then give the repository to an agent with this prompt:

```text
Set up this Agents Fleet for me. Read SETUP_WITH_AN_AGENT.md in full and continue until every required check passes. Use local-only mode unless I explicitly ask for more than one device. Preserve my current setup, never put secrets or OAuth state in Git, and install Claude-Mem for memory.
```

The full contract is in [SETUP_WITH_AN_AGENT.md](SETUP_WITH_AN_AGENT.md).

## What is empty

```text
rules/                 shared, Claude-only, and Codex-only rules
settings/              safe client settings
skills/                shared skills
commands/              slash commands and prompts
hooks/                 shared hook scripts
hosts/                 one private overlay per device
mcp/                   matching Claude and Codex MCP manifests
plugins.json            shared plugin manifest
memory.example.toml     optional shared-memory example
```

All user-owned files start empty. Add only what you want Fleet to manage.

## How sync works

1. You change your Fleet repository and push `main`.
2. GitHub Actions checks it on macOS and Linux.
3. A passing commit moves to the validated `live` branch.
4. Each device checks `live` every minute and applies it safely.
5. Each device scans hourly for new local skills, hooks, plugins, and MCPs.

GitHub cannot directly enter devices behind a firewall. The one-minute poll gives the same result without opening an inbound port.

Run an update now:

```bash
~/.local/bin/fleet update
```

Inspect local additions:

```bash
~/.local/bin/fleet scan
```

## Safety model

- Fleet refuses dirty releases, symlinks, common secret formats, unsafe paths, and invalid manifests.
- `fleet apply --dry-run` shows targets before a write.
- Apply and update operations create backups and restore the prior state after a failure.
- MCP secrets stay outside Git: macOS Keychain on macOS and `systemd-creds` on Linux.
- OAuth tokens, session history, caches, databases, and machine credentials are never template content.
- Claude-Mem is local by default. One shared Linux memory server for a macOS client is optional.

## Main commands

```bash
./bin/fleet check
./bin/fleet apply --dry-run --host HOST
./bin/fleet apply --host HOST
~/.local/bin/fleet status --host HOST
~/.local/bin/fleet enable-updates
~/.local/bin/fleet update --host HOST
~/.local/bin/fleet scan --host HOST
~/.local/bin/fleet rollback
~/.local/bin/fleet auth set SECRET_NAME
```

## Requirements

- macOS or Linux
- Git
- Bash
- Python 3.11 or newer
- Node.js 20.12 or newer for Claude-Mem
- Claude Code and/or Codex
- `systemd-creds` and passwordless `sudo -n` for Fleet-managed secrets on Linux

## License

[MIT](LICENSE)
