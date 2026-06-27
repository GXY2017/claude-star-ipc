# Project protocol

> **Positioning: cross-vendor LLM collaboration.** This lets persistent, independent
> terminal sessions driven by models from *different companies* collaborate as peers
> (e.g. an Anthropic Opus hub A + a Zhipu GLM worker B) — unlike subagents /
> same-harness agent teams, which are single-vendor. The mailbox is just files on
> disk; it doesn't care which model drives each terminal.

> This CLAUDE.md is the agent behavior spec auto-loaded by Claude Code when cwd =
> this project. The section below is the multi-terminal IPC protocol; it is
> path-agnostic and is copied verbatim into other projects by `install_ipc.py`.

## Multi-Terminal IPC Protocol (star topology)

Several Claude Code terminals can run in this project at once. They are independent
sessions that can't see each other; they exchange messages through `ipc.py` (stdlib
sqlite3, database `_ipc.db`).

**Topology: star, A is the sole hub.** A = master terminal; B, C, D… = worker
terminals. **Workers talk only to A, never to each other** — collaboration is
relayed through A. This keeps the anti-echo invariant (only A decides whether to
continue) linear rather than an N² mesh (a mesh echoes and deadlocks). The number of
workers is set by `ROLES` in `.claude/hooks/ipc_role.py` (currently A,B,C,D; to add
workers, extend that tuple).

> ⚠️ **The star is a CONVENTION, not enforced by code.** `ipc.py` is a neutral
> mailbox — `send --from B --to C` runs fine at the code level (intentional: keeps
> test names and future topologies open). "Workers reply only to A, never to each
> other" is held by the role prompts injected at SessionStart + this protocol. So
> every worker must obey: reply only to A for any message received, and never
> proactively `send` to another worker — otherwise you can trigger the very echo
> deadlock the star prevents. (Names are limited to `[A-Za-z0-9_]+`; names with path
> separators are rejected by ipc.py before any heartbeat file is touched.)

**Prerequisite (hard): all terminals must run under the same Claude Code project**,
i.e. the cwd when launching `claude` is **this project root (the directory where
`ipc.py` lives)**. Reason: `ipc.py` locates the database at `_ipc.db` next to itself,
and this `CLAUDE.md` protocol is auto-loaded only when cwd = this project. A terminal
started elsewhere uses a different `ipc.py` and a different `_ipc.db`, so the two
mailboxes can never connect. Confirm the cwd matches before opening a terminal.
(This section onward is path-agnostic and can be copied verbatim to other projects;
install in one step with `python install_ipc.py <target-project>`.)

**Fixed master/worker roles, to avoid echo loops:**
- **A = master terminal (hub)**: initiator/decider. Only A decides whether to
  continue; A orchestrates dispatch, synthesis, and reconciliation.
- **B/C/D = worker terminals**: executors. Respond passively, stop when done, don't
  proactively ask follow-ups, don't chit-chat, don't decide on A's behalf.

**Commands (run from the project root):**
```
python ipc.py send --from A --to B "msg"      # send to one worker
python ipc.py send --from A --to B,C "task"   # fan out to several workers (one row each, independent read-flags)
python ipc.py send --from A --to ALL "task"   # broadcast to every live worker (claimed roles except A)
python ipc.py send --from A --to B "task" --require-watcher  # refuse (exit 3) & don't queue if the recipient's watcher isn't parked (with several recipients: judged per-recipient — live ones queued, dead ones refused, exit 3 if any refused)
python ipc.py status --watch B                # is B's watcher parked? ALIVE(exit 0)/DOWN(exit 1)
python ipc.py recv --me B                     # take NEW unread messages addressed to me, mark them read
python ipc.py recv --me A --block             # block until a new message arrives (prints NONE (timeout) on timeout)
python ipc.py recv --me A --block --count 3   # BARRIER: after fanning out to 3 workers, one call blocks until all 3 reply (timeout returns the k<3 received)
python ipc.py peek --me A --tail 5            # view the last 5 without marking read
python ipc.py archive --keep 50               # trim old read messages, keep the most recent 50
```

**Watcher liveness (heartbeat):** each poll of `recv --block` (default every 2s)
touches a `_watcher_<me>.alive` heartbeat file, removed on exit/timeout; killed by
`/clear`, the file goes stale. From this A can tell whether a worker's watcher is
actually listening right now — note the role registry in `.claude/ipc_roles.json`
**cannot** be used as the criterion (the role survives `/clear` while the watcher
process is dead). **A always dispatches to a worker with `send --require-watcher`**:
if the watcher isn't parked it is refused (exit 3) and not queued, avoiding sending a
task into a black hole; on refusal, nudge that worker's window to re-park its watcher,
then resend. A worker replies to A with plain `send` (no `--require-watcher` — A's
reply shouldn't be blocked even when A isn't watching).

