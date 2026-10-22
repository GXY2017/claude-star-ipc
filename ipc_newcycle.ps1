# Start a fresh IPC task cycle: batch /clear + role recovery across the WezTerm
# panes recorded by ipc_wezterm_launch.ps1. Context refresh only: it never reads
# or changes a pane's cwd (the one Push-Location below runs `status` in the
# mailbox of the project recorded in the pane map, then pops). To move the fleet
# to another project use ipc_fleet1_restart.ps1 -Project <root>.
#
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1              # all mapped roles
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Roles B,C   # subset
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Force       # don't skip BUSY workers
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -SkipHub     # leave the hub pane alone
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Relaunch -Roles D,F   # exit each CLI and start it again in its pane
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Relaunch -Roles A -Detach   # from A's own pane
#
# Per role: send /clear, wait, send /ipc-recover <role> (hub included since
# 2026-08-21 - A re-parks its `watch --me A` watcher after /clear like everyone
# else). A BUSY worker (mid-task) is skipped unless -Force - clearing it would
# strand the in-flight task as NEEDS-REVIEW/requeue.
#
# -Relaunch (2026-10-18, user "不重启wezterm，但每个模型exit再进入"; DeepSeek design
# #task20.D): instead of /clear, exit the CLI and retype the launcher's own line in
# the same pane (Invoke-PaneRelaunch in ipc_wezterm_common.ps1): a fresh process,
# for a TUI that is stuck, outdated or must reload its config. The mux, the GUI and
# every other pane stay up. A pane still producing output is skipped unless -Force.
# The role whose pane runs this script ($env:IPC_ROLE) cannot exit its own CLI
# without killing the script: it is skipped with the command to run, or with
# -Detach a copy started outside this process tree (CIM, like launch_watcher_d.py)
# relaunches it after -DelaySec 45; its result goes to %TEMP%\ipc_relaunch_<role>.log.
# End the turn right after a -Detach run.

param(
    [string[]]$Roles,
    [switch]$Force,
    [switch]$SkipHub,
    [switch]$Relaunch,
    [switch]$Detach,
    [switch]$Detached,      # internal: set on the copy -Detach starts
    [int]$DelaySec = 0,
    [string]$Log = ''
)

. "$PSScriptRoot\ipc_wezterm_common.ps1"
if ($DelaySec -gt 0) { Start-Sleep -Seconds $DelaySec }

