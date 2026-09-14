---
name: multi-terminal-ipc
description: >-
  Operate cross-model terminal IPC: onboard projects, dispatch/collect work, recover roles, check or reconnect fleets; 多终端、多模型互通、派活、收回复、主从终端、信箱、编队重连. Use for ipc.py operations. Covers hub/workers and window loss without assuming a fleet restart.
---

# multi-terminal-ipc

Operating manual for the project's **multi-terminal IPC** feature: several
persistent, independent Claude Code terminals — which may be driven by models
from **different vendors** — collaborate as peers through a **file-based sqlite
mailbox**. Unlike subagents/same-harness agent teams (single-vendor, single
process), each terminal here is a full separate session; the mailbox is just
files on disk and doesn't care which model drives each terminal.

**This skill is the single authority for the protocol (2026-08-21).** Project
`CLAUDE.md` files no longer restate it — they carry only that deployment's own
facts and hard rules (mailbox key, model bindings, dispatch exceptions), which
override this skill on exactly those points. The claude-star-ipc repo's
`CLAUDE.md` is likewise a pointer here; `install_user.py` ships this skill
user-level, version-locked to the machinery.

## Mental model

- **Star topology, A is the sole hub.** A = master/decider; B, C, D… = workers.
  **Workers talk only to A, never to each other** — collaboration is relayed
  through A. This keeps the anti-echo invariant linear, not an N² mesh.
- **The star is code-enforced**, not just convention: `send()` rejects any
  non-hub→non-hub message (`send --from B --to C` → `StarViolation`, exit 3), and
  an echo ceiling (`hop > ttl`) kills relay loops. A worker driven by *any*
  vendor's model literally cannot message a peer or echo a loop. Hub is
  configurable via `IPC_HUB` (default `A`).
- **The mailbox is `ipc.py` beside `_ipc.db`.** `ipc.py` resolves the DB to its
  own directory. Two terminals share a mailbox **iff both launched with cwd =
  the same project root**. A terminal started elsewhere runs a different (or no)
  `ipc.py` and can never connect. `ipc.py` below = `python ipc.py` run from that
  root.
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

## Hard prerequisite

All terminals **must launch from the same project root** (the cwd when starting
`claude`). Confirm cwd matches before opening a terminal. This is the #1 cause of
"they can't see each other".

## Enable in a new project (onboarding) — `references/onboarding.md`

One per-project artifact only: from the project root run `python ~/.claude/ipc/ipc_role.py enable`
(creates `.claude/ipc.enabled`; validates the user-level install; refuses the home dir and `~/.claude`;
idempotent). Then launch panes via the fleet script, type one line in each worker, verify with
`ipc_role.py status` plus one A→B round-trip. Guards, the per-project CLAUDE.md pointer rule and the
fresh-machine path (`install_user.py`) are in the reference file — read it when the trigger is
在新项目启用 IPC / enable IPC.

## Bringing terminals online

The SessionStart hook auto-claims a role (first-in = A, rest take lowest free
slot B, C, D…) and injects that role's behavior + watcher instructions as
context — so a worker usually needn't type any command to get its role. The
hook is guarded (user rule 2026-08-09): it fires only when `WEZTERM_PANE` or
`IPC_ROLE` is set, so terminals outside WezTerm without an explicit role never
claim. Since 2026-09-02 the gate is inside `ipc_role.py claim` itself
(`_auto_claim_allowed`), not only on Claude's settings.json hook line — before
that, the Codex hooks (`~/.codex/hooks.json` → `codex-compat.js
session-start-ipc`, and a project-level `.codex/hooks.json` calling `claim`
directly) skipped the guard, so a bare `codex` opened in any `ipc.enabled`
project silently auto-claimed a free role (became C in AI财务分析). Codex is
stricter than Claude (user rule 2026-09-02): it claims only with an explicit
`IPC_ROLE`, i.e. only as a fleet member launched by the fleet script; a codex
opened by hand, even inside a WezTerm pane, stays standalone. The project-level
`.codex/hooks.json` claim in AI财务分析 was removed the same day so every codex
claim goes through `codex-compat.js`. Fleet panes are unaffected: the launcher
sets `IPC_ROLE` per pane.

