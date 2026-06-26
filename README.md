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

## Why

Subagents are great for one-shot parallel fan-out within a single session. This is
different: **persistent, independent sessions**. Use it when you want, e.g., an
Opus hub (A) orchestrating a cheaper Sonnet worker (B) that does token-heavy data
hauling, a Haiku worker (C) chewing through large files, and a different-provider
model (D) doing an adversarial cross-check — each watchable in its own window.

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

The star rule is a **convention** carried by the role prompts injected at
`SessionStart` + the `CLAUDE.md` protocol. `ipc.py` itself is a neutral mailbox
and does not enforce it (intentional: keeps test names and future topologies open).

## How it works

- **Mailbox** — every message is one row with a single recipient and a `handled`
  flag, so `recv` only ever returns *new* messages addressed to you. History never
  re-enters context; token cost stays flat as the log grows.
- **Push wake** — a worker parks `recv --me B --block` as a *background* process.
  When A sends, that process exits and the harness re-invokes the worker with the
  task. No polling loop in the agent.
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

## Files

| File | Role |
|---|---|
| `ipc.py` | the mailbox CLI (send/recv/peek/archive/status) — stdlib only |
| `.claude/hooks/ipc_role.py` | `SessionStart`/`SessionEnd` hook: auto-assigns roles, injects behavior |
| `CLAUDE.md` | the protocol the terminals follow (path-agnostic; Chinese) |
| `install_ipc.py` | one-command installer into another project |

## Install

Into an existing Claude Code project:

```sh
python install_ipc.py /path/to/your/project
```

It copies `ipc.py` + the hook, merges the `SessionStart`/`SessionEnd` hooks into
the target's `.claude/settings.local.json` (preserving everything already there),
and appends the protocol to the target's `CLAUDE.md`. It is **idempotent** and
**never copies runtime state** (`_ipc.db`, `_watcher_*.alive`, `ipc_roles.json` are
created fresh per project). Manual install: copy those 4 files yourself and wire the
two hooks (see `examples/settings.snippet.json`).

## Usage

```sh
# A dispatches to one worker, only if its watcher is parked:
python ipc.py send --from A --to B "your task" --require-watcher

# A dispatches to several / broadcasts:
python ipc.py send --from A --to B,C "task"
python ipc.py send --from A --to ALL "task"

# A waits for replies (run in the background; it exits on the first reply):
python ipc.py recv --me A --block

# A worker drains its inbox / parks its watcher:
python ipc.py recv --me B
python ipc.py recv --me B --block

# Probe a worker's watcher: ALIVE (exit 0) / DOWN (exit 1)
python ipc.py status --watch B

# Look without consuming; trim old read rows:
python ipc.py peek --me A --tail 5
python ipc.py archive --keep 50
```

**Requirements**: all terminals must launch with `cwd` = the project root (where
`ipc.py` lives) — that's how they share the same `_ipc.db`.

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
- **Conventions, not hard guards.** Star topology and "workers reply to A only" are
  enforced by prompt discipline, not code.
- Names (sender/recipient) must match `[A-Za-z0-9_]+` (they become heartbeat
  filenames).

## License

MIT — see [LICENSE](LICENSE).
