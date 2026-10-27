# Set up Agents Fleet

This file is the execution contract for an agent that sets up Fleet. Finish every required step. Do not stop after a plan, a clone, or a summary.

## Start from the repository URL

The user can give you only `https://github.com/Bil0000/agents-fleet` and ask for setup. Read this file from that repository. Then do the work. The user must not have to clone the template or write configuration by hand.

When you finish, the user has:

- A private Fleet repository that they own.
- The same Fleet engine and workflow as this template: check, test, apply, backup, rollback, tested `live` releases, automatic updates, MCP launcher and secrets, account router, and optional shared memory.
- Their own rules, settings, skills, commands, hooks, plugins, MCP servers, device profiles, accounts, and memory data. Nothing personal comes from this template.

### Same engine, different content and infrastructure

The public template is the engine with blank configuration. Its rules, settings, hooks, skills, commands, plugins, MCP declarations, and host profiles are empty on purpose. Never try to get the maintainer's private Fleet, rules, skills, accounts, hosts, or data.

Some things are not the same and must not be presented as the same:

- **Content.** The user's rules, skills, and MCP servers replace the maintainer's. Behavior follows the user's content.
- **Infrastructure.** The maintainer runs shared memory on a Linux server that clients reach over SSH. Each client keeps its own capture queue. To get that topology, the user needs their own always-on Linux server, their own SSH access, and their own private network. This guide does not supply hardware or a network.
- **One-device mode.** The default local mode uses one Claude-Mem database on one device. It is supported, but it is a different topology. Memory is not shared across devices.
- **Dashboard.** The maintainer's private dashboard is not part of this template. Do not promise it.

### Defaults

Use these defaults unless the user asked for something else:

- One device, the current one.
- Local Claude-Mem.
- Account routing installed, automatic fallback off, paid credit use off.
- No paid add-ons, trials, or new subscriptions.
- The user's current clients and model choices stay as they are. Fleet has no model list.
- Reuse working tools and logins. Install a missing prerequisite with its supported installer. Do not install a second client unless the user asks.

Stop and ask only for: a login or OAuth screen, a secret that only the user can type, payment, or an action that can lose data. Do all other safe work while you wait. After a pause, continue from the last completed step. Do not create a second repository or repeat a completed install.

## Before anything else: confirm repository ownership

**Do not fork** `Bil0000/agents-fleet`. A fork of a public repository stays public. Never push personal rules, host profiles, or setup data to the public template.

### Create the private copy

If the user already gave you their own private Fleet repository, clone it and skip to the checks below.

Otherwise create a private copy from the template with an authenticated GitHub CLI:

```bash
gh auth status
gh repo view OWNER/my-agent-fleet
```

`OWNER` is the authenticated user's login. The second command must fail with "not found". If the name exists, pick a different free name. Never replace or reuse an existing repository.

If `gh` is missing, install it with the platform's supported installer. If it is not logged in, ask the user to run `gh auth login`, then continue.

Run this in a new, empty parent directory:

```bash
gh repo create OWNER/my-agent-fleet --private --template Bil0000/agents-fleet --clone
cd my-agent-fleet
gh repo view --json nameWithOwner,isPrivate,isFork,defaultBranchRef,url
```

Do not add `--include-all-branches`. The template's `live` branch must not be copied. The user's first passing push creates their own `live`.

The manual alternative is GitHub's **Use this template** button with **Private** visibility and **Include all branches** off. Then clone the new repository.

Local-only setup (no GitHub) is supported only when the user asks for it. It has no CI and no automatic updates.

### Check the clone

Run these before you write any personal content:

```bash
git remote get-url origin
git remote -v
git status --short --branch
```

If `origin` points to `Bil0000/agents-fleet`, stop using that clone. Create the private copy above and work there. For a local-only setup, run `git remote remove origin` only after the user confirms they do not want GitHub sync.

For a GitHub setup, all of these must be true:

