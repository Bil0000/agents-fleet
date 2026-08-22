# Agents Fleet

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A blank, working source of truth for Claude Code and Codex across macOS and Linux.

Fleet keeps your own rules, settings, skills, commands, hooks, plugins, MCP servers, secrets, updates, and Claude-Mem setup consistent. This repository contains the engine and empty folders. It does not contain my own configuration.

## Start here

Do not fork this repository. [A fork of a public repository is public](https://docs.github.com/en/pull-requests/reference/forks#visibility-of-forks). Your Fleet will contain personal rules and device details.

1. Click [Use this template](https://github.com/new?template_owner=Bil0000&template_name=agents-fleet&visibility=private).
2. Create a new **private** repository under your GitHub account or organization. Leave **Include all branches** off.
3. Clone your new repository. Its `origin` must point to your repository, not `Bil0000/agents-fleet`.
4. Open that clone with Claude Code or Codex and paste this prompt:

```text
Set up this Agents Fleet in the repository I own. Before writing anything, verify that git remote get-url origin does not point to Bil0000/agents-fleet. Read SETUP_WITH_AN_AGENT.md in full and continue until every required check passes. Start with one device and local Claude-Mem unless I tell you that I have more devices. Preserve my current setup. Never put secrets, OAuth state, databases, or session history in Git.
```

The agent will back up your current setup, fill only the files you choose, install Claude-Mem, push to your private repository, wait for the checks, apply Fleet, and enable updates. The full contract is in [SETUP_WITH_AN_AGENT.md](SETUP_WITH_AN_AGENT.md).

If you do not want a GitHub repository, run `git remote remove origin` in a local clone and use manual apply. Automatic cross-device updates need your own remote repository.

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

1. You change your private Fleet repository and push `main`.
2. GitHub Actions checks it on macOS and Linux.
3. A passing commit moves to the validated `live` branch.
4. Each device checks `live` every minute and applies it safely.
5. Each device scans hourly for new local skills, hooks, plugins, and MCPs.

GitHub cannot directly enter devices behind a firewall. The one-minute poll gives the same result without opening an inbound port.

Your repository is independent from this template. Future template changes do not enter your Fleet automatically.

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
