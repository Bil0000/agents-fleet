# Set up Agents Fleet

This file is an execution contract for the agent that sets up Fleet. Finish every required step. Do not stop after a plan.

## Start from the repository URL

The user can give you only `https://github.com/Bil0000/agents-fleet` and ask for setup. Read this file from that repository first. Then execute it. Do not make the user clone the template or write configuration by hand.

The goal is the same Fleet engine and workflow with the new owner's configuration. The public repo has no personal rules, hooks, skills, host profiles, MCP registrations, accounts, or memory data. Do not try to retrieve the maintainer's private Fleet. The private dashboard is not part of this template.

Inspect the user's current clients and device. Use one device, local memory, account routing with fallback off, and no paid add-ons unless the user requested a different setup. Keep the user's model choices. Reuse working tools and logins. Install missing prerequisites with supported installers. Do not install a second client unless requested.

If the user already supplied a private Fleet repository, use it after the ownership checks below. Otherwise, use an authenticated GitHub CLI to create a private template copy. Choose a free repo name under the user's account, such as `my-agent-fleet`, in a new local directory. Check `gh auth status` and `gh repo view OWNER/NAME` first. A repository that already exists must not be replaced.

```bash
gh repo create OWNER/my-agent-fleet --private --template Bil0000/agents-fleet --clone
cd my-agent-fleet
gh repo view --json nameWithOwner,isPrivate,isFork,defaultBranchRef,url
```

Replace `OWNER` with the authenticated user's login. Do not use `--include-all-branches`. If GitHub login is missing, let the user complete `gh auth login`, then continue. The manual alternative is GitHub's **Use this template** button with private visibility. Local-only setup is available when the user requests it.

Creation is not permission to put secrets in Git. Verify the private destination before importing any personal content. Complete all safe work while a login or key entry is pending. Resume from the last completed step; do not start another repository or repeat completed installs.

## Before anything else: confirm repository ownership

Do not fork `Bil0000/agents-fleet`. A public fork stays public.

The normal setup is a new private repository created with GitHub's **Use this template** button. Use only the default `main` branch from the template. GitHub Actions creates `live` after the user's first passing push.

Before writing any personal content, run:

```bash
git remote get-url origin
git remote -v
git status --short --branch
```

If `origin` points to `Bil0000/agents-fleet`, do not continue in that clone.

- For GitHub sync, create a private repository from the template with the authenticated GitHub CLI, clone it, and continue there. Use the manual template flow only if the CLI is unavailable.
- For a local-only setup, run `git remote remove origin` only after the user confirms they do not want GitHub sync.

For a GitHub setup, verify all of these before continuing:

- The user or their organization owns `origin`.
- The repository is private.
- The default branch is `main`.
- No writable remote points to `Bil0000/agents-fleet`.

Never push personal rules, host profiles, or setup data to the public template.

## Required outcome

- The user's current Claude Code and Codex setup is preserved.
- The Fleet repository contains only the user's chosen portable configuration.
- Claude-Mem works for every selected client.
- Check, tests, dry run, apply, and status work. Automatic updates work when a remote is configured.
- No secret, OAuth state, session transcript, cache, database, or private machine path is committed.

## Safe defaults

