# Project protocol

> **Positioning: cross-vendor LLM collaboration.** This lets persistent, independent
> terminal sessions driven by models from *different companies* collaborate as peers
> (e.g. an Anthropic Opus hub A + a Zhipu GLM worker B) — unlike subagents /
> same-harness agent teams, which are single-vendor. The mailbox is just files on
> disk; it doesn't care which model drives each terminal.

> This CLAUDE.md is the agent behavior spec auto-loaded by Claude Code when cwd =
> this project. The section below is the multi-terminal IPC protocol; it is
> path-agnostic and applies to any project that opts in.

> **Deployment (current): IPC is a USER-LEVEL feature, enabled per project.**
> `ipc.py` and `ipc_role.py` live once at `~/.claude/ipc/` (Windows:
> `C:\Users\<you>\.claude\ipc\`); there is no per-project copy. The mailbox /
> sqlite DB is resolved by the launch cwd — each enabled project gets its own
> mailbox under `~/.claude/projects/<encoded-cwd>/ipc/`, so two terminals share a
> mailbox iff they launch from the same project root. A project opts in with a gate
> file `.claude/ipc.enabled` (present in this project); the global SessionStart hook
> only claims a role when that gate exists. Install the user-level machinery once
> with `python install_user.py`; migrate a legacy in-project mailbox with
> `migrate_ipc.py`. (The old per-project installer `install_ipc.py`, which copied
> `ipc.py` into each project root, is the LEGACY local-install topology — superseded
> and archived under `_archive/`.) In the command examples below, `ipc.py` is
> shorthand for `python ~/.claude/ipc/ipc.py`.

## Multi-Terminal IPC Protocol (star topology)

Several Claude Code terminals can run from the same project at once. They are
independent sessions that can't see each other; they exchange messages through
the user-level `ipc.py` (stdlib sqlite3), against this project's own mailbox DB
(resolved by cwd, see Deployment note above).

**Topology: star, A is the sole hub.** A = master terminal; B, C, D… = worker
terminals. **Workers talk only to A, never to each other** — collaboration is
relayed through A. This keeps the anti-echo invariant (only A decides whether to
continue) linear rather than an N² mesh (a mesh echoes and deadlocks). The number of
workers is set by `_BASE_ROLES` in `~/.claude/ipc/ipc.py` — the single source of truth,
which `ipc_role.py` imports (currently A,B,C,D; to add workers, extend that tuple).

> ✅ **The star is now CODE-ENFORCED (2026-06-28).** `send()` rejects any
> non-hub→non-hub message with `StarViolation` (`send --from B --to C` → `REJECTED`,
> exit 3), and an echo ceiling (`hop > ttl`) kills relay loops — so a worker driven
> by ANY vendor's model literally cannot message a peer or echo a loop, regardless
> of how it reads this prose. The hub is configurable via `IPC_HUB` (default `A`).
> Workers still reply only to A as the normal flow, but the invariant no longer
> depends on the model obeying the convention. (Names are limited to `[A-Za-z0-9_]+`;
> path separators are rejected before any heartbeat file is touched.)

**Prerequisite (hard): all terminals must launch from the same project root**, i.e.
the cwd when launching `claude` is **this project's root directory**. Reason: the
user-level `ipc.py` resolves the mailbox DB from the launch cwd (one mailbox per
encoded-cwd under `~/.claude/projects/<encoded-cwd>/ipc/`), and this `CLAUDE.md`
protocol is auto-loaded only when cwd = this project. A terminal started from a
different directory resolves a different mailbox, so the two can never connect.
Confirm the cwd matches, and that `.claude/ipc.enabled` exists (the opt-in gate),
before opening a terminal. (This section onward is path-agnostic and applies to any
opted-in project; enable a new project with `python ~/.claude/ipc/ipc_role.py enable`
run from its root — validates the install, refuses the home dir, creates the
`.claude/ipc.enabled` gate — after the one-time `python install_user.py`.)

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
python ipc.py send --from A --to B --body-file task.md   # body from file — REQUIRED for bodies with backticks/$()/quotes (the shell would mangle/execute them)
python ipc.py send --from A --to B "task" --require-watcher  # refuse (exit 3) & don't queue if the recipient's watcher isn't parked (with several recipients: judged per-recipient — live ones queued, dead ones refused, exit 3 if any refused)
python ipc.py status --watch B                # is B's watcher parked? ALIVE(exit 0)/DOWN(exit 1)
python ipc.py recv --me B                     # take NEW unread messages addressed to me, mark them read
python ipc.py recv --me A --block             # block until a new message arrives (prints NONE (timeout) on timeout)
python ipc.py recv --me A --block --count 3   # BARRIER: after fanning out to 3 workers, one call blocks until all 3 reply (timeout returns the k<3 received)
python ipc.py peek --me A --tail 5            # view the last 5 without marking read
python ipc.py archive --keep 50               # trim old read messages, keep the most recent 50 (also auto-trims lazily once the table exceeds 300 rows, inside recv/watch/pending)
python ipc.py pending --hub A                 # tasks A dispatched with NO reply yet (empty=fan-out complete); replaces counting replies by hand
python ipc.py status --watch B                # now also prints pid/session of B's watcher (ghost-detectable)
```

**Task lifecycle + weak rollback (2026-06-30; lease semantics updated 2026-07-02):**
a hub→worker message defaults to `msg_type='task'` carrying a **lease** (`--lease`
seconds, default 1800, **counted from CLAIM time** — the lease is reset when the worker
claims the row, so queue wait doesn't eat the runway and a requeued task retries under
a fresh ceiling; `--lease 0` = pure heartbeat). A claimed task that goes stale —
the worker's watcher heartbeat dies (process gone) **or** the hard lease ceiling falls
(alive-but-stuck) — is lazily **requeued** by a reaper (runs inside recv/watch/pending)
and re-delivered, or marked `failed` after `--max-attempts` (default 3). Verbs:
```
python ipc.py done --me B --task N            # register task N done (bodyless ack reply)
python ipc.py ack  --me B [--task N]          # extend the lease on a long task (no --task = renew all my claimed)
python ipc.py fail --me B --task N [--reason ...]   # mark task failed (tombstone), won't requeue
python ipc.py cancel --task N --by A          # hub-only: cancel a dispatched task
python ipc.py reap --me B | --hub A           # force the lazy reaper (debug; auto-runs in recv/watch/pending)
python ipc.py pending --hub A --detail        # each task's derived state QUEUED/IN_PROGRESS/STALE/FAILED + attempts
```
Two disciplines this lifecycle REQUIRES (a plain prose reply is no longer enough by itself):
- **A — tag coordination as `--type note`.** Only *real dispatched work* should be a
  `task`. Acks, wrap-ups, FYIs, "restart your watcher", etc. must be sent
  `--type note` — `note` is exempt from the lease/reaper and never appears in `pending`,
  so it won't get phantom-re-delivered 1800s later. A `note` is FYI and needs no reply.
- **B/C/D — close the task.** When a dispatched task is done, run
  `done --me <self> --task N` (a plain reply also marks done, but replies auto-link to
  the *oldest* unanswered task, so `done` is safer when several are open). During a long
  task call `ack --me <self>` periodically or the 1800s ceiling reaps it as "stuck" —
  `ack` also refreshes the heartbeat, so it keeps a watcher-less worker (bash-fallback
  mid-task) alive to the reaper too. For
  a non-idempotent task A should dispatch with `--max-attempts 1` so a stuck one fails
  instead of silently re-running.
- **Workers start the watcher BEFORE doing any work.** The reaper AND-joins heartbeat
  with the lease ceiling, so work executed without a beating watcher reads as stale and
  gets requeued mid-flight. (Requeued-but-already-answered rows are done-dropped at claim
  — claimed silently, never redelivered — so the failure mode is bounded, but
  watcher-first is still the discipline.)

**Role registry management (`~/.claude/ipc/ipc_role.py`, 2026-06-28):** role
assignment is no longer pure launch-order roulette.
```
IPC_ROLE=A claude ...                              # designate THIS window's role at launch (overrides launch order)
python ~/.claude/ipc/ipc_role.py status           # reconciled view: registry ownership X heartbeat liveness (FREE/live/DORMANT/SQUATTER)
python ~/.claude/ipc/ipc_role.py take A --session <sid>   # registry-level (re)assign a role to this session (what /main should call; evicts prior holder)
python ~/.claude/ipc/ipc_role.py reclaim-dead     # free slots whose watcher heartbeat is gone (safer than reset; keeps live roles)
```
`claim()` now reclaims a DORMANT slot (stale/absent heartbeat) instead of being
blocked forever by a dead claim. Heartbeats carry `{ts,pid,session}` so a ghost
watcher is identifiable.

**Watcher liveness (heartbeat):** each poll of `recv --block` (default every 2s)
touches a `_watcher_<me>.alive` heartbeat file, removed on exit/timeout; killed by
`/clear`, the file goes stale. From this A can tell whether a worker's watcher is
actually listening right now — note the role registry (`ipc_roles.json`, under this
project's user-level mailbox dir `~/.claude/projects/<encoded-cwd>/ipc/`)
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
6. **A waits for worker replies (watcher).** **[LOCAL TRIAL 2026-06-27 — default
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
`python ~/.claude/ipc/ipc_role.py claim` wired into the global SessionStart hook
(`~/.claude/settings.json`, gated on the project's `.claude/ipc.enabled`)
assigns a role the moment a terminal enters an opted-in project, by
"first-come-first-served + a `session_id` registry (`ipc_roles.json` under
`~/.claude/projects/<encoded-cwd>/ipc/`)" (the first in gets A, the rest
take the lowest free slot B, C, D…; a lockfile guards races; `/clear` doesn't release
the role, the same session reuses it), and injects that role's behavior (A = hub
dispatch/synthesis; worker = start ONE persistent Monitor running `ipc.py watch --me
<self>` **FIRST** — any queued backlog arrives as signals within seconds, no separate
drain step, and watcher-first keeps the lease reaper off work that would otherwise run
heartbeat-less; bash `recv --block` is the fallback, on which a mid-task worker should
`ack --me <self>` every few minutes since ack also beats the heartbeat) as
`additionalContext`.

> **Single-consumer is now CODE-ENFORCED (2026-06-28).** `recv`/`watch` claim each
> row with one atomic `UPDATE ... RETURNING` under SQLite's write lock, so two
> consumers on the same inbox can never both get the same message — the old "one
> watcher per inbox" discipline is no longer load-bearing (double-delivery is
> structurally impossible). Running a second `recv` alongside a Monitor `watch` is
> now merely wasteful, not corrupting. Still prefer one watcher per inbox for clarity.

