# Shared helpers for WezTerm-based IPC terminal control. Dot-source from
# ipc_wezterm_launch.ps1 / ipc_newcycle.ps1. Verified 2026-08-03 against
# wezterm nightly 20260803; see memory wezterm-pane-injection-verified.

# wezterm emits UTF-8; under -NoProfile the console decodes native output with the
# OEM codepage, so a CJK pane title (e.g. a Chinese session title) turns into
# mojibake and `cli list --format json | ConvertFrom-Json` throws. Observed
# 2026-08-11 - every wake against the formation failed until this was pinned.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script:WeztermExe  = "C:\Program Files\WezTerm\wezterm.exe"
$script:WeztermGui  = "C:\Program Files\WezTerm\wezterm-gui.exe"
# Pane shell MUST be the MSI pwsh by absolute path, never bare `pwsh`: a bare
# name can resolve to the Store MSIX alias, and Store servicing force-terminates
# every running package process - on 2026-08-17 07:22 an unattended PowerShell
# 7.6.4->7.6.5 MSIX update killed both fleets' panes at once (memory
# store-pwsh-update-kills-fleet).
$script:PaneShell   = "C:\Program Files\PowerShell\7\pwsh.exe"
$script:WeztermMux  = "C:\Program Files\WezTerm\wezterm-mux-server.exe"

# Mux-domain world (2026-08-17, adopted from the paseo evaluation): pane processes
# live under a per-fleet wezterm-mux-server, the GUI is a detachable VIEW attached
# via `wezterm connect fleet1|fleet2` (domains defined in ~/.wezterm.lua). A GUI
# death (accidental close, crash, unattended OS/Store update) no longer kills the
# fleet - reconnect and everything is back. Sockets are FIXED per-fleet paths, so
# the old gui-sock-<pid> scan was removed same day (see Resolve-WeztermSocket).
$script:MuxSock = @{
    fleet1 = Join-Path $env:USERPROFILE ".local\share\wezterm\fleet1.sock"
    fleet2 = Join-Path $env:USERPROFILE ".local\share\wezterm\fleet2.sock"
}
$script:MuxCfg = @{
    fleet1 = Join-Path $env:USERPROFILE ".claude\ipc\mux-fleet1.lua"
    fleet2 = Join-Path $env:USERPROFILE ".claude\ipc\mux-fleet2.lua"
}
$script:MuxPidPath = Join-Path $env:USERPROFILE ".claude\ipc\wezterm_mux_pids.json"  # { fleet1 = pid; fleet2 = pid }

function Read-MuxPids {
    if (Test-Path $script:MuxPidPath) {
        try {
            $raw = Get-Content $script:MuxPidPath -Raw | ConvertFrom-Json
            $m = @{}; foreach ($p in $raw.PSObject.Properties) { $m[$p.Name] = [int]$p.Value }
            return $m
        } catch {}
    }
    return @{}
}

function Save-MuxPid([string]$Fleet, [int]$MuxPid) {
    $m = Read-MuxPids; $m[$Fleet] = $MuxPid
    $m | ConvertTo-Json | Set-Content $script:MuxPidPath -Encoding utf8
}
$script:PaneMapPath = Join-Path $env:USERPROFILE ".claude\ipc\wezterm_panes.json"
$script:GuiPidPath  = Join-Path $env:USERPROFILE ".claude\ipc\wezterm_gui_pid.txt"   # fleet-1 GUI pin
$script:Fleet2Path  = Join-Path $env:USERPROFILE ".claude\ipc\wezterm_fleet2.json"   # fleet-2 state

function Test-IpcInteractive {
    # A menu only makes sense with a human at a real console. Redirected stdin
    # (Claude's harness, a detached child, a piped run) makes Read-Host return
    # EOF instantly, so callers must fall back instead of "prompting" into a log.
    if (-not [Environment]::UserInteractive) { return $false }
    try { $null = [Console]::KeyAvailable } catch { return $false }
    return $true
}

