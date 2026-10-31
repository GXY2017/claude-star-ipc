# FLEET-TRIAGE — 编队异常排障清单（常设，X 维护）

用法：告警当场按「症状」找条目，**照抄那一条命令**，按判据裁，按处置做。每条只读探查命令都带硬超时、`--no-auto-start`、rc+JSON 双判。
异常归属：编队里没法预料的异常一律派 fleet2 的 X（用户 2026-08-30 定）。改工具脚本要落在下一批真会跑的代码里，不落一次性脚本。

固定路径：
- `python ~/.claude/ipc/fleet_list_panes.py [sock名...]` — 列每个 socket 上的 pane（默认 fleet1.sock fleet2.sock gui-sock-<fleet1 GUI pid> gui-sock-<fleet2 GUI pid>；GUI pid 在 `wezterm_gui_pid.txt` / `wezterm_fleet2.json`）
- `python ~/.claude/ipc/fleet_get_text.py <sock名> <pane_id> [尾行数]` — 只读某 pane 屏幕尾部
- `python ~/.claude/ipc/ipc_pane_probe.py <角色> [--json]` — 读 context % / 状态旗标 / 裁决（已带 --no-auto-start + 25 s 超时）
- `python ~/.claude/ipc/ipc_role.py status` — 注册表 × 心跳对账
- `python ~/.claude/ipc/ipc.py pending --hub A --detail` — 在途任务（在项目根跑）
- socket 正确路径：`~\.local\share\wezterm\fleet1.sock` / `fleet2.sock`（**没有 `sock\` 子目录**）

---

## 0. 两条原则（今天三件的共同病根，2026-08-30）

**P1 量尺读不到输入 = 硬失败，不是 WARNING。** 容量闸 / `Resolve-WeztermSocket` / `stall_watch` 一天跑出三个实例：读不到 pane 被当成「pane 空闲且没变」，闸跳过照发、时钟对健康的 B 打 ACT NOW。已修：`ipc_dispatch.ps1` UNKNOWN → throw；回复时钟（当时的 `stall_watch.py`，2026-10-18 起并入 `ipc.py` 的 `clock_tick`）→ `PROBE-FAILED` 不裁决；`Send-PaneText` 非零退出 → throw。
判据：任何告警行里出现 `ctx ?` / `flags=`（空）/ `error=` 非空 / `UNKNOWN` / `PROBE-FAILED`，先当「没读到」，不当「读到了」。

**P2 socket 没有身份。** `fleet1.sock` 只是路径，谁绑上都算数；`cli list` 退出码 0 只证明「有个 mux 在答」，不证明是编队那个。判「编队在不在」要核 pane 集合：list 出的 pane id 必须包含 `wezterm_panes.json` 里全部 id，且 title/cwd 像编队（cwd=项目根，不是 home）。
```
python ~/.claude/ipc/fleet_list_panes.py fleet1.sock
```
判据：6 个 pane、cwd 都是项目根 → 真编队；只有 1 个 `pwsh.exe` cwd=home → 被别的 mux 顶了（见 T1）。
地面真值（不经 wezterm）：worker 增量落盘文件的 mtime + 字节数；回复时钟每行末尾已自动附 `artifact=... (Nm ago)`。

**P3 一个开关只管一件性质的事。** `-Force` 曾同时赦免环境闸和简报 lint（#1208：lint FAIL 照发）。已拆：`-Force` 只越环境闸（socket/pane/ctx 读不到），`-ForceLint` 单独跳 lint 且打 `SKIPPED EXPLICITLY`。判据：看到 `LINT FAIL` 紧接 `SENT` 而没有 `SKIPPED EXPLICITLY` 行 = 开关又混了。

---

## T1. 派单报 `no such pane N`、容量闸 UNKNOWN、nudge 失败（fleet1.sock 被流氓 mux 劫持）

症状：`[B] pane 1 ctx_left=? verdict=UNKNOWN` + `Error: no such pane 1`（F 是 5）；X（fleet2）却正常。
探查：
```
python ~/.claude/ipc/fleet_list_panes.py fleet1.sock gui-sock-<fleet1 GUI pid>
pwsh -NoProfile -c "Get-CimInstance Win32_Process -Filter \"Name='wezterm-mux-server.exe'\" | Select-Object ProcessId,CreationDate,CommandLine | Format-List"
```
判据：mux 进程里有一条**没有 `--config-file`** 的裸 `wezterm-mux-server.exe`，且 fleet1.sock 上只有它的空 pwsh pane → 劫持确认。成因几乎总是：有人跑了 `wezterm cli ...` **没带 `--no-auto-start`** 又连错 socket，wezterm 自动 spawn 了裸 mux，裸 mux 读 `~/.wezterm.lua` 第一个 unix_domain（=fleet1）重绑了真 socket（`~/.local/share/wezterm/wezterm.exe-log-<pid>.txt` 里有 "Will try spawning the server" 即为铁证）。
处置：
1. `Stop-Process` 那个裸 mux（先核 cmdline 精确等于 `"C:\Program Files\WezTerm\wezterm-mux-server.exe"`）。真编队的 mux（带 `--config-file mux-fleet1.lua`）和 GUI 不受影响，worker 任务不断。
2. 真 socket 文件已被顶掉无法挂回：**等在途任务交付后**跑 `pwsh ~/.claude/ipc/ipc_fleet1_restart.ps1`。重启前派 fleet1 角色会被闸拦（正确行为），要派用 `-Force` 并写理由。
3. 过渡期让探针 / 派单闸 / 时钟自动走 GUI socket：写 `~/.claude/ipc/wezterm_sock_override.json`（`{"fleet1": {"sock": "<gui-sock-<pid> 绝对路径>", "pane_offset": 1, "expect_pane_ids": [1,2,3,4,5,6], "assumes": "...", "reason": "..."}}`）。`ipc_pane_probe.py resolve()` 与 `Resolve-WeztermSocket` 读到它就改走该 socket、pane id 加偏移，但**先核 pane 集合**：`cli list` 返回的 id 集合必须恰等于 `expect_pane_ids`，否则硬失败（P2）——fleet1 新开/关任何 pane 偏移即作废。`ipc_fleet1_restart.ps1` 在 pending 检查通过后、kill mux 之前**自动删除**它，不会隔夜生效。红线：过渡期 GUI 窗口不能关（它是真 mux 唯一入口）。
   手工只读真编队走 GUI socket：`fleet_list_panes.py gui-sock-<pid>` / `fleet_get_text.py gui-sock-<pid> <映射 id + 1>`（GUI 本地 pane id = `wezterm_panes.json` id **+1**，用 title 核）。不要往在途 worker 的 pane send-text。
验证：`python ~/.claude/ipc/ipc_pane_probe.py B F --json` 全部 `error=null`；`ipc_dispatch.ps1 -Role B -BodyFile <简报> -DryRun` 打 `DRY RUN: gates passed`。

## T2. `wezterm cli list` 挂死 / 返回错误文本 / exit 0 但不是 JSON

症状：命令跑满超时零输出；或 `ConvertFrom-Json: ... position 2` 遇到 `:`（时间戳开头的 ERROR 行被当 JSON）；外层 exit 0。
成因：三件叠加——socket 路径错（多了 `sock\`，或单引号里 `$HOME` 没展开）、**缺 `--no-auto-start`**（触发 spawn 裸 mux，daemon 继承 stdout 管道，管道不关外层就等）、pwsh 管道外层 exit 不反映 native rc（native rc=1 时外层仍 0）。
探查（不会挂，0.03 s 返回）：
```
python ~/.claude/ipc/fleet_list_panes.py fleet1.sock fleet2.sock
```
判据：`ERR: failed to connect` → socket 死/不存在（看 T1）；`(0 panes)` 或只 1 个 home pwsh → 劫持（T1）；正常 → 6 pane。
处置：以后**所有** cli 调用只用上面两个脚本或 `ipc_pane_probe.py`；手写时三铁律：正确路径、`--no-auto-start`、判 rc==0 且 JSON 可解析（不看 pwsh 外层 exit）。
验证：同一命令 1 秒内返回且输出以 `pane=` 开头。

## T3. 注册表 C / E DORMANT、owner 还在、心跳死（codex 常态，不是故障）

症状：`ipc_role.py status` 里 C/E `DORMANT (reclaimable)`，pane 屏幕却写着 "recv --block ... active"。
探查：
```
pwsh -NoProfile -c "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'ipc.py' } | Select-Object ProcessId,CreationDate,CommandLine | Format-Table -AutoSize"
python ~/.claude/ipc/fleet_get_text.py gui-sock-<fleet1 GUI pid> <C 或 E 的映射 id + 1> 20
```
判据：进程表里没有 `recv --me C/E` → 收件器已随 codex 回合结束被杀，屏幕那句是假的；状态行 `Context NN% left` 健康（>20%）→ pane 本身没病。若 `Context 0%` + 屏幕反复 `recv --block` → 另一种病（memory `codex-context-zero-recv-loop`），处置=撤单改派+刷新 pane。
处置：**什么都不做**。不要 `reclaim-dead`（owner 清成 `-` 后派单撞 `REFUSED-SQUATTER`），不要刷新 pane（白烧上下文）。派单时 `ipc_dispatch.ps1` 步骤 1 会自动往 pane 打 `$multi-terminal-ipc recover <角色>` 再排队——前提是 fleet1 socket 健康（T1）。
验证：派单后 `python ~/.claude/ipc/ipc.py status --watch C` 出 ALIVE。
备注：E 曾在 `recv --me E` 抛 `sqlite3.OperationalError: attempt to write a readonly database`（2026-08-30 11:0x），DB 及 -wal/-shm 实测可写、C 同为 codex 写库正常，原因未定；复现时让 E 把完整 traceback 落盘。

## T4. 15 分钟回复时钟打 `ACT NOW: pane idle & unchanged`

症状：`REPLY-CLOCK #id -> B 15m | ctx ? flags= tail=SAME | ACT NOW: pane idle ...`。
判据：`ctx ?` + `flags=` 空 = 探针没读到（P1），不是 worker 停摆。新版已改打 `PROBE-FAILED: cannot read pane (<原因>)` 且不裁决；行尾 `artifact=<文件> (Nm ago)` 是地面真值——几分钟内有更新 = worker 在干活。
处置：先看 artifact 字段；`no-artifact` 且 pane 也读不到时，再用 `fleet_get_text.py`（GUI socket）看屏幕，仍不明才叫人。**改了 `ipc.py`（时钟在 `clock_tick`）后必须 TaskStop 旧的 `watch --me A` Monitor 重挂**，旧进程跑的是旧代码。
已知边界（A #1190，2026-08-30，未修）：`artifact_truth()` 是「简报里全文最新的绝对路径」，若简报把产物写成**裸文件名**、只有输入文件是绝对路径，它会挑中一个旧输入（实例：#1167 挑到 `分析素材.md` 662m ago，看着像停摆）。读 artifact 字段时先核那个文件名是不是本单产物；`(Nm ago)` 很大而文件名像输入/库文件 → 当 `no-artifact` 看。计划修法：① `ipc_brief_lint.py` D 档加「产物路径必须绝对路径」，源头拦；② `artifact_truth()` 只认产物词（产物/追加进/写入/新建）同行或下一行的绝对路径，挑不到诚实写 `no-artifact`。写简报的规矩：更正类简报也把产物路径写全。

