---
name: multi-terminal-ipc
description: >-
  Operate cross-model terminal IPC: onboard projects, dispatch/collect work, recover roles, start, restart or reconnect fleets; 多终端、多模型互通、派活、收回复、主从终端、信箱、编队重连、启动/重启 wezterm. Use for ipc.py operations. Covers hub/workers and window loss without assuming a fleet restart.
---

# multi-terminal-ipc

Operating manual for the project's **multi-terminal IPC** feature: several
persistent, independent Claude Code terminals — which may be driven by models
from **different vendors** — collaborate as peers through a **file-based sqlite
mailbox**. Unlike subagents/same-harness agent teams (single-vendor, single
process), each terminal here is a full separate session; the mailbox is just
files on disk and doesn't care which model drives each terminal.

**This skill is the single authority for the protocol (2026-08-21).** Project
`AGENTS.md` files no longer restate it — they carry only that deployment's own
facts and hard rules (mailbox key, model bindings, dispatch exceptions), which
override this skill on exactly those points. The claude-star-ipc repo's
`CLAUDE.md` is likewise a pointer here; `install_user.py` ships this skill
user-level, version-locked to the machinery.

## Mental model

- **Star topology, A is the sole hub.** A = master/decider; B, C, D… = workers.
  **Workers talk only to A, never to each other** — collaboration is relayed
  through A. This keeps the anti-echo invariant linear, not an N² mesh.
- **The star is code-enforced**, not just convention: `send()` rejects any
  non-hub→non-hub message (`send --from B --to C` → `StarViolation`, exit 3), and
  an echo ceiling (`hop > ttl`) kills relay loops. A worker driven by *any*
  vendor's model literally cannot message a peer or echo a loop. Hub is
  configurable via `IPC_HUB` (default `A`).
- **One mailbox per project root.** The user-level `~/.claude/ipc/ipc.py` keeps
  state (`_ipc.db`, registry, heartbeats) in `~/.claude/projects/<slug>-<hash>/ipc/`
  (`ipc.py:124-132`). The hash is SHA-1 of the normalized project root:
  `CLAUDE_PROJECT_DIR` if set, else the nearest ancestor of cwd holding
  `AGENTS.md` / `CLAUDE.md` / `.claude` / `.git` (home and `~/.claude` skipped), else cwd. It is
  never Claude's encoded `projects/` dir name (`ipc.py:79-83`: that encoding is
  lossy and CJK paths collide). Two terminals share a mailbox **iff both resolve
  to the same project root**; a terminal started elsewhere gets a different
  mailbox and can never connect. `ipc.py` below = `python ~/.claude/ipc/ipc.py`
  run from that root. Legacy project-local installs (a `_ipc.db` beside the
  script) keep state in the script's own directory — see Deployment topologies.
- **More code-level guarantees** (atomic single-consumer claim, `alive`/`busy`
  heartbeat split, QUEUED-BUSY, reaper caveats): read
  `references/state-and-lifecycle.md` when judging whether a worker is parked,
  busy or dead, or why a queued task has not moved.

## Hard prerequisite

All terminals **must launch from the same project root** (the cwd when starting
`claude`). Confirm cwd matches before opening a terminal. This is the #1 cause of
"they can't see each other".

## Enable in a new project (onboarding) — `references/onboarding.md`

One per-project artifact only: run `python ~/.claude/ipc/ipc_role.py enable` with `CLAUDE_PROJECT_DIR`
set to the project root (unset = resolved from cwd) (creates `.claude/ipc.enabled`; validates the user-level install; refuses the home dir and `~/.claude`;
idempotent). Then launch panes via the fleet script (it types `/ipc-recover <role>` into each pane; Codex panes `$ipc-recover <role>`), verify with
`ipc_role.py status` plus one A→B round-trip. Guards, the per-project AGENTS.md pointer rule and the
fresh-machine path (`install_user.py`) are in the reference file — read it when the trigger is
在新项目启用 IPC / enable IPC.

## Bringing terminals online