- The user or their organization owns `origin`.
- `isPrivate` is `true` and `isFork` is `false`.
- The default branch is `main`, and you are on `main`.
- No remote points to `Bil0000/agents-fleet`.

## Required outcome

- The user's existing Claude Code and Codex setup is backed up and still works.
- The repository contains only the user's chosen portable configuration.
- Every change is committed. For GitHub sync, GitHub Actions passed, and `live` equals `main`.
- `fleet apply` installed that exact commit. The final status fields pass; local-only mode has the expected nonzero exit because updates stay off.
- Account routing passes `fleet account setup --check`.
- Claude-Mem captures memory in each selected client.
- No secret, OAuth file, session transcript, cache, or database is in Git. Device paths stay under `hosts/`.

## 1. Check the device

Run read-only checks first:

```bash
uname -s
uname -m
hostname -s
echo "$SHELL"
git --version
python3 --version
/usr/bin/python3 --version
node --version
command -v claude codex gh bun go cc || true
```

Requirements, from the source:

| Need | Why |
| --- | --- |
| macOS or Linux, arm64 or x86_64 | `fleet` and the account router support only these. |
| Python 3.11+ at `/usr/local/bin/python3`, `/opt/homebrew/bin/python3`, or `/usr/bin/python3` | `bin/fleet` searches only those paths. |
| Git at `/usr/bin/git` | Apply, update, and adopt call that path. |
| bash or zsh as the login shell | `fleet account setup` edits `~/.zshrc` or `~/.bashrc`. |
| Claude Code, Codex, or both | Use the existing clients. The manual path supports one client. |
| Node.js 20.12+ | Claude-Mem installer and `fleet onboard`. |
| Go 1.26+ and a working `cc` | Building the tested account router. |
| `gh` with login | Private repository and CI checks. |
| Linux only: `/usr/bin/systemd-creds` with non-interactive `sudo` for it | Storing MCP secrets on Linux. |
| Linux only: a user systemd session | Update timer, router, and memory services. |
| Bun | Claude-Mem's worker runtime. Its installer can install Bun. |
| Shared memory only: `ssh` | The client tunnel to the memory server. |

Install missing tools with their supported installers. On macOS, make sure `xcode-select -p` works so `cc` exists. On Linux, use the distribution's build tools package. Do not use `sudo` for Fleet itself.

### Choose clients

Ask which clients the user wants only if it is not clear from the device. Record the result:

- **Both clients:** the automated `fleet onboard` path works.
- **One client:** use the manual path. `fleet onboard` refuses to run unless `bash`, `git`, `node`, `bun`, `claude`, and `codex` are all present.

## 2. Back up current state

Create a timestamped backup directory outside the repository, for example under `~/fleet-setup-backup-DATE/`. Copy only paths that exist. Do not create empty placeholders. Never add this backup to Git.

```text
~/.agents/
~/.codex/AGENTS.md
~/.codex/config.toml
~/.codex/hooks.json
~/.codex/skills/
~/.codex/prompts/
~/.claude/CLAUDE.md
~/.claude/settings.json
~/.claude/commands/
~/.claude/hooks/
~/.claude/skills/
~/.claude.json
~/.claude-mem/settings.json
~/.zshrc or ~/.bashrc
```

