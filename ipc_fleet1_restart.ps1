# Restart fleet 1 precisely: kill pinned GUI + orphan watchers, relaunch, verify parks.
# Born 2026-08-15: an ad-hoc restart took ~20 min - a first-run modal ate the wake
# lines, hand-rolled send-text turned slash commands into chat text (a worker then
# spawned phantom subagents off stale mailbox notes), and verification ran on blind
# multi-minute sleeps. This wrapper encodes the precise sequence; target 3-4 min.
#
# !! -Project is REQUIRED and must come from ASKING THE USER (rule 2026-08-13
#    covers restarts too - never infer it from wezterm_panes.json or the old pin).
#
# Usage:
#   pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1 -Project "C:\path\to\project"
#   pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1 -Project ... -Roles A,B,C,E,F
#   ... -Force        # proceed even if `pending --hub A` is non-empty
#
# Sequence (deviations from the shutdown runbook are deliberate):
#   1. pending --hub A must be empty (or -Force) - never kill mid-flight fan-out.
#      Checked in the OLD docked project's mailbox (from the pane map), not the
#      new -Project one - a project-switch restart otherwise checks/sweeps an
#      empty mailbox (bit 2026-08-15).
#   2. Kill ONLY the pinned GUI pid (fleet 2 / X runs its own wezterm-gui).
#   3. Kill orphan watchers immediately (pane children don't die with the GUI on
#      Windows); ancestor-trace before killing; sweep runs in the OLD mailbox
#      too. NO reclaim-dead wait: the launcher's per-pane IPC_ROLE claim
#      deterministically takes each slot.
#   4. Relaunch via ipc_wezterm_launch.ps1 (Wait-PaneReady dismisses first-run
#      modals since 2026-08-15).
#   5. Verify with tight per-role probes (ALIVE/BUSY via status --watch), one
#      scripted repair per stalled role (modal-Esc + ipc_wake_pane) - no blind
#      sleeps, no hand-rolled slash-command injection.
param(
    [Parameter(Mandatory)][string]$Project,
    [string[]]$Roles = @('A','B','C','D','E','F'),
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\ipc_wezterm_common.ps1"
# Strip inherited WEZTERM_* up front (2026-08-17, memory
# wezterm-env-inheritance-gui-launch): run from inside a fleet-2 pane this
# script carries fleet2's WEZTERM_CONFIG_FILE/WEZTERM_UNIX_SOCKET, which reach
# the relaunched mux/GUI and every child (`& pwsh ipc_wezterm_launch.ps1`
# inherits env) - the GUI then loads the wrong mux config and dies with
# "domain not found". Resolve-WeztermSocket re-pins the socket it needs.
Get-ChildItem env: | Where-Object { $_.Name -match '^WEZTERM_' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
$IpcPy = Join-Path $env:USERPROFILE '.claude\ipc\ipc.py'
# `pwsh -File` passes "-Roles A,B" as one literal string; normalize.
$Roles = @($Roles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$Hub = if ($env:IPC_HUB) { $env:IPC_HUB } else { 'A' }
$CodexRoles = @('E','CODEX')

$Project = (Resolve-Path $Project).Path
# Old docked project, read from the pane map the previous launch wrote (this is
# NOT inferring the -Project target, which must still come from the user): the
# old fleet's mailbox resolves under ITS cwd, so the pending check and the
# orphan sweep must run there. A project-SWITCH restart that runs them under
# the NEW project sweeps an empty mailbox and misses every orphan (bit
# 2026-08-15: three 9h-old B/C/F watchers kept beating in the old project's
# registry after the fleet moved to another project).
$OldProject = $Project
try {
    $oldMap = Read-PaneMap
    if ($oldMap -and $oldMap.Count -gt 0) {
        $op = ($oldMap.Values | Select-Object -First 1).project
        if ($op -and (Test-Path $op)) { $OldProject = (Resolve-Path $op).Path }
    }
} catch {}
Push-Location $OldProject
try {
    # --- 1. refuse to kill mid-flight work (checked in the OLD mailbox) ---
    $pending = (& python $IpcPy pending --hub $Hub 2>&1) -join "`n"
    if ($pending -notmatch 'NONE' -and -not $Force) {
        throw "pending --hub $Hub is non-empty - workers may be mid-task:`n$pending`nResolve (recv/cancel) or rerun with -Force."
    }

    # --- 2. tear down fleet 1: the MUX server first (mux world 2026-08-17 -
    # the pane tree lives under the mux; killing only the GUI would leave every
    # agent running headless), then the pinned GUI view. Fleet 2 is untouched by
    # construction (its own socket + pid entries).
    $muxPids = Read-MuxPids
    if ($muxPids.ContainsKey('fleet1')) {
        $mp = Get-Process -Id $muxPids['fleet1'] -ErrorAction SilentlyContinue
        if ($mp -and $mp.ProcessName -eq 'wezterm-mux-server') {
            Stop-Process -Id $mp.Id -Confirm:$false
            Write-Host "killed fleet-1 mux pid $($mp.Id) - pane tree down"
            Start-Sleep -Seconds 2
        } else {
            Write-Host "recorded fleet-1 mux pid $($muxPids['fleet1']) not a live wezterm-mux-server - mux already down"
        }
    } else {
        Write-Host "no fleet-1 mux pid recorded (pre-migration state?) - falling through to the GUI kill"
    }
    # Verify against the SOCKET, not just the recorded pid: a missing/stale
    # wezterm_mux_pids.json with a live mux would leave the pane tree running
    # headless, and the relaunch would then double-spawn every role into the
    # old mux. Kill any answering stray by process identity (cmdline carries
    # the per-fleet config file), abort loudly if one still answers.
    if (Resolve-WeztermSocket) {
        $strays = Get-CimInstance Win32_Process -Filter "Name='wezterm-mux-server.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -match 'mux-fleet1\.lua' }
        foreach ($s in $strays) {
            Stop-Process -Id $s.ProcessId -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "killed stray fleet-1 mux pid $($s.ProcessId) (socket still answered after recorded-pid kill)"
        }
        Start-Sleep -Seconds 2
        if (Resolve-WeztermSocket) {
            throw "fleet-1 mux socket still answers after teardown - a mux-server outside wezterm_mux_pids.json is alive; inspect Get-Process wezterm-mux-server before relaunching"
        }
    }
    Remove-Item $script:MuxSock['fleet1'] -ErrorAction SilentlyContinue
    if (Test-Path $script:GuiPidPath) {
        $gp = [int](Get-Content $script:GuiPidPath | Select-Object -First 1)
        $proc = Get-Process -Id $gp -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'wezterm-gui') {
            Stop-Process -Id $gp -Confirm:$false
            Write-Host "killed pinned fleet-1 GUI pid $gp"
            Start-Sleep -Seconds 2
        } else {
            Write-Host "pinned pid $gp not a live wezterm-gui - GUI already down"
        }
    } else {
        Write-Host "no GUI pid pin found - assuming fleet 1 already down"
    }

    # --- 3. orphan watchers (pane children survive the GUI on Windows) ---
    # Includes the hub: its old watcher orphans too, and a beating hub orphan
    # swallows every signal meant for the fresh hub pane (bit 2026-08-15: X->A
    # note #1135 sat unread while the dead hub watcher looked "park live").
    # The ancestor-trace guard below protects any live watcher, hub included.
    foreach ($r in $Roles) {
        $s = (& python $IpcPy status --watch $r 2>&1) -join ' '
        if ($s -notmatch '(ALIVE|BUSY)') { continue }
        if ($s -notmatch 'pid=(\d+)') { continue }
        $wpid = [int]$Matches[1]
        # Trace ancestry: a live watcher belongs to a claude/codex session
        # (node/claude/codex in the chain). Bash-only chain = orphan.
        $names = @()
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$wpid" -ErrorAction SilentlyContinue
        while ($p) {
            $names += $p.Name
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        }
        if (($names -join ' ') -match 'claude|node|codex') {
            Write-Warning "[$r] watcher pid $wpid still has a live session ancestor ($($names -join '<-')) - NOT killing; investigate before rerunning"
            continue
        }
        Stop-Process -Id $wpid -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[$r] orphan watcher pid $wpid killed (chain: $($names -join '<-'))"
    }
    # No reclaim-dead wait here: relaunch claims each slot via IPC_ROLE take.

    # Cleanup done against the old mailbox; everything from here on (relaunch,
    # park verification, final registry) targets the NEW project's mailbox.
    Pop-Location
    Push-Location $Project

    # --- 4. relaunch ---
    & pwsh "$PSScriptRoot\ipc_wezterm_launch.ps1" -Roles ($Roles -join ',') -Project $Project
    if ($LASTEXITCODE -ne 0) { throw "ipc_wezterm_launch.ps1 exited $LASTEXITCODE" }

    # --- 5. verify parks: tight probes, one scripted repair per role ---
    # Re-resolve the CLI socket to the FRESH pin first: a caller session (e.g.
    # fleet-2's X) inherits its own WEZTERM_UNIX_SOCKET, and pane probes would
    # silently hit the wrong GUI ("no such pane N" - observed on the first live
    # run, 2026-08-15; only ipc_wake_pane's own re-resolve masked it).
    [void](Resolve-WeztermSocket)
    $map = Read-PaneMap
    $todo = @{}; foreach ($r in $Roles) { if ($r -ne $Hub) { $todo[$r] = $true } }
    $repaired = @{}
    $deadline = (Get-Date).AddSeconds(240)   # kimi (B) parks slowest, ~200s observed
    $grace = (Get-Date).AddSeconds(30)   # let SessionStart hooks land before repairing
    while ($todo.Count -gt 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        foreach ($r in @($todo.Keys)) {
            $s = (& python $IpcPy status --watch $r 2>&1) -join ' '
            if ($s -match 'ALIVE') { $todo.Remove($r); Write-Host "[$r] parked"; continue }
            if ($s -match 'BUSY')  { continue }   # processing its wake line - will park
            if ((Get-Date) -lt $grace -or $repaired[$r]) { continue }
            # DOWN past grace: one scripted repair - dismiss a possible modal,
            # then the official wake script. Never hand-inject slash commands.
            if ($map.ContainsKey($r)) {
                $paneId = [int]$map[$r].pane_id
                $txt = (Get-PaneText $paneId) -join "`n"
                if ($txt -match 'Esc to cancel') { Send-PaneText $paneId ([string][char]27); Start-Sleep -Seconds 1 }
                if ($txt -match '402|Insufficient Balance|rate limit|quota') {
                    Write-Warning "[$r] pane shows a quota/limit banner - leaving it down (dispatch capacity: see ipc-dispatch-quota-awareness)"
                    $todo.Remove($r); continue
                }
                & pwsh "$PSScriptRoot\ipc_wake_pane.ps1" -Role $r
                $repaired[$r] = $true
            }
        }
    }
    foreach ($r in $todo.Keys) {
        Write-Warning "[$r] not parked within deadline - inspect its pane (wezterm cli get-text) before dispatching"
    }
    # Codex roles park a heartbeat, not a watcher; DORMANT afterwards is their
    # normal idle shape - the hub must wake E per dispatch anyway.

    Write-Host "--- final registry ---"
    & python (Join-Path $env:USERPROFILE '.claude\ipc\ipc_role.py') status
    & python $IpcPy pending --hub $Hub
} finally {
    Pop-Location
}
