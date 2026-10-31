# Fleet operations: scripts, reattach, launch menu, role bindings, fleet cautions

> Split out of `SKILL.md` on 2026-10-03 (lint-skills size rule: body ≤ ~20 KB). Text below is moved verbatim unless marked. Read before running any fleet script (launch, newcycle, one-role relaunch, whole-fleet restart, fleet 2, pane exit), when a pane, GUI window or watcher goes missing, or when checking role→model bindings. Caution numbers match `SKILL.md` § 注意事项; "all of the above" in the fleet-scripts preface means `SKILL.md` § Bringing terminals online.

## Fleet scripts

**Preferred since 2026-08-03 — WezTerm pane fleet (automates all of the above):**
terminals live as panes in one WezTerm window, launched and batch-controlled by
two user-level scripts (role→pane-id map, no window guessing, no hand-typing):

```powershell
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles A -Project <root>          # hub only (user rule 2026-10-27, -Roles default A); A adds workers after the first task with the -Roles <workers> form below
pwsh ~/.claude/ipc/ipc_newcycle.ps1 [-Roles C] [-Force] [-SkipHub]          # refresh context: per pane kind sends /clear + /ipc-recover (claude) or /new (codex, no wake, never Ctrl+C); BUSY roles skipped; dead codex TUI relaunched instead. This is A's tool when a role runs low on context — clearing beats restarting the pane. It never switches projects or changes a pane's cwd; a project switch is ipc_fleet1_restart.ps1 -Project <root>
pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Relaunch -Roles D,F [-Force] [-Detach]  # fresh PROCESS per role without touching the mux (2026-10-18, #task20.D): /exit (Exit-and-stop-tasks dialog answered) or codex Esc+Ctrl+C, retype the launcher's line in the same pane, wait ready, wake. Esc on a permission prompt first (Enter would approve it), Enter on a notice overlay. Skips a pane still producing output (-Force cuts it) and an unreadable one. The pane that runs the script ($env:IPC_ROLE, normally A) is skipped with the command to run; -Detach starts that command outside the process tree, 45 s later (log %TEMP%\ipc_relaunch_<role>.log; end the turn). Use it instead of a whole-fleet restart when TUIs are stuck or stale
pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles C -Project <root>          # roles into the LIVE fleet: mux reused, other panes untouched — the normal way A adds the workers it picked (2026-10-27, orchestration-and-dispatch.md § Lineup decided by the hub), or brings back a role whose pane is GONE (`wezterm cli list` lacks its wezterm_panes.json pane id). A DOWN watcher with a live pane is woken in place: claude roles `ipc_wake_pane.ps1 -Role D -Message '/ipc-recover D'`, codex roles `-Message '$multi-terminal-ipc recover C'`. Since 2026-09-15 the launcher REFUSES a role whose mapped pane is still alive and prints that wake command (`-Force` adds the duplicate anyway). Bit 2026-09-14: A saw C DOWN, ran `-Roles C` then `-Roles D`; fleet ended with two C and two D panes and A's kill-pane cleanup was blocked
pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1 -Project <root>                   # WHOLE-FLEET restart only, run by the user from a window OUTSIDE fleet 1 (plain PowerShell or another wezterm window; refused from a fleet-1 pane, which the teardown would kill mid-run). The hub stays in fleet 1 (user 2026-10-18), so A writes its handoff first and loses its session. Stuck or stale TUIs need `newcycle -Relaunch`, not this. Steps: pending check (only tasks to roles in the fleet-1 pane map block; a task to X does not, 2026-10-18) -> tear down mux + GUI + orphan watchers -> relaunch -> verify parks. `-Roles` picks what comes BACK, never what gets torn down: `-Roles C` still kills the mux and every pane incl. A itself (bit 2026-09-03, [[ipc-single-role-restart]]); since 2026-09-03 a `-Roles` list without the hub is REFUSED unless `-Force` Orphan-watcher sweep covers `$FleetRoles` = A–F ∪ old fleet-1 pane map ∪ `-Roles` (fixed 2026-09-04: it used to loop over `-Roles` only, so `-Roles A,C` left B/D/E/F watchers alive = fake ALIVE + stolen mail; [[ai-fin::fleet-shutdown-orphan-watchers]] 第三型).
pwsh ~/.claude/ipc/ipc_fleet2_launch.ps1 -Project <root>                   # second formation: its own mux + GUI, role X only (Claude, --permission-mode auto); ask which project first unless the user already named the project and scope in this request, same rule as fleet 1
pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role F [-KeepRole]                   # clean exit of ONE pane: /exit -> release slot -> confirm watcher retired (caution 10); fleet 1 only
```

## Mux-domain reattach

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

## Launch menu and `-Project`

In an interactive console outside Claude, omit `-Project` and both launchers show the same history-derived menu as
`cc project`, with the home dir appended as an explicit last entry and
pre-selected as the default (launch-dir rule revised 2026-08-11, memory
`home-dir-launch-default`). A headless caller — a Claude session, a piped run —
must pass `-Project` explicitly; without it the launchers throw before spawning
panes, rather than selecting the home dir. Home can never get an
`ipc.enabled` gate (`ipc_role.py enable` refuses it), but fleet panes still
claim roles there: the launchers set `IPC_ROLE` per pane, which alone opts a
session in (`ipc_role.py:92`) and claims deterministically (`ipc_role.py:427`).
The gate only matters for `WEZTERM_PANE` sessions without an explicit
`IPC_ROLE` (bare terminals outside WezTerm never run claim at all — hook guard
2026-08-09); whether a claim lands depends on the harness firing the
SessionStart hook (GLM/DeepSeek panes park a watcher but may never claim). [Changed at the split: the fleet-launch gate that closed this paragraph (ask which project first, pass `-Project`) stays in `SKILL.md` § Bringing terminals online.]