## T5. `SENT #id but NUDGE FAILED`

症状：派单打出这一行（新版 `ipc_dispatch.ps1` 步骤 3）。
判据：任务已排队；已挂盯哨的角色（`ipc.py status --watch <角色>` ALIVE）会自己收到，无伤；DOWN 的角色**不会醒**。
处置：ALIVE → 不用管，等步骤 4 的 `IN_PROGRESS` 确认；DOWN → 走 T1 查 socket，修好后手动 `pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role <角色> -Message "/ipc-recover <角色>"`。

## T6. worker 撞 5 小时 / 周限额——横幅含限额词 + pane idle 即判 QUOTA，不是卡死；时间戳有则记、无则估

症状：任务已认领 → 产物零增长（`stall_watch` 行尾 `artifact=…` 十几分钟不动或 `no-artifact`）→ pane 尾部有**限额横幅**。各家写法不同（状态码 403/429 不一、有无重置时刻不一、可能夹带 `Please run /login` 之类误导句），**不要拿任何一家的形状当判据**。已采样本（以后每见一家新的就往下加）：
```
# glm（F，2026-08-30 12:42 / 13:03）：429，带重置时刻
● API Error: Request rejected (429) · [1308][已达到 5 小时的使用上限。
  您的限额将在 2026-08-30 15:39:19 重置。]
# kimi（B，2026-08-30 13:47，#1251 时钟自动命中）：403，无时刻，多一句 Please run /login（不是掉登录）
● Please run /login · API Error: 403 You've reached your 5-hour usage
  limit. Your quota will reset when the current 5-hour window ends. To
  continue now, purchase extra usage or upgrade your plan:
  https://www.kimi.com/membership/subscription?tab=quota
```
```
# deepseek（D，2026-08-30 14:33 重启后首个请求）：402，**不是限额是余额**——重置时刻不存在，要用户充值；探针同样判 QUOTA（拒发），处置只有「报用户」
● API Error: 402 Insufficient Balance
```
探查：
```
python ~/.claude/ipc/ipc_pane_probe.py <角色> --json
python ~/.claude/ipc/fleet_get_text.py fleet1.sock <映射 id> 30      # socket 正常时直连；被劫持期间改 gui-sock-<pid> 与 id+1
```
账户级 vs 会话级：**重启 pane/fleet 不解限额**（2026-08-30 14:33 整队重启后 B 403、F 429 原样重现），限额挂在账户上；只有等重置（或充值）。
判据（与「卡死」的区分，**不依赖时间戳**）：尾部含限额词（`usage limit` / `reached your … limit` / `使用上限` / `quota will reset` / `限额将在`）**且** pane 在提示符（`flags` 含 `idle`）→ QUOTA。探针可达时直接 `verdict=QUOTA`（正则 `ipc_pane_probe.py:53` 已含两家词形）。卡死是：**没有**任何横幅、屏幕不动、`working`/`idle` 都不明。`Please run /login` 与限额词同屏时按 QUOTA 处理，不当掉登录。
重置时刻：横幅有 → 原样记下；没有 → 记「窗口结束时间未知」，按「首次撞限额时刻 + 5 小时」估上界（kimi 5 小时窗口按首次调用起算，实际可能更早），到估计时刻前每 30 分钟探一次。
处置：① `python ~/.claude/ipc/ipc.py cancel --task <id> --by A`（项目根跑）撤在途单；② 记下（或估出）重置时刻，到点前不派该角色（派单闸以 `QUOTA` 拦，`-Force` 也别越）；③ **不要重启它的 pane**——会话上下文完好，到点自动恢复（无时刻的那家也一样）；④ 先读盘：worker 往往在撞限额前已把产物落盘（F 的 `_F_review_slice7.md` 8/8 齐全），直接收。
验证：`ipc_pane_probe.py <角色>` 回到 `OK/LOW-CTX`，或 `fleet_get_text.py` 尾部不再有限额行；派一单轻任务确认认领。有明确时刻的到点即验；无时刻的按估计时刻起每 30 分钟验一次。

