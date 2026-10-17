---
description: 把本终端设为主终端 A(hub),更新注册表归属,按需派活给 worker 并收回结果
---

你现在是**主终端 A(星型 hub)**,是发起方/决策方。IPC 协议的唯一权威是用户级 `multi-terminal-ipc` skill(`~/.claude/skills/multi-terminal-ipc/SKILL.md`;2026-08-21 起项目 AGENTS.md/CLAUDE.md 不再重述协议,只写该部署自己的硬规则)。本命令只管 A 的角色流程。

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
- **收回复**:起 ONE 常驻 Monitor(`timeout_ms: 1800000`;`persistent` 参数已不存在,Monitor 硬顶 30 分钟,到期通知一到就重挂,细则见 `/ipc-recover` 第 3 步)跑 `watch --me A`——每条回复以 `NEW MSG #id from ...` 小信号到达,收到后 `peek --me A --id <id>` 读全文(信号里的 id;一批多条就列全部——固定 `--tail 3` 会漏掉前面的)。**例外(2026-09-19 RESULT 行协议)**:worker 回复首行 `RESULT: <绝对路径>` 且 `send` 核盘通过的,盯哨不发 NEW MSG,而是攒到该轮派单结清后一次打出(只等预算截止落在 15 分钟窗口内的兄弟单;预算 40 分钟的长单和已超时的单不会拖住它) `DONE #N <- 角色 RESULT: 路径 (#id, no peek needed)`——照这一行记 ledger 即可,**不要 peek**;要看正文再 `peek --me A --id <id>`(DONE 行里的 #id)。收到 NEW MSG 的才是要细读的(提问/部分完成/失败/路径核不过)。2026-08-21 起这个盯哨在引导时就该挂上(fleet 启动脚本对 A 也发 `/ipc-recover A`,其第 4 步即挂哨)——若发现没挂(如本命令在裸终端手动执行),现在补挂。Monitor 不可用时退回后台 Bash `recv --me A --block`(exit 0=有消息,2=空超时重挂),或 `--block --count N` 屏障式收齐 N 条。
- **查派单完成度**:`pending --hub A [--detail]`(状态 QUEUED / QUEUED-STALLED / IN_PROGRESS / IN-PROGRESS-SILENT / STALE / NEEDS-REVIEW;失败与取消的单会离开 pending,所以"空"只说明没有在途单,结清与否用 `task --id N` 确认)。
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
- 引导时除 `watch --me A` 外再起**第二个常驻 Monitor**(同样 30 分钟到期须重挂):`python ~/.claude/ipc/stall_watch.py`
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
- **收到的最终回执若是无关联 note(`peek --json` 里 `type=note`、`task=null`)→ 先 `python ~/.claude/ipc/ipc.py done --me <角色> --task N` 补结清,再存回执**;
  否则该单一直挂在 `pending`,时钟每轮误报,租约到期后重新入队直至自动 failed(2026-09-16 NetLink 案 #1592–#1598,E 评审)。不要用 cancel 代替 done,不要重启编队,也不要反复忽略报警。

**4. 任务中途不给用户选项;一个会话跑到底(用户 2026-09-16 定)。**
- **`AskUserQuestion` 只允许在开工前一次性问完**(编队名单、正式件语言等)。派单发出之后直到交付,
  **一律不再问用户**——用户常不在电脑前,一个选项框会让整条编队空等。中途遇到分岔按既有规则自决并写进派单记录:
  角色不可用 → 同 `--submit-id` 改派空闲角色(本节第 3 条已有此规定);语言按市场;名单按可用角色。
  证据:2026-09-15 NetLink 案,D 余额耗尽,A 15:41 弹选项问"改派给谁",用户次日 00:09 才答,编队空转 8 小时 28 分
  (transcript `a7c92c44-…jsonl`,两次 AskUserQuestion 记录)。
- **上下文靠自动压缩续命,不停下来等用户 `/compact`。** harness 会在下一次调用前自动压缩,A 不能自己触发,
  也不需要:接近上限时照常继续下一步,不要以"请先 /compact"结束回合(2026-09-16 00:57 A 在 987K/1M 处停下等用户)。
  为让压缩后能无缝接续:开工即建 `temp/<案例>/_A_ledger.md`(派单号、状态、产物路径、待办),每次收回或裁定后追加一行;
  大块原文(年报、抽取结果)不进 A 的上下文,只落文件、只读摘要(本节第 1 条)。

(worker 自报 5 小时/周额度一事**暂缓**——用户 2026-08-29 决定,各厂商 CLI 都没有回合内读额度的路径;
配额状态由 A 在第 3 条的主动查看里从 pane 横幅判断,见 `fleet-worker-capacity` 派单纪律。)
