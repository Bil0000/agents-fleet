# Agents Fleet

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A blank, working source of truth for Claude Code and Codex across macOS and Linux, with optional multi-account failover.

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

## Multiple accounts

Fleet can route any number of Claude and Codex subscriptions through one shared client home. Only the account that pays for a request changes. Threads, transcripts, memory, rules, settings, models, reasoning options, and native client behavior stay shared and provider-owned.

Set it up once on each macOS or Linux device:

```bash
fleet account setup
```

Fleet downloads and verifies CLIProxyAPI 7.2.149, installs a small retry bridge, and keeps both endpoints and their private keys on localhost. No separate proxy install or sudo access is needed. If Fleet finds a proxy setup that it does not own, it stops without changing it.

Use this only with accounts you own and where each provider's terms and plan allow it.

Open a new terminal, then use the normal provider commands to add or refresh accounts:

```bash
codex login
claude auth login
fleet account list
```

The same native logout commands remove the matching routed account. Logging in to the same account refreshes it instead of creating a duplicate. Fleet labels can be changed at any time.

```bash
fleet account rename codex ACCOUNT "Work Pro"
fleet account use codex "Work Pro"
fleet account auto codex on
fleet account current codex
```

Auto mode defaults to `limits only`: it switches accounts only for a structured subscription-limit response before output starts. `fleet account auto codex on --when any` also switches for temporary provider errors and can spend fallback quota. The retry bridge caps each wait at five minutes. The bundled provider-routing patch also caps quota cooldowns and clears them from fresh provider usage data. Fleet returns to the chosen primary when it is ready. Fleet never replays a request after output starts because that could repeat tool work or other side effects.

Set a fallback order with every enrolled account listed once, starting with the primary:

```bash
fleet account order codex "Work Pro" "Backup 1" "Backup 2"
fleet account order codex
fleet account order codex reset
fleet account order rebalance
fleet account order codex manual
```

`reset` puts the account whose weekly limit resets soonest first. Fleet checks the order after login and hourly when automatic updates are on. `manual` stops these order changes. Setting an explicit order also returns to manual mode. The order stays saved when auto switching is turned off. New accounts go last; removing the primary promotes the next account.

The same commands work with `claude`. Run `fleet account auto --help` to see the switching modes. `fleet account list` shows order, usage, reset times, and the accounts used in the latest successful 10-minute request bucket. `current` and `quota` are aliases for this list. Use `fleet account login codex --existing ACCOUNT` to adopt credentials from an existing CLIProxyAPI setup; `enroll` still works.

Fleet enables upstream WebSocket connections for enrolled Codex accounts. The retry bridge leaves slow responses open and sends small output chunks without delay. The bundled provider-routing patch preserves healthy accounts after connection failures and retries eligible Codex connection failures once before a response starts.

Fleet does not contain a model list. New models, aliases, reasoning controls, auth screens, and non-auth client commands continue to come directly from Claude and Codex.

Newer Fleet-managed CLIProxyAPI builds are supported. The macOS updater source and provider-routing patch are included for existing custom builds. They are not installed by `fleet account setup`. The updater needs Go, a C compiler, and an existing Fleet provider-router setup. It checks a new build and restores the old build if activation fails.

## Optional shared account hub

One host can provide account routing for other devices on the same Tailscale network. Set up Fleet account routing on each device first. Tailscale must be installed and signed in, with MagicDNS enabled. On the host that will serve requests:

```bash
fleet account hub setup
```

On another device, use the hub's MagicDNS name and an SSH target for its user:

```bash
fleet account hub join hub.tail123abc.ts.net --ssh user@hub
fleet account hub
fleet account hub leave
```

Setup serves the router on port 8317 through Tailscale. Access needs the recorded Tailscale login and a hub key. Joining copies the client and management keys over SSH into private local files, then runs a local relay. The client endpoint stays on localhost. Native login commands add accounts to the hub. Local account labels and order settings remain on each device; changes to proxy account priority affect all devices using the hub.

`hub` shows the role and checks the relay connection. `hub leave` restores the device's local router. The local proxy binary, settings, and credentials stay on disk while it uses the hub. The optional proxy updater skips devices that use a hub.

## Host setup

After you create your own private repository, add a device profile:

```bash
./bin/fleet host add my-laptop --os macos --role workstation
```

Use `--os linux` for a Linux device. This adds a host manifest and the Claude-Mem plugin entry. It does not apply settings.

Commit and push the new profile to your private repository, then wait for its checks to pass and its `live` branch to update. For a full setup on that device:

```bash
./bin/fleet onboard --host my-laptop --memory local
```

This checks the tools and tests, applies Fleet, sets up account routing, and enables updates. It needs both Claude and Codex, Bun, Node.js 20.12 or newer, and a working private `origin`. Use the manual setup guide if you want only one client or no account routing.

## Optional Codex settings and hooks

The template leaves settings empty. In your private repo, you can add these to `settings/codex.toml`:

```toml
[features]
context_management = true

[agents]
max_concurrent_threads_per_session = 64
```

Use a Codex version that supports these settings. Fleet validates the thread limit as a positive whole number. It keeps your other agent roles and settings.

Shared hooks can go in `settings/codex-hooks.json`. Fleet combines them with `hosts/HOST/codex-hooks.json`. Shared and host Claude hooks are also combined.

## Shared memory

Local Claude-Mem is still the default. For shared memory, copy `memory.example.toml` to `memory.toml` in your private repo. Set one Linux server and a `client_hosts` list of macOS or Linux clients. The older single `client_host` field also works. Use only one of these fields.

The shared setup uses Claude-Mem 13.24.0 and checks its worker before applying the known session and health fixes. Set up Fleet account routing on the server first. Clients connect through SSH. See [the shared-memory setup steps](SETUP_WITH_AN_AGENT.md).

## Safety model

- Fleet refuses dirty releases, symlinks, common secret formats, unsafe paths, and invalid manifests.
- `fleet apply --dry-run` shows targets before a write.
- Apply and update operations create backups and restore the prior state after a failure.
- MCP secrets stay outside Git: macOS Keychain on macOS and `systemd-creds` on Linux.
- OAuth tokens, session history, caches, databases, and machine credentials are never template content.
- Claude-Mem is local by default. One shared Linux memory server for macOS and Linux clients is optional.

## Main commands

```bash
./bin/fleet check
./bin/fleet apply --dry-run --host HOST
./bin/fleet apply --host HOST
~/.local/bin/fleet status --host HOST
~/.local/bin/fleet update on
~/.local/bin/fleet update --host HOST
~/.local/bin/fleet scan --host HOST
~/.local/bin/fleet rollback
~/.local/bin/fleet auth set SECRET_NAME
~/.local/bin/fleet account setup
~/.local/bin/fleet account list
~/.local/bin/fleet account current
~/.local/bin/fleet account use codex ACCOUNT
~/.local/bin/fleet account auto codex on
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