For an existing `~/.claude-mem/claude-mem.db`, do not copy the file while it is in use. Leave it in place for local mode. For shared memory, see [Optional shared memory](#optional-shared-memory).

Fleet also makes its own backup on every apply. `fleet rollback` restores it.

## 3. Create the host profile

The host ID must equal this command's output:

```bash
python3 -c 'import socket; print(socket.gethostname().split(".", 1)[0].lower().replace("_", "-"))'
```

The automatic updater, MCP launcher, and `fleet status` find the profile from the hostname. A different ID breaks them. If the output has characters other than lowercase letters, numbers, and hyphens, stop and ask the user to rename the device.

```bash
./bin/fleet host add HOST --os macos --role workstation
```

Use `--os linux` on Linux. The role is a free label, such as `workstation` or `server`.

This creates exactly two files:

- `hosts/HOST/host.toml` with the keys `id`, `os`, and `role` only. `fleet check` rejects any other key.
- `hosts/HOST/plugins.json`, which declares the Claude-Mem Codex plugin.

That `plugins.json` adds the Codex marketplace `claude-mem-local` from `thedotmack/claude-mem`. The Claude-Mem installer registers the same marketplace name from a local folder. If both point to different sources, `fleet apply` fails with "marketplace source differs". After Claude-Mem is installed (step 5), make the host file match the installer:

```json
{
  "codex": {
    "marketplaces": {
      "claude-mem-local": "$HOME/.claude/plugins/marketplaces/thedotmack"
    },
    "plugins": ["claude-mem@claude-mem-local"]
  },
  "claude": {
    "marketplaces": {},
    "plugins": []
  }
}
```

Use that file only on devices with Codex. **Claude Code only:** set both `codex` lists empty in `hosts/HOST/plugins.json`. Otherwise apply looks for the `codex` binary and fails.

## 4. Import the user's configuration

Read the backup. Show a short inventory, then import the user's existing portable configuration. Keep unknown or unsupported settings local. Honor any exclusions the user gave you.

| Put this | Here |
| --- | --- |
| Rules for both clients | `rules/common.md` |
| Claude-only rules | `rules/claude.md` |
| Codex-only rules | `rules/codex.md` |
| Rules for one device | `hosts/HOST/rules.md` |
| Claude settings | `settings/claude.json` or `hosts/HOST/claude.json` |
| Codex settings | `settings/codex.toml` or `hosts/HOST/codex.toml` |
| The user's own skills | `skills/NAME/` or `hosts/HOST/skills/NAME/` |
| Prompts and slash commands | `commands/NAME.md` |
| Hook scripts | `hooks/NAME.sh` or `hosts/HOST/hooks/NAME.sh` |
| Plugins | `plugins.json` or `hosts/HOST/plugins.json` |
| MCP servers | `mcp/`, as shown in [mcp/README.md](mcp/README.md) |

Rules:

- Fleet owns only allowlisted settings keys. The lists are at the top of `lib/fleet_apply.py`. `fleet check` rejects other keys. Leave other settings in the client files; apply keeps them.
- Absolute home-directory paths (under the macOS `Users` or Linux `home` folder) are allowed only below `hosts/`. Elsewhere, use `$HOME` or `~`.
- Do not copy third-party skills or plugins into the repository. Declare plugins by their GitHub `owner/repo` source.
- Put portable plugin declarations in root `plugins.json`. Put local marketplace paths and device-only plugins in `hosts/HOST/plugins.json`. The public template keeps its plugin lists empty until setup.
- Every MCP server must exist in both `mcp/codex.toml` and `mcp/claude.json`. Secret names go in `mcp/required-secrets.txt`; values never go in Git.
- Never move OAuth state, account files, trust decisions, chat history, caches, logs, databases, or generated files into Fleet.

If the user has no configuration, keep the blank files and finish the install.

Run this after each change:

```bash
./bin/fleet check
```

## 5. Install Claude-Mem

Claude-Mem gives each client persistent memory. Fleet does not run its installer. Use the official installer, never `npm install -g claude-mem`. Read the official [installation guide](https://github.com/thedotmack/claude-mem/blob/main/docs/public/installation.mdx) first.

Keep an existing working Claude-Mem install in local mode. For a new install, use the tested version and run one command per selected client:

```bash
npx claude-mem@13.24.0 install --ide claude-code --provider claude --runtime worker
npx claude-mem@13.24.0 install --ide codex-cli --provider claude --runtime worker
```

`--runtime worker` selects the SQLite worker used by Fleet. `--provider claude` uses the user's own Claude plan and skips the hosted provider sign-in. A non-interactive run needs an explicit provider. Do not choose a paid provider, trial, or sign-in without the user's permission.

Shared memory requires Claude-Mem `13.24.0` on every device. Fleet's worker patches target that bundle. Check an existing install before changing it; back it up before any replacement. Do not substitute a newer package to complete shared-memory setup.

Then check it:

```bash
npx claude-mem@13.24.0 status
npx claude-mem@13.24.0 doctor
```

If the installer reports a stale version marker, run `npx claude-mem@13.24.0 repair`.

Now update `hosts/HOST/plugins.json` as shown in step 3.

### Local mode (default)

1. Keep `memory.toml` absent.
2. Restart each selected client.
3. Start one short session in each client.
4. Confirm `~/.claude-mem/claude-mem.db` exists and `npx claude-mem@13.24.0 status` shows a running worker.
5. Start a second session and confirm memory context loads.

`fleet status` reports `memory: local (plugin-managed)` in this mode.

### Optional shared memory

Use this when the user asks for one memory database across devices or names their own Linux memory server. It reproduces the shared-memory workflow on the user's own hardware. Prepare the configuration now; start the services only after step 8 has applied the tested release and set up the server's account router.

- One Linux **server** runs the Claude-Mem worker, the database, and daily verified backups.
- Each **client** (macOS or Linux) keeps a local capture queue and reaches the server through an SSH tunnel. Captures wait in the queue while the server is offline.
- The memory worker binds to `127.0.0.1`. Clients reach it through SSH; the memory port is not public.

The user must supply:

- An always-on Linux server with a user systemd session.
- Key-based SSH from every client to that server. `BatchMode=yes` is used, so no password prompts.
- Their own private networking between devices, for example a VPN or tailnet.
- Fleet applied on every device with its own host profile.

Steps:

1. Install Claude-Mem `13.24.0` on the server and every client, as above.
2. On the server, run `fleet account setup` and the router bootstrap (step 8). The worker sends memory requests through the server's local router.
3. Back up every existing `~/.claude-mem/claude-mem.db` with SQLite's backup API before any merge or move. Do not merge databases unless the user asks. A merge works on copies, checks schema and foreign keys, and keeps both originals.
4. Copy `memory.example.toml` to `memory.toml`. Set `server_host`, `client_hosts`, `ssh_target`, and `version`. Keep `worker_host = "127.0.0.1"`. `ssh_target` must be `user@host`.
5. Commit `memory.toml` in the user's private Fleet repository. It contains host IDs, ports, and an SSH target, never keys or database contents. Complete sections 6–8 using the manual path for shared memory, then update every device to that tested `live` commit. Fleet includes this file in the installed release.
6. On the server: `~/.local/bin/fleet memory enable --host SERVER_HOST`.
7. On each client: `~/.local/bin/fleet memory enable --host CLIENT_HOST`.
8. On every device: `~/.local/bin/fleet memory status --host HOST`. It must exit 0.
9. Start a session on each client. Confirm its new memory reaches the server database.

`fleet memory status` does not send a model request. On the server, `fleet memory backup` writes a verified, compressed snapshot; `backup_retention_days` sets how many days to keep.

The server worker sends memory requests through the local Fleet router. If the Claude allowance is not available, it retries with the Codex model `gpt-6.1-terra`. This model name is in `lib/fleet_memory_runtime.cjs`. It affects only the shared-memory worker. Client model choices do not change.

## 6. Configure secrets safely

List each MCP secret name in `mcp/required-secrets.txt`, one per line, sorted, uppercase, such as `SERVICE_API_KEY`. Then store each value on each device:

```bash
./bin/fleet auth set SERVICE_API_KEY
./bin/fleet auth status SERVICE_API_KEY
```

The user types the value at the prompt. Never print, log, or commit it. Fleet stores it in macOS Keychain or as a host-bound `systemd-creds` credential on Linux.

## 7. Commit and verify

Check the diff for secrets and machine paths outside `hosts/`. Then commit only the chosen configuration on `main`:

```bash
git status --short
git diff --stat
./bin/fleet check
bash tests/test_fleet.sh
git add -A
git commit -m "Add my Fleet configuration"
./bin/fleet apply --dry-run --host HOST
```

`fleet apply` needs a clean checkout. The dry run lists every target. All targets must be under the user's home directory.

## 8. Push, apply, and enable updates

### Push and wait for CI

For a local-only repository, skip this subsection. Use the manual apply path and leave automatic updates off. Your local checks replace CI; do not claim that `live` was checked.

```bash
git push origin main
gh run list --workflow Check --branch main --limit 1
git fetch origin main live
test "$(git rev-parse origin/main)" = "$(git rev-parse origin/live)"
```

Use the host's PR or build watcher when one is available. Otherwise read the exact run's result with `gh run view RUN_ID`; a queued or running job is pending, not a pass.

The **Check** workflow runs `fleet check`, builds and tests the patched router, and runs all tests on macOS and Linux. Only a passing push to `main` moves `live`. If it fails, fix the cause and push again. Do not enable updates while `live` is behind `main`.

### Option A: both clients, GitHub sync, automated

```bash
./bin/fleet onboard --host HOST --memory local
```

This runs check, tests, dry run, apply, `fleet account setup`, `fleet update on`, one update, and `fleet auth status`. For [shared memory](#optional-shared-memory), use the manual path below so the server's router is bootstrapped before you enable its memory worker. The command also supports `--memory shared` once those prerequisites are ready.

### Option B: manual (any client set)

```bash
./bin/fleet apply --host HOST
~/.local/bin/fleet account setup
```

### Launcher and PATH

Apply installs `~/.local/bin/fleet`, `~/.local/bin/fleet-codex`, and `~/.local/bin/fleet-claude`. Make sure the login shell has `~/.local/bin` on `PATH`. Add one line to `~/.zshrc` or `~/.bashrc` only if it is missing. Do not add it twice. Do not use `sudo`.

`fleet account setup` also adds one line to that file. The line loads `fleet-shell`, which sends `codex login`, `codex logout`, `claude auth login`, and `claude auth logout` through Fleet. All other client commands run unchanged.

Open a fresh login shell in `/tmp` and confirm `fleet` resolves to the installed launcher. Run the final status check after the memory steps below.

### Account router

`fleet account setup` installs the local CLIProxyAPI router, a retry bridge, and the shell hook. The router listens on `127.0.0.1:8317`. Then build and activate the tested, patched router:

```bash
python3 -B lib/fleet_cliproxy_update.py --bootstrap
fleet account setup --check
```

The bootstrap clones CLIProxyAPI `7.2.151`, applies Fleet's patches from `patches/`, runs the Go tests and Fleet's failover test, and activates it only if they pass. It keeps a newer installed version. It needs Go 1.26+ and `cc`.

Then install the router updater:

```bash
python3 -B lib/fleet_cliproxy_update.py --install
```

It checks upstream every six hours. A new major version is blocked until a configuration migration is verified. A blocked update keeps the tested router running. Report that separately; it is not a failed Fleet install. Never bypass the block.

Accounts:

- The user adds accounts in a new terminal with the normal `codex login` or `claude auth login`. Do not enter their credentials.
- OAuth files stay in the router's private directory, never in Git.
- Keep automatic fallback off. Turn it on with `fleet account auto PROVIDER on` only after the user has enrolled a second account.
- Paid credits stay off unless the user asks. `fleet account credits PROVIDER never` stops before credits.
- Never send a model request to test health. Use `fleet account setup --check` and `fleet account list`.

To keep native client routing instead, skip `fleet account setup` and the bootstrap. Undo client routing later with `fleet account setup --restore-clients`.

### Finish memory and automatic updates

For shared memory, now complete the server and client enable steps in [Optional shared memory](#optional-shared-memory). Until those steps finish, a shared-memory error in `fleet status` is a pending setup step.

For GitHub sync, enable updates after the tested release and router are ready:

```bash
~/.local/bin/fleet update on
```

For local-only use, leave updates off. Apply each new clean, committed change with `./bin/fleet apply --host HOST` after local checks.

### Optional account hub

Use this only when the user asks for one router shared by several devices. It needs Tailscale with MagicDNS on each device and `fleet account setup` on each device first.

```bash
fleet account hub setup
fleet account hub join HUB_MAGICDNS_NAME --ssh USER@HUB
fleet account hub
```

Run `hub setup` on the device that keeps the accounts. Run `hub join` on the others. `fleet account hub leave` returns a device to its local router.

### Prove it works

Run each check on each device and record the result:

```bash
fleet check
fleet status --host HOST
fleet account setup --check
fleet account list
fleet auth status
fleet apply --dry-run --host HOST
```

With GitHub sync, `fleet status` must exit 0 and show `updater: active`. Local-only use leaves updates off, so exit 1 is expected even when the configuration is healthy. In either mode, verify `applied` equals `repo`, `dirty: no`, `drift: no`, `discoveries: 0`, no missing or skipped MCP secrets, and a healthy memory line. In local-only mode, accept exit 1 only when the updater being off is the sole unmet check.

Then confirm by hand, only for installed clients:

- `~/.codex/AGENTS.md` contains the common and Codex rules.
- `~/.claude/CLAUDE.md` contains the common and Claude rules.
- Managed skills, commands, and hooks are links into `~/.local/share/fleet/current/`.
- Settings that Fleet does not own are still present.
- `claude` and `codex` start normally. Their model pickers are unchanged.
- Declared plugins and MCP servers are listed by the clients.
- Claude-Mem loads memory in a new session.

A login that only the user can do is a pending step, not a pass. Report it as pending.

### Daily use, updates, and rollback

- Change configuration in the repository, commit, and push `main`. CI tests it and moves `live`. Every device with updates on checks `live` every minute and applies it.
- `fleet update` applies the latest `live` now. It needs a clean `main` checkout and a fast-forward.
- If a device has local edits to managed files, updates stop. Run `fleet scan`. Then save the edits in the repository, or run `fleet apply` to discard them.
- `fleet adopt KIND NAME --shared` or `--host` imports a scanned skill, hook, plugin, or MCP, then commits and pushes it.
- `fleet rollback` restores the latest successful local backup. `fleet rollback BACKUP-ID` restores a specific one. Backups are in `~/.local/state/fleet/backups/`. Run `fleet update off` first if the device must stay on the old state; otherwise the next update applies `live` again.

## 9. Add more devices

Add devices only when the user asks. Use the user's own devices, host IDs, and SSH targets. Never copy the maintainer's.

For each device:

1. Clone the user's private Fleet repository.
2. Run step 1 checks.
3. Create its host profile on that device (step 3), commit, push, and wait for CI.
4. Install Claude-Mem for its selected clients (step 5).
5. Store its MCP secrets (step 6).
6. Apply, account setup, router bootstrap, and updates (step 8).
7. Run [Prove it works](#prove-it-works) on that device.

Each device must pass its own proofs. A pass on one device is not proof for another.

## 10. Update the Fleet engine

A repository created from a template is independent. New versions of `Bil0000/agents-fleet` do not reach it on their own. When the user asks for an engine update:

1. Compare the public template with the user's Fleet.
2. Back up the user's Fleet and managed client files.
3. Copy only the engine and documentation changes the user wants: `bin/`, `lib/`, `patches/`, `system/`, `tests/`, `.github/`, and docs.
4. Keep every user rule, setting, host, skill, hook, command, plugin, MCP, and secret name.
5. Run check, tests, dry run, CI, apply, and status again.

## 11. Final report

Report only:

- Repository URL, visibility, and local path.
- Host IDs and which clients each one has.
- Memory mode: local, or shared with the server host.
- Router state: setup check result, router version, fallback and credit settings.
- What Fleet now manages, and what stayed local.
- Results of check, tests, CI, apply, status, update service, and memory.
- Any blocked step with its exact cause, such as a pending login or a blocked router major update.