# `pwsh -File` passes "-Roles B,C" as one literal string; normalize.
if ($Roles) { $Roles = @($Roles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }

$Hub   = if ($env:IPC_HUB) { $env:IPC_HUB } else { 'A' }
$IpcPy = Join-Path $env:USERPROFILE ".claude\ipc\ipc.py"

# Non-claude TUIs need different commands; empty wake = nothing sent after clear.
# Keep in sync with $CodexRoles in ipc_wezterm_launch.ps1 / ipc_dispatch.ps1 (C = codex since 2026-08-30, #1381; luna max from 09-19).
$ClearCmd = @{ CODEX = '/new'; E = '/new'; C = '/new' }
$WakeCmd  = @{ CODEX = ''; E = ''; C = '' }

$map = Read-PaneMap
if ($map.Count -eq 0) { throw "No pane map at $script:PaneMapPath - run ipc_wezterm_launch.ps1 first" }
if (-not $Roles) { $Roles = @($map.Keys) }
if ($SkipHub) { $Roles = @($Roles | Where-Object { $_ -ne $Hub }) }
# Workers first, hub last.
$Roles = @($Roles | Where-Object { $_ -ne $Hub }) + @($Roles | Where-Object { $_ -eq $Hub })

if (-not (Resolve-WeztermSocket)) { throw "WezTerm GUI not running - nothing to control" }
$livePanes = @(Get-WeztermPanes | ForEach-Object { $_.pane_id })

$results = @()
foreach ($r in $Roles) {
    $entry = $map[$r]
    if (-not $entry) { $results += "[$r] SKIP - not in pane map"; continue }
    $paneId = [int]$entry.pane_id
    if ($livePanes -notcontains $paneId) { $results += "[$r] SKIP - pane $paneId gone (relaunch or edit $script:PaneMapPath)"; continue }

    # BUSY guard (workers only; the hub never claims dispatched tasks, so BUSY
    # means nothing there even now that it parks a watcher - 2026-08-21)
    if ($r -ne $Hub -and -not $Force) {
        Push-Location $entry.project
        try { $status = (& python $IpcPy status --watch $r 2>&1) -join ' ' } finally { Pop-Location }
        if ($status -match '\bBUSY\b') { $results += "[$r] SKIP - BUSY (mid-task); use -Force to clear anyway"; continue }
    }

    if ($Relaunch) {
        if ($env:IPC_ROLE -eq $r -and -not $Detached) {
            $rlog = Join-Path $env:TEMP "ipc_relaunch_$r.log"
            $line = "pwsh -NoProfile -File `"$PSCommandPath`" -Roles $r -Relaunch -Detached -DelaySec 45 -Log `"$rlog`"$(if ($Force) { ' -Force' })"
            if ($Detach) {
                $cim = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
                         -Arguments @{ CommandLine = $line; CurrentDirectory = $env:USERPROFILE }
                if ($cim.ReturnValue -eq 0) {
                    $results += "[$r] pane $paneId : detached relaunch pid $($cim.ProcessId) in 45 s, log $rlog - end this turn now"
                } else {
                    $results += "[$r] ERROR - detached start failed (Win32_Process.Create $($cim.ReturnValue))"
                }
            } else {
                $results += "[$r] SKIP - this script runs in $r's own pane; rerun with -Detach, or from outside the fleet: $line"
            }
            continue
        }
        $results += Invoke-PaneRelaunch -Role $r -PaneId $paneId -Project $entry.project -Force:$Force
        continue
    }

    $clear = if ($ClearCmd.ContainsKey($r)) { $ClearCmd[$r] } else { '/clear' }

    if ($ClearCmd.ContainsKey($r)) {
        # Codex pane hazards (2026-08-05): the TUI may have died back to the shell
        # (then /new becomes a pwsh command error), and Ctrl+C during its MCP
        # startup kills the TUI. Probe the screen first.
        $lastLine = (Get-PaneText $paneId) | Where-Object { $_ -match '\S' } | Select-Object -Last 1
        if ($lastLine -match '^\s*PS .*>') {
            # TUI dead - relaunch instead; a fresh process IS a fresh context, no /new needed.
            Submit-PaneText $paneId 'codex'
            $deadline = (Get-Date).AddSeconds(90)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 3
                if (((Get-PaneText $paneId) -join ' ') -match 'Context \d+% (left|used)') { break }
            }
            $results += "[$r] pane $paneId : TUI was dead - relaunched codex (fresh context)"
            continue
        }
        $deadline = (Get-Date).AddSeconds(90)
        while (((Get-PaneText $paneId) -join ' ') -match 'Starting MCP servers' -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
        }
        # Ctrl+C QUITS the codex TUI even when idle (verified 2026-08-05) - never
        # Clear-PaneInput here; Esc clears the composer without killing the app.
        Send-PaneText $paneId ([string][char]27)
        Start-Sleep -Milliseconds 400
        Submit-PaneText $paneId $clear
        $results += "[$r] pane $paneId : $clear (no wake)"
        continue
    }

    Clear-PaneInput $paneId
    Submit-PaneText $paneId $clear
    Start-Sleep -Seconds 3

    $wake = if ($WakeCmd.ContainsKey($r)) { $WakeCmd[$r] }
            else { "/ipc-recover $r" }
    if ($wake) { Submit-PaneText $paneId $wake }

    $suffix = if ($wake) { " -> $wake" } else { " (no wake)" }
    $results += "[$r] pane $paneId : $clear" + $suffix
}

Write-Host ($results -join "`n")
if ($Log) { Add-Content -LiteralPath $Log -Value ("$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') " + ($results -join "`n")) -Encoding utf8 }

# Readback so the operator can eyeball each pane's state.
Start-Sleep -Seconds 6
foreach ($r in $Roles) {
    $entry = $map[$r]
    if (-not $entry -or ($livePanes -notcontains [int]$entry.pane_id)) { continue }
    $tail = (Get-PaneText ([int]$entry.pane_id)) | Where-Object { $_ -match '\S' } | Select-Object -Last 3
    Write-Host "--- [$r] pane $($entry.pane_id) screen tail:"
    $tail | ForEach-Object { Write-Host "    $_" }
}