The SessionStart hook claims roles (first-in = A, the rest take the lowest free
slot). Read `references/bring-up.md` when opening, claiming or re-claiming a
terminal: the claim guard (Claude claims only with `WEZTERM_PANE` or `IPC_ROLE`
set; Codex only with an explicit `IPC_ROLE`), the manual floor (the fleet
launcher types `/ipc-recover <role>` — Codex: `$ipc-recover <role>` — into each
pane itself; only a window opened by hand needs it typed), forcing a role with `IPC_ROLE=A claude …`, and the role
registry commands.

**Bring-up order: `ipc_role.py take <role> --session <sid>` FIRST, `watch --me <role>` SECOND.**
Why watch-then-take fails (owner snapshot, `ALIVE [SQUATTER]`, the `_REG_ANCHOR_GRACE` safety net): `references/bring-up.md` § Bring-up order.
Mid-task the inverse holds: never `take` or re-park while a claimed task runs — the busy-beater fork rewrites the owner with an empty session, the
parent watcher retires, the task drops to QUEUED; close with `send --in-reply-to N` (a linked reply closes the task; adding `done` sends a second row), then `take`, then re-park. [[C--Users-<user>-Downloads-AI-----::ipc-claim-busy-beater-evicts-owner]] [[C--Users-<user>-Downloads-----::orphan-watcher-survives-tui-death]]

**WezTerm pane fleet (preferred since 2026-08-03).** Read
`references/fleet-operations.md` before running any fleet script (launch,
newcycle, one-role relaunch, whole-fleet restart, fleet 2, pane exit), when a
pane or GUI window is gone, or to check role→model bindings. Routing:
- GUI window gone → check `status --watch <role>`: workers ALIVE = reattach only
  (`wezterm connect fleet1` or `fleet2`, no relaunch, context intact); workers
  DOWN = real teardown, use the restart script.
- Role DOWN = no parked watcher; the pane may be alive. Check `wezterm cli list`
  against the pane map, wake a live pane with `ipc_wake_pane.ps1`, relaunch only
  a pane that no longer exists.
- A lock, state or heartbeat file is not proof a worker is alive, and a poll
  timeout or missing reply is not a terminal state: re-poll the same handle
  (`status --watch <role>`, the pane in `wezterm cli list`) before recycling.
  (Codex goal-continuation template, openai/codex codex-rs, read 2026-10-04.)
- Pane alive but silent after a launch, refresh or `/ipc-recover`: probe it (`python ~/.claude/ipc/ipc_pane_probe.py <role>`); verdict `BLOCKED-NOTICE` means a harness notice or the Codex update prompt sits over the TUI and the model never sees the next tool result. Dismiss it (Enter; Codex update prompt: Esc), re-probe, only then dispatch. the hub's reply clock (built into `watch --me A` since 2026-10-18) reports it at its 15/30/45-minute checks (2026-10-17, see fleet-operations gotcha).
- `ipc_fleet1_restart.ps1` is whole-fleet only: `-Roles` picks what comes back,
  never what gets torn down.

