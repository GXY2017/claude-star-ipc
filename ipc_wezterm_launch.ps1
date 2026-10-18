# Launch IPC terminals as WezTerm panes, one role per tab, and record the
# role -> pane-id map that ipc_newcycle.ps1 batch-controls later.
#
#   pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1                    # A,B; menu picks the project (cwd pre-selected)
#   pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Roles B,C,DS      # workers only
#   pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -Project "D:\proj" -Roles A,B
#   pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1 -DryRun
#
# Each pane runs an interactive pwsh (profile loaded, so profile functions like
# claude-ds work), into which we type: $env:IPC_ROLE='<role>'; <launch command>.
# IPC_ROLE makes the SessionStart hook claim that exact role (ipc_role.py honors
# it), so role assignment is deterministic, not launch-order roulette.

param(
    [string[]]$Roles = @('A','B'),
    [string]$Project,   # empty = ask (console) / error (headless, incl. Claude sessions)
    [switch]$DryRun,
    [switch]$Force     # add a pane even if the role's mapped pane is still alive (normally refused, 2026-09-15)
)

. "$PSScriptRoot\ipc_wezterm_common.ps1"

# Pick the project before touching anything else. With no -Project this shows
# the same history-derived menu as `cc project`; Enter selects the HOME DIR
# per the revised launch-dir rule 2026-08-11 (memory home-dir-launch-default).
# Claude sessions and headless callers must pass -Project explicitly; otherwise
# the checks below throw before spawning any panes.
$insideClaude = @(Get-ChildItem env: | Where-Object { $_.Name -like 'CLAUDE*' }).Count -gt 0
if (-not $Project) {
    # User rule 2026-08-13: EVERY wezterm launch asks which project. Callers with
    # no console (a Claude session, a piped run) must ask the human and pass
    # -Project rather than silently inheriting the home dir - the mailbox is
    # resolved from this path, so a wrong default strands the whole fleet.
    if ($insideClaude) {
        throw "no -Project: ask the user which project first, then re-run with -Project '<path>' (never let this default silently)"
    }
    if (-not (Test-IpcInteractive)) {
        throw "no console for the picker and no -Project: pass -Project '<path>' explicitly"
    }
    $Project = Select-IpcProject -Default $env:USERPROFILE -Title "Fleet 1 ($($Roles -join ',')) - which project?"
}

# `wezterm cli spawn` forwards the CALLER's environment to the new pane (verified
# 2026-08-03: GUI-clean but spawned panes dirty), so this launcher must itself be
# free of CLAUDE_* before ANY spawn - strip unconditionally, no restore needed
# (dedicated short-lived process).
# WEZTERM_* stripped too (2026-08-17, memory wezterm-env-inheritance-gui-launch):
# when this launcher runs inside the OTHER fleet's pane, inherited
# WEZTERM_CONFIG_FILE/WEZTERM_UNIX_SOCKET make any spawned GUI load the wrong mux
# config ("domain 'fleet1' was not found in mux" - silent under Start-Process)
# and would forward the wrong socket/config into every pane. Safe to drop here:
# Resolve-WeztermSocket re-pins WEZTERM_UNIX_SOCKET before every cli call.
Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE|^WEZTERM_' } | ForEach-Object { Remove-Item "env:$($_.Name)" }

