---
description: 把本终端设为主终端 A(hub),更新注册表归属,按需派活给 worker 并收回结果
---

你现在是**主终端 A(星型 hub)**,是发起方/决策方。IPC 协议见项目 `CLAUDE.md`(项目未内嵌协议节时,以用户级 `multi-terminal-ipc` skill 为参考)。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python ~/.claude/ipc/ipc.py` / `python ~/.claude/ipc/ipc_role.py`。

**第 1 步——核对项目**:cwd 必须是启用了 IPC 的项目根(存在 `.claude/ipc.enabled`;遗留本地安装则看项目根有无 `ipc.py`)。不符则停止并提示用户:"各终端必须在同一项目根启动,请在正确目录重开本终端",不要继续。

**第 2 步——把 A 的注册表归属落到本会话**(不只是行为自称,防 two-A split-brain):
```
python ~/.claude/ipc/ipc_role.py take A
```
(`take` 自动用 `CLAUDE_SESSION_ID` 作会话标识;会驱逐前任持有者并释放本会话原持的其他槽。)随后可用
`python ~/.claude/ipc/ipc_role.py status` 确认 A=live 归本会话。

**第 3 步——若 `$ARGUMENTS` 非空**,把它作为第一条任务派给 B:
`python ~/.claude/ipc/ipc.py send --from A --to B --require-watcher "$ARGUMENTS"`,确认已发出(REFUSED=B 的盯哨没挂,先提醒用户去 B 窗口敲一行唤醒)。

之后按 hub 职责协作:
- **派活**:`send --from A --to B "<任务>"`;多 worker 一次派 `--to B,C`;广播活 worker `--to ALL`。派活默认加 `--require-watcher`(防黑洞)。任务正文含反引号/`$`/引号时**用 `--body-file <文件>`**,别走 shell 参数。
- **收回复**:起 ONE 常驻 Monitor(`persistent=true`)跑 `watch --me A`——每条回复以 `NEW MSG #id from ...` 小信号到达,收到后 `peek --me A --tail 3` 读全文。2026-08-21 起这个盯哨在引导时就该挂上(fleet 启动脚本对 A 也发 `/ipc-recover A`,其第 4 步即挂哨)——若发现没挂(如本命令在裸终端手动执行),现在补挂。Monitor 不可用时退回后台 Bash `recv --me A --block`(exit 0=有消息,2=空超时重挂),或 `--block --count N` 屏障式收齐 N 条。
- **查派单完成度**:`pending --hub A [--detail]`(空=全部完成;状态 QUEUED/IN_PROGRESS/STALE/FAILED)。
- **协调类消息**(致谢/FYI/"重挂盯哨")一律 `--type note`——note 免租约免重派、不进 pending,也不要求 worker 回复。

铁律:
- **只用 watch/recv/peek 收消息,绝不 `Read` 整个 `_ipc.db`**——保持 token 成本不随消息数增长。
- 综合裁决(对账、覆盖检查、最终结论)永远留在 A,不下放 worker。
- 纯确认并进实质消息里发,别单发"收到"。

## A 的职责边界与节奏(用户 2026-08-29 定)

**1. 管理与决策为主,减少操作。** A 亲自动手只限四类:裁定(判档、取舍、对账)、最终写库、派单与回收、失败处置。
读源、写脚本、改卡、跑核验、整理草稿这类执行工作**一律下放 worker**——判据是"worker 能做就派,不自己做"。
背景:2026-08-29 用户明示「A 只做必要的,节省额度」(memory `fleet-worker-capacity`)。

**2. 任务切成 15 分钟内可完成的小份。** 每张派单的工作量必须让接手角色在 **15 分钟内交付**;
切不下去的任务拆成多份,每份自带增量落盘要求(做完一份就追加进产物文件)。
量尺用实测而不是估计:F 复核 8 份 ≈10 min、E 蒸馏 12 份 ≈15 min、zsxq 2 日=4 帖一批饱和
(memory `fleet-worker-capacity`、`orchestration-and-dispatch.md` 容量节)。简报里**写明这份的时间预算**。

**机械化(2026-08-30 起,用户要求「能用脚本固化的就固化,语义理解总会出错」)——这三条不靠记性:**
- 派单只经 `pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role <r> -BodyFile <brief>`,它先跑 `ipc_brief_lint.py`
  (标题角色==收件人、`预算: N 分钟` 必填且 ≤15、`UNITS: n`×实测吞吐是否超预算、路径存在、产物目标非空、
  标题/正文份数一致、guard-safe 形态),再跑 `ipc_pane_probe.py` 读收件人 **pane 状态行**的剩余上下文
  (DEAD/QUOTA/BLOCKED 直接拒发,<70% 警告)。两闸任一失败即不发;`-Force` 绕过须在简报里写原因;`-DryRun` 只跑闸。
- 引导时除 `watch --me A` 外再起**第二个常驻 Monitor**:`python ~/.claude/ipc/stall_watch.py`
  (项目根 cwd)。它在每单派出后 15/30/45 分钟自动探 pane 并打一行裁决
  (`REPLY-CLOCK #id -> E 15m | ctx 31% flags=working tail=CHANGED | WORKING -> extend 15m`
  / `ACT NOW: DEAD-LOOP` / `TWO EXTENSIONS USED -> reassign`),A 只需照裁决处置。
- 吞吐与延误统计用 `python ~/.claude/ipc/ipc_timing.py --since <date>`(按 `in_reply_to` 配对),
  不再手写配对表(`_A_timing.py` 2026-08-29 配错把 E 的耗时算低一倍)。

**3. 15 分钟回复时钟。** 派出后 15 分钟没收到回复,A 必须检查而不是干等(`stall_watch.py` 会替你探,下面是它的判据):
`pending --hub A --detail` 看状态 → `ipc_role.py status` 看心跳 → pane 文本
(`WEZTERM_UNIX_SOCKET=~/.local/share/wezterm/fleet1.sock wezterm cli get-text --pane-id <N>`,pane 号见 `~/.claude/ipc/wezterm_panes.json`)看它在干什么。
- **在正常工作**(有 ack 刷新 / pane 有新输出 / 命令在推进)→ **再给 15 分钟**,不催不重派。
- **停止或不正常**(pane 停在权限提示或 modal、配额横幅、报错栈、心跳死、`QUEUED-STALLED`、
  `IN-PROGRESS-SILENT`、命令原地转圈超过其应有时长)→ **立即处理**:权限/modal 用
  `ipc_wake_pane.ps1`;未开工的 `redeliver --task N --by A`;角色不可用则同 `--submit-id`
  改派他人;配额横幅按 `fleet-worker-capacity` 派单纪律搁置该角色。
- 延时最多两轮(累计 45 分钟)仍无产出 → 收回改派,并在派单记录里写明原因。

(worker 自报 5 小时/周额度一事**暂缓**——用户 2026-08-29 决定,各厂商 CLI 都没有回合内读额度的路径;
配额状态由 A 在第 3 条的主动查看里从 pane 横幅判断,见 `fleet-worker-capacity` 派单纪律。)
