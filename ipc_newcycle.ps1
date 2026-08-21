# Start a fresh IPC task cycle: batch /clear + role recovery across the WezTerm
# panes recorded by ipc_wezterm_launch.ps1. This is the "reset all terminals for
# a new project" button.
#
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1              # all mapped roles
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Roles B,C   # subset
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -Force       # don't skip BUSY workers
#   pwsh ~/.claude/ipc/ipc_newcycle.ps1 -SkipHub     # leave the hub pane alone
#
# Per role: send /clear, wait, send /ipc-recover <role> (workers; the hub just
# continues after /clear). A BUSY worker (mid-task) is skipped unless -Force -
# clearing it would strand the in-flight task as NEEDS-REVIEW/requeue.

param(
    [string[]]$Roles,
    [switch]$Force,
    [switch]$SkipHub
)

. "$PSScriptRoot\ipc_wezterm_common.ps1"

# `pwsh -File` passes "-Roles B,C" as one literal string; normalize.
if ($Roles) { $Roles = @($Roles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }

$Hub   = if ($env:IPC_HUB) { $env:IPC_HUB } else { 'A' }
$IpcPy = Join-Path $env:USERPROFILE ".claude\ipc\ipc.py"

# Non-claude TUIs need different commands; empty wake = nothing sent after clear.
$ClearCmd = @{ CODEX = '/new'; E = '/new' }
$WakeCmd  = @{ CODEX = ''; E = '' }

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

    # BUSY guard (workers only; the hub is watcher-less so status means nothing there)
    if ($r -ne $Hub -and -not $Force) {
        Push-Location $entry.project
        try { $status = (& python $IpcPy status --watch $r 2>&1) -join ' ' } finally { Pop-Location }
        if ($status -match '\bBUSY\b') { $results += "[$r] SKIP - BUSY (mid-task); use -Force to clear anyway"; continue }
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
                if (((Get-PaneText $paneId) -join ' ') -match 'Context \d+% left') { break }
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
            elseif ($r -eq $Hub) { '' }
            else { "/ipc-recover $r" }
    if ($wake) { Submit-PaneText $paneId $wake }

    $suffix = if ($wake) { " -> $wake" } elseif ($r -eq $Hub) { " (hub, no recover)" } else { " (no wake)" }
    $results += "[$r] pane $paneId : $clear" + $suffix
}

Write-Host ($results -join "`n")

# Readback so the operator can eyeball each pane's state.
Start-Sleep -Seconds 6
foreach ($r in $Roles) {
    $entry = $map[$r]
    if (-not $entry -or ($livePanes -notcontains [int]$entry.pane_id)) { continue }
    $tail = (Get-PaneText ([int]$entry.pane_id)) | Where-Object { $_ -match '\S' } | Select-Object -Last 3
    Write-Host "--- [$r] pane $($entry.pane_id) screen tail:"
    $tail | ForEach-Object { Write-Host "    $_" }
}
