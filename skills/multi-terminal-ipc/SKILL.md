---
name: multi-terminal-ipc
description: >-
  Operate cross-model terminal IPC: onboard projects, dispatch/collect work, recover roles, check or reconnect fleets; 多终端、多模型互通、派活、收回复、主从终端、信箱、编队重连. Use for ipc.py operations. Covers hub/workers and window loss without assuming a fleet restart.
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
- `ipc_fleet1_restart.ps1` is whole-fleet only: `-Roles` picks what comes back,
  never what gets torn down.

**When launching a fleet on the
user's behalf, ask which project first and pass `-Project` — unless the user already named the
project and scope in this request (2026-09-05 relaxation: do not re-confirm what was just stated;
still validate the `-Project` value and never widen the scope to a whole-fleet restart, another
project, or someone else's running task).**

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
ipc.py pending --hub A [--detail]                     # tasks dispatched with no reply yet (empty = done)
ipc.py cancel --task N --by A                          # retract a dispatched task
# --submit-id / --no-requeue, --json envelopes, pending --json states (QUEUED-STALLED / IN-PROGRESS-SILENT / NEEDS-REVIEW), redeliver: references/command-details.md
# --- mechanical gates & clocks (2026-08-30, from the 2026-08-29 postmortem; user: "能用脚本固化的就固化") ---
pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile brief.md [-SubmitId k] [-MaxMinutes 15] [-DryRun] [-Force]
                                                      # THE dispatch path: brief lint + recipient pane probe, then park -> send -> nudge -> confirm claim. Gate rules, probes, stall_watch, timing, budget estimates: references/command-details.md
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

1. **Same project root, always.** Different project root → different mailbox
   under `~/.claude/projects/<key>/ipc/` → no connection. Verify before opening
   any terminal.
2. **Receive only with `recv`; never `Read` the whole `_ipc.db`.** `recv` returns
   only new unread rows, so history doesn't re-enter context and token cost
   doesn't grow with message count. Use `peek --tail N` to review.
3. **Worker: every A→worker `task` MUST get exactly one closing message back to A**
   — the linked result (`send --in-reply-to N`), or `done` / `fail` when there is
   no text result. A `--type note` needs none (caution 4). Once a worker `recv`s a
   task A can no longer see it; "consume without replying" is, to A, a lost task
   its `--block` watcher waits on forever. Never chit-chat, never decide on A's
   behalf, stop when done.
4. **A: tag coordination as `--type note`, not task.** Only *real dispatched work*
   is a `task`. Acks, wrap-ups, FYIs, "restart your watcher" must be
   `--type note` — notes are exempt from the lease/reaper and never appear in
   `pending`, so they won't get phantom-re-delivered 1800s later. A note needs no
   reply.
5. **A dispatches with `--require-watcher`.** The role registry survives `/clear`
   while the watcher process is dead, so registry ≠ "listening now". Without the
   flag a task can drop into a black hole. On refusal (exit 3), nudge the worker
   window to re-park its watcher, then resend.
6. **Workers close tasks by the result itself: `send --to A --in-reply-to N`.**
   The linked reply IS the completion; `done --task N` is only for a task with no
   text result. **Open the reply with `RESULT: <absolute path>` (one line per product)**
   when the task produced a file: `send` verifies the path against the disk and a CLEAN
   reply reaches A as a one-line `DONE #N <- B RESULT: <path>` summary instead of a
   `NEW MSG` it must peek — this is what keeps A's context flat across a fan-out
   (2026-09-19). Questions and partial results carry NO status line so A reads
   them in full. Two more opening lines (2026-10-02): `UNSOLVABLE: <why>` = cannot be
   done, nothing guessed from memory; `REPLAN: <wrong premise + evidence>` = the brief
   itself is wrong. Both reach A as `NEW MSG … [UNSOLVABLE]` / `[REPLAN]`; on REPLAN, A
   rewrites the brief instead of re-sending it. `ipc_dispatch.ps1` appends this protocol
   and today's date to every brief by code. Auto-link happens only when exactly one task is open; with several,
   an unlinked reply is refused (2026-09-16 — the old oldest-open guess bound F's
   "#1566 回执" to #1567). A `--type note` never closes anything. Call `ack` periodically on a long
   task or the 1800s ceiling reaps it as "stuck" — `ack` also beats the heartbeat,
   so it is the keep-alive for a watcher-less worker mid-task (bash-fallback mode,
   whose heartbeat dies the moment a task is delivered). For a **non-idempotent**
   task, A dispatches with `--no-requeue`: a stale claim then parks as
   NEEDS-REVIEW instead of being auto-requeued or auto-failed — A decides
   (cancel / probe the worker), the reaper never re-runs it.
7. **Synthesis stays with A.** Reconciliation, coverage check, final call are the
   hub's. On a barrier timeout returning k<N, diff senders got vs dispatched-to,
   probe the absent (`status --watch X`), re-dispatch ≤2 tries, then `log` the
   slice failed. **Dispatch with `--submit-id` so re-dispatch is idempotent** —
   a resend with the same key prints DUP and reuses the queued row instead of
   double-running; without a key, don't blind-re-dispatch a non-idempotent task
   (a second parallel copy is worse than waiting).
8. **Fold bare acks into substantive replies.** Don't send a message just to say
   "收到" — save tokens.
9. **Cross-vendor is the point, but the invariant is in code, not prose.** Don't
   rely on a worker's model "reading the rules" — the star rejection and echo
   ceiling hold regardless of which vendor drives B. Names are `[A-Za-z0-9_]+`;
   path separators are rejected before any heartbeat file is touched.

Cautions 10–13, 15 and 16 in full: `references/fleet-operations.md` § Fleet
cautions. Read before exiting a pane, crossing fleets from inside a pane, editing
a pane-spawn site, upgrading Node.js / wezterm, or handling a fleet anomaly.

10. **Clean exit is a script, not Ctrl+C / closing the pane.**
11. **From inside a pane, launch or connect to the OTHER fleet only with
    `WEZTERM_*` env stripped.**
12. **Every pane-spawn site uses `$script:PaneShell` (MSI pwsh, absolute path),
    never bare `pwsh`.**
13. **Node.js / wezterm upgrades happen only with BOTH fleets down.**

14. **A runs a 15-minute reply clock; workers do not self-report quota** (user
    decision 2026-08-29). No reply 15 min after dispatch → A inspects (`pending
    --hub A --detail`, `ipc_role.py status`, pane text); working normally → one
    more 15 min; stalled/abnormal → act now; two extensions max. Tasks are sliced
    so a recipient can deliver in ≤15 min. Full procedure and the
    manage-not-operate boundary: `~/.claude/commands/main.md` § A 的职责边界与节奏.
    Worker quota self-reporting was considered and deferred: no vendor CLI
    exposes an in-turn quota read (Claude `/usage` and Codex `/status` are
    TUI-only; `~/.claude/hooks/statusline.js:10-11`), so A reads quota banners from the pane
    during the 15-min inspection instead.
15. **Both fleet GUIs vanish at once while every pane and mux server is alive =
    system commit (virtual memory) exhaustion, not a wezterm bug**: diagnose,
    then reattach, never relaunch.
16. **Unforeseeable fleet anomalies go to X in fleet 2, not to A debugging on the
    spot** (user rule 2026-08-30).
17. **A worker is one context window; a long token-heavy serial queue exhausts it
    mid-queue**: dispatch 2–3 items per batch. Full rule and evidence:
    `references/orchestration-and-dispatch.md` § Worker context and queue length.

## Deployment topologies (where state lives)

User-level shared install is the current default; legacy project-local installs
keep `_ipc.db`, registry and heartbeats beside the script. Layout of both and the
migration rule: `references/state-and-lifecycle.md` § Deployment topologies.

Confirm which topology a project uses from its `AGENTS.md` Deployment note before
assuming where `_ipc.db` and the registry live.
