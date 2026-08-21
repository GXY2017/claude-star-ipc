# Wake a role's WezTerm pane by injecting a short drain instruction.
# Reason to exist (2026-08-06): the codex pane (role E) cannot self-wake on IPC
# delivery - its $ipc-recover supervisor does not survive turn end (observed: only
# `ipc.py keepalive --me E` left running, no recv armed), and the keepalive beat
# makes `status --watch E` report ALIVE while the mailbox is actually unattended.
# So the hub MUST follow every `send --to E` with this wake. Works for any mapped
# role; claude panes are woken with Ctrl+C-clear + message, codex panes with
# Esc-clear + message (Ctrl+C QUITS the codex TUI - verified 2026-08-05).
# Usage:
#   pwsh ~/.claude/ipc/ipc_wake_pane.ps1                 # wake E, default drain msg
#   pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role E -Message 'custom instruction'
#   pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role E -Model luna   # cheap-tier task: restart codex on gpt-5.6-luna
# -Model (codex roles only): 'luna'|'terra'|'sol' or a full model id. Quota economics
# (2026-08-08): Luna usage counts ~80% cheaper against the Codex subscription, so
# simple dispatches (search digests, doc reading) go to luna; keep sol max for hard
# tasks. Switching RESTARTS the codex TUI (fresh context — fine, every dispatch
# re-briefs E anyway); same model = no restart, context kept.
param(
    [string]$Role = 'E',
    [string]$Message = '',
    [string]$Model = '',
    [switch]$NoClear   # skip the Ctrl+C input-clear (hub wake: never interrupt A's turn)
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\ipc_wezterm_common.ps1"

$CodexRoles = @('E', 'CODEX')
$IpcPy = Join-Path $env:USERPROFILE '.claude\ipc\ipc.py'

if (-not $Message) {
    # Claim reminder (2026-08-17, #1186 fix): require-watcher refuses roles with
    # no registry owner (REFUSED-SQUATTER), so a woken pane must ensure its claim.
    $RolePy = Join-Path $env:USERPROFILE '.claude\ipc\ipc_role.py'
    $Message = "IPC wake: mailbox has messages for $Role. Run python `"$IpcPy`" recv --me $Role to fetch, execute the task, send the result back to A (use --body-file for CJK/backtick bodies), then done --me $Role --task N. Also verify your registry claim: if python `"$RolePy`" status shows role $Role owner '-', run python `"$RolePy`" take $Role (unclaimed roles are REFUSED-SQUATTER on A's require-watcher dispatch)."
}

if (-not (Resolve-WeztermSocket)) { throw 'WezTerm GUI not running / socket unreachable' }
$map = Read-PaneMap
if (-not $map.ContainsKey($Role)) { throw "role $Role not in pane map $script:PaneMapPath" }
$paneId = [int]$map[$Role].pane_id
$livePanes = @(Get-WeztermPanes | ForEach-Object { $_.pane_id })
if ($livePanes -notcontains $paneId) { throw "pane $paneId for role $Role is gone - relaunch via ipc_wezterm_launch.ps1" }

if ($CodexRoles -contains $Role) {
    $modelMap = @{ luna = 'gpt-5.6-luna'; terra = 'gpt-5.6-terra'; sol = 'gpt-5.6-sol' }
    $targetModel = ''
    if ($Model) {
        $targetModel = if ($modelMap.ContainsKey($Model.ToLower())) { $modelMap[$Model.ToLower()] } else { $Model }
    }
    $launchCmd = if ($targetModel) { "codex -m $targetModel" } else { 'codex' }

    $lastLine = (Get-PaneText $paneId) | Where-Object { $_ -match '\S' } | Select-Object -Last 1
    if ($lastLine -match '^\s*PS .*>') {
        # TUI died back to the shell: relaunch codex (fresh process = fresh context)
        # and drive it to ready. Wait-CodexReady also rides out the npm self-update
        # flow unattended (confirm + re-relaunch, user rule 2026-08-06).
        Submit-PaneText $paneId $launchCmd
        if (-not (Wait-CodexReady $paneId -LaunchCmd $launchCmd)) {
            Write-Warning "[$Role] pane $paneId : codex did not reach ready after relaunch - injecting anyway"
        }
        Write-Host "[$Role] pane $paneId : TUI was dead - relaunched codex first"
    }
    $deadline = (Get-Date).AddSeconds(90)
    while (((Get-PaneText $paneId) -join ' ') -match 'Starting MCP servers' -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
    }
    if ($targetModel -and ((Get-PaneText $paneId) -join "`n") -notmatch [regex]::Escape($targetModel)) {
        # Footer shows the live model ("gpt-5.6-sol max · Context ..."); wrong one ->
        # restart on target. Ctrl+C QUITS the codex TUI - normally the trap, here the tool.
        for ($try = 0; $try -lt 2; $try++) {
            Send-PaneText $paneId ([string][char]3)
            $qdl = (Get-Date).AddSeconds(10)
            $atShell = $false
            while ((Get-Date) -lt $qdl) {
                Start-Sleep -Seconds 2
                $last = (Get-PaneText $paneId) | Where-Object { $_ -match '\S' } | Select-Object -Last 1
                if ($last -match '^\s*PS .*>\s*$') { $atShell = $true; break }
            }
            if ($atShell) { break }
        }
        if (-not $atShell) { throw "[$Role] pane $paneId : could not quit codex TUI for model switch" }
        Submit-PaneText $paneId $launchCmd
        if (-not (Wait-CodexReady $paneId -LaunchCmd $launchCmd)) {
            Write-Warning "[$Role] pane $paneId : codex did not reach ready on $targetModel - injecting anyway"
        }
        Write-Host "[$Role] pane $paneId : codex restarted on $targetModel"
    }
    # Esc clears the composer; never Ctrl+C on a codex pane (it quits the TUI).
    Send-PaneText $paneId ([string][char]27)
    Start-Sleep -Milliseconds 400
} else {
    if ($Model) { Write-Warning "-Model only applies to codex roles; ignored for $Role" }
    if (-not $NoClear) { Clear-PaneInput $paneId }
}

Submit-PaneText $paneId $Message
Write-Host "[$Role] pane $paneId : wake injected"

# Readback so the caller can confirm the message actually submitted.
Start-Sleep -Seconds 4
$tail = (Get-PaneText $paneId) | Where-Object { $_ -match '\S' } | Select-Object -Last 4
Write-Host "--- [$Role] screen tail:"
$tail | ForEach-Object { Write-Host "    $_" }
