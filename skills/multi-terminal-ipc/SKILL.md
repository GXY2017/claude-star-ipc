---
name: multi-terminal-ipc
description: >-
  Shared Claude Code / Codex terminal IPC: claim hub (main), recover standby (recover ROLE), onboard projects, dispatch/collect work, recover roles, start, restart or reconnect fleets; 多终端、多模型互通、派活、收回复、主从终端、信箱、编队重连、启动/重启 wezterm. Use for ipc.py operations. Covers hub/workers and window loss without assuming a fleet restart.
---

# multi-terminal-ipc

Operating manual for **multi-terminal IPC**: several persistent, independent
Claude Code terminals, possibly driven by models from **different vendors**,
collaborate as peers through a **file-based sqlite mailbox**. Unlike subagents or
same-harness agent teams, each terminal is a full separate session; the mailbox
is files on disk and does not care which model drives each terminal.

**This skill is the single authority for the protocol (2026-08-21).** Project
`AGENTS.md` files carry only that deployment's own facts and hard rules (mailbox
key, model bindings, dispatch exceptions), which override this skill on exactly
those points. The claude-star-ipc repo's `CLAUDE.md` is a pointer here;
`install_user.py` ships this skill user-level, version-locked to the machinery.

## Shared entrypoints

Maintainer: this shared skill; canonical files live in `~/.claude/skills/multi-terminal-ipc`, exposed to Codex by the `~/.agents/skills/multi-terminal-ipc` junction.
- Claude: `/multi-terminal-ipc main [task]` or `/multi-terminal-ipc recover ROLE`; `/main` and `/ipc-recover ROLE` remain thin compatibility commands.
- Codex: `$multi-terminal-ipc main [task]` or `$multi-terminal-ipc recover ROLE`.
- Main: read `references/claude-main.md` on Claude or `references/codex-main.md` on Codex.
- Recover: read `references/claude-recover.md` on Claude or `references/codex-recover.md` on Codex. Explicit role arguments take precedence.
- Other operations: use the protocol and operation-specific references below. Reading or maintaining this skill does not claim a role or start a receiver.

Run IPC commands from the intended project root with the installed stdlib Python runtime. Main changes role ownership and may dispatch the supplied task; recover restores that role's receiver. Preserve project validation and restart approval boundaries. Claude uses its Monitor/background receive adapter; Codex uses the in-turn receiver and explicit full CODEX_THREAD_ID. Never substitute one host's receiver for the other.
Verified 2026-10-29: merged role procedures and launcher invocation routes; static lint and PowerShell parser checks. No live fleet was started for migration validation.

## Mental model

- **Star topology, A is the sole hub.** A = master/decider; B, C, D… = workers.
  **Workers talk only to A, never to each other** — collaboration is relayed
  through A. This keeps the anti-echo invariant linear, not an N² mesh.
- **The star is code-enforced**, not just convention: `send()` rejects any
  non-hub→non-hub message (`send --from B --to C` → `StarViolation`, exit 3), and
  an echo ceiling (`hop > ttl`) kills relay loops. A worker driven by *any*
  vendor's model literally cannot message a peer or echo a loop; the only
  code-generated messages are the hub→worker notes `send()` writes for
  `--for-task` forwards (2026-10-31). Hub is configurable via `IPC_HUB` (default `A`).
- **One mailbox per project root.** The user-level `~/.claude/ipc/ipc.py` keeps
  state (`_ipc.db`, registry, heartbeats) in `~/.claude/projects/<slug>-<hash>/ipc/`
  (`ipc.py:124-132`). The hash is SHA-1 of the normalized project root:
  `CLAUDE_PROJECT_DIR` if set, else the nearest ancestor of cwd holding
  `AGENTS.md` / `CLAUDE.md` / `.claude` / `.git` (home and `~/.claude` skipped), else cwd. It is
  never Claude's encoded `projects/` dir name (`ipc.py:79-83`: that encoding is
  lossy and CJK paths collide). Two terminals share a mailbox **iff both resolve
  to the same project root**; a terminal started elsewhere gets a different
  mailbox and can never connect. `ipc.py` below = `python ~/.claude/ipc/ipc.py`
  run from that root. Legacy project-local installs keep state beside the
  script: see Deployment topologies.
- **More code-level guarantees** (atomic single-consumer claim, `alive`/`busy`
  heartbeat split, QUEUED-BUSY, reaper caveats): read
  `references/state-and-lifecycle.md` when judging whether a worker is parked,
  busy or dead, or why a queued task has not moved.

## Hard prerequisite

All terminals **must launch from the same project root** (the cwd when starting
`claude`); confirm it before opening a terminal. This is the #1 cause of
"they can't see each other".

## Enable in a new project (onboarding) — `references/onboarding.md`

