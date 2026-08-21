# 干净地关掉一个角色的 pane，并收回它的盯哨。
#
#   pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role F              # 退出 + 释放角色 + 确认 watcher 退休
#   pwsh ~/.claude/ipc/ipc_pane_exit.ps1 -Role F -KeepRole    # 只退 TUI 与 watcher，角色留着（例如马上原地重拉）
#
# 为什么需要这个脚本（2026-08-21 实测）：
#   * watcher 有两道锚——父进程死、注册表 owner 变——但在这套编队里两道都不响：
#     父进程是比 TUI 活得久的中间进程；owner 则永远停在 "manual"。
#   * owner 停在 "manual" 是因为 SessionEnd 钩子的 release() 按真实 session_id 匹配，
#     而 manual 不是身份，匹配不上。该缺口已在 ipc_role.py::release() 用 IPC_ROLE 兜底修掉。
#   * 但**第三方路由的 pane（glm / deepseek 等）压根不触发 SessionStart/SessionEnd 钩子**，
#     所以不能指望钩子去调 release。退出流程必须自己调。
#   结果：TUI 一关，watcher 却继续跳心跳，status --watch 报 ALIVE、--require-watcher 放行，
#   派出去的任务掉进没人读的信箱——全绿的指示灯下丢消息，比直接 DOWN 危险。
#
# 实测过的两个顺序陷阱：
#   1. 退出确认框里必须选「1. Exit and stop tasks」，选「2. Move to background and exit」
#      正是制造孤儿 watcher 的开关。
#   2. 重新上线时**先 take 角色、再挂 watcher**。反过来的话，take 造成的 owner 变化
#      会立刻把刚挂上的新 watcher 当成过期的踢退休。
param(
    [Parameter(Mandatory = $true)][string]$Role,
    [switch]$KeepRole,
    [int]$TimeoutSec = 60
)
$ErrorActionPreference = 'Stop'
. "$env:USERPROFILE\.claude\ipc\ipc_wezterm_common.ps1"

if (-not (Resolve-WeztermSocket)) { throw 'WezTerm mux 不可达' }
$map = Read-PaneMap
if (-not $map.ContainsKey($Role)) { throw "角色 $Role 不在 pane 映射表里" }
$paneId = [int]$map[$Role].pane_id
$proj = $map[$Role].project

Write-Output "[$Role] pane $paneId : 发 /exit"
Send-PaneText $paneId ([string][char]27)   # 先清 composer，别把 /exit 追加到半截输入后面
Start-Sleep -Milliseconds 400
& $script:WeztermExe cli send-text --pane-id $paneId --no-paste "/exit`r" | Out-Null
Start-Sleep -Seconds 6
# 「Background work is running」确认框：默认高亮就是 1. Exit and stop tasks，回车即可。
& $script:WeztermExe cli send-text --pane-id $paneId --no-paste "`r" | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$atShell = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $last = (Get-PaneText $paneId) | Where-Object { $_ -match '\S' } | Select-Object -Last 1
    if ($last -match 'PS .*>\s*$') { $atShell = $true; break }
}
if (-not $atShell) {
    Write-Output "[$Role] WARN: ${TimeoutSec}s 内没回到 shell —— 停手，别硬拉，去看那个 pane"
    exit 1
}
Write-Output "[$Role] TUI 已退出"

if ($KeepRole) {
    Write-Output "[$Role] -KeepRole：跳过 release（角色保留），直接收 watcher"
} else {
    # 显式 release：不靠钩子。ipc_role.py 只释放「无主 / 本会话 / manual」的槽，
    # 别人真实会话持有的槽它不碰。
    Push-Location $proj
    try {
        $env:IPC_ROLE = $Role
        '{"session_id":"pane-exit","reason":"exit"}' |
            python "$env:USERPROFILE\.claude\ipc\ipc_role.py" release
        Write-Output "[$Role] 已释放注册表槽位"
    } finally {
        Remove-Item env:IPC_ROLE -ErrorAction SilentlyContinue
        Pop-Location
    }
}

# ---- 收 watcher ----
# 判据不是「有没有叫这个名字的进程」，而是「**本项目**的这个角色还有没有心跳」——
# 后者去问 ipc.py（它按 cwd 解析项目状态，所以整段必须在项目目录里跑）。
# 按进程名扫是 X 在 #538 指出的跨项目误杀源：每个 IPC 项目都有 A，
# 别的项目的 A 不会因本项目的 release 退休，等待必然超时，然后被兜底杀掉。
function Get-RoleWatchers([string]$role) {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -match "watch --me $role\b" })
}