**When launching a fleet on the
user's behalf, ask which project first and pass `-Project` — unless the user already named the
project and scope in this request (2026-09-05 relaxation: do not re-confirm what was just stated;
still validate the `-Project` value and never widen the scope to a whole-fleet restart, another
project, or someone else's running task).**
**Every WezTerm start or restart is fleet form (user rule 2026-10-18):** never an ad-hoc standalone
`wezterm-gui start` relaunch and never `claude --resume` of the old session. Route by what is
running (is the fleet-1 mux socket reachable, then `status --watch <role>`):
- "启动/打开 wezterm" and the fleet-1 mux is alive (GUI window closed or never attached) → the
  GUI-gone routing above: workers ALIVE = reattach only (`wezterm connect fleet1`, contexts intact,
  no project question); workers DOWN = restart script.
- "启动 wezterm" and no fleet-1 mux → `ipc_wezterm_launch.ps1`; ask the project.
- "重启 wezterm" with the fleet up → the user asking for the whole-fleet restart
  (`ipc_fleet1_restart.ps1`), so the never-widen clause above does not block it.
Lineup size, launch method and routing are Claude's call per [[fleet-default-lineup-20261017]]; a
launch or restart still asks the project. A request that explicitly names a standalone window, a
single pane or one command is done as named. Why and the incident: [[fleet-launch-ask-project]].

## Command surface (run from project root)

### Hub A — dispatch & collect
```
ipc.py send --from A --to B "task"                    # one worker
ipc.py send --from A --to B,C "task"                  # fan out (one row each)
ipc.py send --from A --to ALL "task"                  # broadcast to every live worker
ipc.py send --from A --to B --body-file task.md       # body from file — REQUIRED for bodies with backticks/$()/quotes (shell mangles them)
ipc.py send --from A --to B "task" --require-watcher  # parked=SENT; mid-task (busy fresh)=QUEUED-BUSY exit 0, delivers when B finishes; parked-but-role-unclaimed=REFUSED-SQUATTER exit 3 (2026-08-17 #1186: an orphan heartbeat is not dispatch evidence — remedy printed: ipc_role.py take <role>); neither=REFUSED exit 3, not queued
ipc.py recv --me A                                    # take unread replies (NONE = nothing yet)
ipc.py recv --me A --block                            # block until a reply arrives
ipc.py recv --me A --block --count 3                  # BARRIER: after fanning to 3, wait for all 3 — COUNT TRAP: a worker that text-replies AND runs `done` sends TWO rows (reply + bodyless ack), so the quota can fill on the fastest workers' pairs; size ~2×workers, or tell workers to close with a single message (a text reply alone already marks done)
ipc.py peek --me A --tail 5                            # review last 5 WITHOUT marking read
ipc.py peek --me A --id 12 15                          # exactly those ids (from NEW MSG / DONE signals), no marking
ipc.py peek --me C --batch [N] / --batches             # a complete saved recv --json batch (latest or N; numbered files, last 20 kept): recovery when tool output was truncated
ipc.py pending --hub A [--detail]                     # tasks dispatched with no reply yet (empty = done)
ipc.py task --id N [--json]                           # ONE task's state (QUEUED|IN_PROGRESS|DONE|FAILED|CANCELLED|NEEDS-REVIEW|STALE|UNKNOWN), read-only; use it to confirm a claim - pending omits DONE tasks (2026-10-15)
ipc.py status --watch B --json                        # {alive,busy,squatter,owner_valid,parked,reason}; scripts gate on `parked` only (2026-10-15)
ipc.py cancel --task N --by A                          # retract a dispatched task
# --submit-id / --no-requeue, --json envelopes, pending --json states (QUEUED-STALLED / IN-PROGRESS-SILENT / NEEDS-REVIEW), redeliver, archive table, routing ledger (`类型:`, `ipc_estimate.py rank`, `grade`): references/command-details.md
# --- mechanical gates & clocks (2026-08-30, from the 2026-08-29 postmortem; user: "能用脚本固化的就固化") ---
pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile brief.md [-SubmitId k] [-MaxMinutes 15] [-DryRun] [-Force]
                                                      # THE dispatch path: brief lint + recipient pane probe, then park -> send -> nudge -> confirm claim. Gate rules, probes, the reply clock, timing, budget estimates: references/command-details.md
```

### Worker B/C/D — execute & report
```
ipc.py recv --me B                 # take my unread tasks (mark read)
# Watcher hosting (2026-08-17 #1188 HARD RULE): on Claude, `watch --me <role>` runs under the Monitor tool ONLY — a background-Bash watch buffers its signals to a file the harness surfaces only on exit (observed 1h55m delivery gap); watch now refuses file-stdout hosts with exit 4
# Codex has no Monitor and an infinite watch never wakes the model (2026-07-28): stand by with the `$ipc-recover` step 3 supervisor — one-shot `recv --me <role> --block --keep-heartbeat` in a loop, re-armed before each notify(); it lives only within the current turn
# Monitor host is BASH (2026-08-23): a backslash path `C:\Users\<user>\.claude\ipc\ipc.py` is stripped to `Users<user>.claudeipcipc.py` (exit 2) — write `python ~/.claude/ipc/ipc.py watch --me <role>` or `C:/Users/...` forward slashes; PowerShell-tool calls are unaffected [[C--Users-<user>-Downloads-------::monitor-host-bash-forward-slash]]
ipc.py send --from B --to A --in-reply-to N --body-file result.md   # THE result path (2026-09-16): a linked reply closes task N by itself — no separate `done`, no `--type note`. With several open tasks an unlinked reply is REFUSED-LINK (exit 3), never guessed; --in-reply-to naming a task dispatched to someone else is refused too
                                   # RESULT-line protocol (2026-09-19): result.md OPENS with one `RESULT: <absolute path>` line per product file (caution 6). send()'s disk check, the `DONE #N` summary and its hold rule: references/command-details.md
ipc.py send --from B --to A --type note "progress"   # progress/FYI only — a note never closes a task (F's four note-typed results left #1592-#1598 open, 2026-09-16)
ipc.py done --me B --task N         # close task N WITHOUT a text result (bodyless ack); redundant after a linked reply
ipc.py ack  --me B --task N         # FIRST ACTION after reading a task (2026-08-17): stamps last_seen_ts so A's IN-PROGRESS-SILENT alarm clears; also the lease-extender for long tasks (no --task = all my claimed)
ipc.py fail --me B --task N [--reason ...]   # mark failed (won't requeue)
```

### Role registry (`~/.claude/ipc/ipc_role.py`; legacy project-local installs used `.claude/hooks/ipc_role.py`)

`ipc_role.py status` / `take` / `reclaim-dead` and `ipc.py status --watch <role>`
(ALIVE / BUSY / DOWN / `[SQUATTER]` exit codes): `references/bring-up.md` § Role
registry. Read it when verifying a claim, re-taking a role or reading a watcher state.

## Orchestration, brief standard, capacity — `references/orchestration-and-dispatch.md`

Read that file before composing a dispatch round. It holds the named dispatch
shapes (handoff / advisor / committee), the three seam-sealing brief rules from
arXiv:2608.16801 (A rules on every cross-worker interface; verifier briefs land
only after the producer delivers; shared context ships as ONE file by path),
and capacity-aware dispatch (scrape each pane's `Ctx N% left` through the fleet
mux before a heavy batch; below ~70% refresh the pane instead of gambling;
context % and vendor quota are independent axes; any refresh discards the
worker's task context, so re-brief with a fresh `--body-file`).

## Task lifecycle

Leases, the reaper, the requeue cap (`MAX_ATTEMPTS` / `IPC_MAX_ATTEMPTS`),
`--no-requeue` → NEEDS-REVIEW, done-drop at claim and mailbox self-trim: read
`references/state-and-lifecycle.md` § Task lifecycle when a task is requeued,
STALE, NEEDS-REVIEW or failed after retries, or before changing `--lease`.

## How A waits / recovery after /clear — see the role procedures

This skill owns the protocol; the two role procedures own their steps (not
restated here). Claude forms: `~/.claude/commands/main.md`, `ipc-recover.md`.
Codex forms: `~/.agents/skills/ipc-main/`, `ipc-recover/` (in-turn `recv`
supervisor in place of the Monitor `watch`). Edit both forms in the same pass;
lint-codex COMMAND-PAIR flags a one-sided edit (2026-10-04: `$ipc-main` lacked
two 2026-09-16 rules).
- **Hub duties & collecting replies** → `/main` / `$ipc-main`:
  persistent Monitor on `watch --me A`, signal → `peek`, bash fallback. Since
  2026-08-21 (user decision) the hub parks this watcher at BOOT like a worker —
  the fleet launcher sends `/ipc-recover A` to the hub pane too, and newcycle
  re-parks it after /clear. Registry exemption is unchanged: a dead hub
  heartbeat is never auto-reclaimed (`status` shows "watcher down", role kept).
- **Recovery after /clear / compaction / hook failure** →
  `/ipc-recover <role>` / `$ipc-recover <role>`. Key invariant:
  `/clear` keeps the role, kills only the watcher — recovery = re-park the
  watcher, never re-claim or switch roles.

## 注意事项 (the cautions that actually bite)

Before any IPC operation, read `references/operational-cautions.md`. It retains all numbered cautions (1–17), result protocol, watcher requirements, restart boundaries and reply clocks. References to caution numbers below refer to that file.

## Deployment topologies (where state lives)

User-level shared install is the current default; legacy project-local installs
keep `_ipc.db`, registry and heartbeats beside the script. Layout of both and the
migration rule: `references/state-and-lifecycle.md` § Deployment topologies.

Confirm which topology a project uses from its `AGENTS.md` Deployment note before
assuming where `_ipc.db` and the registry live.