- Use one local device unless the user explicitly asks for multiple devices.
- Use local Claude-Mem unless the user explicitly asks for one shared memory database.
- Use a private repository owned by the user. A local clone with no remote is also supported.
- Install local account routing for the standard setup. Leave automatic account fallback off until the user adds their own fallback account. Keep paid credit use off unless requested.
- Import the user's existing portable rules, allowed settings, chosen MCPs, and their own skills. Show a short inventory before applying. Retain blank defaults where the user has no configuration.
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
command -v gh || true
git status --short
git remote -v
```

Fleet supports macOS and Linux. Python must be 3.11 or newer. Claude-Mem requires Node.js 20.12 or newer. Fleet-managed Linux secrets also require `/usr/bin/systemd-creds` and non-interactive `sudo` access to that command.

If the ownership checks above did not pass, stop here. Do not solve an ownership problem by pushing to a different branch of the public template.

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

Choose a lowercase host ID that uses only letters, numbers, and hyphens. Use `fleet host add` when both clients and managed plugin installation are wanted. For a single-client setup, create `hosts/HOST/host.toml` directly and declare plugins only for installed clients:

```toml
id = "HOST"
os = "macos"
role = "workstation"
```

Use `os = "linux"` on Linux. Keep device-only settings, hooks, skills, and MCPs under `hosts/HOST/`.

## 4. Add the user's configuration

Read the backed-up setup. Import portable rules and supported settings without changing their meaning. Put device paths only in the host profile. Keep third-party skills and plugins tied to their original sources. Recreate chosen MCP registrations in both manifests using the examples in `mcp/README.md`; keep their secret values in local credential storage. If no configuration exists, keep the shared files blank and finish the basic install.

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

Select every installed client that the user wants, including Claude Code and Codex CLI. Check the installer's current help and official guide before running it. Keep an existing working memory provider. For a new install, use the user's existing Claude plan when available; do not accept a paid provider or trial without permission. Let the user complete provider login or enter a provider key when needed. Do not print or copy the credential.

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

Use this only when the user asks for one database shared by macOS or Linux clients and a Linux server. It uses an SSH tunnel. It does not expose the database or worker to the public internet.

1. Install the supported Claude-Mem 13.24.0 bundle on the server and all clients. Fleet's shared-memory patches target this version; an unpinned latest install is not a substitute. Verify the installed version before proceeding. Run `fleet account setup` on the server before enabling shared memory.
2. Back up every existing `~/.claude-mem/claude-mem.db` with SQLite's backup API before any merge or move.
3. Copy `memory.example.toml` to `memory.toml`.
4. Set `server_host`, `client_hosts`, `ssh_target`, and the installed Claude-Mem `version`.
5. Make sure each client can use key-based SSH to the Linux server.
6. Apply Fleet on the Linux server, then run `~/.local/bin/fleet memory enable --host SERVER_HOST`.
7. Apply Fleet on each client, then run `~/.local/bin/fleet memory enable --host CLIENT_HOST`.
8. Run `~/.local/bin/fleet memory status --host HOST` on all devices.
9. Confirm new observations written from each client appear in the server database.

Shared memory keeps captures in a local queue while the worker is unavailable. `fleet memory status` reports pending work. It does not send a model request. Backups are verified before compression; `backup_retention_days` controls how many daily backups remain. The worker uses the local Fleet router and can fall back from Claude to `gpt-6.1-terra` when its allowance is unavailable. This applies only to shared memory; client model choices stay separate.

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

Run `fleet check` and inspect the diff for secrets first. Commit only the chosen configuration so Fleet can build an exact release. Then run:

```bash
./bin/fleet check
bash tests/test_fleet.sh
./bin/fleet apply --dry-run --host HOST
```

Inspect the dry-run targets. They must stay under the current user's home directory.

## 8. Push, apply, and enable updates

When the user has a private GitHub repository, push `main`:

```bash
git push origin main
```

Wait for the repository's **Check** workflow to pass on macOS and Linux. It must create or update `live`. Then verify the two remote branches match:

```bash
git fetch origin main live
test "$(git rev-parse origin/main)" = "$(git rev-parse origin/live)"
```

Do not enable updates if the workflow failed or `live` does not match `main`.

Apply the tested commit:

```bash
./bin/fleet apply --host HOST
~/.local/bin/fleet status --host HOST
```

The apply installs `~/.local/bin/fleet`. Make sure the user's shell includes `~/.local/bin` in `PATH`. Preserve the startup file, do not add the path twice, and do not use sudo. Start a fresh login shell in `/tmp` and confirm that `fleet status` works.

For the standard setup, run `fleet account setup` once on this device. Skip it only when the user requested native client routing. It must finish with `fleet account setup --check` passing. Then open a new terminal and let the user add accounts with the normal `codex login` or `claude auth login` flow. Do not add OAuth files to the repository. Keep the proxy on localhost. Use `fleet account auto PROVIDER on` only after the user has added a fallback account.

Install Go 1.26 or newer and a working C compiler before the patched router step. On macOS, check that the Command Line Tools compiler works. On Linux, use the distribution's C build tools. Then build and activate Fleet's tested router version:

```bash
python3 -B lib/fleet_cliproxy_update.py --bootstrap
fleet account setup --check
```

This uses version 7.2.151 and runs its Go and local proxy tests before activation. It keeps a newer installed version. It does not fetch an untested major release. The updater blocks major-version changes until a configuration migration is verified. Do not bypass that check to finish setup.

Install the proxy updater after bootstrap:

```bash
python3 -B lib/fleet_cliproxy_update.py --install
```

It checks upstream every six hours. A blocked major update keeps the installed tested router in use. Report that state separately from a failed Fleet install. Confirm credit routing and session-affinity support through the local router's read-only management config before enabling advanced account controls. Never use model requests for health checks.

Enable automatic updates:

```bash
~/.local/bin/fleet update on
```

For a clone with no remote, use `./bin/fleet apply --host HOST` after local checks. Do not push or enable the updater.

Restart each selected client. Confirm only the files for installed clients:

- `~/.codex/AGENTS.md` has common and Codex rules.
- `~/.claude/CLAUDE.md` has common and Claude rules.
- Managed skills, commands, and hooks resolve to the Fleet release.
- Existing unmanaged configuration remains present.
- Native Claude and Codex commands still work, and the router uses only the user's own enrolled accounts. Credit use stays off unless requested.
- Declared plugins and MCPs are available.
- Claude-Mem loads memory in a new session.
- The update service is active when a remote is configured.

## 9. Add more devices

Add devices only when the user asks. Use their own host IDs and SSH targets. Do not invent or copy the maintainer's server. Shared-memory mode needs the supported pinned bundle on every device; keep local mode if those prerequisites are absent.

For each device:

1. Clone the user's Fleet repository.
2. Create its own host profile.
3. Install Claude-Mem with `npx claude-mem install`.
4. Store its MCP secrets locally.
5. Run check, dry run, apply, enable updates, and status.

Push user changes to their repository's `main` branch. GitHub Actions checks macOS and Linux, then advances that repository's `live` branch. Devices apply `origin/live` on their next one-minute check.

## 10. Update the Fleet engine

A repository created from this template is independent. New versions of `Bil0000/agents-fleet` do not enter the user's repository automatically.

When the user asks for a template update:

1. Compare the public template with the user's current Fleet.
2. Back up the user's Fleet and managed client files.
3. Move only the engine and documentation changes that the user wants.
4. Preserve all user rules, hosts, skills, hooks, plugins, MCPs, secrets, and memory data.
5. Run check, tests, dry run, GitHub Actions, apply, and status again.

## 11. Final report

Report only:

- Fleet repository path and remote.
- Device host IDs.
- Local or shared memory mode.
- What Fleet manages and what stayed local.
- Check, test, dry-run, apply, status, update-service, and memory results.
- Any exact login or credential step that still requires the user.