## T6b. QUOTA 假阳性：角色在**谈论**限额时被判成自己撞了限额（已修）

症状：派单闸/时钟对一个健康角色打 `verdict=QUOTA flags=idle,quota_banner`，pane 里没有错误横幅，只有正文在汇报别人的限额（实例 2026-08-30 14:32：X 汇报「B、F 均在限额中 / quota will reset」，闸拒发 #1255 给 X）。
判据（形态，不是否定词黑名单）：限额词只在**API 错误横幅块**里计数——起首为助手圆点 `●`/`•` 且同一行含 `API Error` 的行，加上它的缩进续行（到下一个圆点/提示符/分隔线/非缩进行为止）；窗口仍是尾部 40 个非空行。`ipc_pane_probe.py` `BANNER_START` / `banner_blocks()`。正文里的限额词（缩进段落、表格、引用）一律不计。
验收样本（`~/.claude/ipc/samples/`，探针改动后必须重跑）：`quota_kimi_B_403.txt`、`quota_deepseek_D_402.txt`、`quota_glm_F_429.txt`（14:4x 活采，须 QUOTA）；`falsepos_X_report1252_synth.txt`（X #1252 汇报正文按 claude pane 排版合成，须 OK）。
codex 形态（2026-08-30 17:0x，C/E 双双撞额度，#1294）：**行首 `■ `**（codex 的系统通知标记；助手正文用 `•`，工具拒绝用 `✗`）起头、续行**顶格不缩进**、到 `› Ask Codex` 提示符或状态行为止；没有 `●`、没有 `API Error`。样本 `quota_codex_C_credits.txt` / `quota_codex_E_credits.txt`：
```
■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro),
visit https://chatgpt.com/codex/settings/usage to purchase more credits or try
again at 9:05 PM.
› Ask Codex to do anything
```
探针 `CODEX_BANNER_START` / `CODEX_BANNER_STOP` 单独成一种块语法；E 那份里还有缩进的「Automatic approval review failed: You've hit your usage limit …」与 `✗ Request denied …`，不作锚点（缩进正文可能是汇报），靠 `■` 块即可命中。
重置时刻：探针 `reset_hint` 字段按各家原文抓——codex `try again at 9:05 PM` → `9:05 PM`；glm `限额将在 2026-08-30 15:39:19 重置` → 完整时刻；kimi / deepseek 无时刻 → `null`（不硬凑）。派单闸拒发语带上它。
修正记录：第一版 `BANNER_STOP` 的 `^\s*\S` 把缩进续行也当块结束，claude 横幅块只剩首行，三家旧样本靠首行碰巧命中的词才判对——已改为「标记行（可缩进）或顶格行才结束」，glm 块回到 3 行。教训：**回归表里 verdict 对了不等于块切对了**，改横幅语法时要同时打印块行数。
样本清单（七份，探针每次改动都重跑）：`quota_kimi_B_403` / `quota_deepseek_D_402` / `quota_glm_F_429` / `quota_codex_C_credits` / `quota_codex_E_credits` 须 QUOTA；`falsepos_X_report1252_synth` / `falsepos_X_reporting_quotas` 须 OK。

