# One-shot dispatch to a worker role: re-park its watcher if needed, queue the
# task idempotently, nudge codex-family workers to actually start, and confirm
# the claim. Encodes the traps from memory solution-codex-pane-operation:
#   - never Ctrl+C a codex pane (quits the TUI);
#   - codex recv's a task then idles -> needs a typed "start now" nudge;
#   - a freshly parked watcher fails --require-watcher's <8s freshness race,
#     so we park first, then queue WITHOUT the flag and let the watcher pull it;
#   - never send a --type note while a task is queued (reply auto-links wrong).
#
#   pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile task.md -SubmitId mat-b3
#   pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role B -BodyFile task.md            # claude-family: no nudge
#   pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role E -BodyFile task.md -Nudge "自定义开工提示"

param(
    [Parameter(Mandatory)][string]$Role,
    [Parameter(Mandatory)][string]$BodyFile,
    [string]$SubmitId,
    [string]$Nudge,        # override the default start-now nudge (codex roles only)
    [string]$From = 'A',
    [int]$ParkTimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\ipc_wezterm_common.ps1"
Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE' -or $_.Name -eq 'IPC_ROLE' } | ForEach-Object { Remove-Item "env:$($_.Name)" }

$ipc = Join-Path $PSScriptRoot 'ipc.py'
if (-not (Test-Path $BodyFile)) { throw "body file not found: $BodyFile" }
# Keep in sync with $CodexRoles in ipc_wezterm_launch.ps1.
$CodexRoles = @('E', 'CODEX')
$isCodex = $CodexRoles -contains $Role

function Get-WatcherState([string]$r) {
    $o = (& python $ipc status --watch $r 2>&1) -join ' '
    if ($o -match 'ALIVE') { return 'ALIVE' }
    if ($o -match 'BUSY')  { return 'BUSY' }
    return 'DOWN'
}

# --- 1. ensure the watcher is parked ---
$state = Get-WatcherState $Role
if ($state -eq 'BUSY') { throw "$Role is BUSY on another task - not dispatching a second one" }
if ($state -ne 'ALIVE') {
    if (-not (Resolve-WeztermSocket)) { throw "$Role watcher DOWN and no wezterm socket to re-park it" }
    $map = Read-PaneMap
    if (-not $map.ContainsKey($Role)) { throw "$Role watcher DOWN and no pane recorded in wezterm_panes.json" }
    $pane = [int]$map[$Role].pane_id
    Write-Host "[$Role] watcher DOWN - re-parking via pane $pane"
    if ($isCodex) { Submit-PaneText $pane "`$ipc-recover $Role" 800 }
    else          { Submit-PaneText $pane 'standby' 800 }
    $deadline = (Get-Date).AddSeconds($ParkTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if ((Get-WatcherState $Role) -eq 'ALIVE') { break }
    }
    # Not fatal: some workers answer the wake line without actually parking a
    # blocking recv (observed on the GLM/ollama pane - it prints "Standing by."
    # and stays watcher-less). The task still queues; the nudge below makes the
    # worker pull it with an explicit recv.
    if ((Get-WatcherState $Role) -ne 'ALIVE') {
        Write-Warning "[$Role] no watcher heartbeat after ${ParkTimeoutSec}s - queueing anyway and nudging it to recv"
    } else {
        Write-Host "[$Role] watcher parked"
    }
}

# --- 2. queue the task (no --require-watcher: freshly parked heartbeats lose its <8s race) ---
$sendArgs = @('send', '--from', $From, '--to', $Role, '--body-file', $BodyFile)
if ($SubmitId) { $sendArgs += @('--submit-id', $SubmitId) }
$out = (& python $ipc @sendArgs 2>&1) -join "`n"
Write-Host $out
if ($out -notmatch '(?:SENT|DUP)\s+#(\d+)') { throw "send failed: $out" }
$taskId = $Matches[1]

# --- 3. every worker family needs the nudge ---
# codex prints the delivered task and then idles; the GLM/ollama pane often has
# no parked watcher at all. Either way the fix is the same: type it in.
if (-not (Resolve-WeztermSocket)) { throw "task #$taskId queued but no wezterm socket to nudge $Role" }
$map = Read-PaneMap
if (-not $map.ContainsKey($Role)) { throw "task #$taskId queued but no pane recorded for $Role" }
$pane = [int]$map[$Role].pane_id
if (-not $Nudge) {
    $pull = if ($isCodex) { "" } else { "请先跑 python `"$ipc`" recv --me $Role 取出任务全文，然后" }
    $Nudge = "A 给你派了任务 #$taskId。$pull" + "立刻开始动手执行，不要只回执、不要只做计划；" +
             "长任务每约 20 分钟跑一次 python `"$ipc`" ack --me $Role --task $taskId 续租，全部做完再 send 汇报给 $From。"
}
Start-Sleep -Seconds 15   # give a parked watcher time to deliver first
Submit-PaneText $pane $Nudge 1200
Write-Host "[$Role] nudge sent"

# --- 4. confirm the claim ---
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $p = (& python $ipc pending --hub $From --detail 2>&1) -join "`n"
    if ($p -match "#$taskId .*IN_PROGRESS") { Write-Host "task #$taskId IN_PROGRESS - claimed by $Role"; exit 0 }
}
Write-Host "task #$taskId queued but not yet claimed - check: python `"$ipc`" pending --hub $From --detail"
exit 1