## Role→model bindings and DOWN watchers

Role→model bindings (A=claude claude-opus-5-5 hub — 1M native, rebound 2026-10-05, was
opus-5[1m] 09-14..10-05; B=kimi k3.1-pro-highspeed (user 2026-10-26, was k3[1m]), C=codex gpt-6-luna
max — rebound 2026-10-04 (GPT-6 templates carry the base rules ~/.codex/AGENTS.md no longer restates;
`ipc_wake_pane.ps1 -Model` maps only to the GPT-6 family, sol = gpt-6.1-sol since 10-05), was gpt-5.6-luna max 09-19..10-04, sol xhigh 08-30..09-19,
D=deepseek-flash[1m] incl. haiku/sub-agent slots,
E=codex gpt-6.1-terra xhigh (user 2026-10-27, pinned by flags in `ipc_wezterm_launch.ps1` $LaunchCmd; gpt-6.1-sol xhigh 10-20..10-27; before 10-20 bare `codex` following ~/.codex/config.toml: gpt-6.1-sol high 2026-10-05..10-17, gpt-6-astra medium seen 2026-10-18), F=glm-5.3[1m] via Zhipu direct — added
2026-08-14, G=minimax-m3.1-flash:cloud via `ollama launch claude` — added 2026-10-21, free
until 2026-10-22 23:59 UTC; past the launcher's `$RoleFreeUntil` deadline the launcher and
`newcycle -Relaunch` skip G and `ipc_quota.py` reports it HOLD) are a table at the top of
the launch script; that table is the authority, this list is a snapshot. The old keepalive daemon
is DECOMMISSIONED (same date): before dispatching, check `status --watch <role>`.
DOWN means the watcher is not parked, NOT that the pane is gone: check the pane
first (`wezterm cli list` against the pane map), wake a live pane by typing into
it (`ipc_wake_pane.ps1`), and relaunch only a pane that no longer exists. Full findings,
traps, and per-bridge caveats: [[ai-fin::wezterm-pane-injection-verified]].

## Fleet cautions (`SKILL.md` § 注意事项 10–13, 15, 16)

10. **Clean exit is a script, not Ctrl+C / closing the pane.**
    `pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role <r>` sends `/exit`, takes "Exit and
    stop tasks" ("Move to background" is the orphan-watcher switch), releases the
    slot and confirms the watcher retired. Third-party panes (glm / deepseek) never
    fire SessionEnd, so a hand-closed pane leaves a watcher beating under a green
    light and dispatches fall into an unread mailbox. Fleet 2's X is not in the
    pane map: run the same steps against `fleet2.sock` + `wezterm_fleet2.json`
    pane 0 ([[runtime-upgrade-fleets-down]] has the verified sequence).
11. **From inside a pane, launch or connect to the OTHER fleet only with
    `WEZTERM_*` env stripped** — the child inherits this fleet's
    `WEZTERM_CONFIG_FILE`/`WEZTERM_UNIX_SOCKET`, loads the wrong mux config and
    dies with "domain not found" ([[wezterm-env-inheritance-gui-launch]];
    `ipc_fleet1_restart.ps1` strips them itself).
12. **Every pane-spawn site uses `$script:PaneShell` (MSI pwsh, absolute path),
    never bare `pwsh`.** A Microsoft-Store MSIX PowerShell update force-kills every
    process launched from the Store package — it wiped both fleets on 2026-08-17
    ([[store-pwsh-update-kills-fleet]]). `mux-fleet{1,2}.lua` `default_prog`
    pins the same path for the initial pane.
13. **Node.js / wezterm upgrades happen only with BOTH fleets down.** Pane-spawned
    `node.exe` MCP servers (dozens) lock `node.exe` — the MSI defers to reboot and
    npm/node mismatch meanwhile — and a wezterm upgrade replaces the mux server
    under every pane. Non-fleet MCP `node.exe` (Claude Desktop, other CLI sessions)
    must go too. Runbook incl. the winget-nightly hash-mismatch fallback: memory
    `runtime-upgrade-fleets-down`.

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
    shared code, never a one-off script. [[C--Users-<user>-Downloads-----::fleet-anomalies-to-x]]

**Gotcha — gateway panes freeze on the boot `/ipc-recover`** (2026-09-20): a Claude Code pane routed through a non-Anthropic gateway (kimi / glm) runs one step, then stalls behind an auto-mode notice; `status --watch <role>` reads DOWN while the pane is alive. Send a bare `\r` (`wezterm cli --no-auto-start send-text --no-paste --pane-id N` against the fleet socket) and re-check after 60 s. 2026-10-17: a DeepSeek-gateway pane froze the same way on a release notice mid-task, and a Codex pane can sit on its update prompt (`enter continue · esc skip`: send Esc). `ipc_pane_probe.py` now flags `notice_overlay` and returns verdict `BLOCKED-NOTICE`; the hub reply clock (built into `watch --me A` since 2026-10-18; `stall_watch.py` before) prints the dismiss command; after any launch, refresh or `/ipc-recover`, probe once and dismiss before dispatching. Memory `fleet-notice-overlay-blocks-recover`.