## T7. 整队重启把 hub A 也带走了（用户 2026-10-18 定：hub 留在编队，接受此代价）

症状：`ipc_fleet1_restart.ps1` 后 A 的上下文消失；日志出现 `[A] orphan watcher pid … killed` + `[A] pane 0: claude …`。
根因：A 的 pane 与 worker 同在 fleet1 的 mux 里，kill mux 即 kill A；脚本当时默认 `-Roles A,B,C,D,E,F`(2026-10-27 起默认只 A)，`-Roles` 去掉 A 也救不了（pane 随 mux 死）。实例 2026-08-30 14:33（#1255）：新 A 14:33:15 起，从信箱重建了在途状态，但上午对话没了。
探查：
```
pwsh -NoProfile -c "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' } | Select-Object ProcessId,CreationDate,CommandLine | Sort-Object CreationDate | Format-Table -AutoSize"
```
判据：没有早于重启时刻的 `claude.exe` = 旧 A 已死；pane 0 显示的「A 还记得」是新 A 读信箱重建的，不是存活。
处置：重启前默认假设 **A 会丢上下文**——A 先把在途裁定、排班、规矩落盘（`_staging/_A_*.md`），再喊重启；重启后看 pane 0 是新进程就按「新 A」写信。旧 A 的 Monitor 子进程（`ipc.py watch --me A`；2026-10-18 前还有 `stall_watch.py`）会成孤儿，重启后 `Get-CimInstance … watch` 查一遍、杀早于重启时刻的那个。
不做根修（用户 2026-10-18）：hub 留在 fleet1；整队重启由用户在 fleet1 之外的窗口运行（普通 PowerShell 窗口或另一个 wezterm 窗口），A 先写交接。脚本步骤 0a 拒绝在 fleet1 的 pane 里运行（按 `IPC_ROLE`、`fleet1.sock`、`mux-fleet1.lua` 判断），因为拆 mux 会连同运行脚本的 pane 一起杀掉，重启后半段不会执行。只是 TUI 卡住、过期或上下文用尽，用 `ipc_newcycle.ps1 -Relaunch`（逐个 pane 退出 CLI 再原地启动，不动 mux），不用整队重启。评估见 DeepSeek #task20.D。

