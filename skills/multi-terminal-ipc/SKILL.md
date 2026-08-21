---
name: multi-terminal-ipc
description: >-
  Operating guide for the cross-vendor multi-terminal IPC mailbox — several
  independent Claude Code terminals (possibly driven by different companies'
  models, e.g. an Anthropic hub A + a Zhipu/GLM worker B) collaborating as peers
  through a file-based sqlite mailbox (`ipc.py`). Use whenever the user wants to
  ENABLE this collaboration in a new project (onboarding), dispatch work to
  another terminal, coordinate a hub/worker (A/B/C/D/E/F) setup, fan out a task to
  workers and collect replies, bring a worker online, recover an IPC role after
  /clear, or asks about the multi-terminal / 多终端 / 多大模型 互通 / 派活给 B /
  主终端从终端 / 星型拓扑 / 信箱 mailbox protocol. Triggers on "在新项目启用/
  接入/开通 多终端协作/IPC"、"这个项目也要多模型互通"、"enable IPC in this
  project"、"set up multi-terminal collaboration"、"派活/派给 B"、"多终端协作"、
  "跨模型互通"、"收 B 的回复"、"让 B 上线"、"hub/worker"、"fan out to workers",
  reattaching or checking a fleet whose window died — "编队窗口关了/没了"、
  "重连编队"、"编队还在吗"、"reattach the fleet"、"fleet GUI closed"、
  `wezterm connect fleet1/fleet2` — and any operation against `ipc.py`.
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

## Enable in a new project (onboarding)

The machinery is USER-LEVEL (one copy at `~/.claude/ipc/`, global hooks in
`~/.claude/settings.json`); a new project needs exactly ONE per-project artifact —
the opt-in gate file. One command creates it, with guards:

```
cd <the new project root>
python ~/.claude/ipc/ipc_role.py enable   # write the expanded path on Windows/PowerShell — a quoted "~" is never expanded
```

`enable` validates the user-level install (ipc.py importable, claim hook
registered), **refuses the home dir and `~/.claude`** (every launch dir becomes a
registered project; user config must never be one), and creates
`.claude/ipc.enabled` at the project root. Idempotent. Manual equivalent: create
that empty gate file yourself.

Then bring terminals online:
1. Open each terminal with cwd = the project root. **Claim is guarded (user rule
   2026-08-09): the SessionStart hook runs only when `WEZTERM_PANE` or
   `IPC_ROLE` is set** (never in child sessions) — a bare terminal outside
   WezTerm never claims a role. Launch via the fleet script (presets `IPC_ROLE`
   per pane) or pin a window's role with `IPC_ROLE=B claude`; among
   guard-passing sessions without a preset role, first in = A/hub, the rest
   take the lowest free slot.
2. Type one line (`ok`/`standby`) in each worker window — the manual floor; its
   watcher parks and any backlog arrives as signals.
3. Verify: `ipc_role.py status` shows the roles live, then one A→B round-trip
   (`send` a test task, worker replies, `pending --hub A` returns to empty).

What you do NOT need: no per-project copy of `ipc.py`, no per-project hooks, no
mailbox setup — state auto-creates under `~/.claude/projects/<key>/ipc/` on first
use, one isolated mailbox per project. In the project's own CLAUDE.md add only a
one-line pointer ("协作协议见 `multi-terminal-ipc` skill") plus any
deployment-specific rules — never paste the protocol (user rule 2026-08-21:
project docs must not restate user-level content).

Fresh **machine** rather than fresh project? Run `python install_user.py` from the
claude-star-ipc repo once (deploys machinery + slash commands + this skill), then
per-project `enable` as above.

## Bringing terminals online

The SessionStart hook auto-claims a role (first-in = A, rest take lowest free
slot B, C, D…) and injects that role's behavior + watcher instructions as
context — so a worker usually needn't type any command to get its role. The
hook is guarded (user rule 2026-08-09): it fires only when `WEZTERM_PANE` or
`IPC_ROLE` is set, so terminals outside WezTerm without an explicit role never
claim.

**The one manual floor the hook can't cross:** it can inject instructions but
can't fire a worker's first tool call. So **after opening a worker window, type
any one line (`ok`/`standby`)** — that parks its watcher; any queued backlog then
arrives as signals (WATCHER FIRST, no separate drain step: work done without a
beating watcher reads as stale to the lease reaper and gets requeued mid-flight).
Before dispatching, A should remind the user to type one line in each worker
window.

Force a specific role at launch: `IPC_ROLE=A claude …` (overrides launch order).

**Preferred since 2026-08-03 — WezTerm pane fleet (automates all of the above):**
terminals live as panes in one WezTerm window, launched and batch-controlled by
two user-level scripts (role→pane-id map, no window guessing, no hand-typing):

```powershell
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles A,B,C,D,E,F -Project <root>  # spawn lineup, claim roles, park watchers
pwsh ~/.claude/ipc/ipc_newcycle.ps1                                         # new task cycle: /clear + /ipc-recover per pane
pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1 -Project <root>                   # restart fleet 1: pending check -> tear down mux + GUI + orphan watchers -> relaunch -> verify parks
pwsh ~/.claude/ipc/ipc_fleet2_launch.ps1                                    # second formation: its own mux + GUI, role X only
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
user's behalf, ask which project first and pass `-Project`.**

Role→model bindings (A=claude hub, B=kimi, C=glm via ollama, D=deepseek,
E=codex, F=glm via Zhipu direct — added 2026-08-14) are a table at the top of
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
```