One per-project artifact only: run `python ~/.claude/ipc/ipc_role.py enable` with `CLAUDE_PROJECT_DIR`
set to the project root (unset = resolved from cwd) (creates `.claude/ipc.enabled`; validates the user-level install; refuses the home dir and `~/.claude`;
idempotent). Then launch the hub pane via the fleet script (`-Roles A`; A adds the workers it picks after reading the task, `references/orchestration-and-dispatch.md` § Lineup decided by the hub, user rule 2026-10-27; the script types `/ipc-recover <role>` into each pane; Codex panes `$multi-terminal-ipc recover <role>`), verify with
`ipc_role.py status` plus one A→B round-trip. Guards, the per-project AGENTS.md pointer rule and the
fresh-machine path (`install_user.py`) are in the reference file — read it when the trigger is
在新项目启用 IPC / enable IPC.

## Bringing terminals online

The SessionStart hook claims roles (first-in = A, the rest take the lowest free
slot). Read `references/bring-up.md` when opening, claiming or re-claiming a
terminal: the claim guard (Claude claims only with `WEZTERM_PANE` or `IPC_ROLE`
set; Codex only with an explicit `IPC_ROLE`), the manual floor (the fleet
launcher types `/ipc-recover <role>` — Codex: `$multi-terminal-ipc recover <role>` — into each
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
- Pane alive but silent after a launch, refresh or `/ipc-recover`: probe it (`python ~/.claude/ipc/ipc_pane_probe.py <role>`); verdict `BLOCKED-NOTICE` means a harness notice or the Codex update prompt sits over the TUI and the model never sees the next tool result. Dismiss it (Enter; Codex update prompt: Esc), re-probe, only then dispatch. The hub's reply clock (in `watch --me A` since 2026-10-18) reports it at its 15/30/45-minute checks (fleet-operations gotcha).
- `ipc_fleet1_restart.ps1` is whole-fleet only: `-Roles` picks what comes back,
  never what gets torn down. It is also the project-switch path (`-Project`);
  `ipc_newcycle.ps1` never changes a pane's cwd. The user runs it from a window
  outside fleet 1 (plain PowerShell or another wezterm window; a fleet-1 pane is
  refused); the hub stays in fleet 1 (user 2026-10-18), so A writes its handoff
  and gives the user the command. Stuck or stale TUIs: `ipc_newcycle.ps1 -Relaunch`.

**When launching a fleet on the
user's behalf, ask which project first and pass `-Project` — unless the user already named the
project and scope in this request (2026-09-05: do not re-confirm what was just stated;
still validate the `-Project` value and never widen the scope to a whole-fleet restart, another
project, or someone else's running task).**
**Every WezTerm start or restart is fleet form (user rule 2026-10-18):** never an ad-hoc standalone
`wezterm-gui start` relaunch and never `claude --resume` of the old session. Route by what is
running (is the fleet-1 mux socket reachable, then `status --watch <role>`):
- "启动/打开 wezterm" and the fleet-1 mux is alive (GUI window closed or never attached) → the
  GUI-gone routing above: workers ALIVE = reattach only (`wezterm connect fleet1`, contexts intact,
  no project question); workers DOWN = restart script.
- "启动 wezterm" and no fleet-1 mux → `ipc_wezterm_launch.ps1 -Roles A`; ask the project. Only the hub
  comes up (user rule 2026-10-27); A launches the workers it needs after the first task.
- "重启 wezterm" with the fleet up → the user asking for the whole-fleet restart
  (`ipc_fleet1_restart.ps1`), so the never-widen clause above does not block it.
The lineup is the hub's call after the task arrives (user rule 2026-10-27:
`references/orchestration-and-dispatch.md` § Lineup decided by the hub; role profiles in
[[fleet-default-lineup-20261017]]); launch method and routing are Claude's call; a
launch or restart on the user's behalf still asks the project unless the user already named it in
the same request (the exception above); the hub launching its own workers after a task has
arrived is not a project question at all ([[hub-no-midtask-questions]]). A request that explicitly names a standalone window, a
single pane or one command is done as named. Why and the incident: [[fleet-launch-ask-project]].

## Command surface

Read `references/command-surface.md` for hub, worker and role-registry commands before sending or collecting messages.

## Orchestration, brief standard, capacity — `references/orchestration-and-dispatch.md`

Read that file before composing a dispatch round. It holds the named dispatch
shapes (handoff / advisor / committee), the three seam-sealing brief rules from
arXiv:2608.16801 (A rules on every cross-worker interface; verifier briefs land
only after the producer delivers; shared context ships as ONE file by path),
the shared-view rule (2026-10-22: A keeps one case brief and one read-only
board with IPC execution state and A's acceptance state kept apart; one file
one writer; reviewer findings and author dispositions are separate files with
stable issue ids; drafts are visible, only the `RESULT:` version is citable),
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

Read the matching main or recover adapter listed under Shared entrypoints. Both host adapters are maintained inside this single shared skill; compatibility commands contain no protocol.

## 注意事项 (the cautions that actually bite)

Before any IPC operation, read `references/operational-cautions.md`: all numbered cautions (1–18), result protocol, watcher requirements, restart boundaries and reply clocks. Caution numbers in this file refer to it.

## Deployment topologies (where state lives)

User-level shared install is the default; legacy project-local installs keep
`_ipc.db`, registry and heartbeats beside the script. Layout and migration rule:
`references/state-and-lifecycle.md` § Deployment topologies. Read the project's
`AGENTS.md` Deployment note before assuming where state lives.
