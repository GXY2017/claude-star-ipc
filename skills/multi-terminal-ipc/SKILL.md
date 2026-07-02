---
name: multi-terminal-ipc
description: >-
  Operating guide for the cross-vendor multi-terminal IPC mailbox — several
  independent Claude Code terminals (possibly driven by different companies'
  models, e.g. an Anthropic hub A + a Zhipu/GLM worker B) collaborating as peers
  through a file-based sqlite mailbox (`ipc.py`). Use whenever the user wants to
  ENABLE this collaboration in a new project (onboarding), dispatch work to
  another terminal, coordinate a hub/worker (A/B/C/D) setup, fan out a task to
  workers and collect replies, bring a worker online, recover an IPC role after
  /clear, or asks about the multi-terminal / 多终端 / 多大模型 互通 / 派活给 B /
  主终端从终端 / 星型拓扑 / 信箱 mailbox protocol. Triggers on "在新项目启用/
  接入/开通 多终端协作/IPC"、"这个项目也要多模型互通"、"enable IPC in this
  project"、"set up multi-terminal collaboration"、"派活/派给 B"、"多终端协作"、
  "跨模型互通"、"收 B 的回复"、"让 B 上线"、"hub/worker"、"fan out to workers",
  and any operation against `ipc.py`.
---

# multi-terminal-ipc

Operating manual for the project's **multi-terminal IPC** feature: several
persistent, independent Claude Code terminals — which may be driven by models
from **different vendors** — collaborate as peers through a **file-based sqlite
mailbox**. Unlike subagents/same-harness agent teams (single-vendor, single
process), each terminal here is a full separate session; the mailbox is just
files on disk and doesn't care which model drives each terminal.

This skill is the *operational* distillation. The authoritative, per-project
spec lives in that project's `CLAUDE.md` (auto-loaded when cwd = project root).
When a project's `CLAUDE.md` disagrees with this skill, **the project wins** —
paths and role count are per-deployment.

## Mental model

- **Star topology, A is the sole hub.** A = master/decider; B, C, D… = workers.
  **Workers talk only to A, never to each other** — collaboration is relayed
  through A. This keeps the anti-echo invariant linear, not an N² mesh.
- **The star is code-enforced**, not just convention: `send()` rejects any
  non-hub→non-hub message (`send --from B --to C` → `StarViolation`, exit 3), and
  an echo ceiling (`hop > ttl`) kills relay loops. A worker driven by *any*
  vendor's model literally cannot message a peer or echo a loop. Hub is
  configurable via `IPC_HUB` (default `A`).
- **The mailbox is `ipc.py` beside `_ipc.db`.** `ipc.py` resolves the DB to its
  own directory. Two terminals share a mailbox **iff both launched with cwd =
  the same project root**. A terminal started elsewhere runs a different (or no)
  `ipc.py` and can never connect. `ipc.py` below = `python ipc.py` run from that
  root.

## Hard prerequisite

All terminals **must launch from the same project root** (the cwd when starting
`claude`). Confirm cwd matches before opening a terminal. This is the #1 cause of
"they can't see each other".

## Enable in a new project (onboarding)

The machinery is USER-LEVEL (one copy at `~/.claude/ipc/`, global hooks in
`~/.claude/settings.json`); a new project needs exactly ONE per-project artifact —
the opt-in gate file. One command creates it, with guards:

```
cd <the new project root>
python "~/.claude/ipc/ipc_role.py" enable
```

`enable` validates the user-level install (ipc.py importable, claim hook
registered), **refuses the home dir and `~/.claude`** (every launch dir becomes a
registered project; user config must never be one), and creates
`.claude/ipc.enabled` at the project root. Idempotent. Manual equivalent: create
that empty gate file yourself.

