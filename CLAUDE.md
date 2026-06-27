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
6. **A waits for worker replies (watcher).** **[2026-06-27 — default
   watcher is now a persistent Monitor]** Start ONE persistent Monitor (the Monitor
   tool, `persistent=true`) running `python ipc.py watch --me A`: each reply fires a
   tiny **SIGNAL** notification (`NEW MSG #id from …`, **never the body** — a fixed
   short line can't be truncated, unlike inline content which the harness silently
   clips on long messages); on the signal, read the **full** message with `python
   ipc.py peek --me A --tail 3`. The watcher lives the whole session and idle costs
   **~zero turns** — it only fires on a real message, no re-arming. This beats the old re-arm-bash watcher (which paid one full agent turn
   per ~580s of idle, since a background bash is capped at ~600s and a new turn/user
   input can kill it; the Monitor survives both — verified). To wait on several
   workers, let replies stream in and track "how many of N have replied" in your
   **harness task list** (TaskCreate/TaskList, survives compaction). **Fallback if
   Monitor is unavailable:** `python ipc.py recv --me A --block` as a background Bash
   — exits `0` = message (read it), `2` = empty timeout (re-arm without re-reading;
   harness shows it as `status=failed`, a normal timeout not an error); or
   `--block --count N` to barrier-collect N replies in one call. Only use
   watch/recv/peek to receive; never Read the whole `_ipc.db`.

**Auto-relay for workers:** roles and watcher instructions are injected by the
SessionStart hook, so **a worker usually needn't type any command to come online**
(for explicit/manual recovery use `/ipc-recover`; see Recovery below). Mechanism: the
`python .claude/hooks/ipc_role.py claim` wired in `.claude/settings.local.json`
assigns a role the moment a terminal enters the project, by "first-come-first-served
+ a `session_id` registry (`.claude/ipc_roles.json`)" (the first in gets A, the rest
take the lowest free slot B, C, D…; a lockfile guards races; `/clear` doesn't release
the role, the same session reuses it), and injects that role's behavior (A = hub
dispatch/synthesis; worker = `recv --me <self>` once to drain the backlog + start ONE
persistent Monitor running `ipc.py watch --me <self>` as its watcher; bash
`recv --block` is the fallback) as `additionalContext`.

> **Hard rule while a Monitor `watch` is running on an inbox: do NOT also run
> `recv --block` or a manual `recv` on that same inbox.** Both call the same receive
> and would race the unhandled rows → the SAME message delivered twice (a task run
> twice). The heartbeat file is liveness, not a lock — nothing in `ipc.py` enforces
> single-consumer, so this is held by discipline: one watcher per inbox, receive only
> through it.

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
the worker's Monitor signals it, it executes and `send`s the result back, and the same
Monitor keeps listening — no `/loop`, no re-arm.

**Recovery after `/clear` / compaction / hook failure.** `/clear` does NOT release the
role (the registry keeps `session_id`→role; `release()` early-returns on `reason=="clear"`);
it only kills the background **watcher process**. So recovery = re-establish the watcher,
not re-claim the role — and `recv`/`watch` only need the `--me <role>` argument, not the
registry. **Do NOT rely on a bare nudge after `/clear`** — observed (2026-06-28): a cleared worker
comes back without acting on its role (the SessionStart hook does not reliably re-inject on
`/clear`, and even if it did, the "manual floor" means a blank `ok` won't tell the worker
what to do). **Recover explicitly:** run **`/ipc-recover B`** (the command takes the role as
`$ARGUMENTS`, or reads it from any injected context; it drains `recv --me <role>` then starts
the persistent Monitor `watch --me <role>`). If that slash command isn't loaded in the cleared
session, paste the equivalent recipe instead: tell the worker its role, then `recv --me B`
(drain) → start a persistent Monitor `watch --me B` → end turn. A (hub) needs no standby
watcher — it just continues per its hub role.
(Replaces the old `/sub`; `/main` is kept as A's optional hub self-assertion / hook-failure
fallback. A bash `recv --block` fallback watcher, if used instead of the Monitor, must be
re-armed each wake: exit `0`=message, `2`=empty timeout/re-arm without reading, `killed`
[e.g. by `/clear`]=`peek --tail 3` to check for a missed message then re-arm.)
