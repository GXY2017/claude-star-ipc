# Launch IPC terminals as WezTerm panes, one role per tab, and record the
# role -> pane-id map that ipc_newcycle.ps1 batch-controls later.
#
#   pwsh ~/.claude/ipc/ipc_wezterm_launch.ps1                    # A,B in cwd project
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
    [string]$Project = (Get-Location).Path,
    [switch]$DryRun
)

. "$PSScriptRoot\ipc_wezterm_common.ps1"

# `wezterm cli spawn` forwards the CALLER's environment to the new pane (verified
# 2026-08-03: GUI-clean but spawned panes dirty), so this launcher must itself be
# free of CLAUDE_* before ANY spawn - strip unconditionally, no restore needed
# (dedicated short-lived process).
Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE' } | ForEach-Object { Remove-Item "env:$($_.Name)" }

# `pwsh -File` passes "-Roles A,B" as one literal string; normalize.
$Roles = @($Roles | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# Per-role launch command. Edit bindings here. An empty value = refuse to
# launch that role until you fill it in.
# User lineup 2026-08-03: A=Anthropic hub, B=Kimi, C=GLM via ollama cloud, D=DeepSeek.
# All four in --permission-mode auto (user decision 2026-08-03: manual mode stalls
# unattended panes on tool-approval prompts). Wrapper fns forward extra args to claude;
# ollama forwards what follows `--` to the integration.
$LaunchCmd = @{
    A     = 'claude --permission-mode auto'
    B     = "claude-kimi 'k3[1m]' --permission-mode auto"    # positional -Model MUST come first or it swallows the flag (ValidateSet)
    C     = 'ollama launch claude --model glm-5.2:cloud -y -- --permission-mode auto'  # model preselected, -y skips prompts
    D     = 'claude-ds --permission-mode auto'               # profile fn, DeepSeek env block
    E     = 'codex'      # codex CLI slot (letter binding 2026-08-03)
    CODEX = 'codex'      # legacy named channel, redundant with E since 2026-08-03
    DS    = 'claude-ds'  # legacy named channel, redundant with D since 2026-08-03
}
# Per-role model tag for the tab title: "<role>-<tag>" (e.g. A-opus5). Keep in
# sync with $LaunchCmd when rebinding a role; a role without a tag falls back to
# the bare letter.
$ModelTag = @{
    A     = 'opus5'
    B     = 'kimi-k3'
    C     = 'glm5.2'
    D     = 'deepseek'
    E     = 'codex'
    CODEX = 'codex'
    DS    = 'deepseek'
}
# Roles running the codex CLI, not the claude harness: no IPC_ROLE/SessionStart
# hook (registry needs a one-time placeholder take), different TUI (skip the
# claude readiness probe and the standby wake).
$CodexRoles = @('E','CODEX')
$Hub = if ($env:IPC_HUB) { $env:IPC_HUB } else { 'A' }

$Project = (Resolve-Path $Project).Path
if (-not (Test-Path (Join-Path $Project '.claude\ipc.enabled'))) {
    Write-Warning "$Project has no .claude\ipc.enabled gate - the SessionStart hook will NOT claim roles there. Run 'python ~/.claude/ipc/ipc_role.py enable' from that project first."
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
        $envPrefix = if ($r -ne 'CODEX') { "`$env:IPC_ROLE='$r'; " } else { '' }
        $title = if ($ModelTag[$r]) { "$r-$($ModelTag[$r])" } else { $r }
        Write-Host "  [$r] spawn pane (tab '$title', cwd=$Project), type: $envPrefix$($LaunchCmd[$r])"
    }
    return
}

# --- ensure GUI + socket ---
$freshGui = $false
if (-not (Resolve-WeztermSocket)) {
    Write-Host "Starting WezTerm GUI..."
    if (-not (Start-WeztermGuiAndWait $Project)) { throw "WezTerm GUI failed to come up / socket unreachable" }
    $freshGui = $true
}

$map = Read-PaneMap

# A freshly started GUI already owns one pane; use it for the first role
# instead of leaving a stray shell tab.
$initialPane = if ($freshGui) { (Get-WeztermPanes | Select-Object -First 1).pane_id } else { $null }

$launched = @()
foreach ($r in $Roles) {
    if ($null -ne $initialPane) {
        $paneId = [int]$initialPane
        $initialPane = $null
    } else {
        $out = & $script:WeztermExe cli spawn --cwd $Project -- pwsh
        if ($LASTEXITCODE -ne 0) { throw "cli spawn failed for role $r" }
        $paneId = [int]($out | Select-Object -Last 1).Trim()
    }
    $title = if ($ModelTag[$r]) { "$r-$($ModelTag[$r])" } else { $r }
    & $script:WeztermExe cli set-tab-title --pane-id $paneId $title *> $null

    $envPrefix = if ($CodexRoles -notcontains $r) { "`$env:IPC_ROLE='$r'; " } else { '' }
    Submit-PaneText $paneId "$envPrefix$($LaunchCmd[$r])"

    $map[$r] = @{ pane_id = $paneId; project = $Project; launched_at = (Get-Date -Format s) }
    $launched += @{ role = $r; pane = $paneId }
    Write-Host ("[{0}] pane {1}: {2}" -f $r, $paneId, $LaunchCmd[$r])
}

Save-PaneMap $map
Write-Host "pane map saved: $script:PaneMapPath"

# --- wait for TUIs, then wake workers (manual-floor keystroke) ---
foreach ($l in $launched) {
    if ($CodexRoles -contains $l.role) { Write-Host "[$($l.role)] skipping readiness/wake (codex TUI, not claude)"; continue }
    if (Wait-PaneReady $l.pane) {
        if ($l.role -ne $Hub) {
            Submit-PaneText $l.pane "standby"
            Write-Host "[$($l.role)] TUI ready, wake line sent - watcher should park"
        } else {
            Write-Host "[$($l.role)] TUI ready (hub - no watcher needed)"
        }
    } else {
        Write-Warning "[$($l.role)] pane $($l.pane) TUI not detected within timeout - check it manually"
    }
}