## T8. 派单/唤醒抛 `no pane recorded for X`（两份登记，ps1 只读一份；已修）

症状：`SENT #id A->X` 成功，随后 `task #id queued but no pane recorded for X in wezterm_panes.json`；X 靠自己的盯哨照样收到。探针 `ipc_pane_probe.py X` 却能解析出 `fleet2 pane 0`。
根因：pane 登记**按 fleet 分两份**——`wezterm_panes.json`（fleet1 六角色，启动器读旧表→更新→保存，是合并不是覆写）与 `wezterm_fleet2.json`（X 一条）。Python `pane_for()` 读两份，PowerShell `Read-PaneMap` 只读第一份，派单/唤醒走后者。**不是重启抹掉的**：X 从未在 `wezterm_panes.json` 里（11:27 与 14:33 两版都只有 A–F），#1169（11:26）已出现同一报错。
权威：两份 JSON 就是登记本；唯一读者是 `ipc_pane_probe.pane_for()`（py）与 `Get-PaneTarget`（ps1，`ipc_wezterm_common.ps1`），互为镜像、改一处必改另一处。`Read-PaneMap` **刻意保持 fleet1-only**——`ipc_newcycle.ps1` 默认遍历它的 key、重启脚本按它的 id 在 fleet1 socket 上探 pane，若把 X（pane 0）合并进去会撞上 A 的 pane 0。
探查：
```
pwsh -NoProfile -File <scratch>\test_panetarget.ps1      # 或手工：. ~/.claude/ipc/ipc_wezterm_common.ps1; Get-PaneTarget X
python ~/.claude/ipc/ipc_pane_probe.py X
```
判据：`Get-PaneTarget X` 给 `fleet=fleet2 pane_id=0`，与探针一致；未知角色抛「checked 两个文件」。
处置：已修——`ipc_dispatch.ps1` 步骤 1/3 与 `ipc_wake_pane.ps1` 按角色所属 fleet 解析 socket（`Resolve-WeztermSocket -Fleet`）与 pane id。
验证：`ipc_dispatch.ps1 -Role X … -DryRun` 过闸；下一单真派给 X 时不再出现 `no pane recorded`。