**The one manual floor the hook can't cross:** it can inject instructions but
can't fire a worker's first tool call. So **after opening a worker window, type
any one line (`ok`/`standby`)** — that parks its watcher; any queued backlog then
arrives as signals (WATCHER FIRST, no separate drain step: work done without a
beating watcher reads as stale to the lease reaper and gets requeued mid-flight).
Before dispatching, A should remind the user to type one line in each worker
window.

Force a specific role at launch: `IPC_ROLE=A claude …` (overrides launch order).

**Bring-up order: `ipc_role.py take <role> --session <sid>` FIRST, `watch --me <role>` SECOND.**
The watcher snapshots the registry owner at start and retires on any later change (`WATCHER for X retired: registry owner changed`),
so watch-then-take retires the watcher just parked (D, 2026-08-13) and a watcher over an unclaimed slot is `ALIVE [SQUATTER]`, refused by
`--require-watcher`; `ipc.py` `_REG_ANCHOR_GRACE = 45` s adopts a None/manual → claimed change as the pane's own take — a safety net, not permission.
Mid-task the inverse holds: never `take` or re-park while a claimed task runs — the busy-beater fork rewrites the owner with an empty session, the
parent watcher retires, the task drops to QUEUED; close with `send` + `done --task N`, then `take`, then re-park. [[C--Users-gxy49-Downloads-AI-----::ipc-claim-busy-beater-evicts-owner]] [[C--Users-gxy49-Downloads-----::orphan-watcher-survives-tui-death]]

**Preferred since 2026-08-03 — WezTerm pane fleet (automates all of the above):**
terminals live as panes in one WezTerm window, launched and batch-controlled by
two user-level scripts (role→pane-id map, no window guessing, no hand-typing):

```powershell
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles A,B,C,D,E,F -Project <root>  # spawn lineup, claim roles, park watchers
pwsh ~/.claude/ipc/ipc_newcycle.ps1 [-Roles C] [-Force] [-SkipHub]          # refresh context: per pane kind sends /clear + /ipc-recover (claude) or /new (codex, no wake, never Ctrl+C); BUSY roles skipped; dead codex TUI relaunched instead. This is A's tool when a role runs low on context — clearing beats restarting the pane
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles C -Project <root>          # ONE role back into the LIVE fleet: mux is reused, other panes untouched (this is how A restarts a single worker)
pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1 -Project <root>                   # WHOLE-FLEET restart only: pending check -> tear down mux + GUI + orphan watchers -> relaunch -> verify parks. `-Roles` picks what comes BACK, never what gets torn down: `-Roles C` still kills the mux and every pane incl. A itself (bit 2026-09-03, memory `ipc-single-role-restart`); since 2026-09-03 a `-Roles` list without the hub is REFUSED unless `-Force` Orphan-watcher sweep covers `$FleetRoles` = A–F ∪ old fleet-1 pane map ∪ `-Roles` (fixed 2026-09-04: it used to loop over `-Roles` only, so `-Roles A,C` left B/D/E/F watchers alive = fake ALIVE + stolen mail; memory `fleet-shutdown-orphan-watchers` 第三型).
pwsh ~/.claude/ipc/ipc_fleet2_launch.ps1 -Project <root>                   # second formation: its own mux + GUI, role X only (Claude, --permission-mode auto); ask which project first unless the user already named the project and scope in this request, same rule as fleet 1
pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role F [-KeepRole]                   # clean exit of ONE pane: /exit -> release slot -> confirm watcher retired (caution 10); fleet 1 only
```

**Mux-domain world (2026-08-17, adopted from the paseo evaluation):** pane
processes live under a per-fleet `wezterm-mux-server` (sockets
`~/.local/share/wezterm/fleet{1,2}.sock`, domains defined in `~/.wezterm.lua`);
the GUI window is only a detachable VIEW. A GUI death — accidental close, crash,
an unattended OS/Store update — no longer kills the fleet: **reattach with
`wezterm connect fleet1` (or `fleet2`), no relaunch, context intact.** Check
first with `status --watch <role>`: workers ALIVE + GUI gone = reattach only;
workers DOWN = real teardown, use the restart script. Tearing a fleet down now
means killing its MUX server (the restart/relaunch scripts do this via
`wezterm_mux_pids.json`), not the GUI.

