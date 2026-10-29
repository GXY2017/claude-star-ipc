# claude-star-ipc

Multi-terminal IPC for Claude Code / Codex agent fleets: a small control-plane
runtime that lets several AI coding agents, each in its own terminal, cooperate
on the same project through a shared mailbox.

## What it is, in current agent-engineering terms

- **Control-plane orchestration (governance-first).** A hub agent decomposes
  work and dispatches briefs to worker agents; agents never call each other
  directly. All traffic goes through a SQLite-backed mailbox with roles,
  blocking receive, task-cycle resets and crash recovery — coordination and
  audit sit in one place instead of inside framework code.
- **Heterogeneous agents, policy-based model routing.** Each role runs the CLI
  and model configured for it (Claude Code, Codex, or other vendor CLIs).
  Route judgment-heavy roles to a frontier model and mechanical work to
  cheaper tiers by editing the launch config, not application code.
- **Quota-aware dispatch.** A pre-dispatch snapshot reads each vendor's
  remaining usage window and returns an OK / THROTTLE / HOLD verdict per role,
  so a brief is sized to the capacity that is actually left — cost and quota
  are inputs to routing, not an afterthought.
- **State as data, supervisor out of the loop.** With the state integrations
  configured (see Install), model, context use, rate limits and prompt-cache
  warmth are read from local files where available, and
  an out-of-session supervisor process runs the reply clock without a model in
  it. Mechanical events are handled and logged automatically; the hub agent
  spends turns only on real decisions.
- **Briefs as context packaging.** A dispatch is a markdown brief
  (goal, constraints, done-criteria), linted by `ipc_brief_lint.py`. The brief
  is the worker's context package — the same discipline as sub-agent context
  isolation, applied across processes.
- **Human-in-the-loop by default.** Workers run inside the CLI harness's normal
  permission model; high-stakes actions still ask the human. The runtime moves
  messages, never authority.
- **Built for recovery, not just the happy path.** Pane probing and waking,
  role re-claiming after window loss, and a whole-fleet restart path. The
  lifecycle is covered by a three-state protocol test
  (`tests/ipc_three_state_full_test.py`).

## Architecture

```
              one project directory, several WezTerm panes

   ┌─────────┐   brief    ┌──────────────┐    brief / reply
   │ hub (A) │──────────▶│   mailbox    │◀──────────────▶ worker (B) Claude Code
   │ agent   │◀──────────│   (SQLite)   │◀──────────────▶ worker (C) Codex CLI
   └─────────┘   reply    └──────────────┘◀──────────────▶ worker (D...F) ...
        │                        │
        └─ recv --block, collect ─┘   stall watch / pane probe / role recover
```

## Components

| File | Role |
|---|---|
| `ipc.py` | Neutral SQLite mailbox: `send`, `recv --block`, watch/clock, roles |
| `ipc_state.py` | Available per-role state from configured integrations: model, context use, rate limits, cache |
| `ipc_quota.py` | Pre-dispatch quota snapshot per vendor; OK / THROTTLE / HOLD verdicts |
| `ipc_supervisor.py` | Out-of-session supervisor: runs the reply clock with no model in the loop |
| `install_user.py`, `migrate_ipc.py` | User-level install and migration |
| `ipc_wezterm_launch.ps1`, `mux-fleet*.lua` | Launch a whole fleet into panes, auto-submit recovery |
| `ipc_dispatch.ps1`, `ipc_newcycle.ps1` | Dispatch a brief, reset the task cycle |
| `ipc_brief_lint.py`, `ipc_estimate.py` | Brief quality gate and size estimate |
| `ipc_pane_probe.py`, `ipc_wake_pane.ps1` | Keep the fleet alive |
| `skills/multi-terminal-ipc/` | Operating guide for the agents themselves |

## Install

Requires Windows, Python 3.8+, Git, PowerShell 7, WezTerm nightly, and an
authenticated Claude Code CLI. Install and authenticate any other worker CLIs
you plan to use.

```powershell
git clone https://github.com/GXY2017/claude-star-ipc.git
Set-Location claude-star-ipc
python install_user.py
```

In `~/.claude/ipc/ipc_wezterm_launch.ps1`, configure `$LaunchCmd` for each role's
CLI and model, and `$Roles` for the default fleet. Pass `-Roles` at launch to
choose a different fleet.

File-based state monitoring needs additional integrations that this repository
does not ship or install: Claude-family roles expect a status-line hook
(statusline.js) to write `~/.claude/ipc/state/<ROLE>.json`; Codex roles need
their thread IDs registered so `ipc_state.py` can locate their rollout files
(the author's setup uses `codex-compat.js`). See the input contract in
[ipc_state.py](ipc_state.py). Without these integrations, per-role file-based
telemetry is unavailable; installing the mailbox alone does not provide it.

## Use

Enable IPC in your project:

```powershell
Set-Location "D:/path/to/project"
python "$HOME/.claude/ipc/ipc_role.py" enable
```

Launch the whole fleet in that project, using your configured role list:

```powershell
pwsh "$HOME/.claude/ipc/ipc_wezterm_launch.ps1" -Project "D:/path/to/project" -Roles A,B,C,D,E,F
```

The launcher starts each pane and submits its recovery command automatically.
No manual input is needed in the worker windows. An AI session can run the
launcher and control the panes through WezTerm.

In hub A, prepare `task.md` using the
[brief format](skills/multi-terminal-ipc/references/orchestration-and-dispatch.md),
then dispatch it and read replies:

```powershell
pwsh "$HOME/.claude/ipc/ipc_dispatch.ps1" -Role B -BodyFile task.md
python "$HOME/.claude/ipc/ipc.py" recv --me A --block
```

Start a fresh task cycle:

```powershell
pwsh "$HOME/.claude/ipc/ipc_newcycle.ps1"
```

For dispatch, replies, and recovery, see the
[operating guide](skills/multi-terminal-ipc/SKILL.md).

## Scope

Self-hosted and single-machine (Windows-first). The runtime coordinates agents
you already run: it adds no model calls of its own. Messages live in a
project-isolated SQLite mailbox, normally at
`~/.claude/projects/<key>/ipc/_ipc.db`. Role registries, heartbeats and
supervisor state use separate files; shared fleet telemetry and quota caches
also live under `~/.claude/`. Backups and migrations must account for these
files as well as the database. MIT License.