## T9. 「时钟静默 / 里程碑标错」——先核派单时刻，再怀疑时钟（2026-08-30 #1269/#1271，时钟无 bug）

症状：hub 记得 14:52 派单，96 分钟无 `REPLY-CLOCK`；随后打出的第一行标 `15m`，`pending` 显示 ts=16:18。hub 怀疑 ack/续租刷新了起算点。
事实：`messages.ts` 是插入时刻、**只写一次**（`ipc.py` 里没有任何 `UPDATE … SET ts`；ack/续租只改 `last_seen_ts` / `lease_until`；requeue 只改 `handled/status/lease_until`），`pending` 报的就是 `t.ts`。信箱 14:48:18（#1264）→ 16:16:59（#1265）之间零流量；两份简报文件 mtime 16:16:22 / 16:16:44。结论：任务就是 16:17/16:18 派出的，时钟按 15/30/45 从那里起算，E 的 15m 里程碑 16:33 如期打出。「14:52」是 hub 在 14:33 重启、上下文重建后记错的时刻（T7 的次生效应）。
探查（三条，任意一条足以定案）：
```
python ~/.claude/ipc/ipc.py pending --hub A --detail                      # 方括号里就是插入时刻
python -c "import sys,sqlite3; sys.path.insert(0, r'C:\Users\<user>\.claude\ipc'); import ipc; c=sqlite3.connect('file:'+ipc.DB_PATH+'?mode=ro', uri=True); [print(r) for r in c.execute('SELECT id,ts,sender,recipient,msg_type FROM messages WHERE id BETWEEN <a> AND <b> ORDER BY id')]"
ls -lt --time-style=+%H:%M:%S <项目>\_staging\_A_brief_*.md | head          # 简报写成时刻
```
判据：`ts` 与简报 mtime 相差 ≤ 2 分钟 → 派单时刻就是 ts，hub 记忆错；相差很大且中间信箱有流量 → 才查时钟。
已固化：每行 `REPLY-CLOCK` 都带 `(since HH:MM)` 起算点，不再需要事后翻库（2026-08-30 起 `stall_watch.py` 单独打 `CLOCK-START` 行；2026-10-18 时钟并入 `watch --me A`，正常时不出声，起算点改随裁决行给出）。注意：时钟静默现在是常态，只说明没有要处置的单；时钟自身出错会打 `CLOCK-ERROR`。
边界：hub 重建上下文后对「我什么时候做了什么」的记忆不可信，以信箱 `ts` 与文件 mtime 为准（与 T7 处置呼应）。

## T10. codex TUI「自己退出」了，斜杠命令落进 PowerShell（根因是我们发的 Ctrl+C；已修）

