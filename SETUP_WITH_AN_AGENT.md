# Set up Agents Fleet

This file is an execution contract for the agent that sets up Fleet. Finish every required step. Do not stop after a plan.

## Required outcome

- The user's current Claude Code and Codex setup is preserved.
- The Fleet repository contains only the user's chosen portable configuration.
- Claude-Mem works for every selected client.
- Check, tests, dry run, apply, and status work. Automatic updates work when a remote is configured.
- No secret, OAuth state, session transcript, cache, database, or private machine path is committed.

## Safe defaults

- Use one local device unless the user explicitly asks for multiple devices.
- Use local Claude-Mem unless the user explicitly asks for one shared memory database.
- Keep the repository private when it contains personal rules or machine overlays.
- Add the smallest configuration that reproduces the user's current intent.
- Do not copy third-party skills or plugins into this repository. Declare or install them from their original source.
- Stop only for a login, an OAuth screen, a missing key that only the user can provide, payment, or an irreversible action.

## 1. Check the device

Run read-only checks first:

```bash
uname -s
git --version
python3 --version
node --version
npx --version
command -v claude || true
command -v codex || true
git status --short
git remote -v
```

Fleet supports macOS and Linux. Python must be 3.11 or newer. Claude-Mem requires Node.js 20.12 or newer. Fleet-managed Linux secrets also require `/usr/bin/systemd-creds` and non-interactive `sudo` access to that command.

If this is the public template, fork it or copy it to a repository owned by the user. Use a private repository when personal configuration will be pushed. Local-only users can keep the clone without a remote.

## 2. Back up current state

Create a timestamped backup outside this repository. Preserve files that exist; do not create fake source files.

Inspect and back up these paths as applicable:

```text
~/.agents/
~/.codex/AGENTS.md
~/.codex/config.toml
~/.codex/hooks.json
~/.codex/skills/
~/.claude/CLAUDE.md
~/.claude/settings.json
~/.claude/commands/
~/.claude/hooks/
~/.claude/skills/
~/.claude.json
~/.claude-mem/
```

Never add this backup to Git.

## 3. Create the host profile

Choose a lowercase host ID that uses only letters, numbers, and hyphens. Create `hosts/HOST/host.toml`:

```toml
id = "HOST"
os = "macos"
role = "workstation"
```

Use `os = "linux"` on Linux. Keep device-only settings, hooks, skills, and MCPs under `hosts/HOST/`.

## 4. Add the user's configuration

The files are blank by design:

- Put rules used by both clients in `rules/common.md`.
- Put Claude-only rules in `rules/claude.md`.
- Put Codex-only rules in `rules/codex.md`.
- Put portable client settings in `settings/claude.json` and `settings/codex.toml`.
- Put shared skills in `skills/NAME/SKILL.md`.
- Put shared commands in `commands/`.
- Put shared shell hooks in `hooks/`.
- Put device-only content below `hosts/HOST/`.
- Declare plugins in `plugins.json`.
- Configure MCPs as described in `mcp/README.md`.

Do not move OAuth state, account data, trust decisions, project history, caches, logs, generated files, or databases into Fleet. Do not copy unknown settings. The allowlists in `lib/fleet_apply.py` define which settings Fleet can own.

Run this after each small change:

```bash
./bin/fleet check
```

## 5. Install Claude-Mem

Claude-Mem is required for this setup. Follow its [official installation guide](https://github.com/thedotmack/claude-mem/blob/main/docs/public/installation.mdx). Use its supported installer, not `npm install -g`:

```bash
npx claude-mem install
```

Select every installed client that the user wants, including Claude Code and Codex CLI. Let the user complete provider login or enter a provider key when the installer asks. Do not print or copy the credential.

For the default local mode:

1. Keep `memory.toml` absent.
2. Restart each selected client.
3. Start one short session in each client so its hooks run.
4. Confirm `~/.claude-mem/claude-mem.db` exists.
5. Confirm the worker is healthy by using the installed Claude-Mem status or repair command shown by the installer.

Claude-Mem's official installer detects supported clients, registers hooks, installs its worker, and creates its local data directory. If its version marker is stale, run:

```bash
npx claude-mem repair
```

### Optional shared memory

Use this only when the user asks for one database shared by a macOS client and a Linux server. It uses an SSH tunnel. It does not expose the database or worker to the public internet.

1. Install the same Claude-Mem version on both devices.
2. Back up every existing `~/.claude-mem/claude-mem.db` with SQLite's backup API before any merge or move.
3. Copy `memory.example.toml` to `memory.toml`.
4. Set `server_host`, `client_host`, `ssh_target`, and the installed Claude-Mem `version`.
5. Make sure the macOS client can use key-based SSH to the Linux server.
6. Apply Fleet on the Linux server, then run `~/.local/bin/fleet memory enable --host SERVER_HOST`.
7. Apply Fleet on the macOS client, then run `~/.local/bin/fleet memory enable --host CLIENT_HOST`.
8. Run `~/.local/bin/fleet memory status --host HOST` on both devices.
9. Confirm new observations written from each client appear in the server database.

Do not merge old databases unless the user asks. When a merge is required, work on snapshots, verify schema compatibility and foreign keys, keep both originals, and replace nothing until the merged copy passes checks.

## 6. Configure secrets safely

List each required MCP secret name in `mcp/required-secrets.txt`. Use uppercase names such as `SERVICE_API_KEY`; never put the value in Git.

Store each value on each device:

```bash
./bin/fleet auth set SERVICE_API_KEY
./bin/fleet auth status SERVICE_API_KEY
```

Fleet stores the value in macOS Keychain or as a host-bound `systemd-creds` credential on Linux.

## 7. Verify before apply

Commit the configuration so Fleet can build an exact release. Then run:

```bash
./bin/fleet check
bash tests/test_fleet.sh
./bin/fleet apply --dry-run --host HOST
```

Inspect the dry-run targets. They must stay under the current user's home directory.

## 8. Apply and enable updates

Run:

```bash
./bin/fleet apply --host HOST
~/.local/bin/fleet status --host HOST
```

When the repository has an `origin` remote and GitHub Actions promotes passing commits to `live`, enable automatic updates:

```bash
~/.local/bin/fleet enable-updates
```

For a clone with no remote, use `./bin/fleet apply --host HOST` after local edits and do not enable the updater.

Restart Claude Code and Codex. Confirm:

- `~/.codex/AGENTS.md` has common and Codex rules.
- `~/.claude/CLAUDE.md` has common and Claude rules.
- Managed skills, commands, and hooks resolve to the Fleet release.
- Existing unmanaged configuration remains present.
- Declared plugins and MCPs are available.
- Claude-Mem loads memory in a new session.
- The update service is active when a remote is configured.

## 9. Add more devices

For each device:

1. Clone the user's Fleet repository.
2. Create its own host profile.
3. Install Claude-Mem with `npx claude-mem install`.
4. Store its MCP secrets locally.
5. Run check, dry run, apply, enable updates, and status.

Push user changes to `main`. GitHub Actions checks macOS and Linux, then advances `live`. Devices apply `live` on their next one-minute check.

## 10. Final report

Report only:

- Fleet repository path and remote.
- Device host IDs.
- Local or shared memory mode.
- What Fleet manages and what stayed local.
- Check, test, dry-run, apply, status, update-service, and memory results.
- Any exact login or credential step that still requires the user.
