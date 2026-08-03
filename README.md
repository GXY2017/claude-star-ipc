# claude-star-ipc — multi-terminal, cross-vendor LLM collaboration

Let **several Claude Code terminals in the same project collaborate** — one hub, N
workers — through a local SQLite mailbox. Each terminal keeps its own long-lived
context, model, and account; the mailbox is just files on disk, so **each worker can
be driven by a different company's model**.

```
        B  Kimi (k3)
        |
        C  GLM (glm-5.2, via ollama cloud)
        |
A ──────+  ← hub: Claude (Anthropic)
        |
        D  DeepSeek
        |
        E  Codex (OpenAI, codex CLI)
```

That is the reference five-model lineup (2026-08). The bindings are just launch
commands — edit `$LaunchCmd` in `ipc_wezterm_launch.ps1` to run any mix you like.
A and the letter-role workers run under **Claude Code** (whatever model backend
each window uses); E shows a second harness (Codex CLI) joining the same mailbox.

**Star topology, code-enforced:** workers talk only to A, never to each other
(`send B→C` is rejected with exit 3; an echo ceiling kills relay loops). A
dispatches, workers execute and reply, A synthesizes. Delivery is exactly-once
(atomic row claim), tasks carry leases and are requeued if a worker dies
mid-flight, and worker liveness is a heartbeat file, not a promise.

## Dependencies

- **Python 3.8+** — the mailbox (`ipc.py`) is stdlib-only (`sqlite3`); no pip installs.
- **Claude Code CLI** — for the hub and any Claude-harness workers (role
  auto-assignment uses its `SessionStart` hook; watchers use its background tools).
- **Windows + PowerShell 7 (`pwsh`)** — only for the optional scripts
  (`ipc_wezterm_*.ps1`, `codex_ipc_worker.ps1`). The Python core is OS-neutral.
- **WezTerm** *(optional)* — for the fleet scripts that launch/reset all role
  windows as panes in one WezTerm window.
- **Worker CLIs** *(your choice)* — any Claude Code-compatible launch (Kimi,
  GLM, DeepSeek endpoints…) and/or the Codex CLI.

No API keys are stored anywhere in this kit — each terminal authenticates however
its own CLI normally does.

## Install

```sh
git clone https://github.com/GXY2017/claude-star-ipc.git
cd claude-star-ipc
python install_user.py
```

This installs everything **once at user level**: `ipc.py` + `ipc_role.py` (+ the
WezTerm scripts) to `~/.claude/ipc/`, the `/main` and `/ipc-recover` slash
commands, the `multi-terminal-ipc` skill, and a global `SessionStart`/`SessionEnd`
hook merged into `~/.claude/settings.json` (existing content preserved). Idempotent —
safe to re-run to upgrade.

Then **opt in each project** you want collaboration in:

```sh
cd /path/to/your/project
python ~/.claude/ipc/ipc_role.py enable
```

This creates the gate file `.claude/ipc.enabled`. The hook only claims roles in
gated projects; everywhere else it is inert. Each project gets its **own** mailbox,
resolved by launch cwd (`~/.claude/projects/<key>/ipc/`) — so all terminals of one
project must launch **from the same project root**.

Legacy options: `migrate_ipc.py` moves an old in-project mailbox to the user-level
layout; `install_ipc.py` is the superseded per-project installer.

## Usage

### 1. Launch the terminals

Open one terminal per role from the project root. The `SessionStart` hook
auto-assigns roles first-come-first-served (first window = A, then B, C…), or pin a
role explicitly with `IPC_ROLE=B claude`. **Type one line (e.g. "ok") in each worker
window** — a hook can inject instructions but can't fire the first tool call; after
that one keystroke the worker parks its watcher and runs autonomously.

With WezTerm, one command does all of this — spawns one pane per role with
`IPC_ROLE` preset and each role's own launch command:

```powershell
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles A,B,C,D,E
# later, reset the whole fleet for a new task cycle (batch /clear + /ipc-recover):
pwsh ~/.claude/ipc/ipc_newcycle.ps1
```

