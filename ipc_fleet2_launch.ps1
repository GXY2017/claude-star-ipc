# Second-formation launcher (project rule 08-09, revised same day): fleet 2 runs
# in its OWN wezterm-gui process — ONE window, ONE pane, role X only — so the two
# formations restart independently (killing fleet-1's pinned pid no longer takes
# X down, and vice versa). State goes to wezterm_fleet2.json; wezterm_panes.json
# (fleet 1) is never touched, so newcycle/wake ignore X by construction.
#
#   pwsh ~/.claude/ipc/ipc_fleet2_launch.ps1                     # menu: pick the project
#   pwsh ~/.claude/ipc/ipc_fleet2_launch.ps1 -Project "D:\proj"   # no prompt
#
# The project is no longer hard-coded: with no -Project it shows the same
# history-derived menu as `cc project` (Select-IpcProject in the common file),
# defaulting to the HOME DIR (launch-dir rule revised 2026-08-11). Headless
# callers (a Claude session, a piped run) get an ERROR, not a default: user rule
# 2026-08-13 says every wezterm launch asks which project, so a Claude session
# must ask the human and pass -Project.
#
# Safe to invoke from inside a Claude session: it detects CLAUDE* env and
# re-launches itself DETACHED first (verified 2026-08-09: a pane spawned from a
# sandboxed harness process inherits a crippled PATH — cmd.exe couldn't even
# find pwsh; Start-Process detachment is the fix, same as ipc_wezterm_launch).
param(
    [switch]$Force,
    [switch]$Detached,
    [string]$Project   # empty = ask (console) / home dir (headless), per rule 2026-08-11
)
$ErrorActionPreference = 'Stop'

. "$env:USERPROFILE\.claude\ipc\ipc_wezterm_common.ps1"

$insideClaude = @(Get-ChildItem env: | Where-Object { $_.Name -like 'CLAUDE*' }).Count -gt 0
Get-ChildItem env: | Where-Object { $_.Name -like 'CLAUDE*' } |
    ForEach-Object { Remove-Item "env:$($_.Name)" -ErrorAction SilentlyContinue }
# Strip inherited WEZTERM_* too (2026-08-17, memory
# wezterm-env-inheritance-gui-launch): run from inside a fleet-1 pane this
# script carries fleet1's WEZTERM_CONFIG_FILE/WEZTERM_UNIX_SOCKET; the detached
# re-launch (Start-Process below) and the spawned mux/GUI inherit them, and the
# GUI then loads the wrong mux config and dies with "domain not found".
# Resolve-WeztermSocket -Fleet fleet2 re-pins the socket this script needs.
Get-ChildItem env: | Where-Object { $_.Name -match '^WEZTERM_' } |
    ForEach-Object { Remove-Item "env:$($_.Name)" -ErrorAction SilentlyContinue }

# --- resolve the project BEFORE any detach ---
# The detached child has no console, so a menu there would read EOF and silently
# take the fallback. Ask here, in the process the human is looking at, and hand
# the answer down via -Project.
if (-not $Project) {
    # Default (Enter / headless) = HOME DIR, per the revised launch-dir rule
    # 2026-08-11 (memory home-dir-launch-default). Pass -Project for real
    # project work. X still claims its role at home: IPC_ROLE (set below) is a
    # sufficient opt-in (ipc_role.py:92) - the missing ipc.enabled gate only
    # affects bare sessions launched there without IPC_ROLE.
    if ($insideClaude) {
        # User rule 2026-08-13: EVERY wezterm launch asks which project. A Claude
        # session has no console for the picker, so it must ask the human itself
        # and pass -Project - silently taking the home dir once put role X on a
        # different mailbox than the session that launched it.
        throw "no -Project: ask the user which project first, then re-run with -Project '<path>' (never let this default silently)"
    }
    if (Test-IpcInteractive) {
        $Project = Select-IpcProject -Default $env:USERPROFILE -Title 'Fleet 2 (role X) - which project?'
    } else {
        throw "no console for the picker and no -Project: pass -Project '<path>' explicitly"
    }
}
if (-not (Test-Path -LiteralPath $Project)) { throw "Project path does not exist: $Project" }
$Project = (Resolve-Path -LiteralPath $Project).Path
Write-Output "project: $Project"

