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
    `orchestration-and-dispatch.md` (this directory) § Worker context and queue length.