# `pwsh -File` passes "-Roles A,B" as one literal string; normalize.
$Roles = @($Roles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# Per-role launch command. Edit bindings here. An empty value = refuse to
# launch that role until you fill it in.
# User lineup 2026-08-03: A=Anthropic hub, B=Kimi, C=GLM via ollama cloud, D=DeepSeek.
# C rebound 2026-08-30 (user): codex gpt-5.6-sol, reasoning xhigh.
# C rebound 2026-09-19 (user): codex gpt-5.6-luna, reasoning max — cheaper codex
#   tokens than sol. (A same-day GLM-5.3-Flash binding was dropped within the hour:
#   Zhipu's night offer, docs.bigmodel.cn/cn/coding-plan/notice/event-glm-5.3-flash,
#   ran 09-03..09-20 23:00-09:00 and is x2 quota in Claude Code, free only in ZCode.)
# C rebound 2026-10-04 (user): codex gpt-6-luna, reasoning max. The GPT-6 model templates
#   carry the base behavior rules (slop list, bias to action, ...) that gpt-5.6-* lack,
#   so ~/.codex/AGENTS.md no longer restates them. Quota ratio vs 5.6-luna not measured yet.
# A rebound 2026-10-05 (user /model in the A pane on 10-04): Opus 5.5. Its window is 1M
#   natively: `claude -p --model claude-opus-5-5 --output-format json` reports
#   modelUsage contextWindow=1000000 with or without [1m], and A's transcript ran to 689K
#   uncompacted. E's ~/.codex/config.toml moved to gpt-6.1-sol high the same day;
#   C stays gpt-6-luna max (models_cache.json lists no gpt-6.1-luna).
# F added 2026-08-14: GLM via Zhipu direct (claude-glm profile fn, glm-5.3[1m]).
# All four in --permission-mode auto (user decision 2026-08-03: manual mode stalls
# unattended panes on tool-approval prompts). Wrapper fns forward extra args to claude;
# ollama forwards what follows `--` to the integration.
$LaunchCmd = @{
    A     = 'claude --model claude-opus-5-5 --permission-mode auto'   # user pick 2026-08-03: hub on Opus; Opus 5.5 from 2026-10-05, 1M native (was claude-opus-5[1m] 09-14..10-05: fixed ctx 206K > 200K caused an auto-compact loop, so A needs a 1M window)
    B     = "claude-kimi 'k3[1m]' --permission-mode auto"    # positional -Model MUST come first or it swallows the flag (ValidateSet)
    C     = 'codex --model gpt-6-luna -c model_reasoning_effort=max'  # user 2026-10-04: C = codex gpt-6-luna max (was gpt-5.6-luna max 09-19..10-04, sol xhigh 08-30..09-19); flags pin it even if ~/.codex/config.toml defaults change
    D     = 'claude-ds --permission-mode auto'               # profile fn, DeepSeek env block
    E     = 'codex'      # codex CLI slot (letter binding 2026-08-03); model/effort follow ~/.codex/config.toml (read it for the current pair; gpt-6.1-sol high 2026-10-05..10-17, gpt-6-astra medium seen 2026-10-18)
    F     = 'claude-glm --permission-mode auto'              # profile fn, Zhipu direct (glm-5.3[1m]); added 2026-08-14
    CODEX = 'codex'      # legacy named channel, redundant with E since 2026-08-03
    DS    = 'claude-ds'  # legacy named channel, redundant with D since 2026-08-03
}
# Per-role model tag for the tab title: "<role>-<tag>" (e.g. A-opus5). Keep in
# sync with $LaunchCmd when rebinding a role; a role without a tag falls back to
# the bare letter.
$ModelTag = @{
    # Vendor names only, no version — models get switched/upgraded, version tags go stale.
    # The per-model-and-parameter key for timing stats is NOT this table: ipc.py
    # _role_variant() parses $LaunchCmd above (--model / model_reasoning_effort /
    # quoted positional model; bare `codex` reads ~/.codex/config.toml) and writes
    # it as `variant` in timing.jsonl (2026-09-19). Keep launch flags explicit
    # on the $LaunchCmd line if a role's model must be attributable.
    A     = 'Claude'
    B     = 'Kimi'
    C     = 'Codex-gpt6-luna'  # 2026-10-04: second codex pane, gpt-6-luna max (was Codex-luna = gpt-5.6-luna 09-19..10-04, Codex-sol 08-30..09-19; E stays the config.toml-driven one)
    D     = 'DeepSeek'
    E     = 'Codex'
    F     = 'GLM-Zhipu'
    CODEX = 'Codex'
    DS    = 'DeepSeek'
}
# Roles running the codex CLI, not the claude harness: different TUI (skip the
# claude readiness probe and the standby wake). NOTE codex DOES claim a role at
# SessionStart nowadays (~/.codex/hooks.json -> codex-compat.js session-start-ipc
# -> ipc_role.py claim), so it needs $env:IPC_ROLE like everyone else - without
# it, an inherited IPC_ROLE squats that role (X-eviction bug, 2026-08-12) and a
# clean env roulette-claims the lowest free slot.
$CodexRoles = @('C','E','CODEX')   # C codex since 2026-08-30 (luna max from 09-19); keep in sync with ipc_dispatch.ps1 / ipc_wake_pane.ps1 / ipc_fleet1_restart.ps1 / ipc_newcycle.ps1
$Hub = if ($env:IPC_HUB) { $env:IPC_HUB } else { 'A' }

$Project = (Resolve-Path $Project).Path
if (-not (Test-Path (Join-Path $Project '.claude\ipc.enabled'))) {
    Write-Warning "$Project has no .claude\ipc.enabled gate. Fleet panes still claim roles (IPC_ROLE is set per pane, a sufficient opt-in - ipc_role.py:92), but BARE sessions launched there later won't join IPC. For a real project run 'python ~/.claude/ipc/ipc_role.py enable' from its root (enable refuses the home dir)."
}

foreach ($r in $Roles) {
    if (-not $LaunchCmd.ContainsKey($r) -or -not $LaunchCmd[$r]) {
        throw "No launch command bound for role '$r' - edit `$LaunchCmd in $PSCommandPath"
    }
}

if ($DryRun) {
    Write-Host "DRY RUN - would do the following:"
    Write-Host "  project: $Project"
    foreach ($r in $Roles) {
        $envPrefix = "Set-Location -LiteralPath '$Project'; `$env:IPC_ROLE='$r'; "
        $title = if ($ModelTag[$r]) { "$r-$($ModelTag[$r])" } else { $r }
        Write-Host "  [$r] spawn pane (tab '$title', cwd=$Project), type: $envPrefix$($LaunchCmd[$r])"
    }
    return
}

# --- ensure mux + attached GUI view (mux world 2026-08-17: panes live in the
# fleet-1 wezterm-mux-server; the GUI is a detachable view) ---
$freshGui = $false
if (-not (Resolve-WeztermSocket)) {
    Write-Host "Starting fleet-1 mux server + GUI..."
    if (-not (Start-WeztermGuiAndWait $Project)) { throw "fleet-1 mux failed to come up / socket unreachable" }
    $freshGui = $true   # fresh mux: its default window already owns one pane
}
# Panes survive without a GUI, but a launch should be watchable: attach a view
# if none is connected, then pin its pid (fleet-2 exclusion + restart targeting).
$guiClients = @(& $script:WeztermExe cli --no-auto-start list-clients --format json 2>$null | ConvertFrom-Json)
if ($guiClients.Count -eq 0) {
    [void](Connect-WeztermGui 'fleet1')
    $guiClients = @(& $script:WeztermExe cli --no-auto-start list-clients --format json 2>$null | ConvertFrom-Json)
}
$guiPid = ($guiClients | Where-Object { $_.pid } | Select-Object -First 1).pid
if ($guiPid) {
    Set-Content $script:GuiPidPath $guiPid -Encoding ascii
    Write-Host "fleet-1 gui pid pinned: $guiPid (view only - killing it no longer kills the fleet)"
} else {
    Write-Warning "no GUI client visible - fleet runs headless; attach later with: wezterm connect fleet1"
}

$map = Read-PaneMap
# A fresh mux numbers its panes from 0 again, so every entry left by the previous
# mux points at the wrong pane (2026-10-18: B=1/C=2 from 10-10 survived a launch of
# A,D,E,F and aliased the new D=1/E=2; a later `-Roles B` or newcycle would have
# typed into D's pane). Start the map empty.
if ($freshGui -and $map.Count -gt 0) {
    Write-Host ("fresh mux: dropping stale pane map entries " + (($map.Keys | Sort-Object | ForEach-Object { "$_=$($map[$_].pane_id)" }) -join ' '))
    $map = @{}
}

# --- 0. refuse to duplicate a role whose pane is still alive (2026-09-15) ---
# A DOWN watcher means "not parked", not "pane gone". On 2026-09-14 hub A saw C
# DOWN, ran `-Roles C` then `-Roles D`, and the fleet ended with two C and two D
# panes (its kill-pane cleanup was then blocked by the permission classifier).
# A live pane is woken in place; only a pane missing from `wezterm cli list` is
# relaunched here. -Force keeps the old behaviour for a deliberate duplicate.
if (-not $freshGui -and -not $Force) {
    $livePanes = @(Get-WeztermPanes | ForEach-Object { $_.pane_id })
    $dupes = @()
    foreach ($r in $Roles) {
        $entry = $map[$r]
        if ($entry -and $entry.pane_id -ne $null -and $livePanes -contains [int]$entry.pane_id) {
            $recover = if ($CodexRoles -contains $r) { "`$ipc-recover $r" } else { "/ipc-recover $r" }
            $dupes += "  role $r -> pane $($entry.pane_id) is still alive. Wake it in place instead:`n" +
                      "    pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role $r -Message '$recover'`n" +
                      "  (low context? pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Roles $r ; TUI dead back to the shell? type its launch command into that pane)"
        }
    }
    if ($dupes.Count -gt 0) {
        throw ("refusing to add a duplicate pane:`n" + ($dupes -join "`n") +
               "`nOnly a role whose pane is GONE from `wezterm cli list` belongs here. Pass -Force to add a duplicate anyway (then kill the old pane yourself).")
    }
}

# A freshly started GUI already owns one pane; use it for the first role
# instead of leaving a stray shell tab.
$initialPane = if ($freshGui) { (Get-WeztermPanes | Select-Object -First 1).pane_id } else { $null }

$launched = @()
foreach ($r in $Roles) {
    if ($null -ne $initialPane) {
        $paneId = [int]$initialPane
        $initialPane = $null
        Start-Sleep -Seconds 5   # let the initial pane's pwsh profile finish loading (parity with fleet2's wait)
    } else {
        # mux spawn has no "focused pane" - anchor on a live pane explicitly
        # (spawns a new tab in that pane's window; without --pane-id the cli
        # errors "$WEZTERM_PANE is not set").
        $anchor = [int](Get-WeztermPanes | Select-Object -First 1).pane_id
        $out = & $script:WeztermExe cli spawn --pane-id $anchor --cwd $Project -- $script:PaneShell
        if ($LASTEXITCODE -ne 0) { throw "cli spawn failed for role $r" }
        $paneId = [int]($out | Select-Object -Last 1).Trim()
    }
    $title = if ($ModelTag[$r]) { "$r-$($ModelTag[$r])" } else { $r }
    & $script:WeztermExe cli set-tab-title --pane-id $paneId $title *> $null

    # Explicit Set-Location for EVERY pane (2026-08-17): the mux's initial pane
    # does NOT inherit the mux server's -WorkingDirectory (observed: first role
    # landed in home while cli-spawned panes honored --cwd), and the harness in
    # the pane resolves its IPC mailbox from cwd - a wrong cwd strands the role
    # on a different mailbox than the rest of the fleet. Redundant-but-harmless
    # for spawned panes; required for the initial one.
    $envPrefix = "Set-Location -LiteralPath '$Project'; `$env:IPC_ROLE='$r'; "
    Submit-PaneText $paneId "$envPrefix$($LaunchCmd[$r])"

    $map[$r] = @{ pane_id = $paneId; project = $Project; launched_at = (Get-Date -Format s) }
    $launched += @{ role = $r; pane = $paneId }
    Write-Host ("[{0}] pane {1}: {2}" -f $r, $paneId, $LaunchCmd[$r])
}

Save-PaneMap $map
Write-Host "pane map saved: $script:PaneMapPath"

# --- wait for TUIs, then wake workers (manual-floor keystroke) ---
foreach ($l in $launched) {
    if ($CodexRoles -contains $l.role) {
        # Codex slot (user rule 2026-08-05): wait for its TUI (different footer
        # than claude), then park a heartbeat via the .agents mirror skill so
        # --require-watcher / --to ALL work on it too. Wait-CodexReady also
        # drives the npm self-update flow unattended (confirm + relaunch,
        # user rule 2026-08-06).
        if (Wait-CodexReady $l.pane) {
            Submit-PaneText $l.pane "`$ipc-recover $($l.role)"
            Write-Host "[$($l.role)] codex TUI ready, `$ipc-recover $($l.role) sent - heartbeat should park"
        } else {
            Write-Warning "[$($l.role)] pane $($l.pane) codex TUI not ready within timeout (first-run trust dialog?) - send `$ipc-recover $($l.role) manually"
        }
        continue
    }
    if (Wait-PaneReady $l.pane) {
        # 2026-08-17: was a bare "standby" — Kimi-B answered it with prose and
        # parked nothing (incident #1186: no watcher, no registry claim, an
        # orphan heartbeat then spoofed ALIVE). /ipc-recover <role> is the
        # deterministic boot: parks the watcher under Monitor AND verifies the
        # registry claim (unclaimed roles are REFUSED-SQUATTER on dispatch).
        # 2026-08-21 (user decision): the HUB gets it too — A parks a persistent
        # `watch --me A` at boot instead of collecting on demand (reply signals
        # land immediately; registry hub-exemption from reclaim is unchanged).
        Submit-PaneText $l.pane "/ipc-recover $($l.role)"
        Write-Host "[$($l.role)] TUI ready, /ipc-recover $($l.role) sent - watcher should park + claim"
    } else {
        Write-Warning "[$($l.role)] pane $($l.pane) TUI not detected within timeout - check it manually"
    }
}