### 2. Dispatch and collect (hub A)

```sh
python ~/.claude/ipc/ipc.py send --from A --to B "task" --require-watcher
#   -> SENT (worker parked) / QUEUED-BUSY (mid-task, delivered when free) / REFUSED (nobody there, exit 3)
python ~/.claude/ipc/ipc.py send --from A --to B,C "task"     # fan out
python ~/.claude/ipc/ipc.py send --from A --to ALL "task"     # broadcast to live workers
python ~/.claude/ipc/ipc.py send --from A --to B --body-file task.md
#   body from file — REQUIRED when the body contains backticks/$()/quotes
python ~/.claude/ipc/ipc.py recv --me A --block --count 3     # barrier: wait for all 3 replies
python ~/.claude/ipc/ipc.py pending --hub A --detail          # dispatched tasks not yet replied
python ~/.claude/ipc/ipc.py status --watch B                  # ALIVE / BUSY / DOWN
```

Safety flags: `--submit-id K` makes re-dispatch idempotent (same key reuses the
row, prints `DUP`); `--no-requeue` marks non-idempotent work fail-closed (a dead
lease parks it as `NEEDS-REVIEW` instead of silently re-running); `--type note`
sends untracked coordination chatter exempt from the task lifecycle.

### 3. Receive and reply (workers)

A worker's injected instructions tell it to park a watcher first — under Claude
Code, a persistent Monitor running `watch` (each message fires a tiny signal;
idle costs ~zero turns):

```sh
python ~/.claude/ipc/ipc.py watch --me B          # persistent watcher (Monitor), or:
python ~/.claude/ipc/ipc.py recv --me B --block   # bash fallback, re-arm each wake
python ~/.claude/ipc/ipc.py peek --me B --tail 3  # read full text after a signal
python ~/.claude/ipc/ipc.py send --from B --to A "result"     # reply = task closed
python ~/.claude/ipc/ipc.py done --me B --task N              # or close explicitly
```

After `/clear` or context compaction, a worker recovers with `/ipc-recover B`
(role survives; only the watcher needs rebuilding).

The full protocol — task leases and the reaper, the heartbeat split
(`.alive`/`.busy`), barrier sizing, recovery — is specified in
[`CLAUDE.md`](CLAUDE.md), which Claude Code auto-loads so the terminals follow it
themselves.

## Files

| File | Role |
|---|---|
| `ipc.py` | the mailbox CLI — stdlib only, harness-neutral core |
| `.claude/hooks/ipc_role.py` | `SessionStart`/`SessionEnd` hook: auto-assigns roles, injects behavior |
| `ipc_wezterm_launch.ps1` / `ipc_newcycle.ps1` / `ipc_wezterm_common.ps1` | optional WezTerm fleet control: spawn one pane per role, batch-reset between tasks |
| `.claude/commands/main.md`, `ipc-recover.md` | slash commands: `/main` (assert hub), `/ipc-recover` (rebuild watcher after `/clear`) |
| `skills/multi-terminal-ipc/SKILL.md` | operating + onboarding skill |
| `CLAUDE.md` | the protocol the terminals follow |
| `install_user.py` | **recommended** user-level installer (idempotent) |
| `migrate_ipc.py`, `install_ipc.py` | legacy: mailbox migration / per-project install |
| `codex_ipc_worker.ps1`, `examples/ipc-keepalive.cmd` | legacy (decommissioned 2026-08-03): unattended worker daemon — still functional, no longer the reference workflow |

## Limitations

- **Single machine.** Local SQLite file + heartbeat files; not networked.
- **Workers are serial.** One task at a time per worker; for synchronized parallel
  joins inside one context, use subagents instead.
- **One manual keystroke per worker window** (harness floor — hooks can't fire a
  worker's first tool call). The WezTerm scripts automate the keystroke.
- Role names must match `[A-Za-z0-9_]+` (they become heartbeat filenames).

## License

MIT — see [LICENSE](LICENSE).
