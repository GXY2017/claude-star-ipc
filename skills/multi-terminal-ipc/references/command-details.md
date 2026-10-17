# Command details: flags, JSON outputs, pending states, redeliver, dispatch gates and clocks, RESULT check

> Split out of `SKILL.md` on 2026-10-03 (lint-skills size rule: body ≤ ~20 KB). Text below is moved verbatim unless marked. Run from the project root (`ipc.py` = `python ~/.claude/ipc/ipc.py`). Read when choosing `--submit-id` / `--no-requeue`, parsing `--json` output, when `pending` shows QUEUED-STALLED / IN-PROGRESS-SILENT / NEEDS-REVIEW, before `redeliver`, when a dispatch gate refuses, when sizing a brief's `预算:` line, or when a RESULT reply prints UNVERIFIED or a `DONE` summary is held.

## Hub: flags, JSON outputs, pending states, redeliver

```
ipc.py send --from A --to B "task" --submit-id fx7    # idempotency key: a resend with the same key prints DUP and reuses the row — re-dispatch after a barrier timeout is SAFE
ipc.py send --from A --to B "task" --no-requeue       # fail-closed non-idempotent task: stale lease parks as NEEDS-REVIEW (never auto-requeued/auto-failed)
ipc.py recv --me A --json                             # NDJSON envelopes {id,ts,from,type,task,session,body}: type is the machine-readable outcome (reply|ack|fail), task echoes in_reply_to, session identifies the sender session — reply session ≠ the task claimant's => that worker /clear-ed, its context is gone, re-brief before a follow-up (grok-build envelope + sessionId-echo ideas)
ipc.py recv --me C --json --no-body                   # same envelopes with body "" and body_chars: for signal-only receivers (the Codex supervisor loop) so no body text is parsed as protocol and ids never fall past an output cap; read text with peek --id
ipc.py peek --me A --id 12 15                          # exact rows by id (signals name them; a batch recv can exceed any --tail)
ipc.py peek --me C --batch [N] [--json] / --batches    # a complete saved recv --json batch (latest, or number N; envelopes carry "batch"), files <state dir>/_recv_<role>_batch_<N>.jsonl, last 100 per role kept; never estimated from truncated output
ipc.py peek --me A --tail 5 --json                     # same, as NDJSON (adds to/unread fields)
ipc.py task --id N [--json]                            # 2026-10-15: one task by id -> {id,state,terminal,from,to,ts,attempts,result,claimed_ts,replies}; state as pending plus DONE/FAILED/CANCELLED; UNKNOWN (exit 1) for a missing id or a non-task row. Read-only (no reap/archive), so a probe loop never mutates the mailbox; STALE = lease dead and not yet reaped. ipc_dispatch.ps1 confirms a claim with it (IN_PROGRESS or DONE = claimed; FAILED/CANCELLED = stop) because pending omits DONE and a quick finish used to read as "not yet claimed"
ipc.py status --watch B --json                         # 2026-10-15: {role,state,alive,busy,squatter,owner_valid,parked,reason,pid,session,busy_pid,claimed}; exit code as the text form. `parked` = alive AND not busy AND registry owner present; ipc_dispatch.ps1 gates on it via Resolve-IpcDispatchGate (BUSY covers ALIVE+BUSY; REPARK covers DOWN and SQUATTER). The text line's compound states must not be substring-matched (the old gate read ALIVE+BUSY as parked). Offline regression: `uv run ~/.claude/evals/test_ipc_states.py`
ipc.py pending --hub A --json                         # NDJSON {id,to,ts,state,attempts} — state machine-readable (QUEUED|QUEUED-STALLED|IN_PROGRESS|IN-PROGRESS-SILENT|STALE|NEEDS-REVIEW); empty --json output = fan-out complete. QUEUED-STALLED = unclaimed >5min with recipient not busy (#1186 black-hole alarm); IN-PROGRESS-SILENT = claimed but no life-sign (claim/ack/linked reply) >30min (#1188 alarm) — display-only, NEVER auto-requeued; check the pane, or redeliver deliberately
ipc.py redeliver --task N --by A                       # reset a claimed-but-never-started task to QUEUED for a fresh delivery (2026-08-17 #1188) — same row, submit-id/attempts kept, so idempotent re-dispatch stays safe; refused if done/tombstoned; pair with ipc_wake_pane.ps1 if the worker is not parked. This replaces the old undocumented "cancel + new submit-id + manual wake" recovery
```

