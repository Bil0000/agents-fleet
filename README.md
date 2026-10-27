# Agents Fleet

**One private Git repo holds your Claude Code and Codex setup. Fleet tests it and applies it to each of your Mac and Linux devices.**

You still work in Claude Code or Codex. Fleet manages their setup files. It does not contain a model list; models and reasoning options come from the clients.

## What you get

After setup, you have:

- **A private Fleet repo that you own.** It holds your rules, settings, skills, commands, hooks, plugins, and MCP servers (tools your agents can call).
- **Tested releases.** You push `main`. GitHub Actions tests it on macOS and Linux and moves the passing commit to `live`. Devices with automatic updates enabled follow `live`.
- **Backups and rollback.** Each apply backs up the files it replaces. `fleet rollback` restores them.
- **Account routing.** A local router sends Claude Code and Codex requests through the accounts you choose. Fallback to another account is optional.
- **Safe MCP secrets.** Secret values stay in macOS Keychain or Linux `systemd-creds`, never in Git.
- **Claude-Mem memory.** Local memory on each device, or one shared database on your own Linux server.

## Set up with an agent

Copy this prompt into Claude Code or Codex. You do not need to clone anything first.

```text
Set up https://github.com/Bil0000/agents-fleet for me. Read SETUP_WITH_AN_AGENT.md in that repo and follow it to the end.

1. Inspect my devices, my Claude Code and Codex settings, and the tools I already have. Change nothing yet.
2. Create my own private copy from the template. Do not fork it.
3. Back up my current setup before you change anything.
4. Install the Fleet engine, account routing, and Claude-Mem.
5. Show a short inventory. Import my existing portable rules, settings, skills, and MCP servers. Keep unknown or unsupported settings local.
6. Run the checks, tests, and dry run. Then apply, enable updates, and report the results.

Memory: if I name a Linux server I own, set up shared memory on it so all my devices use one database. If not, use local memory on this device and tell me what local mode does not do.
Accounts: keep fallback off until I add my own second account.
Stop and ask me only for a login, a secret, anything that costs money, or anything that can delete data.
```

The agent stops for a login, a secret, a paid service, or an action that can lose data. It does all other work itself. The full agent checklist is [SETUP_WITH_AN_AGENT.md](SETUP_WITH_AN_AGENT.md).