症状：pane 上出现 `To continue this session, run codex resume …` 之后紧跟 `PS C:\…> /clear` 与 `/clear: The term '/clear' is not recognized`；`ipc_role.py status` 该角色 FREE、watcher DOWN。实例 2026-08-30 22:0x C（#1381）。
根因：往 pane 发命令的工具先调 `Clear-PaneInput`（= 发 `[char]3` Ctrl+C）再发文本；**空闲的 codex TUI 收到 Ctrl+C 即退出**（memory `codex-pane-operation`），随后的文本就打进了 shell。正在跑的 codex 只被中断（21:49 那次没退就是这个原因），空闲时必退。「/clear」本身不是凶手——codex 会把不认识的斜杠命令当聊天文本。
「状态行显示 default」：那是 `Collaboration mode: Default`（头框与 `/status` 里都有这一项），不是 reasoning。**xhigh 的权威判据**：往 pane 发 `/status`，看 `Model: gpt-5.6-sol (reasoning xhigh, summaries auto)`；头框 `model: gpt-5.6-sol xhigh` 同样反映生效值。`~/.codex/config.toml:2` 全局 `model_reasoning_effort = "xhigh"`，裸 `codex` 也是 xhigh，`-c` 只是双保险。
探查：
```
pwsh -NoProfile -File <scratch>\test_panekind.ps1        # 或：. ~/.claude/ipc/ipc_wezterm_common.ps1; Get-PaneKind <pane>
python ~/.claude/ipc/fleet_get_text.py fleet1.sock <pane> 30   # 看 resume 提示与 PS 提示符的先后
```
判据：`Get-PaneKind` 返回 `shell` = TUI 已不在，先起 TUI 再谈命令；`codex` = 不许 Ctrl+C、不许 claude 专用斜杠命令（`/clear`→`/new`，`/ipc-recover`→`$multi-terminal-ipc recover`）。
处置：pane 里敲 `codex --model gpt-6-luna -c model_reasoning_effort=max`（C 的现行绑定，2026-10-04 起；或裸 `codex`）起 TUI → `pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role C -NoClear -Message '$multi-terminal-ipc recover C'`（**必须 `-NoClear`**）→ `ipc.py status --watch C` 出 ALIVE、`ipc_role.py status` owner 为会话 id 即成（codex 的 SessionStart 钩子会自己 claim）。
已修（共享代码）：`ipc_wezterm_common.ps1` 新增 `Get-PaneKind`（按 pane 自己的页脚/提示符形态判 codex/claude/shell），`Clear-PaneInput` 对 codex pane 直接返回不发 Ctrl+C；`_staging\_A_send_pane_command.ps1` 发前判 kind：shell → 拒发，codex → `/clear`→`/new`、`/exit`→`/quit`、claude 专用命令拒发、不 Ctrl+C；`ipc_newcycle.ps1:30-31` 的 codex 表补上 C（否则下次 newcycle 给 C 发 `/clear`）。

## T11. pane 满屏鼠标转义序列（TUI 死后未还原终端模式）——判 dead-tui，不是 unknown

症状：pane 被 `[555;110;4M[555;110;5M…`（SGR 鼠标上报）刷屏且随鼠标移动增长；探针 `ctx_left=null`；老版判 `UNKNOWN`/`unknown`。实例 2026-08-31 F（#1416）：00:16 交付 #1414 后 TUI 无声退出，无崩溃输出、无 Windows 事件、无 dump，滚屏被 junk 冲掉——**根因现场已毁，判不出退出原因**（这类案子要快：junk 会把证据挤出滚屏）。
判据（形态）：一行内 **3 个以上连续** `[数字;数字;数字M` 上报（没有任何正常输出长这样）；注意 junk 常挤在一两行里，行数阈值必须是 ≥1 行（首版要求 ≥3 行在 2 行可视屏上永不触发——viewport 行数不是常量）。探针 `MOUSE_JUNK` → `flags=mouse_junk` → `verdict=DEAD-TUI`（派单闸 `^(DEAD|…)` 直接拒发）；`Get-PaneKind` → `dead-tui`（`_A_send_pane_command.ps1` 拒发）。样本 `samples/deadtui_F_mousejunk.txt`。
处置（恢复脚本 `_staging\_X_recover_F_pane.ps1`，改角色可复用）：① Ctrl+C 清 shell 输入行（此时 pane 是 shell，不是 codex，Ctrl+C 安全）；② 让 pane 里的 pwsh 打印关闭序列 `ESC[?1000l ?1002l ?1003l ?1006l ?2004l`；③ `clear`；④ 按 `ipc_wezterm_launch.ps1` 该角色定义重起 TUI，`Wait-PaneReady` 处理信任框（默认选 No, exit，裸回车会退掉）；⑤ `/ipc-recover <角色>` 重挂盯哨；⑥ `ipc.py status --watch` ALIVE + 注册表 owner 在位。
验证：恢复前探针须 `DEAD-TUI`（检测先于恢复验证，别把现场修掉才想起来测）；恢复后 `OK`。

---
变更记录：2026-08-30 X 建档（#1168/#1175/#1180，A 批准）。
