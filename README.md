# Claude Code Multi-Terminal IPC (star topology)

A tiny, dependency-free way to let **several Claude Code terminals running in the
same project collaborate** — one hub plus N workers — through a SQLite mailbox.

Two Claude Code sessions can't share context. This kit gives them a mailbox
(`ipc.py`, stdlib `sqlite3`, DB file `_ipc.db`) plus a *push* wake mechanism, so a
master terminal **A** can dispatch work to worker terminals **B, C, D…** and get
results back — each worker keeping its own long-lived context, model, and account.

> The agent-behavior spec (the protocol the terminals follow) lives in
> [`CLAUDE.md`](CLAUDE.md) and is written in Chinese. This README is the English
> overview.

## Why — cross-vendor LLM collaboration

Subagents and "agent teams" live inside a single vendor's harness: one orchestrator
spawns short-lived helpers that share the same provider. **This is different — it
lets independent, persistent terminal sessions backed by models from *different
companies* collaborate as peers.** The mailbox is just files on disk, so it does
not care which model drives each terminal.

So you can pair, e.g., an Anthropic **Opus** hub (A) with a **GLM** (Zhipu) worker
(B) — or any cross-vendor mix — each in its own window, with its own context,
account, and cost profile. A natural split: Opus orchestrates and makes the calls;
a cheaper worker does token-heavy data hauling; another chews through large files;
a **different-vendor** model runs an adversarial cross-check that doesn't share the
hub's blind spots (no single-model monoculture). That kind of cross-company pairing
is exactly what subagents and same-harness agent teams can't do.

## Topology — star, A at the hub

```
        B (worker)
        |
A (hub) + ── C (worker)
        |
        D (worker)
```

- **A** = master: initiates, decides, dispatches, reconciles. Only A decides
  whether to continue.
- **B/C/D** = workers: respond to A and stop. They talk **only to A, never to each
  other** — keeping the anti-echo invariant linear instead of N².

The star rule is **code-enforced**: `send` rejects any non-hub→non-hub message
(`StarViolation`, exit 3) and an echo ceiling (`hop > ttl`) kills relay loops — a
worker driven by any vendor's model literally cannot message a peer. The hub is
configurable via `IPC_HUB` (default `A`). Delivery to arbitrary *names* stays open
(role assignment, not delivery, is what `ROLES` governs).

## How it works

- **Mailbox** — every message is one row with a single recipient and a `handled`
  flag, so `recv` only ever returns *new* messages addressed to you. History never
  re-enters context; token cost stays flat as the log grows.
- **Push wake** — a worker keeps a watcher alive so the harness re-invokes it when
  a message lands. Two forms: **(recommended, Claude Code)** one persistent **Monitor**
  running `ipc.py watch --me B` — each message fires a tiny `NEW MSG #id` *signal*
  notification (never the body, so long messages aren't truncated; read the full text
  with `peek`), the watcher lives the whole session, and idle costs ~zero turns;
  **(fallback / any CLI)** park `recv --me B --block` as a *background* process that
  exits on the first message (re-arm each wake; capped ~600s so long idle re-wakes
  periodically). One watcher per inbox is the convention, and it is now **code-backed**:
  every claim is a single atomic `UPDATE … RETURNING`, so two consumers can never take the
  same row (no double-delivery), and starting a fresh `watch --me B` makes any older/orphan
  watcher of that role **self-retire** within one poll via a generation token — so a watcher
  stranded in a dead/cleared session can't keep claiming (black-holing) the inbox. *(The
  Monitor watcher is validated for surviving turns/compaction and for no-truncation;
  hours-long idle stability is not yet stress-tested.)*
- **Heartbeat liveness** — a parked `--block` watcher touches `_watcher_<me>.alive`
  every poll. `send --require-watcher` refuses to queue to a worker whose watcher
  isn't live (so tasks never vanish into a dead mailbox). A registered role does
  **not** prove liveness — staleness of the heartbeat file is the signal.
- **Auto role assignment** — a `SessionStart` hook (`ipc_role.py`) claims the
  lowest free role (A, then B, C, D…), first-come-first-served, keyed by Claude's
  `session_id`, and injects that role's behavior as context. `/clear` keeps the
  role; the registry survives, the watcher does not.