To make the repo yourself, click [Use this template](https://github.com/new?template_owner=Bil0000&template_name=agents-fleet&visibility=private). Keep it private and leave **Include all branches** off. Then give the agent the prompt above with your repo URL. **Do not fork** for personal configuration. A fork of this public repo stays public.

## What is the same, and what is yours

| The same for every Fleet user | Yours only |
| --- | --- |
| The `fleet` command and its apply, backup, rollback, scan, and update logic | Rules in `rules/` and settings in `settings/` |
| The GitHub Actions check that promotes `main` to `live` | Skills, commands, and hooks |
| The account router: patched CLIProxyAPI, retry bridge, and hub | Plugins in `plugins.json` and MCP servers in `mcp/` |
| The MCP launcher and secret storage | Device profiles in `hosts/` |
| Shared-memory services: capture queue, SSH tunnel, worker, daily backups | Your accounts, logins, secret values, servers, and memory data |

This template ships with empty rules, hooks, skills, MCP declarations, and device profiles. Nothing personal from the maintainer is in it. The maintainer's private dashboard is not part of it. Credentials, chat history, and databases never go into Git.

## Choose a setup

| Setup | What you need | What you get |
| --- | --- | --- |
| **One device, local memory** (default) | This device | Fleet, account routing, and Claude-Mem with its own database on this device. |
| **Several devices, local memory** | Each device clones your repo | The same config on every device. Each device keeps its own memory database. |
| **Several devices, shared memory** | A Linux server you own, with key-based SSH from each client | One memory database on your server. This is the full topology described below. |

**Shared memory topology.** One Linux server runs the Claude-Mem worker and holds the shared database. Each client (macOS or Linux) writes captures to a local queue on disk first. A Fleet service keeps an SSH tunnel to the server and delivers the queue in the background. The memory worker listens on the server's localhost address; memory traffic goes through SSH. The server backs up the database daily, checks the backup, and compresses it. To get this, you must supply the server. Fleet does not provide one.

**What local mode does not do.** Claude-Mem's own installer manages local memory. Fleet's `fleet memory` commands need `memory.toml`, so in local mode you do not get:

- one database across devices,
- Fleet's local capture queue and SSH tunnel,
- Fleet's daily verified backups of the memory database,
- Fleet's memory-worker patches, including the switch from Claude to `gpt-6.1-terra` when the Claude allowance runs out.

You can move from local to shared memory later. The agent follows the shared-memory steps in [SETUP_WITH_AN_AGENT.md](SETUP_WITH_AN_AGENT.md) and backs up every existing database first. It does not merge old databases unless you ask.

## How sync, backup, and update work

```text
edit your repo -> push main -> GitHub Actions tests macOS and Linux -> passing commit moves to live -> devices apply live
```

- **Updates.** With `fleet update on`, each device checks your repo every minute. It fetches `live`, accepts only a fast-forward, checks the new release, then applies it. Updates come from your repo only.
- **Local edits block updates.** Fleet scans hourly for new local items and for edits to files it manages. If a managed file has local edits, updates stop. Run `fleet scan`, then save the edit in your repo or discard it with `fleet apply`.
- **Backups.** Each `fleet apply` backs up the files it replaces under `~/.local/state/fleet/backups/`. Apply needs a clean checkout.
- **Rollback.** `fleet rollback` restores the latest successful backup, or the one you name. It backs up the current state first. Run `fleet update off` if you want to keep the restored state.
- **Engine updates.** Your repo is independent. New versions of this public template do not reach it by themselves. To get them, ask your agent for an engine update. The last part of [SETUP_WITH_AN_AGENT.md](SETUP_WITH_AN_AGENT.md) gives the steps.

## Requirements

- macOS or Linux, Git, Bash, and Python 3.11 or newer.
- Claude Code, Codex, or both.
- Node.js 20.12 or newer for Claude-Mem.
- `gh` and your GitHub login, so the agent can create your private repo. A local repo with no remote also works, but without automatic updates.
- Go 1.26 or newer and a C compiler to build the patched account router.
- On Linux, `systemd-creds` with non-interactive `sudo` access for MCP secrets.
- `fleet onboard` also needs both clients and Bun. With one client, the agent uses `fleet apply` instead.
- Shared memory needs your own Linux server, SSH, and Claude-Mem 13.24.0 on every device.
- A shared account router needs Tailscale with MagicDNS.

The agent installs missing tools with their supported installers.

## Command reference

Before Fleet is installed, run `./bin/fleet` inside your clone. `fleet apply` installs `~/.local/bin/fleet`; add `~/.local/bin` to your shell's `PATH`.

In the tables, replace uppercase words with your values. `[VALUE]` is optional. `HOST` is a device profile name. `PROVIDER` is `claude` or `codex`. Put quotes around labels with spaces.

### Setup and daily use

| Command | What it does |
| --- | --- |
| `fleet check` | Check config, scripts, paths, and common secret patterns. Run it before each commit. |
| `fleet host add HOST --os macos --role workstation` | Make a device profile and its plugin file. Use `--os linux` on Linux. |
| `fleet onboard --host HOST --memory local` | Run check and tests, apply, set up account routing, and enable updates. Commit and push the profile, and wait for `live`, first. Use `--memory shared` after you configure shared memory. |
| `fleet apply --dry-run --host HOST` | Show what apply would change. Changes nothing. |
| `fleet apply --host HOST` | Back up, then apply the committed config. Needs a clean checkout. |
| `fleet status` | Show the release, update service, missing secrets, discoveries, and memory health. |
| `fleet update` | Get and apply your tested `live` release now. |
| `fleet update on` / `off` | Start or stop automatic updates. |
| `fleet scan` | Find new local items and edits to managed files. Add `--json` for machine output. |
| `fleet adopt KIND NAME --shared` | Import a scanned `skill`, `hook`, `plugin`, or `mcp` for all devices. **It checks, commits, and pushes.** Needs a clean, current `main`. |
| `fleet adopt KIND NAME --host` | The same, for this device only. It also commits and pushes. Add `--device HOST` to pick the profile. |
| `fleet rollback [BACKUP-ID]` | Restore a backup. The default is the latest successful one. |

`apply`, `status`, `scan`, `update`, and the memory commands accept `--host HOST`. Without it, Fleet uses the current device's profile. `adopt` cannot import rules or settings; edit those in the repo.

### Accounts and fallback

Routing sends Claude Code and Codex requests through a local proxy, so Fleet can pick the account. Run `fleet account setup`, open a new terminal, then log in with the normal provider commands: `codex login` or `claude auth login`. Logging in again refreshes that account. Logging out with the native command removes its routed account.

The agent builds the tested, patched router with `python3 -B lib/fleet_cliproxy_update.py --bootstrap`. Major upstream versions stay blocked until their config migration is verified.

All commands below start with `fleet account`.

| Command | What it does |
| --- | --- |
| `setup` | Install the local CLIProxyAPI router, retry bridge, and login hooks. |
| `setup --check` | Check the router and client setup. |
| `setup --restore-clients` | Restore client routing settings. Keeps accounts and the proxy. |
| `list [PROVIDER]` | Show accounts, order, fallback mode, usage, reset times, and recent use. |
| `login PROVIDER [LABEL]` | Log in and add an account, with an optional name. |
| `login PROVIDER --existing ACCOUNT` | Add credentials that are already in CLIProxyAPI. |
| `logout PROVIDER [ACCOUNT]` | Remove an account. The default is the primary. |
| `rename PROVIDER ACCOUNT LABEL` | Change an account's display name. |
| `use PROVIDER [ACCOUNT]` | Choose the primary account. Without `ACCOUNT`, it opens a picker. |
| `order PROVIDER` | Show the saved order. |
| `order PROVIDER PRIMARY BACKUP...` | Set the order by hand. List every account once, primary first. |
| `order PROVIDER reset` | Sort by soonest weekly reset, after login and during hourly updates. |
| `order PROVIDER reset-fallbacks` | Keep the primary first. Sort the fallback accounts by soonest reset. |
| `order PROVIDER manual` | Keep the current order. Stop automatic sorting. |
| `order PROVIDER rebalance` | Sort now. Use `order rebalance` for both providers. |
| `auto PROVIDER on` / `off` | Turn fallback on when a limit is reached, or use only the primary. |
| `auto PROVIDER on --when any` | Also switch on temporary provider errors. This can use fallback quota. `--when limits` is the default. |
| `credits PROVIDER never` / `last` / `same-account` | Never use paid credits, use credits after all allowances, or use each account's credits before switching. Needs the patched router. |
| `auto --help`, `credits --help` | Explain the modes. |

Example: `fleet account use codex "Work Pro"`. Use only accounts you own.

How fallback behaves:

- It switches only before output starts. Fleet never replays a request that has partly arrived.
- New sessions try the primary first. Existing sessions keep their account while it works. A manual account change applies on the next request.
- Retries stop after three attempts or 120 seconds. Longer provider waits go back to the client.

### Move an existing CLIProxyAPI

If Fleet finds a CLIProxyAPI binary, config, or service that it does not own, `fleet account setup` stops and changes nothing. To move to Fleet's router:

1. Stop the old CLIProxyAPI service.
2. Back up its binary and config, then move them out of their current paths.
3. Leave `~/.cli-proxy-api` in place. Fleet reuses that login folder.
4. Run `fleet account setup`.
5. Use `fleet account login PROVIDER --existing ACCOUNT` for each saved login.

### Share one account router

Install Tailscale with MagicDNS and run `fleet account setup` on each device first.

| Command | What it does |
| --- | --- |
| `fleet account hub setup` | Share this device's router on your Tailscale network, port 8317. |
| `fleet account hub join HUB --ssh USER@HOST` | Use the hub's full MagicDNS name. Copies the hub's private access keys over SSH and starts a local relay. |
| `fleet account hub` | Show this device's role and check the connection. |
| `fleet account hub leave` | Go back to the local router. |

Clients still connect to localhost. A change to account order on the hub affects all joined devices. Make credit changes on the hub.

### Secrets and MCP servers

List secret names, not values, in `mcp/required-secrets.txt`. See [mcp/README.md](mcp/README.md) for server examples.

| Command | What it does |
| --- | --- |
| `fleet auth set NAME` | Ask for a declared secret and store it. |
| `fleet auth status [NAME]` | Check one or all secrets. Never prints values. |
| `fleet mcp run SERVER -- COMMAND...` | Start a declared server with its secrets. The command must match `mcp/runners.toml`. Clients call this for you. |

### Shared memory

These commands need `memory.toml`. Copy [memory.example.toml](memory.example.toml) and set your own hosts and SSH target.

| Command | What it does |
| --- | --- |
| `fleet memory enable` | Set up and start this device's shared-memory services. Run it on the server first, then on each client. |
| `fleet memory status` | Check services, tunnel, queues, and health. Sends no model request. |
| `fleet memory disable` | Stop the services. Keeps the database and settings. |
| `fleet memory backup` | On the server only: back up the database, check it, and compress it. A daily timer also runs this. |
| `fleet memory harden` | Apply Fleet's Claude-Mem worker patches. `enable` does this too. |
| `fleet memory save` / `update` | Save or change a memory without a model call. Reads JSON from standard input. |

If the server is down, captures wait in the local queue. Idle sessions stop model work and keep queued items.

### Published media

| Command | What it does |
| --- | --- |
| `fleet media track PR-URL IMAGE-URL...` | Track published ghdrop images and where GitHub uses them. Needs a cleanup config in your host profile. |
| `fleet media keep IMAGE-URL...` | Protect tracked images from cleanup. |
| `fleet media cleanup --dry-run` | Show tracked images that cleanup would delete. Without `--dry-run`, it deletes them. |

An image becomes eligible ten days after its last use and after every known GitHub reference closes. Open references, a `keep-media` label, or an unknown state block deletion. Cleanup is off until you configure it.

<details>
<summary>Aliases and internal commands</summary>

These also start with `fleet`. Aliases accept the same arguments.

- `enable-updates` / `disable-updates`: same as `update on` / `off`.
- `account`, `account current`, `account quota`: same as `account list`.
- `account remove`: same as `account logout`.
- `account enroll PROVIDER ACCOUNT`: same as `account login PROVIDER --existing ACCOUNT`.
- `account add`: older form of `account login`. New logins with it need `FLEET_ENABLE_SUBSCRIPTION_ROUTING=1`.
- `account rebalance [PROVIDER]`: same as `account order PROVIDER rebalance`.
- `account order --auto PROVIDER reset` / `manual`: older sorting syntax.
- `account hub status`: same as `account hub`.
- `onboard --os OS --role ROLE`: make the profile during onboarding. Both flags go together. A separate `host add` is better because you can commit the profile first.
- Internal: `account native-auth` wraps provider login and logout. `memory worker` runs the server worker. `memory proxy` keeps the client's SSH tunnel. `memory enqueue`, `init`, `drain`, and `deliver` move captures through the queue.

</details>

## Repo layout

| Path | Contents |
| --- | --- |
| `rules/` | `common.md` for both clients, `claude.md` and `codex.md` for one client. |
| `settings/` | Portable settings for each client. |
| `skills/`, `commands/`, `hooks/` | Your skills, prompts, and hook scripts. |
| `plugins.json`, `mcp/` | Plugin and MCP server declarations. |
| `hosts/HOST/` | Device-only settings, paths, and overrides. |
| `memory.example.toml` | Template for shared-memory `memory.toml`. |
| `bin/`, `lib/`, `patches/`, `system/`, `tests/` | The Fleet engine. Change these only for an engine update. |

Fleet rejects unsafe paths, symlinks, invalid config, home-folder paths outside `hosts/`, and common secret formats. These checks cannot find every secret, so review each diff before you commit.

[MIT license](LICENSE)
