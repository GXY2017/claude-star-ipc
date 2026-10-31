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
   rewrites the brief instead of re-sending it. A third, `HANDOFF: <handoff file>`
   (2026-10-21), is sent only on the hub clock's quota request (caution 14) and reaches
   A as `[HANDOFF]`. `ipc_dispatch.ps1` appends this protocol
   and today's date to every brief by code. Auto-link happens only when exactly one task is open; with several,
   an unlinked reply is refused (2026-09-16 — the old oldest-open guess bound F's
   "#1566 回执" to #1567). A `--type note` never closes anything. Call `ack` periodically on a long
   task or the 1800s ceiling reaps it as "stuck" — `ack` also beats the heartbeat,
   so it is the keep-alive for a worker whose busy beater is missing (any `recv`
   path, parked or one-shot, forks the beater on a delivered task since 2026-09-21;
   `ack` remains the lease extender). For a **non-idempotent**
   task, A dispatches with `--no-requeue`: a stale claim then parks as
   NEEDS-REVIEW instead of being auto-requeued or auto-failed — A decides
   (cancel / probe the worker), the reaper never re-runs it.
   **NEW section (2026-10-17, optional):** after the RESULT lines a worker may add
   NEW items: `- ` bullets under a `NEW:` line (indented lines continue an item);
   text after `NEW:` on that same line counts as one item only when no bullet
   follows. Each item is an assertion plus the consequence of ignoring it. Evidence only: an item cites output, a line number or
   two conflicting sources; speculation and generic reminders are not written.
   **Typed items (2026-10-22, from Agensh arXiv:2609.26781 §2.2 "shared context"):** an
   item may open with `FAIL:` (an approach tried and falsified — the highest-value entry,
   it stops another role from spending budget on it), `FACT:` (confirmed, with evidence)
   or `OBSERVED:` (seen, not confirmed); untyped items stay valid; there is no CLAIM
   (assignment stays with A). Keep an item's first line under 200 chars (send prints
   `NEW-LONG`, never refuses) and put detail on indented lines below it. The tally
   travels with every count (`NEW 3 (FAIL 1, FACT 1)`, `+NEW 3 (FAIL 1): peek …`,
   `recv --json` field `new_types`); `ipc.py board --type FAIL [--grep kw]` lists the
   items across the project's replies and A reads it before writing a brief (case brief
   section 已证伪路径, orchestration-and-dispatch.md rule 6). NEW
   lines count against the brief's existing budget. A closed-type brief (取数,
   mechanical 脚本) says "只交付" and gets no NEW section. A CLEAN reply with items
   reaches A as `DONE … +NEW n: peek …`; every linked reply carries the anchor
   `#task<N>.<role>` for A to quote in later briefs. The item count goes to the
   ledger; A records use with `grade --used-new` (`command-details.md`).
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

Cautions 10–13, 15 and 16 in full: `fleet-operations.md` (this directory) § Fleet
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
    manage-not-operate boundary: `references/claude-main.md` § A 的职责边界与节奏 (Codex: `codex-main.md`).
    Workers still do not self-report quota. **Quota handoff (user 2026-10-21):
    a role about to run out of its window writes its work down before the limit
    stops it**, so another role continues instead of redoing it. The hub clock
    (`ipc.py` `_clock_handoff`, run by `ipc_supervisor.py`) reads each pending
    task's recipient windows from files every tick (`ipc_state.py`); at 90 % of the 5h
    window (`IPC_HANDOFF_PCT`) or 98 % of the 7d window (`IPC_HANDOFF_PCT_7D`; 10 % of
    a week is many 5h windows of work, user 2026-10-21) with the reset more than
    10 min away it types a one-line
    request into a claimed task's pane (a mid-turn message: Claude Code hands it
    to the model when the running tool calls finish; Codex steers; never after
    Ctrl+C or Esc, which interrupt). The worker writes
    `<state dir>/handoff/task-<N>-<role>.md` and sends it as the linked reply,
    first line `HANDOFF: <path>` (verdict `handoff`, kept out of rank and
    estimate stats). A sees `QUOTA-HANDOFF #N -> <role> ...` and, on the reply,
    re-dispatches the rest to a role with room (`ipc_quota.py`): original brief
    plus that file. A queued task is only reported (cancel, re-route); the hub's
    own window at 85 % (`IPC_HANDOFF_HUB_PCT`) prints `QUOTA-HANDOFF hub A`:
    bring `_A_ledger.md` up to date and dispatch nothing new. Once per task,
    role and window reset. Windows come from the statusline state (Anthropic),
    the Codex rollout, and since 2026-10-21 the Kimi Code and Zhipu usage APIs
    (`ipc_quota.py` `kimi()` / `glm()`, cached 60 s). D (DeepSeek) is pay per
    token and has no window: incremental flush (orchestration § brief rule 4) is
    its only protection, together with the balance floor.
15. **Both fleet GUIs vanish at once while every pane and mux server is alive =
    system commit (virtual memory) exhaustion, not a wezterm bug**: diagnose,
    then reattach, never relaunch.
16. **Unforeseeable fleet anomalies go to X in fleet 2, not to A debugging on the
    spot** (user rule 2026-08-30).
17. **A worker is one context window; a long token-heavy serial queue exhausts it
    mid-queue**: dispatch 2–3 items per batch. Full rule and evidence:
    `orchestration-and-dispatch.md` (this directory) § Worker context and queue length.
18. **A refresh per heavy batch is the normal rhythm; plan where it may happen**
    (lifted 2026-10-28 from a project backfill skill; measured 2026-08-16/20: one
    saturated batch took about 50 percentage points of a codex worker's context).
    - Say in the brief that the session is refreshed after each batch, so the worker
      does not treat the interruption as a failure.
    - Before a refresh, let the worker stop at a download or parse stage with its raw
      files, manifest and draft on disk; the next round resumes from disk.
    - Never interrupt a worker while it runs a merge-write script: interrupting the
      model turn kills the child process and can leave the target file half-written.
    - A worker's estimate of its own remaining context is unreliable (five batches in a
      row self-reported about 40% while the footer showed 19%): the hub reads the pane
      footer itself (`orchestration-and-dispatch.md` § Capacity-aware dispatch).
      [[C--Users-<user>-Downloads-----::refresh-codex-pane-e]]