# 归属判定：沿父链上溯，看本编队的 mux server 是不是它的祖先。
# 归属不了就**不杀**——宁可留一个报警，也不碰别的项目的进程。
function Test-BelongsToFleet([int]$procId, [int[]]$muxPids) {
    $seen = @{}
    $cur = $procId
    for ($hop = 0; $hop -lt 12; $hop++) {
        if ($cur -le 0 -or $seen.ContainsKey($cur)) { return $false }
        $seen[$cur] = $true
        if ($muxPids -contains $cur) { return $true }
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if (-not $p) { return $false }
        $cur = [int]$p.ParentProcessId
    }
    return $false
}

$muxPids = @()
$muxFile = "$env:USERPROFILE\.claude\ipc\wezterm_mux_pids.json"
if (Test-Path $muxFile) {
    (Get-Content $muxFile -Raw | ConvertFrom-Json).PSObject.Properties |
        ForEach-Object { $muxPids += [int]$_.Value }
}

Push-Location $proj
try {
    if ($KeepRole) {
        # 这条路径下没有任何东西会触发退休（owner 没变、父链还在、gen 没 bump），
        # 等下去必然白等 —— 直接走归属杀，不打「锚为什么没响」那种误导性 WARN。
        $gone = $false
    } else {
        # owner 一变，watcher 下一轮 poll（约 2-3 秒）就自己退休。轮询到本项目报 DOWN 为止。
        # 判据必须认输出里的字面 DOWN，**不能用退出码**：
        # status --watch 在 DOWN 和 “ALIVE [SQUATTER]” 两种状态下都返回 1，
        # 只看退出码会把「还在跳但注册表已无主」误判成「已退休」而提前收工
        # （2026-08-21 实测踩过：脚本报已退休，最后一行却打出 ALIVE [SQUATTER]）。
        $gone = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 2
            $st = (python "$env:USERPROFILE\.claude\ipc\ipc.py" status --watch $Role 2>&1) -join ' '
            if ($st -match '\bDOWN\b') { $gone = $true; break }
        }
    }

    if ($gone) {
        Write-Output "[$Role] watcher 已自行退休（注册表锚生效）"
    } else {
        $cands = Get-RoleWatchers $Role
        $mine = @($cands | Where-Object { Test-BelongsToFleet ([int]$_.ProcessId) $muxPids })
        if ($mine.Count -gt 0) {
            $mine | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -Confirm:$false
                if ($KeepRole) {
                    Write-Output "[$Role] 已收掉 watcher pid $($_.ProcessId)（-KeepRole：角色保留）"
                } else {
                    Write-Output "[$Role] WARN: watcher 没自行退休，兜底杀掉 pid $($_.ProcessId) —— 锚为什么没响，值得查"
                }
            }
            # Stop-Process 跳过了 watch 的清理路径，心跳文件会留在盘上，
            # 在 max-age（默认 8 秒）内 status 仍会报 ALIVE。等它过期再报，别让最后一行自打嘴巴。
            Start-Sleep -Seconds 10
        } elseif ($cands.Count -gt 0) {
            Write-Output "[$Role] WARN: 本项目仍有心跳，但 $($cands.Count) 个候选进程都归属不到本编队 mux —— 不杀，请人工确认："
            $cands | ForEach-Object { Write-Output "        pid $($_.ProcessId): $($_.CommandLine)" }
        } else {
            Write-Output "[$Role] WARN: 本项目仍报有心跳，却找不到对应进程（心跳文件残留？）"
        }
    }

    # 心跳有 max-age（默认 8 秒）的滞后：进程都没了，最后一拍还在窗口内，status 照样报 ALIVE。
    # 等它落定再打印，别让收尾这行跟上面的结论打架。
    for ($i = 0; $i -lt 8; $i++) {
        $st = (python "$env:USERPROFILE\.claude\ipc\ipc.py" status --watch $Role 2>&1) -join ' '
        if ($st -match '\bDOWN\b') { break }
        Start-Sleep -Seconds 2
    }
    python "$env:USERPROFILE\.claude\ipc\ipc.py" status --watch $Role
} finally {
    Pop-Location
}
