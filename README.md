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
- **Windows + PowerShell 7 (`pwsh`)** — for the fleet scripts
  (`ipc_wezterm_*.ps1`). The Python core is OS-neutral.
- **WezTerm** *(required)* — all role terminals live as panes in one WezTerm
  window, launched/reset by the fleet scripts. WezTerm is what makes the fleet
  **AI-operable**: `wezterm cli` gives stable pane ids at spawn, keystroke
  injection into any pane (`send-text`, no focus needed), and full screen
  readback (`get-text`) — so one AI terminal can drive every other terminal
  and verify each step. On Windows use a **nightly** build (the 2024-02 stable
  CLI cannot reach the GUI socket from an external process; the scripts apply
  the `WEZTERM_UNIX_SOCKET` workaround automatically).
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
`IPC_ROLE` preset and each role's own launch command. You don't have to run it
yourself: **an AI terminal can operate WezTerm** — ask any agent session (e.g.
Claude Code) to run the launcher, and it can then drive the panes over
`wezterm cli` (inject a wake line into a DOWN worker, read a pane's screen to
diagnose it, batch-reset the fleet between task cycles), verifying every step
via screen readback:

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
python ~/.claude/ipc/ipc.py send --from B --to A --in-reply-to N --body-file result.md   # reply = task closed
python ~/.claude/ipc/ipc.py done --me B --task N              # or close explicitly
```

RESULT-line protocol (2026-09-19): when the task produced a file, `result.md`
opens with one `RESULT: <absolute path>` line per product. `send` verifies each
path on disk (absolute, exists, mtime ≥ dispatch) and prints `RESULT CLEAN` /
`RESULT UNVERIFIED: <why>`. A CLEAN reply reaches the hub as a single line
`DONE #N <- B RESULT: <path>` (held until sibling tasks whose budget ends inside
the hold window finish, `IPC_HOLD_SECS`=900) — the hub never reads the body.
Questions, partial work and failures carry no RESULT line and arrive as
`NEW MSG` for a full read. Budgets come from the brief's `预算: N 分钟` line
(or `send --budget N`); `ipc_estimate.py` suggests them from per-model history.

After `/clear` or context compaction, a worker recovers with `/ipc-recover B`
(role survives; only the watcher needs rebuilding).

The full protocol — task leases and the reaper, the heartbeat split
(`.alive`/`.busy`), barrier sizing, recovery — is specified in the operating skill
[`skills/multi-terminal-ipc/SKILL.md`](skills/multi-terminal-ipc/SKILL.md), which
`install_user.py` deploys user-level so every terminal loads the same copy.

## Files

| File | Role |
|---|---|
| `ipc.py` | the mailbox CLI — stdlib only, harness-neutral core |
| `.claude/hooks/ipc_role.py` | `SessionStart`/`SessionEnd` hook: auto-assigns roles, injects behavior |
| `ipc_wezterm_launch.ps1` / `ipc_newcycle.ps1` / `ipc_wezterm_common.ps1` | WezTerm fleet control: spawn one pane per role, batch-reset between tasks |
| `ipc_fleet1_restart.ps1` / `ipc_fleet2_launch.ps1` / `mux-fleet*.lua` | mux-domain fleets (2026-08-17): panes live under a per-fleet `wezterm-mux-server`, the GUI is a detachable view; precise restart + independent second formation (role X) |
| `ipc_dispatch.ps1` / `ipc_wake_pane.ps1` / `ipc_pane_exit.ps1` | per-pane ops: one-shot park+queue+nudge dispatch, wake an unattended pane (codex-safe), clean pane exit that retires the watcher |
| `.claude/commands/main.md`, `ipc-recover.md` | slash commands: `/main` (assert hub), `/ipc-recover` (rebuild watcher after `/clear`) |
| `ipc_brief_lint.py` / `ipc_pane_probe.py` / `ipc_timing.py` / `stall_watch.py` | hub-side tooling (2026-08-29..31): lint a dispatch brief before it is sent, probe a pane's real liveness (context % / stall flags, not just heartbeat), pair dispatch→reply timing by `in_reply_to`, 15-minute stall watchdog for the hub |
| `ipc_estimate.py` | per-model duration ledger (2026-09-19): `send()` appends one metadata row per first linked reply/fail to `~/.claude/ipc/timing.jsonl`; `backfill` rescans every mailbox, `stats` prints p50/p80 per model, `estimate --role R --brief f.md` suggests the brief's `预算:` line (n≥5 per tier, else INSUFFICIENT + fallback); the brief lint takes its per-unit rate from the same ledger |
| `fleet_list_panes.py` / `fleet_get_text.py` / `FLEET-TRIAGE.md` | read-only pane inventory / screen tail per mux socket (hard timeout, `--no-auto-start`), and the symptom→command→verdict triage checklist that uses them |
| `skills/multi-terminal-ipc/SKILL.md` + `references/` | operating skill (single protocol authority); `references/`: `onboarding.md` (enable in a project), `bring-up.md` (role claim, guard, registry), `command-details.md` (flags, JSON outputs, dispatch gates and clocks), `state-and-lifecycle.md` (delivery guarantees, task lifecycle, deployment topologies), `fleet-operations.md` (fleet scripts, launch menu, role bindings), `orchestration-and-dispatch.md` (handoff / advisor / committee patterns, brief standard) |
| `CLAUDE.md` | repo facts for working on this repo; points to the skill for the protocol |
| `install_user.py` | **recommended** user-level installer (idempotent) |
| `migrate_ipc.py`, `install_ipc.py` | legacy: mailbox migration / per-project install |
| `codex_ipc_worker.ps1`, `examples/ipc-keepalive.cmd` | legacy (decommissioned 2026-08-03): unattended worker daemon — still functional, no longer the reference workflow |

## Limitations

- **Single machine.** Local SQLite file + heartbeat files; not networked.
- **Workers are serial.** One task at a time per worker; for synchronized parallel
  joins inside one context, use subagents instead.
- **One manual keystroke per worker window** (harness floor — hooks can't fire a
  worker's first tool call). The WezTerm scripts automate the keystroke, and another AI can handle WezTerm.
- Role names must match `[A-Za-z0-9_]+` (they become heartbeat filenames).

## License

MIT — see [LICENSE](LICENSE).