function Select-IpcProject {
    # Numbered project picker for the launchers. Reuses Get-ClaudeProject from
    # ~/.claude/scripts/project-launcher.ps1 - the same history scan behind
    # `cc project` - so the two menus list the same directories and can't drift.
    # Returns $Default on cancel/bad input, so callers get a usable path or the
    # empty string they passed in.
    param([string]$Default, [string]$Title = 'Which project?')

    $launcher = Join-Path $env:USERPROFILE '.claude\scripts\project-launcher.ps1'
    if (-not (Test-Path $launcher)) {
        Write-Warning "project-launcher.ps1 not found - cannot show the picker"
        return $Default
    }
    . $launcher   # function scope: brings in Get-ClaudeProject (and `cc`)

    # The home dir is the standing default launch dir (rule revised 2026-08-11,
    # memory home-dir-launch-default). It is filtered from the history scan and
    # appended LAST as an explicit, clearly-labeled entry instead of being mixed
    # into history. Note: home can never get a .claude\ipc.enabled gate (enable
    # refuses it), but fleet panes still claim roles there via the IPC_ROLE env
    # var the launchers set (ipc_role.py:92 opt-in, :427 deterministic claim).
    $home_ = $env:USERPROFILE.TrimEnd('\')
    $projects = @(Get-ClaudeProject | Where-Object { $_.Path.TrimEnd('\') -ne $home_ })
    $projects += [pscustomobject]@{ Name = 'gxy49 (home, default dir)'; Path = $env:USERPROFILE }
    if ($projects.Count -eq 0) { return $Default }

    Write-Host ""
    Write-Host $Title -ForegroundColor Cyan
    for ($i = 0; $i -lt $projects.Count; $i++) {
        # [ipc] = has the .claude\ipc.enabled gate. Informational only for
        # fleets: panes claim via IPC_ROLE regardless of the gate; the gate
        # matters for bare sessions launched there without IPC_ROLE.
        $gate = if (Test-Path (Join-Path $projects[$i].Path '.claude\ipc.enabled')) { 'ipc' } else { '   ' }
        $mark = if ($Default -and $projects[$i].Path -eq $Default) { '*' } else { ' ' }
        ("{0}{1,3}) [{2}] {3,-24} {4}" -f $mark, ($i + 1), $gate, $projects[$i].Name, $projects[$i].Path) | Write-Host
    }
    $prompt = if ($Default) { "Select 1-$($projects.Count) (Enter = $Default)" }
              else          { "Select 1-$($projects.Count) (Enter to cancel)" }
    # -NonInteractive hosts throw here rather than returning EOF, and the
    # launchers run with $ErrorActionPreference='Stop' - fall back, never die.
    try { $sel = Read-Host $prompt } catch { Write-Warning "no prompt available - keeping $Default"; return $Default }

    if ([string]::IsNullOrWhiteSpace($sel)) { return $Default }
    $idx = 0
    if (-not [int]::TryParse($sel, [ref]$idx) -or $idx -lt 1 -or $idx -gt $projects.Count) {
        Write-Host "Not a valid choice - keeping $Default" -ForegroundColor Yellow
        return $Default
    }
    return $projects[$idx - 1].Path
}

function Test-WeztermGuiPid([int]$ProcId) {
    $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    return [bool]($p -and $p.ProcessName -eq 'wezterm-gui')
}

function Resolve-WeztermSocket([string]$Fleet = 'fleet1') {
    # Pin WEZTERM_UNIX_SOCKET for all cli calls (wezterm cli resolves the socket
    # path cwd-relatively on Windows, upstream PR #7896 unmerged).
    # Mux world: the FIXED per-fleet mux socket is the ONLY thing this resolves.
    # The old gui-sock-<pid> fallback was removed 2026-08-17 (migration done,
    # both fleets relaunch mux-hosted): with no mux up it would latch onto ANY
    # live wezterm-gui - a manual window - and either fake a "fleet running"
    # answer (small pane_ids collide) or silently spawn a whole fleet into an
    # unprotected GUI window, exactly the failure mode the mux migration kills.
    $msock = $script:MuxSock[$Fleet]
    if ($msock -and (Test-Path $msock)) {
        $env:WEZTERM_UNIX_SOCKET = $msock
        & $script:WeztermExe cli --no-auto-start list *> $null
        if ($LASTEXITCODE -eq 0) { return $msock }
        Remove-Item env:WEZTERM_UNIX_SOCKET -ErrorAction SilentlyContinue
        Remove-Item $msock -ErrorAction SilentlyContinue   # stale socket from a dead mux
    }
    return $null
}

function Invoke-EnvStripped([scriptblock]$Body) {
    # CLAUDE_* leaking into pane processes makes workers run as nested child
    # sessions (transcript saving off, no resume); an inherited IPC_ROLE lets a
    # pane squat the launcher session's role (X-eviction bug, 2026-08-12, memory
    # ipc-child-session-evicts-role). Start-Process -UseNewEnvironment does NOT
    # reliably strip them (verified 2026-08-03), so strip our own env around any
    # process launch whose environment reaches the panes. In the mux world that
    # is the MUX SERVER (pane processes are its children); the GUI is stripped
    # too for safety, and `cli spawn` still forwards the CALLER's env, so the
    # launchers keep their own top-level strip as well.
    # WEZTERM_* added 2026-08-17 (memory wezterm-env-inheritance-gui-launch): a
    # launcher running inside the OTHER fleet's pane carries that fleet's
    # WEZTERM_CONFIG_FILE/WEZTERM_UNIX_SOCKET; a spawned wezterm-gui then loads
    # the wrong mux config and dies with "domain not found" - silently under
    # Start-Process. Restore-after is safe: the launcher re-pins its own
    # WEZTERM_UNIX_SOCKET via Resolve-WeztermSocket before any cli call.
    $saved = @{}
    Get-ChildItem env: | Where-Object { $_.Name -match '^CLAUDE|^WEZTERM_' -or $_.Name -eq 'IPC_ROLE' } | ForEach-Object {
        $saved[$_.Name] = $_.Value
        Remove-Item "env:$($_.Name)"
    }
    try { & $Body } finally {
        foreach ($k in $saved.Keys) { Set-Item "env:$k" $saved[$k] }
    }
}

function Start-WeztermMux([string]$Fleet, [string]$Cwd) {
    # Start the per-fleet mux server (its config pins the socket path + MSI-pwsh
    # default_prog). $Cwd sets the server's working dir, but the default
    # window's INITIAL PANE does NOT inherit it (disproved 2026-08-17: first
    # role landed in home) - launchers must inject an explicit Set-Location
    # into that pane. Deliberately NOT
    # --daemonize: Start-Process already detaches, and skipping the daemon fork
    # keeps -PassThru's pid = the real server pid (pinned for restart teardown).
    # Returns the mux pid on success (socket up + cli answering), $false on failure.
    $proc = Invoke-EnvStripped {
        Start-Process $script:WeztermMux -ArgumentList '--config-file',"`"$($script:MuxCfg[$Fleet])`"" `
            -WorkingDirectory $Cwd -WindowStyle Hidden -PassThru
    }
    foreach ($i in 1..20) {
        Start-Sleep -Milliseconds 800
        if (Test-Path $script:MuxSock[$Fleet]) {
            $env:WEZTERM_UNIX_SOCKET = $script:MuxSock[$Fleet]
            & $script:WeztermExe cli --no-auto-start list *> $null
            if ($LASTEXITCODE -eq 0) { Save-MuxPid $Fleet $proc.Id; return $proc.Id }
        }
    }
    return $false
}

function Connect-WeztermGui([string]$Fleet) {
    # Attach a GUI view to the fleet's mux domain (domain defs + appearance come
    # from ~/.wezterm.lua). Returns the GUI pid once it shows up in list-clients
    # (pid still returned on wait-timeout - the GUI may just be slow to paint;
    # panes never depended on it).
    $proc = Invoke-EnvStripped {
        Start-Process $script:WeztermGui -ArgumentList 'connect',$Fleet -PassThru
    }
    foreach ($i in 1..15) {
        Start-Sleep -Milliseconds 800
        $clients = & $script:WeztermExe cli --no-auto-start list-clients --format json 2>$null | ConvertFrom-Json
        if (@($clients | Where-Object { $_.pid -eq $proc.Id }).Count -gt 0) { return $proc.Id }
    }
    return $proc.Id
}

function Start-WeztermGuiAndWait([string]$Cwd, [string]$Fleet = 'fleet1') {
    # Mux world: "start the fleet host" = ensure the mux server, then attach a
    # GUI view. Returns the GUI pid (truthy) so callers can pin it; $false on
    # mux failure. Killing the returned GUI pid no longer kills the fleet.
    if (-not (Resolve-WeztermSocket -Fleet $Fleet)) {
        if (-not (Start-WeztermMux $Fleet $Cwd)) { return $false }
    }
    return (Connect-WeztermGui $Fleet)
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

function Wait-CodexReady([int]$PaneId, [int]$TimeoutSec = 150, [string]$LaunchCmd = 'codex') {
    # codex (npm-installed) self-updates on launch (user rule 2026-08-06: never
    # wait for a human — if an update lands, take it). Three states to drive
    # through without keystrokes:
    #   1. interactive update/trust confirmation -> send Enter (default = yes);
    #   2. update ran and codex EXITED ("Please restart Codex" + idle shell
    #      prompt) -> type `codex` again, restart the wait window;
    #   3. TUI footer up and MCP handshakes done -> ready.
    # Relaunch cap of 2 guards against a crash loop. The idle-prompt regex
    # requires NOTHING after '>' so the just-typed 'PS ...> codex' echo line
    # can't retrigger a relaunch.
    $relaunches = 0
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $lines = Get-PaneText $PaneId
        $t = $lines -join "`n"
        $last = $lines | Where-Object { $_ -match '\S' } | Select-Object -Last 1
        if ($t -match 'Context \d+% left' -and $t -notmatch 'Starting MCP servers') { return $true }
        if ($t -match '(?i)trust this (folder|directory)') { Send-PaneText $PaneId "`r"; continue }
        if ($t -match '(?i)restart Codex' -and $last -match '^\s*PS .*>\s*$') {
            if ($relaunches -ge 2) { return $false }
            $relaunches++
            Submit-PaneText $PaneId $LaunchCmd
            $deadline = (Get-Date).AddSeconds($TimeoutSec)
            continue
        }
        if ($t -match '(?i)updat.*(\(y/n\)|\[y/n\])|(?i)press enter to (update|continue)') {
            Send-PaneText $PaneId "`r"; continue
        }
    }
    return $false
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
        # First-run announcement/changelog modals OVERLAY a ready-looking footer,
        # so the footer check alone passes while the modal silently eats the wake
        # line (2026-08-15: "Don't show again ... Enter to confirm - Esc to
        # cancel" ate `standby` on 3 panes). Esc dismisses without side effects.
        # Trust dialog is matched above, so this branch never cancels it.
        if ($txt -match 'Esc to cancel') { Send-PaneText $PaneId ([string][char]27); continue }
        # Footer varies by permission mode: manual shows "? for shortcuts",
        # auto shows "auto mode on", etc. Any of these = TUI at its prompt.
        if ($txt -match '\? for shortcuts|auto mode on|accept edits on|plan mode on') { return $true }
    }
    return $false
}
