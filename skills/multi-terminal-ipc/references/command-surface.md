## Command surface (run from project root)

### Hub A — dispatch & collect
```
ipc.py send --from A --to B "task"                    # one worker
ipc.py send --from A --to B,C "task"                  # fan out (one row each)
ipc.py send --from A --to ALL "task"                  # broadcast to every live worker
ipc.py send --from A --to B --body-file task.md       # body from file — REQUIRED for bodies with backticks/$()/quotes (shell mangles them)
ipc.py send --from A --to B "task" --require-watcher  # parked=SENT; mid-task (busy fresh)=QUEUED-BUSY exit 0, delivers when B finishes; parked-but-role-unclaimed=REFUSED-SQUATTER exit 3 (2026-08-17 #1186: an orphan heartbeat is not dispatch evidence — remedy printed: ipc_role.py take <role>); neither=REFUSED exit 3, not queued
ipc.py recv --me A                                    # take unread replies (NONE = nothing yet)
ipc.py recv --me A --block                            # block until a reply arrives
ipc.py recv --me A --block --count 3                  # BARRIER: after fanning to 3, wait for all 3 — COUNT TRAP: a worker that text-replies AND runs `done` sends TWO rows (reply + bodyless ack), so the quota can fill on the fastest workers' pairs; size ~2×workers, or tell workers to close with a single message (a text reply alone already marks done)
ipc.py peek --me A --tail 5                            # review last 5 WITHOUT marking read
ipc.py peek --me A --id 12 15                          # exactly those ids (from NEW MSG / DONE signals), no marking
ipc.py peek --me C --batch [N] / --batches             # a complete saved recv --json batch (latest or N; numbered files, last 20 kept): recovery when tool output was truncated
ipc.py pending --hub A [--detail]                     # tasks dispatched with no reply yet (empty = done)
ipc.py task --id N [--json]                           # ONE task's state (QUEUED|IN_PROGRESS|DONE|FAILED|CANCELLED|NEEDS-REVIEW|STALE|UNKNOWN), read-only; use it to confirm a claim - pending omits DONE tasks (2026-10-15)
ipc.py board --type FAIL [--grep kw]                  # typed NEW items (FAIL / FACT / OBSERVED, 2026-10-22 from Agensh) across this project's replies, read-only; read before writing a brief so no role retries a falsified path: references/command-details.md
ipc.py status --watch B --json                        # {alive,busy,squatter,owner_valid,parked,reason}; scripts gate on `parked` only (2026-10-15)
ipc.py cancel --task N --by A                          # retract a dispatched task
ipc.py send --from A --to D --body-file review.md --for-task N   # task subscription (2026-10-31, from DeepSeek Super-agent article): D's CLEAN reply is copied by code to #N's owner as a note (N open only); A's DONE line ends `->fwd B#<note>`. Hub->worker tasks only, else REFUSED-LINK exit 3; ipc_dispatch.ps1 -ForTask N passes it. What forwards: references/command-details.md § Hub: --for-task forwarding
# --submit-id / --no-requeue, --json envelopes, pending --json states (QUEUED-STALLED / IN-PROGRESS-SILENT / NEEDS-REVIEW), redeliver, archive table, routing ledger (`类型:`, `ipc_estimate.py rank`, `grade`): references/command-details.md
# --- mechanical gates & clocks (2026-08-30, from the 2026-08-29 postmortem; user: "能用脚本固化的就固化") ---
pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile brief.md [-SubmitId k] [-ForTask N] [-MaxMinutes 15] [-DryRun] [-Force]
                                                      # THE dispatch path: brief lint + recipient pane probe, then park -> send -> nudge -> confirm claim. Gate rules, probes, the reply clock, timing, budget estimates: references/command-details.md
python ~/.claude/ipc/ipc_supervisor.py start|stop|status  # OUT-OF-SESSION reply clock (2026-10-20): ticks ipc.clock_tick every 60 s as a detached process, dismisses BLOCKED-NOTICE itself (Enter / Esc), writes the rest to <state>/_supervisor_A.events which `watch --me A` relays verbatim; while its heartbeat is fresh the watch runs no clock and `ipc.py clock` refuses. ipc_wezterm_launch.ps1 starts it; log <state>/_supervisor_A.log
                                                      # QUOTA-HANDOFF lines (2026-10-21): a claimed task's role reached 90 % of its 5h window or 98 % of its 7d window -> the clock typed a handoff request into its pane; on its `[HANDOFF]` reply re-dispatch the rest (original brief + handoff file) to a role with room. `queued, not started` -> cancel + re-route. `hub A` (85 %) -> update _A_ledger.md, dispatch nothing new. Caution 14