**Rules for Claude:**
1. **Only use `recv` to receive; never `Read` the whole `_ipc.db`** — `recv` returns
   only new unread messages, so history doesn't re-enter context and token cost
   doesn't grow with message count. Use `peek --tail N` to review context.
2. Always send via `send`, spelling out `--from` / `--to`.
3. **Worker (B/C/D)**: receive task → execute → `send --from <self> --to A` the
   result → **stop**. Don't proactively `recv` for the next one (unless `/loop` is
   set), and never send directly to another worker (star: reply to A only).
   **Hard rule: any A→worker message returned by the watcher/`recv` MUST get one
   `send` back to A** — even just an acknowledgement or "received, no action needed".
   Never "consume (mark read) without replying". Once a worker `recv`s a message, A
   can no longer see it; not replying is, to A, a lost message — A's `--block`
   watcher will wait forever. Don't judge test/greeting messages as "no reply needed"
   and silently swallow them; reply once first, then decide what's next.
4. **A (master/hub)**: converse normally; `recv --me A` when you need a worker's
   reply. **To dispatch to several workers at once** use `--to B,C` or `--to ALL`.
   **To wait for all of them, use the barrier** `recv --me A --block --count N`
   as a single background Bash: it parks until N replies have arrived and returns
   them together, so the tally lives in that one process and survives A's context
   compression — prefer this over manually re-arming `--block` N times and counting
   replies by hand (that silently breaks when the count is lost on compaction). On
   timeout the barrier returns the k<N replies received; A diffs the senders it got
   against the workers it dispatched to, finds who is absent, probes them
   (`status --watch X`, or a `--require-watcher` ping) and re-dispatches — but cap
   re-dispatch (≤2 tries) and then `log` the slice as failed rather than looping.
   Don't blind-re-dispatch on timeout alone: a worker may just be slow, and a second
   copy of a non-idempotent task running in parallel is worse than waiting (current
   read-only data pulls are idempotent, so the risk is low — but keep it in mind if
   parallel work ever extends to writes). Synthesis (reconciliation, coverage check,
   final call) always stays with A, never delegated to workers.
5. Fold a bare acknowledgement ("got it") into the substantive reply where possible;
   don't send a message just to acknowledge — save tokens.
6. **A waits for a worker reply automatically (lightweight watcher)**: after sending
   a task, run `python ipc.py recv --me A --block` as a **background Bash**
   (`run_in_background`); A immediately regains control to do other things, and when a
   worker `send`s back, that command exits and the harness feeds the reply in and
   wakes A — this is push, no polling by A. A watcher reports once: for multiple
   round-trips (or to wait on several workers), resend/re-arm a background `--block`
   each round (or use the `--count N` barrier to collect all N in one call). **Exit
   code tells you what happened without re-reading the output**: `recv --block` exits
   `0` when it returns message(s) — read them — and `2` on an empty timeout — just
   re-arm, don't re-read. The harness shows exit 2 as `status=failed`/`NONE (timeout)`;
   that is a normal park timeout (default 580s, under bash's 600s cap), **not an
   error**. Skipping the output read on every idle timeout is the main token saving
   for a long-parked hub or worker.

**Auto-relay for workers:** roles and watcher instructions are injected by the
SessionStart hook, so **a worker usually needn't type `/sub`**. Mechanism: the
`python .claude/hooks/ipc_role.py claim` wired in `.claude/settings.local.json`
assigns a role the moment a terminal enters the project, by "first-come-first-served
+ a `session_id` registry (`.claude/ipc_roles.json`)" (the first in gets A, the rest
take the lowest free slot B, C, D…; a lockfile guards races; `/clear` doesn't release
the role, the same session reuses it), and injects that role's behavior (A = hub
dispatch/synthesis; worker = immediately `recv --me <self>` to drain the backlog +
park `recv --me <self> --block` as a background Bash watcher) as `additionalContext`.

**The one manual floor:** the hook can only "inject instructions", it can't fire a
worker's first tool call for it — Claude won't auto-run a background command before
receiving its first user input, and waking a worker depends on Claude's own
background Bash watcher (a pure OS background process can't push-wake Claude). So
**after opening a worker window, type any one line ("standby"/"ok" etc.) and it will
auto-drain the backlog + park its watcher** per the injected instructions;
"self-driving with zero keystrokes on open" is impossible — that's a harness floor,
not a config defect. (Before dispatching, A should remind the user to type one line
in each worker window to bring it online.)

After that it self-drives: a worker idles without spending tokens; when A dispatches,
the watcher exits, the harness wakes the worker to execute and `send` the result
back, then the worker re-arms a new watcher — no `/loop` needed. `/sub` is a
**manual fallback only** (migration period when the hook isn't active, or to re-park
a watcher manually after `/clear`; it drains the unread backlog first, then re-arms).
For only-occasional exchanges, a manual `recv` is cheaper.
