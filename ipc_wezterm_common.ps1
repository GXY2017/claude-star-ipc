# Shared helpers for WezTerm-based IPC terminal control. Dot-source from
# ipc_wezterm_launch.ps1 / ipc_newcycle.ps1. Verified 2026-08-03 against
# wezterm nightly 20260803; see memory wezterm-pane-injection-verified.

$script:WeztermExe  = "C:\Program Files\WezTerm\wezterm.exe"
$script:WeztermGui  = "C:\Program Files\WezTerm\wezterm-gui.exe"
$script:PaneMapPath = Join-Path $env:USERPROFILE ".claude\ipc\wezterm_panes.json"

function Resolve-WeztermSocket {
    # wezterm cli resolves the gui socket path cwd-relatively on Windows (upstream
    # PR #7896 unmerged), so every external call fails unless WEZTERM_UNIX_SOCKET
    # is pinned to the absolute path of the running GUI's socket.
    $gui = Get-Process wezterm-gui -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $gui) { return $null }
    $sock = Join-Path $env:USERPROFILE ".local\share\wezterm\gui-sock-$($gui.Id)"
    if (-not (Test-Path $sock)) { return $null }
    $env:WEZTERM_UNIX_SOCKET = $sock
    return $sock
}

function Start-WeztermGuiAndWait([string]$Cwd) {
    # A GUI launched from inside a Claude session leaks CLAUDE_* vars into every
    # pane (workers then run as nested child sessions with transcript saving off,
    # no resume). Start-Process -UseNewEnvironment does NOT reliably strip them
    # (verified 2026-08-03: marker still present in pane env), so strip explicitly
    # from our own env before launch and restore after.
    $saved = @{}
    Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE' } | ForEach-Object {
        $saved[$_.Name] = $_.Value
        Remove-Item "env:$($_.Name)"
    }
    try {
        Start-Process $script:WeztermGui -ArgumentList 'start','--cwd',"`"$Cwd`"",'pwsh'
    } finally {
        foreach ($k in $saved.Keys) { Set-Item "env:$k" $saved[$k] }
    }
    foreach ($i in 1..20) {
        Start-Sleep -Milliseconds 800
        if (Resolve-WeztermSocket) {
            & $script:WeztermExe cli list *> $null
            if ($LASTEXITCODE -eq 0) { return $true }
        }
    }
    return $false
}

function Get-WeztermPanes {
    (& $script:WeztermExe cli list --format json) | ConvertFrom-Json
}

function Send-PaneText([int]$PaneId, [string]$Text) {
    # --no-paste: characters go in as keystrokes, so an embedded `r submits.
    # Verified: "/clear`r" in ONE send executes the slash command immediately.
    & $script:WeztermExe cli send-text --pane-id $PaneId --no-paste -- $Text
}

function Get-PaneText([int]$PaneId) {
    & $script:WeztermExe cli get-text --pane-id $PaneId
}

function Submit-PaneText([int]$PaneId, [string]$Text, [int]$DelayMs = 400) {
    # Claude Code's paste-guard turns a `r embedded in a rapid injected burst into
    # a NEWLINE (bites long/CJK text: it sits unsubmitted in the input box). Send
    # the text, pause, then a lone CR - recognized as a real Enter.
    Send-PaneText $PaneId $Text
    Start-Sleep -Milliseconds $DelayMs
    Send-PaneText $PaneId "`r"
}

function Clear-PaneInput([int]$PaneId) {
    # One Ctrl+C empties a non-empty input box (a stray draft would otherwise
    # concatenate with our injected command); harmless hint when already empty.
    Send-PaneText $PaneId ([string][char]3)
    Start-Sleep -Milliseconds 300
}

function Read-PaneMap {
    if (Test-Path $script:PaneMapPath) {
        $raw = Get-Content $script:PaneMapPath -Raw | ConvertFrom-Json
        $map = @{}
        foreach ($p in $raw.PSObject.Properties) { $map[$p.Name] = $p.Value }
        return $map
    }
    return @{}
}

function Save-PaneMap([hashtable]$Map) {
    $Map | ConvertTo-Json -Depth 4 | Set-Content $script:PaneMapPath -Encoding utf8
}

function Wait-PaneReady([int]$PaneId, [int]$TimeoutSec = 40) {
    # Poll the screen until the Claude Code TUI is at its input prompt.
    # Handles the first-run folder-trust dialog by confirming it (option 1
    # "Yes, I trust" is pre-selected).
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $txt = (Get-PaneText $PaneId) -join "`n"
        if ($txt -match 'trust this folder') { Send-PaneText $PaneId "`r"; continue }
        # Footer varies by permission mode: manual shows "? for shortcuts",
        # auto shows "auto mode on", etc. Any of these = TUI at its prompt.
        if ($txt -match '\? for shortcuts|auto mode on|accept edits on|plan mode on') { return $true }
    }
    return $false
}