- **Fan-out** — `--to B,C` dispatches to several workers; `--to ALL` broadcasts to
  every live worker. Each recipient gets its own row with its own `handled` flag,
  so a broadcast is never "consumed" by whoever reads first.
- **Task lifecycle + weak rollback** — a dispatched message is a *task* carrying a
  lease, **counted from claim time** (reset when the worker claims the row, so queue
  wait doesn't eat the runway and a requeued task retries under a fresh ceiling). If
  the worker's watcher dies (process gone) **or** the hard `--lease` ceiling passes
  (alive-but-stuck — the Monitor keeps beating while a session is frozen, so the
  ceiling is what catches "stuck"), a lazy reaper requeues the task (or marks it `failed`
  after `--max-attempts`) — so work is never silently lost when a worker dies mid-flight,
  with no daemon and no OS process killing. Rows that already have a reply are
  **done-dropped at claim** (claimed silently, never redelivered), so a phantom requeue
  can't make a worker redo finished work. Verbs: `done` / `ack` (extend lease **and**
  beat the heartbeat — the keep-alive for a watcher-less worker mid-task) / `fail`
  / `cancel` / `reap`; `pending --detail` shows each task's derived state
  (`QUEUED`/`IN_PROGRESS`/`STALE`/`FAILED`). Pure coordination is sent `--type note`,
  which is exempt from the lease/reaper. Discipline: **workers start the watcher
  BEFORE doing any work** (backlog arrives as signals) — the reaper AND-joins
  heartbeat with the ceiling, so heartbeat-less work reads as stale.
- **Self-trimming mailbox** — past 300 rows, handled/terminal history is lazily
  archived inside `recv`/`watch`/`pending` (newest 150 kept); `archive --keep N`
  remains for manual trims.
- **Shell-safe bodies** — `send --body-file <path>` reads the body in Python, never
  the shell: required for bodies containing backticks/`$()`/quotes, which a
  shell-argument body would mangle or even *execute*.

## Files

| File | Role |
|---|---|
| `ipc.py` | the mailbox CLI (send/recv/watch/peek/archive/status) — stdlib only. **Neutral core: any CLI that runs python+bash can use it.** |
| `.claude/hooks/ipc_role.py` | `SessionStart`/`SessionEnd` hook: auto-assigns roles, injects behavior. *Claude Code integration layer.* |
| `.claude/commands/main.md`, `ipc-recover.md` | optional slash commands: `/main` (A self-assert hub), `/ipc-recover` (rebuild role+watcher after `/clear`/compaction/hook-failure). *Claude Code only.* |
| `skills/multi-terminal-ipc/SKILL.md` | the operating + onboarding skill (mental model, command surface, enable-in-a-new-project, recovery, cautions). Installed to `~/.claude/skills/` by `install_user.py`. *Claude Code only.* |
| `CLAUDE.md` | the protocol the terminals follow (path-agnostic; Chinese) |
| `install_user.py` | **recommended** installer — installs the machinery once at `~/.claude/ipc/`, registers the global `SessionStart`/`SessionEnd` hook, and gates it per project on a `.claude/ipc.enabled` file. One copy serves every opted-in project (each gets its own mailbox by launch cwd). |
| `migrate_ipc.py` | migrate a legacy in-project mailbox to the user-level layout. |
| `install_ipc.py` | **legacy** per-project installer (copies `ipc.py` + hook + commands into one project root). Superseded by `install_user.py`; kept for the per-project topology. |

> **Two layers.** The portable, model/CLI-neutral core is `ipc.py` + the `CLAUDE.md`
> protocol — any harness that can run python+bash can drive it. Everything else
> (the `SessionStart` hook, `/main` `/ipc-recover`, the Monitor watcher, the
> background-bash push-wake) is **Claude Code harness integration**; it makes the kit
> ergonomic under Claude Code (any model backend) but does **not** port to a different
> CLI. "Cross-vendor" here means different *models* under the same Claude Code harness,
> not different harnesses.

## Install

**Recommended — user-level (one install serves every project):**

```sh
python install_user.py
```

