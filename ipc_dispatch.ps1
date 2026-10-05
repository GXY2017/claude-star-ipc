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
#   -Force      = environment gates only (socket/pane/ctx unreadable); recipient must have a parked watcher
#   -ForceLint  = skip the brief lint explicitly (printed as such); the two never imply each other

param(
    [Parameter(Mandatory)][string]$Role,
    [Parameter(Mandatory)][string]$BodyFile,
    [string]$SubmitId,
    [string]$Nudge,        # override the default start-now nudge (codex roles only)
    [string]$From = 'A',
    [int]$ParkTimeoutSec = 180,
    [int]$MaxMinutes = 15,   # user rule 2026-08-29: every dispatch must be deliverable in <= 15 min
    [switch]$Force,          # bypass ENVIRONMENT gates only (pane unreadable / socket down / ctx UNKNOWN)
    [switch]$ForceLint,      # bypass the brief lint (separate on purpose, 2026-08-30 #1209: -Force used to pardon both)
    [switch]$DryRun          # run lint + capacity gate only, send nothing
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\ipc_wezterm_common.ps1"
Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE' -or $_.Name -eq 'IPC_ROLE' } | ForEach-Object { Remove-Item "env:$($_.Name)" }

$ipc = Join-Path $PSScriptRoot 'ipc.py'
if (-not (Test-Path $BodyFile)) { throw "body file not found: $BodyFile" }

# --- 0a. brief lint (mechanical; each check is a 2026-08-29 incident) ---
#   header role == -Role (#1144 F's brief sent to C) · `预算: N 分钟` present and <= MaxMinutes
#   (#1138 18 reports) · UNITS x measured rate fits · paths exist · no empty product target
#   (#1097) · title/body "N 份" agree (#1079) · guard-safe shell shapes · `完成判据:` present.
$lintOut = (& python (Join-Path $PSScriptRoot 'ipc_brief_lint.py') $BodyFile --role $Role --max-min $MaxMinutes 2>&1) -join "`n"
Write-Host $lintOut
if ($LASTEXITCODE -ne 0) {
    # Brief quality and environment health are independent; -Force (environment) never pardons lint.
    if ($ForceLint) { Write-Warning "brief lint FAILED - SKIPPED EXPLICITLY by -ForceLint (say why in the brief)" }
    else            { throw "brief lint FAILED - fix the brief (-ForceLint to skip explicitly; -Force does NOT cover lint)" }
}

# --- 0b. recipient capacity gate: read the PANE status line, not the heartbeat ---
#   2026-08-29: E's registry heartbeat was live while its pane said `Context 0% left`
#   and looped on recv; three tasks went into that hole (memory codex-context-zero-recv-loop).
$probeRaw = (& python (Join-Path $PSScriptRoot 'ipc_pane_probe.py') $Role --json 2>&1) -join "`n"
try { $probe = $probeRaw | ConvertFrom-Json } catch { $probe = $null }
if ($probe) {
    $ctxTxt = if ($null -eq $probe.ctx_left) { '?' } else { "$($probe.ctx_left)%" }
    Write-Host "[$Role] pane $($probe.pane_id) ctx_left=$ctxTxt verdict=$($probe.verdict) flags=$($probe.flags -join ',')"
    if ($probe.verdict -match '^(DEAD|QUOTA|BLOCKED)') {
        if ($Force) { Write-Warning "[$Role] $($probe.verdict) - dispatching anyway because -Force" }
        elseif ($probe.verdict -eq 'QUOTA') {
            $rh = if ($probe.reset_hint) { "reset hint from the banner: $($probe.reset_hint)" } else { "no reset time in the banner (kimi/deepseek print none - see FLEET-TRIAGE T6)" }
            throw "[$Role] QUOTA: rate-limited ($rh) - do NOT restart the pane (FLEET-TRIAGE T6); pick another role or wait"
        }
        else        { throw "[$Role] $($probe.verdict): refresh the pane (ipc_newcycle.ps1 / clear + /ipc-recover $Role) or pick another role; -Force to override" }
    } elseif ($probe.verdict -eq 'LOW-CTX') {
        Write-Warning "[$Role] context below 70% - only a light task belongs here (capacity policy 2026-08-19)"
    } elseif ($probe.verdict -eq 'UNKNOWN') {
        # 2026-08-30 (X, #1168): a gate that cannot read its input must FAIL, not skip.
        # A rogue mux hijacked fleet1.sock -> "no such pane 1/5" -> WARNING -> sent anyway.
        if ($Force) { Write-Warning "[$Role] UNKNOWN pane state ($($probe.error)) - dispatching anyway because -Force" }
        else        { throw "[$Role] capacity gate cannot read the pane ($($probe.error)): fix the socket/pane map (fleet restart or ipc_wezterm_common.ps1 Resolve-WeztermSocket) or -Force with a reason" }
    }
} else {
    if ($Force) { Write-Warning "pane probe failed ($probeRaw) - dispatching anyway because -Force" }
    else        { throw "pane probe failed ($probeRaw): capacity gate has no input; fix the probe or -Force with a reason" }
}
if ($DryRun) { Write-Host "DRY RUN: gates passed for $Role - nothing sent"; exit 0 }
# Keep in sync with $CodexRoles in ipc_wezterm_launch.ps1.
$CodexRoles = @('C', 'E', 'CODEX')   # C = codex since 2026-08-30 (luna max from 09-19)
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
    $fleet = Get-FleetForRole $Role   # X lives on fleet2 (2026-08-30 #1262)
    if (-not (Resolve-WeztermSocket -Fleet $fleet)) { throw "$Role watcher DOWN and no $fleet wezterm socket to re-park it" }
    $pane = Get-PaneIdForRole $Role   # override-aware (socket detour, 2026-08-30 #1235)
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

# --- 1b. append the knowledge boundary + closing protocol by code (2026-10-02) ---
#   Book "Build an AI Agent" §2.3.1 (system prompt states the knowledge cutoff) and §3.2.3
#   (values code already knows are injected by code, not copied by the model). The hub's
#   brief file stays untouched; the queued body is a temp copy with this footer.
$footer = @"

---
（以下由 ipc_dispatch.ps1 自动附加）
今天是 $(Get-Date -Format 'yyyy-MM-dd')。你的训练数据有截止日期：此后的事实、价格、版本、文件内容一律检索或读盘，不得凭记忆补。
收尾协议（回复正文第一行，三选一）：
- 产物完成并通过上面的完成判据 → RESULT: <产物绝对路径>（每个产物一行）
- 做不到（缺权限/缺数据/工具失败且无替代） → UNSOLVABLE: <原因与已尝试的办法>
- 发现简报的前提本身错了（目标、口径、输入与实际不符） → REPLAN: <哪条前提错了 + 证据>
只做了一部分、有疑问：不写以上任何一行，直接说明。
"@
$sendFile = Join-Path ([IO.Path]::GetTempPath()) ("ipc-brief-{0}-{1}.md" -f $Role, (Get-Date -Format 'yyyyMMddHHmmss'))
[IO.File]::WriteAllText($sendFile, ([IO.File]::ReadAllText($BodyFile) + $footer), [Text.UTF8Encoding]::new($false))

# --- 2. queue the task (no --require-watcher: freshly parked heartbeats lose its <8s race) ---
$sendArgs = @('send', '--from', $From, '--to', $Role, '--body-file', $sendFile)
if ($SubmitId) { $sendArgs += @('--submit-id', $SubmitId) }
$out = (& python $ipc @sendArgs 2>&1) -join "`n"
Remove-Item $sendFile -ErrorAction SilentlyContinue   # temp copy; the body now lives in the mailbox DB
Write-Host $out
if ($out -notmatch '(?:SENT|DUP)\s+#(\d+)') { throw "send failed: $out" }
$taskId = $Matches[1]

# --- 3. every worker family needs the nudge ---
# codex prints the delivered task and then idles; the GLM/ollama pane often has
# no parked watcher at all. Either way the fix is the same: type it in.
try { $fleet = Get-FleetForRole $Role; $pane = Get-PaneIdForRole $Role } catch { throw "task #$taskId queued but $($_.Exception.Message)" }
if (-not (Resolve-WeztermSocket -Fleet $fleet)) { throw "task #$taskId queued but no $fleet wezterm socket to nudge $Role" }
if (-not $Nudge) {
    $pull = if ($isCodex) { "" } else { "请先跑 python `"$ipc`" recv --me $Role 取出任务全文，然后" }
    $Nudge = "A 给你派了任务 #$taskId。$pull" + "立刻开始动手执行，不要只回执、不要只做计划；" +
             "本单预算 $MaxMinutes 分钟内交付；每做完一份就追加落盘；每约 10 分钟跑一次 python `"$ipc`" ack --me $Role --task $taskId 续租，全部做完再 send 汇报给 $From（正文用 --body-file，并带 --in-reply-to $taskId 关联）。"
}
Start-Sleep -Seconds 15   # give a parked watcher time to deliver first
# The task is already SENT: a nudge failure must be loud but must not read as "task lost".
# A parked watcher (B/F/X today) delivers without the nudge; a watcher-less role will NOT wake.
try {
    Submit-PaneText $pane $Nudge 1200
    Write-Host "[$Role] nudge sent"
} catch {
    Write-Warning "SENT #$taskId but NUDGE FAILED - $($_.Exception.Message) - $Role 若无盯哨将不会醒，需手动叫"
}

# --- 4. confirm the claim ---
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $p = (& python $ipc pending --hub $From --detail 2>&1) -join "`n"
    if ($p -match "#$taskId .*IN_PROGRESS") { Write-Host "task #$taskId IN_PROGRESS - claimed by $Role"; exit 0 }
}
Write-Host "task #$taskId queued but not yet claimed - check: python `"$ipc`" pending --hub $From --detail"
exit 1