### Worker B/C/D — execute & report
```
ipc.py recv --me B                 # take my unread tasks (mark read)
# Watcher hosting (2026-08-17 #1188 HARD RULE): `watch --me <role>` runs under the Monitor tool ONLY — a background-Bash watch buffers its signals to a file the harness surfaces only on exit (observed 1h55m delivery gap); watch now refuses file-stdout hosts with exit 4
ipc.py send --from B --to A "result"   # reply to A (plain send, no --require-watcher)
ipc.py done --me B --task N         # register task N done (bodyless ack)
ipc.py ack  --me B --task N         # FIRST ACTION after reading a task (2026-08-17): stamps last_seen_ts so A's IN-PROGRESS-SILENT alarm clears; also the lease-extender for long tasks (no --task = all my claimed)
ipc.py fail --me B --task N [--reason ...]   # mark failed (won't requeue)
```

### Role registry (`~/.claude/ipc/ipc_role.py`; legacy project-local installs used `.claude/hooks/ipc_role.py`)
```
ipc_role.py status                 # reconciled view: ownership × heartbeat liveness
ipc_role.py take A --session <sid> # (re)assign a role to this session
ipc_role.py reclaim-dead           # free WORKER slots whose watcher heartbeat is gone (hub exempt — watcher-less by design; blind-safe. Rarely needed: claim() sweeps all ghost slots on every SessionStart)
ipc.py status --watch B            # ALIVE(parked, 0) / BUSY(executing, 1 — alive, don't re-dispatch) / DOWN(1) + pid/session; ALIVE with [SQUATTER: no registry owner] exits 1 — matches the send gate's REFUSED-SQUATTER
```

## Orchestration patterns (adopted 2026-08-17 from the paseo evaluation)

Named dispatch shapes for common cross-model collaborations, so A composes a
brief from a template instead of improvising. All are plain `ipc.py` usage — no
new machinery. (Provenance: paseo's `/paseo-handoff` / `/paseo-advisor` /
`/paseo-committee` skills; adapted to the star mailbox.)

- **Handoff (接力: 一家模型出方案, 另一家实现).** A (or one worker) produces a
  PLAN as a file; A reviews/approves it, then dispatches implementation to a
  DIFFERENT role with the plan attached verbatim:
  `send --from A --to D --body-file plan-brief.md --submit-id <key>`.
  The brief must carry the plan file's path AND the guard-safe shell constraints
  (dispatch briefs copy printed values; workers can't read A's history). Use
  when vendors have complementary strengths (e.g. Claude plans, Codex/DeepSeek
  implements) or to keep the planner's context clean for review.
- **Advisor (顾问: 只要第二意见, 不移交工作).** One worker, read-only charter:
  `send --from A --to B --require-watcher "ADVISORY ONLY - do NOT modify any
  file. Question: <decision + the options>. Reply: your recommendation + top
  risks, <=N lines."` A keeps ownership and synthesizes; the advisor's reply is
  input, not a decision (worker rule 3 unchanged). Cheap-tier advisors: wake E
  with `-Model luna` for simple reviews.
- **Committee (合议: 两个立场相反的模型交叉质证).** Same question to TWO roles
  from different vendors, each briefed with an OPPOSING stance ("argue the
  current design is fine" / "argue it must change"), each required to state the
  strongest counter to its own stance. Fan out with `--submit-id`, collect with
  the barrier (`recv --me A --block --count` — mind the 2-rows-per-worker count
  trap), then A reconciles disagreements into the decision. Use for root-cause
  analysis and irreversible calls; two same-vendor panes make a weak committee.

## Capacity-aware dispatch (hub A, adopted 2026-08-19)

The IPC layer carries **no context-capacity telemetry** — heartbeats, registry and
`status --watch` prove liveness only. A judges a worker's remaining context by
reading its TUI status line through the fleet mux (verified 2026-08-19 across all
three vendors: claude/kimi/glm panes show `Ctx N% left`, codex shows
`Context N% left`):

```bash
WEZTERM_UNIX_SOCKET=~/.local/share/wezterm/fleet1.sock wezterm cli get-text --pane-id <N> | grep -iE "context|ctx"
```

Role→pane ids: `~/.claude/ipc/wezterm_panes.json`. No match = pane down or
mid-render — recheck once before concluding.

Dispatch policy:

- **Scrape all worker panes before a dispatch round.** Heavy batches (long-doc
  extraction, bulk transcription) go only to workers at **≥70%** or freshly
  cycled. Observed burn rate: E consumes **40–60 percentage points per zsxq
  batch**, so a sub-70% pane cannot absorb one.
- **Below threshold: refresh, don't gamble.** Reset the pane to a known-full
  state (`pwsh ~/.claude/ipc/ipc_newcycle.ps1`, or per-pane /clear +
  /ipc-recover). Success criterion is the status line back at **`Context 100%`**
  — not the absence of output; /clear must be injected via PowerShell (typed
  text can sit unsubmitted in the composer). Resetting to full beats measuring
  precisely.
- **Context % and provider quota are independent axes.** A quota/limit banner
  (D's usual state) is vendor credit exhaustion — leave the role down per the
  restart script's warning; refreshing context does not help, only waiting does.
- **Any refresh discards the worker's task context.** Its session id changes;
  follow-ups need a fresh self-contained `--body-file` brief (workers can never
  read hub history). Corollary of the existing session-echo rule: a reply whose
  `session` ≠ the claimant's means the worker /clear-ed mid-stream — re-brief
  before dispatching a follow-up.
- **Shape batches so cost is predictable**: preprocessed inputs ship as files,
  briefs are self-contained, and batch sizes come from measured saturation
  (e.g. zsxq: 2 calendar days = 4 posts = one saturated batch), not optimism.

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
  persistent Monitor on `watch --me A`, signal → `peek`, bash fallback.
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
