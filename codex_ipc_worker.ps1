#Requires -Version 7
# codex_ipc_worker.ps1 — external watcher daemon for an IPC worker role.
# Parks `ipc.py recv --block` on the role's inbox (heartbeat file => status ALIVE,
# so hub A can dispatch with --require-watcher), executes each incoming task via
# `codex exec` headless, and replies to A with --in-reply-to (which marks the
# task done — done state is derived from the reply link, ipc.py:600).
# Same pattern as gdrive-guard: OS-level loop owns liveness, not the model.
#
# Run:  pwsh -File codex_ipc_worker.ps1 -Role B
# Stop: Ctrl+C (or kill the process); parked heartbeat expires within seconds.
param(
    [string]$Role = 'B',
    [ValidateSet('keepalive','execute')]
    [string]$Mode = 'keepalive',         # keepalive: beat heartbeat only, tasks queue for
                                         # an interactive window; execute: run tasks headless
    [ValidateSet('codex','claude','claude-ds')]
    [string]$Executor = 'codex',         # execute mode: which headless model runs the tasks
    [string]$CodexProfile = '',          # optional: layer $CODEX_HOME/<name>.config.toml
    [ValidateSet('read-only','workspace-write','danger-full-access')]
    [string]$Sandbox = 'read-only',      # codex sandbox; claude uses AllowedTools instead
    [string]$ClaudeAllowedTools = 'Read,Grep,Glob',  # claude -p read-only tool allowlist
    [int]$AckIntervalSec = 240,          # lease renewal cadence during codex runs
    [int]$TaskTimeoutSec = 1800,         # hard ceiling per codex exec; kill + fail beyond
    [int]$ReplyMaxChars = 6000,          # inline reply cap; full text stays on disk
    [string]$WorkRoot = (Get-Location).Path,  # the opted-in PROJECT ROOT (mailbox resolves by cwd) — pass explicitly when not launching from it
    [string]$CodexExe = ''               # native codex.exe / claude.exe; auto-resolved when empty
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$IpcPy  = Join-Path $env:USERPROFILE '.claude\ipc\ipc.py'
# ipc.py resolves the mailbox by walking up from CWD to a project marker
# (CLAUDE.md/.claude/.git). Pin it so a Startup-folder or scheduler launch
# still lands in the right project's mailbox instead of a silent stray one.
Set-Location $WorkRoot
if ($Mode -eq 'execute' -and $Role -match ',') {
    throw "execute mode serves ONE role; comma lists are keepalive-only"
}
# npm's `codex` is a .ps1 shim — Start-Process needs the real PE executable
if ($Mode -eq 'execute' -and -not $CodexExe) {
    if ($Executor -eq 'codex') {
        $cmd = Get-Command codex -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.CommandType -eq 'Application') { $CodexExe = $cmd.Source }
        else {
            $CodexExe = Get-ChildItem (Join-Path $env:APPDATA 'npm\node_modules\@openai') `
                            -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue |
                        Select-Object -First 1 -ExpandProperty FullName
        }
    } else {
        $CodexExe = (Get-Command claude -ErrorAction SilentlyContinue).Source
    }
}
if ($Mode -eq 'execute' -and (-not $CodexExe -or -not (Test-Path $CodexExe))) {
    throw "executor binary not found — pass -CodexExe explicitly"
}
if ($Mode -eq 'execute' -and $Executor -eq 'claude-ds') {
    # same routing the user's claude-ds profile function sets; children inherit
    if (-not $env:DEEPSEEK_API_KEY) { throw 'DEEPSEEK_API_KEY not set' }
    $env:ANTHROPIC_BASE_URL           = 'https://api.deepseek.com/anthropic'
    $env:ANTHROPIC_AUTH_TOKEN         = $env:DEEPSEEK_API_KEY
    $env:ANTHROPIC_MODEL              = 'deepseek-v4-flash[1m]'
    $env:ANTHROPIC_DEFAULT_OPUS_MODEL = 'deepseek-v4-flash[1m]'
    $env:ANTHROPIC_DEFAULT_SONNET_MODEL = 'deepseek-v4-flash[1m]'
    $env:ANTHROPIC_DEFAULT_HAIKU_MODEL  = 'deepseek-v4-flash'
    $env:CLAUDE_CODE_SUBAGENT_MODEL     = 'deepseek-v4-flash'
    $env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
}
$LogDir = Join-Path $env:USERPROFILE ".claude\ipc\codex_worker_$($Role -replace '[^A-Za-z0-9_]','-')"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'daemon.log'

function Write-Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Encoding utf8NoBOM $Log
}

function Send-Reply([int]$MsgId, [string]$Body) {
    $bf = Join-Path $LogDir "reply_$MsgId.md"
    Set-Content -Path $bf -Value $Body -Encoding utf8NoBOM
    & python $IpcPy send --from $Role --to A --body-file $bf --in-reply-to $MsgId 2>&1 |
        ForEach-Object { Write-Log "send: $_" }
}

function Invoke-CodexTask($Msg) {
    $id = [int]$Msg.id
    $outFile    = Join-Path $LogDir "task_${id}_result.md"
    $promptFile = Join-Path $LogDir "task_${id}_prompt.md"
    $execLog    = Join-Path $LogDir "task_${id}_exec.log"
    $execErr    = Join-Path $LogDir "task_${id}_exec.err.log"
    Remove-Item $outFile -ErrorAction SilentlyContinue
    Set-Content -Path $promptFile -Value $Msg.body -Encoding utf8NoBOM

    # lease renewal while codex runs; bounded by TaskTimeoutSec (C review [HIGH]:
    # an unbounded ack loop would keep a wedged task alive forever)
    $ackJob = Start-Job -ScriptBlock {
        param($py, $role, $interval, $maxRenewals)
        $env:PYTHONIOENCODING = 'utf-8'
        for ($i = 0; $i -lt $maxRenewals; $i++) {
            Start-Sleep -Seconds $interval
            & python $py ack --me $role 2>&1 | Out-Null
        }
    } -ArgumentList $IpcPy, $Role, $AckIntervalSec, ([math]::Ceiling($TaskTimeoutSec / $AckIntervalSec) + 1)

    if ($Executor -eq 'codex') {
        $codexArgs = @('exec', '-s', $Sandbox, '-C', $WorkRoot, '--skip-git-repo-check',
                       '-o', $outFile, '--color', 'never')
        if ($CodexProfile) { $codexArgs += @('--profile', $CodexProfile) }
        $codexArgs += '-'                 # read prompt from stdin (quoting/length safe)
    } else {
        # claude -p headless: prompt from stdin, final answer on stdout ($execLog)
        $codexArgs = @('-p', '--output-format', 'text', '--allowedTools', $ClaudeAllowedTools)
    }
    Write-Log "task #${id}: $Executor exec start (timeout=${TaskTimeoutSec}s)"
    $rc = $null; $timedOut = $false; $launchErr = ''
    # Start-Process joins -ArgumentList with spaces WITHOUT quoting — a spaced
    # path (e.g. "My Project") splits into two args and shifts positionals
    $argv = $codexArgs | ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }
    try {
        $p = Start-Process -FilePath $CodexExe -ArgumentList $argv -NoNewWindow -PassThru `
                -WorkingDirectory $WorkRoot -RedirectStandardInput $promptFile `
                -RedirectStandardOutput $execLog -RedirectStandardError $execErr -ErrorAction Stop
        if ($p.WaitForExit($TaskTimeoutSec * 1000)) {
            $rc = $p.ExitCode
        } else {
            $timedOut = $true
            $p.Kill($true)                # kill whole codex process tree
            Write-Log "task #${id}: TIMEOUT after ${TaskTimeoutSec}s, killed"
        }
    } catch {
        $launchErr = "$_"                 # launch/wait exception must NOT kill the loop
        Write-Log "task #${id}: EXCEPTION $launchErr"
    } finally {
        Stop-Job $ackJob -ErrorAction SilentlyContinue
        Remove-Job $ackJob -Force -ErrorAction SilentlyContinue
    }
    Write-Log "task #${id}: $Executor exec exit=$($rc ?? '(none)') timedOut=$timedOut"

    $resultFile = if ($Executor -eq 'codex') { $outFile } else { $execLog }
    if ($rc -eq 0 -and (Test-Path $resultFile) -and (Get-Item $resultFile).Length -gt 0) {
        $result = Get-Content $resultFile -Raw
        if ($result.Length -gt $ReplyMaxChars) {
            $result = $result.Substring(0, $ReplyMaxChars) + "`n...[truncated; full text: $resultFile]"
        }
        Send-Reply $id $result
    } else {
        $reason = if ($launchErr) { "daemon exception: $launchErr" }
                  elseif ($timedOut) { "codex exec timeout after ${TaskTimeoutSec}s" }
                  else { "codex exec exit=$rc" }
        $tail = ''
        foreach ($f in @($execErr, $execLog)) {
            if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
                $tail = (Get-Content $f -Tail 15) -join ' | '; break
            }
        }
        if ($tail.Length -gt 500) { $tail = $tail.Substring(0, 500) }
        & python $IpcPy fail --me $Role --task $id --reason "$reason; log=$execLog; tail: $tail" 2>&1 |
            ForEach-Object { Write-Log "fail: $_" }
    }
}

