# Bringing terminals online: role claim, guard, manual floor, role registry

> Split out of `SKILL.md` on 2026-10-03 (lint-skills size rule: body ≤ ~20 KB). Text below is moved verbatim unless marked. Read when opening, claiming or re-claiming a terminal, or when verifying a claim or a watcher state. The bring-up order rule (`take` FIRST, `watch` SECOND) stays in `SKILL.md` § Bringing terminals online.

## Role claim, guard, manual floor

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
can't fire a worker's first tool call. `ipc_wezterm_launch.ps1` crosses it: once
each TUI is ready it types `/ipc-recover <role>` (Codex panes: `$ipc-recover
<role>`), which parks the watcher and verifies the claim; any queued backlog then
arrives as signals (WATCHER FIRST, no separate drain step: work done without a
beating watcher reads as stale to the lease reaper and gets requeued mid-flight).
After a fleet launch A does not ask the user for anything: it checks `status
--watch <role>` and acts only on a role still DOWN or a pane the launcher warned
about ("TUI not detected"). A window opened by hand needs `/ipc-recover <role>`
(Codex: `$ipc-recover <role>`) typed into it; a bare `ok`/`standby` can be answered with prose and park nothing
(2026-08-17, #1186).

Force a specific role at launch: `IPC_ROLE=A claude …` (overrides launch order).

## Bring-up order

The rule line and the mid-task inverse stay in `SKILL.md`; the mechanism:

**Bring-up order: `ipc_role.py take <role> --session <sid>` FIRST, `watch --me <role>` SECOND.**
The watcher snapshots the registry owner at start and retires on any later change (`WATCHER for X retired: registry owner changed`),
so watch-then-take retires the watcher just parked (D, 2026-08-13) and a watcher over an unclaimed slot is `ALIVE [SQUATTER]`, refused by
`--require-watcher`; `ipc.py` `_REG_ANCHOR_GRACE = 45` s adopts a None/manual → claimed change as the pane's own take — a safety net, not permission.

## Role registry

```
ipc_role.py status                 # reconciled view: ownership × heartbeat liveness
ipc_role.py take A --session <sid> # (re)assign a role to this session
ipc_role.py reclaim-dead           # free WORKER slots whose watcher heartbeat is gone (hub exempt — never auto-reclaimed, even though A parks a watcher at boot since 2026-08-21; blind-safe. Rarely needed: claim() sweeps all ghost slots on every SessionStart)
ipc.py status --watch B            # ALIVE(parked, 0) / BUSY(executing, 1 — alive, don't re-dispatch) / DOWN(1) + pid/session; ALIVE with [SQUATTER: no registry owner] exits 1 — matches the send gate's REFUSED-SQUATTER
```