## Hub: mechanical gates & clocks

```
# --- mechanical gates & clocks (2026-08-30, from the 2026-08-29 postmortem; user: "能用脚本固化的就固化") ---
pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile brief.md [-SubmitId k] [-MaxMinutes 15] [-DryRun] [-Force]
                                                      # THE dispatch path. Gate 0a = ipc_brief_lint.py (header role == -Role; `预算: N 分钟` required, <= MaxMinutes;
                                                      # `UNITS: n` x measured min/unit must fit; abs paths exist; no empty product target; title/body "N 份" agree;
                                                      # guard-safe shell shapes; `完成判据:` required). Gate 0b = ipc_pane_probe.py on the recipient PANE status line (DEAD-CTX <=20% /
                                                      # DEAD-LOOP / QUOTA banner / BLOCKED-PROMPT refuse; LOW-CTX <70% warns). Then park -> send -> nudge -> confirm claim.
python ~/.claude/ipc/ipc_pane_probe.py E F [--json] / --all   # what the gate reads: ctx_left, flags (working|idle|recv_loop|permission_prompt|quota_banner), verdict
python ~/.claude/ipc/stall_watch.py [--poll 60] [--once]      # hub's SECOND persistent Monitor (project-root cwd): at 15/30/45 min after each dispatch probes the pane
                                                      # and prints one REPLY-CLOCK verdict (WORKING -> extend / ACT NOW: <reason> / TWO EXTENSIONS USED -> reassign)
python ~/.claude/ipc/ipc_timing.py --since 2026-08-29 [--hub A] [--cwd root]   # dispatch->reply minutes per task and per role, paired by in_reply_to (never by hand)
python ~/.claude/ipc/ipc_estimate.py estimate --role E --brief brief.md        # 2026-09-19: pre-dispatch budget from the cross-project ledger ~/.claude/ipc/timing.jsonl (one row per first linked reply/fail, written by send(); `backfill` rescans every mailbox). Keyed by MODEL (launcher $ModelTag), not role; per-unit p80 x UNITS when n>=5, else the model's total p80, else all models — each tier prints n and INSUFFICIENT when it falls through. Write the suggested number as the brief's `预算:` line; ipc_brief_lint.py's "fits in 15 min" gate uses the ledger's p50 min/unit (typical rate, like the old static table) so it does not over-refuse — backfilled rows include queue wait. `stats [--by model|role|project]` shows p50/p80, per-unit rates, fail share
ipc.py send --from A --to B --body-file brief.md --budget 12                    # optional; default = the brief's `预算: N 分钟` line. Stored as budget_min: drives the CLEAN-summary hold rule below (§ Worker: RESULT-line protocol detail) and the ledger
```

## Worker: RESULT-line protocol detail

```
ipc.py send --from B --to A --in-reply-to N --body-file result.md   # THE result path (2026-09-16): a linked reply closes task N by itself — no separate `done`, no `--type note`. With several open tasks an unlinked reply is REFUSED-LINK (exit 3), never guessed; --in-reply-to naming a task dispatched to someone else is refused too
                                   # RESULT-line protocol (2026-09-19): result.md OPENS with one `RESULT: <absolute path>` line per product file (then any summary). send() checks each path on disk (absolute, exists, mtime >= dispatch) and prints `RESULT CLEAN` / `RESULT UNVERIFIED: <why>` / `RESULT none`. CLEAN => A's watcher (and `recv`) prints one line `DONE #N <- B RESULT: <path>` and A never reads the body. Held only while a sibling task's budget end (dispatch + `预算: N 分钟`, default 15) falls before the flush deadline (oldest held + IPC_HOLD_SECS=900); a long-budget or overdue sibling never delays it. Anything else (a question, partial work, a wrong path) still arrives as `NEW MSG` and A reads it in full — so put a RESULT line ONLY when the product really is on disk
```
