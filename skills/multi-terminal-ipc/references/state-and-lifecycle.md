# Delivery guarantees, task lifecycle, deployment topologies

> Split out of `SKILL.md` on 2026-10-03 (lint-skills size rule: body ≤ ~20 KB). Text below is moved verbatim unless marked. Read when judging whether a worker is parked, busy or dead, when a task is requeued / STALE / NEEDS-REVIEW / failed after retries, before changing `--lease`, or when a project may be a legacy project-local install.

## Code-level guarantees (from `SKILL.md` § Mental model)

- **More code-level guarantees you can lean on.** Role names come from
  `_BASE_ROLES` in `~/.claude/ipc/ipc.py` (single source of truth, currently
  A–F + legacy CODEX/DS + second-formation X; extend the tuple to add workers).
  Message claim is one atomic single-consumer `UPDATE … RETURNING` — two
  consumers can never both get a row; a duplicate watcher is wasteful, not
  corrupting. Worker liveness is a two-file heartbeat split:
  `_watcher_<X>.alive` = a consumer is parked; `_worker_<X>.busy` = a claimed
  task is executing (beaten by a daemon `recv` forks automatically at claim
  time, exits at done/fail/cancel or the lease ceiling). The reaper tests
  `alive OR busy`, so a correctly-busy worker is never reaped for forgetting to
  `ack`; `--require-watcher` accepts a fresh busy beat as liveness (QUEUED-BUSY,
  exit 0, delivered at the worker's next park). Caveats: the reaper never
  touches unclaimed rows (a queued task waits for the worker's next recv,
  visible in `pending` the whole time), and a QUEUED-BUSY `--type note` has no
  tracking at all — fire-and-forget.

## Task lifecycle

A hub→worker message defaults to `msg_type='task'` carrying a **lease**
(`--lease` seconds, default 1800, **counted from CLAIM time** — reset when the
worker claims the row, so queue wait doesn't eat the runway and a requeued task
retries under a fresh ceiling). A stale claimed task — worker's heartbeat dies
(process gone) **or** the hard lease ceiling passes (alive-but-stuck) — is lazily
**requeued** by a reaper (runs inside recv/watch/pending) and re-delivered, or
marked `failed` once `attempts` hits the requeue cap (`MAX_ATTEMPTS`, default 3 —
a module-level constant in `ipc.py`, overridable only per-process via env
`IPC_MAX_ATTEMPTS`; there is **no** per-message flag). A `--no-requeue` task is exempt from both outcomes:
its stale claim parks as **NEEDS-REVIEW** in `pending` (fail-closed) until the
hub `cancel`s or the worker `done`/`fail`s it (`ack` revives it to
IN_PROGRESS); unresolved NEEDS-REVIEW rows are also never auto-archived. Requeued rows that already have a reply/ack are
**done-dropped at claim** (claimed silently, never redelivered), so a phantom
requeue can't make a worker redo finished work. A task delivered again (reaper
requeue or `redeliver`, `--no-requeue` included: attempts > 1 at claim) reaches
the worker with the fixed prefix `[REQUEUED attempt k/MAX] 此单此前已投递过、未结清就被收回。…`
(recv text, the `NEW MSG` signal tag, the recipient's own `peek`; `--json` adds
`requeued` and `attempt` instead): check the working directory, the brief's
product paths, git history and files on disk, skip finished parts, and confirm
send/commit/order actions did not already happen; the stored body is never
changed (2026-10-31, from DeepSeek Super-agent article). The mailbox also self-trims:
past 300 rows, handled/terminal history is lazily archived inside
recv/watch/pending (newest 150 always kept). Since 2026-10-17 archived rows are
moved, not deleted: they go to the table `messages_archive` in the same `_ipc.db`
(same columns plus `archived_ts`, key (id, archived_ts); exactly the copied ids
are removed, in one transaction; the manual `archive --keep N` does the same),
and `peek --id` / `task --id` read the newest version back with `[archived]`. Unfinished tasks and
held CLEAN replies are never moved. The archive is unbounded and keeps bodies: it
is the durable record for analysis (a manual prune would be a future command).

## Deployment topologies (where state lives)

- **User-level shared install (CURRENT default)**: machinery once at
  `~/.claude/ipc/`, per-project state (mailbox, registry, heartbeats) under
  `~/.claude/projects/<key>/ipc/` (`<key>` = `<slug>-<hash>` of the project
  root, not Claude's encoded-cwd dir), opt-in via `.claude/ipc.enabled`.
- **Project-local install (LEGACY — superseded)**: `ipc.py` at project root,
  role hook `.claude/hooks/ipc_role.py`, mailbox `./_ipc.db` (+ `-wal`/`-shm`),
  registry `.claude/ipc_roles.json`, heartbeats `_watcher_*.alive` — all
  co-located. SessionStart claim is the project-local hook; the user-level global
  hook auto-defers (`_is_redundant_global`), so no double-claim. Two terminals
  share this mailbox iff both cwd = project root. Migrate a legacy project to
  user-level with `migrate_ipc.py` (already run everywhere here; archived under
  `_archive/2026-07-02-ipc/` in the origin repo); never rename/move a whole
  state directory by hand.