Omit `-Project` and both launchers show the same history-derived menu as
`cc project`, with the home dir appended as an explicit last entry and
pre-selected as the default (launch-dir rule revised 2026-08-11, memory
`home-dir-launch-default`). A headless caller — a Claude session, a piped run —
silently gets the home dir instead of a prompt. Home can never get an
`ipc.enabled` gate (`ipc_role.py enable` refuses it), but fleet panes still
claim roles there: the launchers set `IPC_ROLE` per pane, which alone opts a
session in (`ipc_role.py:92`) and claims deterministically (`ipc_role.py:427`).
The gate only matters for `WEZTERM_PANE` sessions without an explicit
`IPC_ROLE` (bare terminals outside WezTerm never run claim at all — hook guard
2026-08-09); whether a claim lands depends on the harness firing the
SessionStart hook (GLM/DeepSeek panes park a watcher but may never claim). **When launching a fleet on the
user's behalf, ask which project first and pass `-Project` — unless the user already named the
project and scope in this request (2026-09-05 relaxation: do not re-confirm what was just stated;
still validate the `-Project` value and never widen the scope to a whole-fleet restart, another
project, or someone else's running task).**

Role→model bindings (A=claude hub, B=kimi, C=codex gpt-5.6-sol xhigh — rebound
2026-08-30, was glm via ollama, D=deepseek, E=codex, F=glm via Zhipu direct —
added 2026-08-14) are a table at the top of
the launch script; that table is the authority, this list is a snapshot. The old keepalive daemon
is DECOMMISSIONED (same date): before dispatching, check `status --watch <role>`
and wake a DOWN worker by typing into (or relaunching) its pane. Full findings,
traps, and per-bridge caveats: project-lib memory `wezterm-pane-injection-verified`
(AI 财务分析 repo, `.claude/memory/`).

## Command surface (run from project root)

### Hub A — dispatch & collect
```
ipc.py send --from A --to B "task"                    # one worker
ipc.py send --from A --to B,C "task"                  # fan out (one row each)
ipc.py send --from A --to ALL "task"                  # broadcast to every live worker
ipc.py send --from A --to B --body-file task.md       # body from file — REQUIRED for bodies with backticks/$()/quotes (shell mangles them)
ipc.py send --from A --to B "task" --require-watcher  # parked=SENT; mid-task (busy fresh)=QUEUED-BUSY exit 0, delivers when B finishes; parked-but-role-unclaimed=REFUSED-SQUATTER exit 3 (2026-08-17 #1186: an orphan heartbeat is not dispatch evidence — remedy printed: ipc_role.py take <role>); neither=REFUSED exit 3, not queued
ipc.py send --from A --to B "task" --submit-id fx7    # idempotency key: a resend with the same key prints DUP and reuses the row — re-dispatch after a barrier timeout is SAFE
ipc.py send --from A --to B "task" --no-requeue       # fail-closed non-idempotent task: stale lease parks as NEEDS-REVIEW (never auto-requeued/auto-failed)
ipc.py recv --me A                                    # take unread replies (NONE = nothing yet)
ipc.py recv --me A --block                            # block until a reply arrives
ipc.py recv --me A --block --count 3                  # BARRIER: after fanning to 3, wait for all 3 — COUNT TRAP: a worker that text-replies AND runs `done` sends TWO rows (reply + bodyless ack), so the quota can fill on the fastest workers' pairs; size ~2×workers, or tell workers to close with a single message (a text reply alone already marks done)
ipc.py recv --me A --json                             # NDJSON envelopes {id,ts,from,type,task,session,body}: type is the machine-readable outcome (reply|ack|fail), task echoes in_reply_to, session identifies the sender session — reply session ≠ the task claimant's => that worker /clear-ed, its context is gone, re-brief before a follow-up (grok-build envelope + sessionId-echo ideas)
ipc.py peek --me A --tail 5                            # review last 5 WITHOUT marking read
ipc.py peek --me A --tail 5 --json                     # same, as NDJSON (adds to/unread fields)
ipc.py pending --hub A [--detail]                     # tasks dispatched with no reply yet (empty = done)
ipc.py pending --hub A --json                         # NDJSON {id,to,ts,state,attempts} — state machine-readable (QUEUED|QUEUED-STALLED|IN_PROGRESS|IN-PROGRESS-SILENT|STALE|NEEDS-REVIEW); empty --json output = fan-out complete. QUEUED-STALLED = unclaimed >5min with recipient not busy (#1186 black-hole alarm); IN-PROGRESS-SILENT = claimed but no life-sign (claim/ack/linked reply) >30min (#1188 alarm) — display-only, NEVER auto-requeued; check the pane, or redeliver deliberately
ipc.py cancel --task N --by A                          # retract a dispatched task
ipc.py redeliver --task N --by A                       # reset a claimed-but-never-started task to QUEUED for a fresh delivery (2026-08-17 #1188) — same row, submit-id/attempts kept, so idempotent re-dispatch stays safe; refused if done/tombstoned; pair with ipc_wake_pane.ps1 if the worker is not parked. This replaces the old undocumented "cancel + new submit-id + manual wake" recovery
# --- mechanical gates & clocks (2026-08-30, from the 2026-08-29 postmortem; user: "能用脚本固化的就固化") ---
pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile brief.md [-SubmitId k] [-MaxMinutes 15] [-DryRun] [-Force]
                                                      # THE dispatch path. Gate 0a = ipc_brief_lint.py (header role == -Role; `预算: N 分钟` required, <= MaxMinutes;
                                                      # `UNITS: n` x measured min/unit must fit; abs paths exist; no empty product target; title/body "N 份" agree;
                                                      # guard-safe shell shapes). Gate 0b = ipc_pane_probe.py on the recipient PANE status line (DEAD-CTX <=20% /
                                                      # DEAD-LOOP / QUOTA banner / BLOCKED-PROMPT refuse; LOW-CTX <70% warns). Then park -> send -> nudge -> confirm claim.
python ~/.claude/ipc/ipc_pane_probe.py E F [--json] / --all   # what the gate reads: ctx_left, flags (working|idle|recv_loop|permission_prompt|quota_banner), verdict
python ~/.claude/ipc/stall_watch.py [--poll 60] [--once]      # hub's SECOND persistent Monitor (project-root cwd): at 15/30/45 min after each dispatch probes the pane
                                                      # and prints one REPLY-CLOCK verdict (WORKING -> extend / ACT NOW: <reason> / TWO EXTENSIONS USED -> reassign)
python ~/.claude/ipc/ipc_timing.py --since 2026-08-29 [--hub A] [--cwd root]   # dispatch->reply minutes per task and per role, paired by in_reply_to (never by hand)
```

### Worker B/C/D — execute & report
```
ipc.py recv --me B                 # take my unread tasks (mark read)
# Watcher hosting (2026-08-17 #1188 HARD RULE): `watch --me <role>` runs under the Monitor tool ONLY — a background-Bash watch buffers its signals to a file the harness surfaces only on exit (observed 1h55m delivery gap); watch now refuses file-stdout hosts with exit 4
# Monitor host is BASH (2026-08-23): a backslash path `C:\Users\gxy49\.claude\ipc\ipc.py` is stripped to `Usersgxy49.claudeipcipc.py` (exit 2) — write `python ~/.claude/ipc/ipc.py watch --me <role>` or `C:/Users/...` forward slashes; PowerShell-tool calls are unaffected [[C--Users-gxy49-Downloads-------::monitor-host-bash-forward-slash]]
ipc.py send --from B --to A "result"   # reply to A (plain send, no --require-watcher)
ipc.py done --me B --task N         # register task N done (bodyless ack)
ipc.py ack  --me B --task N         # FIRST ACTION after reading a task (2026-08-17): stamps last_seen_ts so A's IN-PROGRESS-SILENT alarm clears; also the lease-extender for long tasks (no --task = all my claimed)
ipc.py fail --me B --task N [--reason ...]   # mark failed (won't requeue)
```

### Role registry (`~/.claude/ipc/ipc_role.py`; legacy project-local installs used `.claude/hooks/ipc_role.py`)
```
ipc_role.py status                 # reconciled view: ownership × heartbeat liveness
ipc_role.py take A --session <sid> # (re)assign a role to this session
ipc_role.py reclaim-dead           # free WORKER slots whose watcher heartbeat is gone (hub exempt — never auto-reclaimed, even though A parks a watcher at boot since 2026-08-21; blind-safe. Rarely needed: claim() sweeps all ghost slots on every SessionStart)
ipc.py status --watch B            # ALIVE(parked, 0) / BUSY(executing, 1 — alive, don't re-dispatch) / DOWN(1) + pid/session; ALIVE with [SQUATTER: no registry owner] exits 1 — matches the send gate's REFUSED-SQUATTER
```

## Orchestration, brief standard, capacity — `references/orchestration-and-dispatch.md`

Read that file before composing a dispatch round. It holds the named dispatch
shapes (handoff / advisor / committee), the three seam-sealing brief rules from
arXiv:2608.16801 (A rules on every cross-worker interface; verifier briefs land
only after the producer delivers; shared context ships as ONE file by path),
and capacity-aware dispatch (scrape each pane's `Ctx N% left` through the fleet
mux before a heavy batch; below ~70% refresh the pane instead of gambling;
context % and vendor quota are independent axes; any refresh discards the
worker's task context, so re-brief with a fresh `--body-file`).

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
requeue can't make a worker redo finished work. The mailbox also self-trims:
past 300 rows, handled/terminal history is lazily archived inside
recv/watch/pending (newest 150 always kept).

## How A waits / recovery after /clear — see the commands

These two procedures live in the user-level slash commands (authoritative, do
not restate here):
- **Hub duties & collecting replies** → `~/.claude/commands/main.md` (`/main`):
  persistent Monitor on `watch --me A`, signal → `peek`, bash fallback. Since
  2026-08-21 (user decision) the hub parks this watcher at BOOT like a worker —
  the fleet launcher sends `/ipc-recover A` to the hub pane too, and newcycle
  re-parks it after /clear. Registry exemption is unchanged: a dead hub
  heartbeat is never auto-reclaimed (`status` shows "watcher down", role kept).
- **Recovery after /clear / compaction / hook failure** →
  `~/.claude/commands/ipc-recover.md` (`/ipc-recover <role>`). Key invariant:
  `/clear` keeps the role, kills only the watcher — recovery = re-park the
  watcher, never re-claim or switch roles.

## 注意事项 (the cautions that actually bite)

1. **Same project root, always.** Different cwd → different `ipc.py` → no
   connection. Verify before opening any terminal.
2. **Receive only with `recv`; never `Read` the whole `_ipc.db`.** `recv` returns
   only new unread rows, so history doesn't re-enter context and token cost
   doesn't grow with message count. Use `peek --tail N` to review.
3. **Worker: every A→worker message MUST get exactly one `send` back to A** — even
   a bare ack. Once a worker `recv`s a message A can no longer see it; "consume
   without replying" is, to A, a lost message its `--block` watcher waits on
   forever. Never chit-chat, never decide on A's behalf, stop when done.
4. **A: tag coordination as `--type note`, not task.** Only *real dispatched work*
   is a `task`. Acks, wrap-ups, FYIs, "restart your watcher" must be
   `--type note` — notes are exempt from the lease/reaper and never appear in
   `pending`, so they won't get phantom-re-delivered 1800s later. A note needs no
   reply.
5. **A dispatches with `--require-watcher`.** The role registry survives `/clear`
   while the watcher process is dead, so registry ≠ "listening now". Without the
   flag a task can drop into a black hole. On refusal (exit 3), nudge the worker
   window to re-park its watcher, then resend.
6. **Workers close tasks explicitly.** `done --me <self> --task N` when finished
   (a plain reply also marks done, but auto-links to the *oldest* open task, so
   `done --task N` is safer with several open). Call `ack` periodically on a long
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

10. **Clean exit is a script, not Ctrl+C / closing the pane.**
    `pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role <r>` sends `/exit`, takes "Exit and
    stop tasks" ("Move to background" is the orphan-watcher switch), releases the
    slot and confirms the watcher retired. Third-party panes (glm / deepseek) never
    fire SessionEnd, so a hand-closed pane leaves a watcher beating under a green
    light and dispatches fall into an unread mailbox. Fleet 2's X is not in the
    pane map: run the same steps against `fleet2.sock` + `wezterm_fleet2.json`
    pane 0 (memory `runtime-upgrade-fleets-down` has the verified sequence).
11. **From inside a pane, launch or connect to the OTHER fleet only with
    `WEZTERM_*` env stripped** — the child inherits this fleet's
    `WEZTERM_CONFIG_FILE`/`WEZTERM_UNIX_SOCKET`, loads the wrong mux config and
    dies with "domain not found" (memory `wezterm-env-inheritance-gui-launch`;
    `ipc_fleet1_restart.ps1` strips them itself).
12. **Every pane-spawn site uses `$script:PaneShell` (MSI pwsh, absolute path),
    never bare `pwsh`.** A Microsoft-Store MSIX PowerShell update force-kills every
    process launched from the Store package — it wiped both fleets on 2026-08-17
    (memory `store-pwsh-update-kills-fleet`). `mux-fleet{1,2}.lua` `default_prog`
    pins the same path for the initial pane.
13. **Node.js / wezterm upgrades happen only with BOTH fleets down.** Pane-spawned
    `node.exe` MCP servers (dozens) lock `node.exe` — the MSI defers to reboot and
    npm/node mismatch meanwhile — and a wezterm upgrade replaces the mux server
    under every pane. Non-fleet MCP `node.exe` (Claude Desktop, other CLI sessions)
    must go too. Runbook incl. the winget-nightly hash-mismatch fallback: memory
    `runtime-upgrade-fleets-down`.

14. **A runs a 15-minute reply clock; workers do not self-report quota** (user
    decision 2026-08-29). No reply 15 min after dispatch → A inspects (`pending
    --hub A --detail`, `ipc_role.py status`, pane text); working normally → one
    more 15 min; stalled/abnormal → act now; two extensions max. Tasks are sliced
    so a recipient can deliver in ≤15 min. Full procedure and the
    manage-not-operate boundary: `~/.claude/commands/main.md` § A 的职责边界与节奏.
    Worker quota self-reporting was considered and deferred: no vendor CLI
    exposes an in-turn quota read (Claude `/usage` and Codex `/status` are
    TUI-only; `statusline.js:10-11`), so A reads quota banners from the pane
    during the 15-min inspection instead.
15. **Both fleet GUIs vanish at once while every pane and mux server is alive =
    system commit (virtual memory) exhaustion, not a wezterm bug** (2026-08-31,
    twice in one day). Diagnose in this order, then reattach — never relaunch:
    (a) `Get-Process wezterm-mux-server, pwsh` alive → GUI-only death;
    (b) `~/.local/share/wezterm/wezterm-mux-server.exe-log-*.txt` shows
    `encoding PDU to client: Allocation error : not enough memory`;
    (c) `Get-WinEvent` provider `Microsoft-Windows-Resource-Exhaustion-Detector`
    id 2004 names the top three commit consumers (vmmemWSL / Bloomberg
    clientrater / AnyTXT ATOHelper that day). Fix the consumer, then
    `wezterm connect fleet1` / `fleet2` (env-stripped when launched from inside
    the other fleet). Measured levers and pagefile/WSL settings: memory
    `fleet-gui-commit-exhaustion` in the home project.
16. **Unforeseeable fleet anomalies go to X in fleet 2, not to A debugging on the
    spot** (user rule 2026-08-30). Toolchain / pane / registry / watcher faults: A
    writes a brief for X — deliverable = root cause / file:line fixed / how to verify;
    "do not touch panes running tasks"; no `ipc_role.py reset` — and the fix lands in
    shared code, never a one-off script. [[C--Users-gxy49-Downloads-----::fleet-anomalies-to-x]]
17. **A worker is one context window; a long token-heavy serial queue exhausts it
    mid-queue** (B died after card 1/6, 2026-06-28 — returned results do not free the
    worker's context). Dispatch 2–3 items per batch and re-dispatch the remainder;
    workers write each product to a file and keep a one-line result in context; a
    dozen mechanical items go to subagents/Workflow, not one serial worker.
    [[C--Users-gxy49-Downloads-AI-----::ipc-worker-queue-context-limit]]

## Deployment topologies (where state lives)

- **User-level shared install (CURRENT default)**: machinery once at
  `~/.claude/ipc/`, per-project state (mailbox, registry, heartbeats) under
  `~/.claude/projects/<encoded-cwd>/ipc/`, opt-in via `.claude/ipc.enabled`.
- **Project-local install (LEGACY — superseded)**: `ipc.py` at project root,
  role hook `.claude/hooks/ipc_role.py`, mailbox `./_ipc.db` (+ `-wal`/`-shm`),
  registry `.claude/ipc_roles.json`, heartbeats `_watcher_*.alive` — all
  co-located. SessionStart claim is the project-local hook; the user-level global
  hook auto-defers (`_is_redundant_global`), so no double-claim. Two terminals
  share this mailbox iff both cwd = project root. Migrate a legacy project to
  user-level with `migrate_ipc.py` (already run everywhere here; archived under
  `_archive/2026-07-02-ipc/` in the origin repo); never rename/move a whole
  state directory by hand.

Confirm which topology a project uses from its `CLAUDE.md` Deployment note before
assuming where `_ipc.db` and the registry live.