if ($insideClaude -and -not $Detached) {
    $log = Join-Path $env:USERPROFILE '.claude\ipc\fleet2_last.log'
    $argList = @('-NoProfile','-File',"`"$PSCommandPath`"",'-Detached','-Project',"`"$Project`"")
    if ($Force) { $argList += '-Force' }
    Start-Process $script:PaneShell -ArgumentList $argList `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Write-Output "caller is a Claude session -> re-launched detached; log: $log"
    exit 0
}

# refuse double-launch unless -Force. Mux world (2026-08-17): "running" means
# the fleet2 mux answers AND the recorded pane is live - the GUI is only a view,
# so a dead GUI with a live pane is NOT down (reattach: wezterm connect fleet2).
# -Force (or a broken mux with no live pane) tears down the MUX server, which is
# what actually kills the pane tree; the old code only killed the GUI.
if (Resolve-WeztermSocket -Fleet fleet2) {
    $st = $null
    if (Test-Path $script:Fleet2Path) { try { $st = Get-Content $script:Fleet2Path -Raw | ConvertFrom-Json } catch {} }
    $livePane = $false
    if ($st -and $null -ne $st.pane_id) {
        $livePane = @(Get-WeztermPanes | Where-Object { $_.pane_id -eq [int]$st.pane_id }).Count -gt 0
    }
    if ($livePane -and -not $Force) {
        Write-Output "fleet2 already running (mux pane $($st.pane_id)); reattach the view with: wezterm connect fleet2; use -Force to relaunch"
        exit 0
    }
    $muxPids = Read-MuxPids
    if ($muxPids.ContainsKey('fleet2')) {
        $mp = Get-Process -Id $muxPids['fleet2'] -ErrorAction SilentlyContinue
        if ($mp -and $mp.ProcessName -eq 'wezterm-mux-server') {
            Stop-Process -Id $mp.Id -Force -Confirm:$false
            Write-Output "killed old fleet2 mux pid $($mp.Id)"
        }
    }
    if ($st -and $st.gui_pid -and (Test-WeztermGuiPid ([int]$st.gui_pid))) {
        Stop-Process -Id ([int]$st.gui_pid) -Force -Confirm:$false
    }
    Start-Sleep -Seconds 2
    # Same socket-verify as the fleet-1 restart: a stale pids file with a live
    # mux would otherwise strand an unreachable orphan mux (deleting a live
    # socket file below cuts new connections but not the process) whose old X
    # watcher then fights the fresh pane over the role.
    if (Resolve-WeztermSocket -Fleet fleet2) {
        Get-CimInstance Win32_Process -Filter "Name='wezterm-mux-server.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'mux-fleet2\.lua' } | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
                Write-Output "killed stray fleet2 mux pid $($_.ProcessId) (socket still answered)"
            }
        Start-Sleep -Seconds 2
        if (Resolve-WeztermSocket -Fleet fleet2) {
            throw "fleet2 mux socket still answers after teardown - inspect Get-Process wezterm-mux-server before relaunching"
        }
    }
    Remove-Item $script:MuxSock['fleet2'] -ErrorAction SilentlyContinue
}

$guiPid = Start-WeztermGuiAndWait $Project -Fleet fleet2
if (-not $guiPid) { throw "fleet2 mux failed to come up / socket unreachable" }
Write-Output "fleet2 mux + GUI up, gui pid $guiPid"

$null = Resolve-WeztermSocket -Fleet fleet2
$paneId = [int](Get-WeztermPanes | Select-Object -First 1).pane_id
& $script:WeztermExe cli set-tab-title --pane-id $paneId 'X-fleet2' *> $null
Write-Output "using initial pane $paneId"

Start-Sleep -Seconds 5   # let the initial pwsh profile finish loading
# Explicit Set-Location (2026-08-17): the mux's initial pane does NOT inherit
# the mux server's -WorkingDirectory (same bug as fleet 1's first role - it
# landed in home, putting the role on the wrong mailbox since the harness
# resolves IPC state from cwd).
Submit-PaneText $paneId "Set-Location -LiteralPath '$Project'; `$env:IPC_ROLE='X'; claude --permission-mode auto"

if (Wait-PaneReady $paneId 90) {
    Write-Output "[X] TUI ready"
} else {
    Write-Output "[X] WARN: readiness timeout; screen tail:"
    (Get-PaneText $paneId | Out-String).Trim() -split "`n" | Select-Object -Last 10
}
# 2026-08-17: was 'standby' — a bare wake parks nothing (see #1186). /ipc-recover X
# deterministically parks the watcher under Monitor and verifies the registry claim.
Submit-PaneText $paneId '/ipc-recover X'

@{ gui_pid = $guiPid; mux_pid = (Read-MuxPids)['fleet2']; pane_id = $paneId; project = $Project; launched_at = (Get-Date -Format s) } |
    ConvertTo-Json | Set-Content $script:Fleet2Path -Encoding utf8
Write-Output "[X] wake sent; state saved: $script:Fleet2Path"