Then bring terminals online:
1. Open each terminal with cwd = the project root (first in = A/hub, the rest take
   B, C, D…; pin a window's role with `IPC_ROLE=X claude`).
2. Type one line (`ok`/`standby`) in each worker window — the manual floor; its
   watcher parks and any backlog arrives as signals.
3. Verify: `ipc_role.py status` shows the roles live, then one A→B round-trip
   (`send` a test task, worker replies, `pending --hub A` returns to empty).

What you do NOT need: no per-project copy of `ipc.py`, no per-project hooks, no
mailbox setup — state auto-creates under `~/.claude/projects/<key>/ipc/` on first
use, one isolated mailbox per project. Optional: paste the protocol section (the
repo `CLAUDE.md`) into the new project's own CLAUDE.md so the injected "see
protocol" pointer resolves in-project; otherwise THIS skill is the reference.

Fresh **machine** rather than fresh project? Run `python install_user.py` from the
claude-star-ipc repo once (deploys machinery + slash commands + this skill), then
per-project `enable` as above.

## Bringing terminals online

The SessionStart hook auto-claims a role (first-in = A, rest take lowest free
slot B, C, D…) and injects that role's behavior + watcher instructions as
context — so a worker usually needn't type any command to get its role.

**The one manual floor the hook can't cross:** it can inject instructions but
can't fire a worker's first tool call. So **after opening a worker window, type
any one line (`ok`/`standby`)** — that parks its watcher; any queued backlog then
arrives as signals (WATCHER FIRST, no separate drain step: work done without a
beating watcher reads as stale to the lease reaper and gets requeued mid-flight).
Before dispatching, A should remind the user to type one line in each worker
window.

Force a specific role at launch: `IPC_ROLE=A claude …` (overrides launch order).

## Command surface (run from project root)

### Hub A — dispatch & collect
```
ipc.py send --from A --to B "task"                    # one worker
ipc.py send --from A --to B,C "task"                  # fan out (one row each)
ipc.py send --from A --to ALL "task"                  # broadcast to every live worker
ipc.py send --from A --to B --body-file task.md       # body from file — REQUIRED for bodies with backticks/$()/quotes (shell mangles them)
ipc.py send --from A --to B "task" --require-watcher  # refuse (exit 3), don't queue, if B's watcher isn't parked
ipc.py recv --me A                                    # take unread replies (NONE = nothing yet)
ipc.py recv --me A --block                            # block until a reply arrives
ipc.py recv --me A --block --count 3                  # BARRIER: after fanning to 3, wait for all 3
ipc.py peek --me A --tail 5                            # review last 5 WITHOUT marking read
ipc.py pending --hub A [--detail]                     # tasks dispatched with no reply yet (empty = done)
ipc.py cancel --task N --by A                          # retract a dispatched task
```

### Worker B/C/D — execute & report
```
ipc.py recv --me B                 # take my unread tasks (mark read)
ipc.py send --from B --to A "result"   # reply to A (plain send, no --require-watcher)
ipc.py done --me B --task N         # register task N done (bodyless ack)
ipc.py ack  --me B [--task N]       # extend lease on a long task (no --task = all my claimed)
ipc.py fail --me B --task N [--reason ...]   # mark failed (won't requeue)
```

### Role registry (`.claude/hooks/ipc_role.py` or `~/.claude/ipc/ipc_role.py`)
```
ipc_role.py status                 # reconciled view: ownership × heartbeat liveness
ipc_role.py take A --session <sid> # (re)assign a role to this session
ipc_role.py reclaim-dead           # free slots whose watcher heartbeat is gone
ipc.py status --watch B            # is B's watcher parked right now? ALIVE(0)/DOWN(1) + pid/session
```

## Task lifecycle

A hub→worker message defaults to `msg_type='task'` carrying a **lease**
(`--lease` seconds, default 1800, **counted from CLAIM time** — reset when the
worker claims the row, so queue wait doesn't eat the runway and a requeued task
retries under a fresh ceiling). A stale claimed task — worker's heartbeat dies
(process gone) **or** the hard lease ceiling passes (alive-but-stuck) — is lazily
**requeued** by a reaper (runs inside recv/watch/pending) and re-delivered, or
marked `failed` after `--max-attempts` (default 3, a `pending`/reaper concept —
**not** a `send` flag). Requeued rows that already have a reply/ack are
**done-dropped at claim** (claimed silently, never redelivered), so a phantom
requeue can't make a worker redo finished work. The mailbox also self-trims:
past 300 rows, handled/terminal history is lazily archived inside
recv/watch/pending (newest 150 always kept).

## How A waits for replies (watcher)

Start **one** persistent Monitor running `ipc.py watch --me A`: each reply fires
a tiny **SIGNAL** (`NEW MSG #id from …`, never the body); on the signal, read the
full message with `ipc.py peek --me A --tail 3`. Idle cost ≈ zero. Fallback if
Monitor is unavailable: `ipc.py recv --me A --block` as a background Bash (exit 0
= message, 2 = timeout), or `--block --count N` to barrier-collect N replies.

## Recovery after /clear / compaction / hook failure

`/clear` does **not** release the role (the registry keeps `session_id`→role); it
only kills the background **watcher process**. So recovery = re-establish the
watcher, **not** re-claim the role.
- Worker: run **`/ipc-recover B`** (real letter; a user-level command in
  `~/.claude/commands/`, available in every opted-in project) — starts the
  persistent Monitor `watch --me B` FIRST; backlog arrives as signals, read via
  `peek` (watcher-first keeps the lease reaper off heartbeat-less work).
- Hub: you are A — continue as hub (or `/main`). A needs no standby watcher.
- **Don't claim a different role** after /clear — the slot is still held under
  the old session; a wrong self-claim is how a cleared B wrong-turns into D.

## 注意事项 (the cautions that actually bite)

1. **Same project root, always.** Different cwd → different `ipc.py` → no
   connection. Verify before opening any terminal.
2. **Receive only with `recv`; never `Read` the whole `_ipc.db`.** `recv` returns
   only new unread rows, so history doesn't re-enter context and token cost
   doesn't grow with message count. Use `peek --tail N` to review.
3. **Worker: every A→worker message MUST get exactly one `send` back to A** — even
   a bare ack. Once a worker `recv`s a message A can no longer see it; "consume
   without replying" is, to A, a lost message its `--block` watcher waits on
   forever. Never chit-chat, never decide on A's behalf, stop when done.
4. **A: tag coordination as `--type note`, not task.** Only *real dispatched work*
   is a `task`. Acks, wrap-ups, FYIs, "restart your watcher" must be
   `--type note` — notes are exempt from the lease/reaper and never appear in
   `pending`, so they won't get phantom-re-delivered 1800s later. A note needs no
   reply.
5. **A dispatches with `--require-watcher`.** The role registry survives `/clear`
   while the watcher process is dead, so registry ≠ "listening now". Without the
   flag a task can drop into a black hole. On refusal (exit 3), nudge the worker
   window to re-park its watcher, then resend.
6. **Workers close tasks explicitly.** `done --me <self> --task N` when finished
   (a plain reply also marks done, but auto-links to the *oldest* open task, so
   `done --task N` is safer with several open). Call `ack` periodically on a long
   task or the 1800s ceiling reaps it as "stuck" — `ack` also beats the heartbeat,
   so it is the keep-alive for a watcher-less worker mid-task (bash-fallback mode,
   whose heartbeat dies the moment a task is delivered). For a **non-idempotent**
   task, A should keep attempts to 1 (via the reaper's `--max-attempts`, not on
   `send`).
7. **Synthesis stays with A.** Reconciliation, coverage check, final call are the
   hub's. On a barrier timeout returning k<N, diff senders got vs dispatched-to,
   probe the absent (`status --watch X`), re-dispatch ≤2 tries, then `log` the
   slice failed. **Don't blind-re-dispatch on timeout** — a second copy of a
   non-idempotent task running in parallel is worse than waiting.
8. **Fold bare acks into substantive replies.** Don't send a message just to say
   "收到" — save tokens.
9. **Cross-vendor is the point, but the invariant is in code, not prose.** Don't
   rely on a worker's model "reading the rules" — the star rejection and echo
   ceiling hold regardless of which vendor drives B. Names are `[A-Za-z0-9_]+`;
   path separators are rejected before any heartbeat file is touched.

## Deployment topologies (where state lives)

- **Project-local install** (state IN the project): `ipc.py` at project root,
  role hook `.claude/hooks/ipc_role.py`, mailbox `./_ipc.db` (+ `-wal`/`-shm`),
  registry `.claude/ipc_roles.json`, heartbeats `_watcher_*.alive` — all
  co-located. SessionStart claim is the project-local hook; the user-level global
  hook auto-defers (`_is_redundant_global`), so no double-claim. Two terminals
  share this mailbox iff both cwd = project root.
- **User-level shared install**: state under `~/.claude/ipc/`. Migrate between the
  two with the project's `migrate_ipc.py`; never rename/move a whole state
  directory by hand.

Confirm which topology a project uses from its `CLAUDE.md` Deployment note before
assuming where `_ipc.db` and the registry live.