Installs `ipc.py` + `ipc_role.py` once under `~/.claude/ipc/`, registers a global
`SessionStart`/`SessionEnd` hook (merged into `~/.claude/settings.json`, preserving
what's there), and ships the `/main` `/ipc-recover` slash commands plus the
`multi-terminal-ipc` skill. The machinery then
sits idle until a project **opts in** with a gate file `.claude/ipc.enabled` — only then
does the hook claim a role. Enable a project with one command (validates the install,
refuses the home dir, creates the gate; idempotent):

```sh
cd /path/to/your/project
python ~/.claude/ipc/ipc_role.py enable
``` Each opted-in project gets its **own** mailbox, resolved by
the launch cwd (`~/.claude/projects/<key>/ipc/`), so two terminals share a mailbox iff
they launch from the same project root. Have a legacy in-project mailbox? `python
migrate_ipc.py` moves it to the user-level layout.

**Legacy — per-project copy:**

```sh
python install_ipc.py /path/to/your/project
```

Copies `ipc.py` + the hook + the slash commands into one project root and merges the
hooks into its `.claude/settings.local.json`. Both installers are **idempotent** and
**never copy runtime state** (`_ipc.db`, `_watcher_*.alive`, `ipc_roles.json` are created
fresh). Manual install: copy the files yourself and wire the two hooks (see
`examples/settings.snippet.json`).

## Usage

```sh
# A dispatches to one worker, only if its watcher is parked:
python ipc.py send --from A --to B "your task" --require-watcher

# A dispatches to several / broadcasts:
python ipc.py send --from A --to B,C "task"
python ipc.py send --from A --to ALL "task"

# Body from a file — REQUIRED when the body contains backticks/$()/quotes
# (a shell-argument body gets mangled or even executed by the shell):
python ipc.py send --from A --to B --body-file task.md

# A waits for replies (run in the background; it exits on the first reply):
python ipc.py recv --me A --block
# A waits for ALL N replies after a fan-out (BARRIER: one call collects all N):
python ipc.py recv --me A --block --count 3
# recv --block exit code: 0 = returned message(s) (read them); 2 = empty timeout
# (re-arm without re-reading — shown as status=failed, but a normal timeout, not an error)

# Recommended watcher (Claude Code): run this under the Monitor tool, persistent=true —
# emits a tiny "NEW MSG #id" signal per message (never the body); read full with peek:
python ipc.py watch --me B

# A worker parks its watcher FIRST (backlog arrives as signals; read via peek) —
# working without a beating watcher reads as stale to the lease reaper:
python ipc.py watch --me B        # under Monitor (persistent), or:
python ipc.py recv --me B --block # bash fallback; mid-task run `ack --me B` to stay alive

# Probe a worker's watcher: ALIVE (exit 0) / DOWN (exit 1)
python ipc.py status --watch B

# Look without consuming; trim old read rows:
python ipc.py peek --me A --tail 5
python ipc.py archive --keep 50   # manual trim; auto-trims lazily past 300 rows anyway
```

**Requirements**: all terminals must launch with `cwd` = the same project root —
that's how they resolve the same mailbox (user-level install: per-cwd under
`~/.claude/projects/<key>/ipc/`; legacy local install: `_ipc.db` beside `ipc.py`).

**The one manual step**: a hook can inject instructions but can't make a worker
fire its first background command before the worker receives its first user input.
So after opening a worker terminal, type any one line (e.g. "ok") and it will park
its watcher and run autonomously thereafter.

## Limitations

- **Workers are serial.** Each worker processes one turn at a time. A's `--block`
  watcher is edge-triggered (wakes on the first reply), so collecting N workers'
  replies means re-arming and counting. For a synchronized parallel join, use
  in-session subagents instead.
- **Single machine.** It's a local SQLite file + heartbeat files; not networked.
- **Mostly code-enforced now.** Star topology ("workers reply to A only") and
  single-consumer delivery used to be prompt conventions; they are now enforced in code —
  `send` rejects a non-hub→non-hub message (`StarViolation`), an echo ceiling (`hop > ttl`)
  kills relay loops, and each claim is one atomic `UPDATE … RETURNING`. What stays a
  convention is the higher-level orchestration discipline: only **A** decides whether to
  continue, and synthesis stays with A.
- Names (sender/recipient) must match `[A-Za-z0-9_]+` (they become heartbeat
  filenames).

## License

MIT — see [LICENSE](LICENSE).