**The one manual floor:** the hook can only "inject instructions", it can't fire a
worker's first tool call for it — Claude won't auto-run a background command before
receiving its first user input, and waking a worker depends on Claude's own
background Bash watcher (a pure OS background process can't push-wake Claude). So
**after opening a worker window, type any one line ("standby"/"ok" etc.) and it will
park its watcher (backlog then arrives as signals)** per the injected instructions;
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
registry. **Do NOT rely on a bare nudge after `/clear`** — observed (2026-06-28): a cleared
worker comes back without acting on its role (the SessionStart hook does not reliably re-inject
on `/clear`, and even if it did, the "manual floor" means a blank `ok` won't tell the worker
what to do). **Recover explicitly:** run **`/ipc-recover B`** (a USER-LEVEL command installed to
`~/.claude/commands/ipc-recover.md`, available in every opted-in project; it takes the role as
`$ARGUMENTS`, or reads it from any injected context; it starts the persistent Monitor
`watch --me <role>` FIRST — backlog arrives as signals, read via `peek`). **孤儿盯哨现由代码处理,不靠语义提醒(2026-06-30 代际令牌):**
起新 `watch --me <role>` 时该 watcher 领一个递增代际号,使**同角色**任何旧/孤儿盯哨(如挺过 `/clear`
仍在跑的旧进程)在下一轮 poll 自动退役(打印 `WATCHER ... retired` 后干净退出)——残留在死会话里的盯哨
不会再黑洞本信箱,恢复时**无需手动找停同角色盯哨**。(唯一例外是代码管不到的:跑错角色——`watch --me D`
不会被 `watch --me B` 退掉,那是两个不同信箱,仍须手动 `TaskStop` 切到对的角色。) If that slash command
isn't loaded in the cleared session, paste the equivalent recipe instead: tell the worker its role, then
start a persistent Monitor `watch --me B` FIRST (backlog arrives as signals; read with `peek`) → end
turn. A (hub) needs no standby
watcher — it just continues per its hub role (or `/main`, which now also updates registry ownership
via `ipc_role.py take A`).
(Replaces the old `/sub`; `/main` is kept as A's optional hub self-assertion / hook-failure
fallback. A bash `recv --block` fallback watcher, if used instead of the Monitor, must be
re-armed each wake: exit `0`=message, `2`=empty timeout/re-arm without reading, `killed`
[e.g. by `/clear`]=`peek --tail 3` to check for a missed message then re-arm.)
