# Agents Fleet

**Keep your Claude Code and Codex setup in one private Git repository. Apply it to your Mac and Linux devices.**

Fleet manages rules, settings, skills, commands, hooks, plugins, and MCP servers (tools your agents can call). Change a shared rule once. Send it to every device. Keep device-specific settings separate.

## When to use Fleet

- **One device:** save a repeatable setup with backups and rollback.
- **Several devices:** keep your laptop and servers in sync.
- **Several accounts:** choose an account, with optional fallback when its limit is reached.
- **Shared memory:** optionally use one Claude-Mem database across devices through SSH.

You still work in Claude Code or Codex. Fleet manages their setup. It does not contain a model list; models and reasoning options come from the clients.

This template has **blank configuration**. Add only what Fleet should manage. Keep credentials, chat history, and databases outside Git.

## Start here

1. Click [Use this template](https://github.com/new?template_owner=Bil0000&template_name=agents-fleet&visibility=private). Create a **private** repository. Leave **Include all branches** off. **Do not fork** for personal configuration.
2. Clone your repository. Open it with Claude Code or Codex.
3. Paste:

```text
Set up Fleet using SETUP_WITH_AN_AGENT.md. First verify that origin points to my own private repository, not Bil0000/agents-fleet. Preserve my current setup. Start with one device and local Claude-Mem. Keep secrets and session data out of Git.
```

The [setup guide](SETUP_WITH_AN_AGENT.md) covers backups, installation, checks, and adding devices. Local-only use without a remote is supported with manual apply.

**Needs:** macOS or Linux, Git, Bash, Python 3.11+, and Claude Code and/or Codex. Claude-Mem needs Node.js 20.12+. Automated onboarding also needs both clients and Bun. Linux MCP secrets need `systemd-creds` with non-interactive sudo access.

## How sync works

Edit your private repo → push `main` → GitHub Actions tests macOS and Linux → passing code advances to `live` → devices with updates enabled check every minute and apply it.

Fleet scans hourly for local additions. Updates stop if managed files have local edits: run `fleet scan`, then save those edits in the repo or discard them with `fleet apply`.

Updates come from **your repo**. Public template changes need a separate [engine update](SETUP_WITH_AN_AGENT.md#10-update-the-fleet-engine).

## Commands

Before installation, replace `fleet` with `./bin/fleet` inside the clone. Apply installs `~/.local/bin/fleet`; add `~/.local/bin` to your shell's `PATH`.

Replace uppercase names with your values. `[VALUE]` is optional. `HOST` names a device profile; `PROVIDER` is `claude` or `codex`. Quote labels with spaces.

### Setup and daily use

| Command | What it does |
| --- | --- |
| `fleet check` | Validate config, scripts, paths, and common secret patterns before committing. |
| `fleet host add HOST --os macos --role workstation` | Create a device profile and Claude-Mem plugin entry. Use `linux` for Linux. |
| `fleet onboard --host HOST --memory local` | Run checks/tests, apply config, install account routing, and enable updates. Commit/push the profile and wait for `live` first. Use `shared` for configured shared memory. |
| `fleet apply --dry-run --host HOST` | Preview managed files without applying. |
| `fleet apply --host HOST` | Back up and apply committed config. Requires a clean checkout. |
| `fleet status` | Show release, update service, missing secrets, discoveries, and memory health. |
| `fleet update` | Fetch and apply your tested `live` release now. |
| `fleet update on` / `off` | Start / stop automatic updates. |
| `fleet scan` | Find local additions and managed-file edits. `--json` gives machine-readable output. |
| `fleet adopt KIND NAME --shared` | Import a scanned `skill`, `hook`, `plugin`, or `mcp` for all devices. **Checks, commits, and pushes it.** Requires clean, current `main`. |
| `fleet adopt KIND NAME --host` | Import for this device only; also commits/pushes. `--device HOST` selects its profile. |
| `fleet rollback [BACKUP-ID]` | Restore a local backup; defaults to the latest successful one. Stop updates to keep that state. |

`apply`, `status`, `scan`, `update`, and memory commands accept `--host HOST`; otherwise Fleet resolves the current profile. Edit managed rules/settings in the repo; `adopt` cannot import them.

### Accounts and fallback

Routing sends requests through a local proxy so Fleet can choose the account. To enable it, run `fleet account setup`, open a new terminal, then use the normal provider commands: `codex login` or `claude auth login`. Repeated login refreshes the account. Native logout removes its routed account.

Every command below starts with `fleet account`.

| Command | What it does |
| --- | --- |
| `setup` | Install the verified local CLIProxyAPI router, retry bridge, and login hooks. |
| `setup --check` | Check router and client setup. |
| `setup --restore-clients` | Restore client routing settings; keep accounts and proxy. |
| `list [PROVIDER]` | Show accounts, order, fallback mode, usage, reset times, and recent use. |
| `login PROVIDER [LABEL]` | Log in and enroll an account; optionally name it. |
| `login PROVIDER --existing ACCOUNT` | Enroll credentials already in CLIProxyAPI. |
| `logout PROVIDER [ACCOUNT]` | Remove an enrolled account; defaults to the primary. |
| `rename PROVIDER ACCOUNT LABEL` | Change its display name. |
| `use PROVIDER [ACCOUNT]` | Choose the primary account; opens a picker if omitted. |
| `order PROVIDER` | Show saved order. |
| `order PROVIDER PRIMARY BACKUP...` | Set manual order. Include every enrolled account once, primary first. |
| `order PROVIDER reset` | Sort by soonest weekly reset, after login and during hourly updates. |
| `order PROVIDER manual` | Keep current order; stop automatic sorting. |
| `order PROVIDER rebalance` | Refresh enabled reset ordering now. Omit `PROVIDER` for both providers. |
| `auto PROVIDER on` / `off` | Enable limit-based fallback / use only the primary. |
| `auto PROVIDER on --when any` | Also switch on temporary provider errors; can spend fallback quota. `--when limits` selects limit-only behavior. |
| `auto --help` | Explain fallback modes. |

For example: `fleet account use codex "Work Pro"`. Use accounts you own. Fallback happens before output starts; Fleet never replays partially delivered requests. It returns to the primary when ready.

### Share an account router

Install Tailscale with MagicDNS and run `fleet account setup` on each device first.

| Command | What it does |
| --- | --- |
| `fleet account hub setup` | Share this device's router through Tailscale on port 8317. |
| `fleet account hub join HUB --ssh USER@HOST` | Use the hub's full MagicDNS name. Copy private access keys through SSH and start a local relay. |
| `fleet account hub` | Show role and check connection. |
| `fleet account hub leave` | Return to the local router. |

Client endpoints stay on localhost. Hub account priority changes affect all connected devices.

### Secrets, tools, and memory

Declare secret names in `mcp/required-secrets.txt`. Values stay in macOS Keychain or Linux `systemd-creds`. See [MCP setup](mcp/README.md).

| Command | What it does |
| --- | --- |
| `fleet auth set NAME` | Prompt for and store a declared secret. |
| `fleet auth status [NAME]` | Check one or all secrets without printing values. |
| `fleet mcp run SERVER -- COMMAND...` | Start a declared server with its secrets. Command must match its runner manifest; clients normally call this. |
| `fleet memory status` | Check shared-memory services and health. |
| `fleet memory enable` | Configure and start this device's shared-memory services. |
| `fleet memory disable` | Stop those services; retain the database and settings. |
| `fleet memory backup` | Back up the database on the configured Linux server. |
| `fleet memory harden` | Apply supported worker session/health fixes. Included in `enable`. |

Local Claude-Mem is the default. Memory commands need `memory.toml`: copy [memory.example.toml](memory.example.toml), then follow the [shared-memory steps](SETUP_WITH_AN_AGENT.md#optional-shared-memory).

<details>
<summary>Aliases and internal commands</summary>

These also start with `fleet`; aliases accept the same arguments.

- `enable-updates` / `disable-updates`: `update on` / `off`.
- `account`, `account current`, `account quota`: `account list`.
- `account remove`: `account logout`.
- `account enroll PROVIDER ACCOUNT`: `account login PROVIDER --existing ACCOUNT`.
- `account add`: legacy `account login`; new logins require `FLEET_ENABLE_SUBSCRIPTION_ROUTING=1`.
- `account rebalance [PROVIDER]`: refresh enabled reset ordering.
- `account order --auto PROVIDER reset` / `manual`: older sorting syntax.
- `account hub status`: `account hub`.
- `onboard --os OS --role ROLE`: create the profile before onboarding. Both flags go together; prefer separate `host add` so you can commit first.
- Internal: `account native-auth` wraps provider login/logout; `memory worker` runs the server worker; `memory proxy` maintains the client's SSH tunnel.

</details>

## Configuration map

| Path | Contents |
| --- | --- |
| `rules/`, `settings/` | Shared and client-specific instructions/settings. |
| `skills/`, `commands/`, `hooks/` | Skills, prompts, and hook scripts. |
| `plugins.json`, `mcp/` | Plugin and tool declarations. |
| `hosts/HOST/` | Device-specific settings and paths. |

Fleet rejects unsafe paths, symlinks, invalid config, and common secret formats. Review before committing: checks cannot detect every secret.

[MIT license](LICENSE)