python ~/.claude/ipc/ipc_state.py A E [--all]          # per-role ctx %, quota windows, cache, activity age FROM FILES (2026-10-20): statusline.js writes ~/.claude/ipc/state/<ROLE>.json (IPC_ROLE + settings.json refreshInterval 30 s); codex roles read their registered rollout; Kimi Code / Zhipu roles (state vendor kimi-code / zhipu-cn) get windows from the vendor usage API via ipc_quota, cached 60 s (2026-10-21). ipc_pane_probe.py / ipc_quota.py / the reply clock use it first; the pane regex is the fallback. A claimed task whose transcript grew within 15 min is not probed (ipc.py _CLOCK_ACTIVE_S)
```

### Worker B/C/D — execute & report
```
ipc.py recv --me B                 # take my unread tasks (mark read)
ipc.py recv --me B --block --signal-only --timeout 3500   # IDLE PARK (2026-10-22, Claude workers, the default park): hosted as a BACKGROUND Bash (run_in_background), no turn while parked; exits on delivery (exit 0: watch-shaped `NEW MSG #id` signals in the task's output file -> peek --id -> ack -> work -> reply -> re-park) or on timeout (exit 2: re-park). recv forks the busy beater for a delivered task; the heartbeat is kept on exit (<= max_age false-alive). Replaces the Monitor watch for workers (idle: 3 turns per 30 min -> 1 per ~58 min); A keeps `watch` (clock relay, DONE compression). From Agensh's event-driven runtime (arXiv:2609.26781 App. B); verified 2026-10-22 (700 s background run survives, delivery wakes the session)
# Watcher hosting (2026-08-17 #1188 HARD RULE): on Claude, `watch --me <role>` runs under the Monitor tool ONLY — a background-Bash watch buffers its signals to a file the harness surfaces only on exit (observed 1h55m delivery gap); watch now refuses file-stdout hosts with exit 4
# Codex has no Monitor and an infinite watch never wakes the model (2026-07-28): stand by with the `$multi-terminal-ipc recover` step 3 supervisor — one-shot `recv --me <role> --block --keep-heartbeat` in a loop, re-armed before each notify(); it lives only within the current turn
# Monitor host is BASH (2026-08-23): a backslash path `C:\Users\<user>\.claude\ipc\ipc.py` is stripped to `Users<user>.claudeipcipc.py` (exit 2) — write `python ~/.claude/ipc/ipc.py watch --me <role>` or `C:/Users/...` forward slashes; PowerShell-tool calls are unaffected [[C--Users-<user>-Downloads-------::monitor-host-bash-forward-slash]]
ipc.py send --from B --to A --in-reply-to N --body-file result.md   # THE result path (2026-09-16): a linked reply closes task N by itself — no separate `done`, no `--type note`. With several open tasks an unlinked reply is REFUSED-LINK (exit 3), never guessed; --in-reply-to naming a task dispatched to someone else is refused too
                                   # RESULT-line protocol (2026-09-19): result.md OPENS with one `RESULT: <absolute path>` line per product file (caution 6). send()'s disk check, the `DONE #N` summary and its hold rule: references/command-details.md
                                   # QUOTA HANDOFF (2026-10-21): a line typed into the pane mid-task, "IPC A: your 5h quota is N% used ...", comes from the hub clock: finish the current step, write the handoff file it names (first line `HANDOFF: <path>`), send it as the linked reply, stop. Hub side and thresholds: references/operational-cautions.md caution 14
ipc.py send --from B --to A --type note "progress"   # progress/FYI only — a note never closes a task (F's four note-typed results left #1592-#1598 open, 2026-09-16)
ipc.py done --me B --task N         # close task N WITHOUT a text result (bodyless ack); redundant after a linked reply
ipc.py ack  --me B --task N         # FIRST ACTION after reading a task (2026-08-17): stamps last_seen_ts so A's IN-PROGRESS-SILENT alarm clears; also the lease-extender for long tasks (no --task = all my claimed)
ipc.py fail --me B --task N [--reason ...]   # mark failed (won't requeue)
```

### Role registry (`~/.claude/ipc/ipc_role.py`; legacy project-local installs used `.claude/hooks/ipc_role.py`)

`ipc_role.py status` / `take` / `reclaim-dead` and `ipc.py status --watch <role>`
(ALIVE / BUSY / DOWN / `[SQUATTER]` exit codes): `references/bring-up.md` § Role
registry. Read it when verifying a claim, re-taking a role or reading a watcher state.