Write-Log "daemon start: role=$Role mode=$Mode executor=$Executor timeout=${TaskTimeoutSec}s workroot=$WorkRoot"
if ($Mode -eq 'keepalive') {
    # liveness only: keep the watcher heartbeat fresh so the hub can dispatch
    # anytime (require-watcher passes); rows queue UNCLAIMED until the user's
    # interactive window parks its own recv/watch and drains them with context.
    while ($true) {
        & python $IpcPy keepalive --me $Role 2>&1 | ForEach-Object { Write-Log "keepalive: $_" }
        Write-Log "keepalive exited (code $LASTEXITCODE) — restarting in 5s"
        Start-Sleep 5
    }
}
try {
    while ($true) {
        $out = & python $IpcPy recv --me $Role --block --json 2>&1
        $code = $LASTEXITCODE
        if ($code -eq 2) { continue }                   # empty timeout — re-park
        if ($code -ne 0) { Write-Log "recv error ($code): $out"; Start-Sleep 5; continue }
        $msgs = @()
        foreach ($line in ($out -split "`r?`n")) {
            if ($line -notmatch '^\s*\{') { continue }
            try { $msgs += ($line | ConvertFrom-Json) }
            catch {
                # claimed but unparseable — must still produce exactly one receipt
                Write-Log "bad json: $line"
                if ($line -match '"id"\s*:\s*(\d+)') {
                    & python $IpcPy fail --me $Role --task $Matches[1] --reason 'daemon json parse failed' 2>&1 |
                        ForEach-Object { Write-Log "fail: $_" }
                } else {
                    & python $IpcPy send --from $Role --to A "[$Role daemon] 收到无法解析的入站消息,原文见 daemon.log" 2>&1 |
                        ForEach-Object { Write-Log "send: $_" }
                }
            }
        }
        if ($msgs.Count -gt 1) {
            # batch claimed: renew every claimed lease up front so queued items
            # don't expire while earlier ones execute (C review [MEDIUM])
            & python $IpcPy ack --me $Role 2>&1 | Out-Null
        }
        foreach ($msg in $msgs) {
            Write-Log "MSG #$($msg.id) from=$($msg.from) type=$($msg.type) len=$($msg.body.Length)"
            if ($msg.from -ne 'A') {
                # never link --in-reply-to here: could mark a foreign task done
                & python $IpcPy send --from $Role --to A "[$Role daemon] 收到非 hub 消息 #$($msg.id)(from=$($msg.from)),已忽略。" 2>&1 |
                    ForEach-Object { Write-Log "send: $_" }
                continue
            }
            if ($msg.type -eq 'note') {
                Send-Reply $msg.id "[$Role daemon] 收到 note #$($msg.id),无需操作。"
                continue
            }
            try { Invoke-CodexTask $msg }                # msg_type task => execute
            catch {
                # last-resort guard: a task must never take the daemon down
                Write-Log "task #$($msg.id): UNHANDLED $_"
                & python $IpcPy fail --me $Role --task $msg.id --reason "daemon unhandled exception: $_" 2>&1 |
                    ForEach-Object { Write-Log "fail: $_" }
            }
        }
    }
} finally {
    Get-Job | Stop-Job -ErrorAction SilentlyContinue
    Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
    Write-Log "daemon exit: role=$Role"
}
