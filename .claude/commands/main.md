---
description: 把本终端设为主终端 A(hub),更新注册表归属,按需派活给 worker 并收回结果
---

你现在是**主终端 A(星型 hub)**,是发起方/决策方。IPC 协议的唯一权威是用户级 `multi-terminal-ipc` skill(`~/.claude/skills/multi-terminal-ipc/SKILL.md`;2026-08-21 起项目 AGENTS.md/CLAUDE.md 不再重述协议,只写该部署自己的硬规则)。本命令只管 A 的角色流程。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python ~/.claude/ipc/ipc.py` / `python ~/.claude/ipc/ipc_role.py`。

**第 1 步——核对项目**:cwd 必须是启用了 IPC 的项目根(任一即可:本会话环境带 `IPC_ROLE` 且 `~/.claude/ipc/wezterm_panes.json` 里 A 的 `project` 等于 cwd,fleet pane 由启动脚本设置,home 目录只能走这条,依据 `ipc_role.py:92`;存在 `.claude/ipc.enabled`;遗留本地安装则看项目根有无 `ipc.py`)。都不满足才停止并提示用户:"各终端必须在同一项目根启动,请在正确目录重开本终端",不要继续。换项目不用 newcycle(它只发 /clear 与 /ipc-recover,不改各 pane 的 cwd),用 `ipc_fleet1_restart.ps1 -Project <新项目根>` 整队重启。

**第 2 步——把 A 的注册表归属落到本会话**(不只是行为自称,防 two-A split-brain):
```
python ~/.claude/ipc/ipc_role.py take A
```
(`take` 读 `--session` 或 `CLAUDE_SESSION_ID` 作会话标识;Claude 的工具 shell 里这个变量通常是空的,owner 会记成占位值 `manual`(`ipc_role.py` 的 `MANUAL_SID`),知道本会话 id 时显式传 `--session <id>`。会驱逐前任持有者并释放本会话原持的其他槽。owner 换成另一个 id 或被清空时,已挂的 `watch --me A` 会自行退出(例外:盯哨刚启动的宽限期内从空或 `manual` 换成真实 id 会被接纳,`ipc.py` 的 `_REG_ANCHOR_GRACE`):take 之后跑 `ipc.py status --watch A`,DOWN 就重挂 Monitor 跑 `python ~/.claude/ipc/ipc.py watch --me A`(参数同 `/ipc-recover` 第 3 步,A 也照用;2026-10-18 实测)。)随后可用
`python ~/.claude/ipc/ipc_role.py status` 确认 A=live 归本会话。

**第 3 步——若 `$ARGUMENTS` 非空**,把它作为第一条任务派给 B:
`python ~/.claude/ipc/ipc.py send --from A --to B --require-watcher "$ARGUMENTS"`,确认已发出(REFUSED=B 的盯哨没挂,先提醒用户去 B 窗口敲一行唤醒)。

之后按 hub 职责协作:
- **派活**:`send --from A --to B "<任务>"`;多 worker 一次派 `--to B,C`;广播活 worker `--to ALL`。派活默认加 `--require-watcher`(防黑洞)。任务正文含反引号/`$`/引号时**用 `--body-file <文件>`**,别走 shell 参数。
- **收回复**:起 ONE 常驻 Monitor(`timeout_ms: 1800000`;`persistent` 参数已不存在,Monitor 硬顶 30 分钟,到期通知一到就重挂,细则见 `/ipc-recover` 第 3 步)跑 `watch --me A`——每条回复以 `NEW MSG #id from ...` 小信号到达,收到后 `peek --me A --id <id>` 读全文(信号里的 id;一批多条就列全部——固定 `--tail 3` 会漏掉前面的)。**例外(2026-09-19 RESULT 行协议)**:worker 回复首行 `RESULT: <绝对路径>` 且 `send` 核盘通过的,盯哨不发 NEW MSG,而是攒到该轮派单结清后一次打出(只等预算截止落在 15 分钟窗口内的兄弟单;预算 40 分钟的长单和已超时的单不会拖住它) `DONE #N <- 角色 RESULT: 路径 (#id, no peek needed)`——照这一行记 ledger 即可,**不要 peek**;要看正文再 `peek --me A --id <id>`(DONE 行里的 #id)。DONE 行带 `+NEW n (FAIL 1, FACT 1): peek --me A --id M  anchor #taskN.B`(n>0;括号是条目类型计数,2026-10-22 起,FAIL=试过且证伪的路径、FACT=已证实、OBSERVED=看到但未证实)时**要 peek 这个 id**,对 NEW 条目逐条裁定(2026-10-17);之后哪张简报或结论用到它,就引用它的 anchor `#taskN.B`(采纳率靠 grep anchor 统计)。收到 NEW MSG 的才是要细读的(提问/部分完成/失败/路径核不过),信号里同样带 `NEW n anchor #taskN.B`。2026-08-21 起这个盯哨在引导时就该挂上(fleet 启动脚本对 A 也发 `/ipc-recover A`,其第 4 步即挂哨)——若发现没挂(如本命令在裸终端手动执行),现在补挂。Monitor 不可用时退回后台 Bash `recv --me A --block`(exit 0=有消息,2=空超时重挂),或 `--block --count N` 屏障式收齐 N 条。
- **查派单完成度**:`pending --hub A [--detail]`(状态 QUEUED / QUEUED-STALLED / IN_PROGRESS / IN-PROGRESS-SILENT / STALE / NEEDS-REVIEW;失败与取消的单会离开 pending,所以"空"只说明没有在途单,结清与否用 `task --id N` 确认)。`task --id` 和 `peek --id` 查到已移入归档表的行照常返回,标 `[archived]`,不是丢失。
- **评分**(2026-10-17,项目根执行):默认 pass,不用操作;失败、重派、REPLAN/UNSOLVABLE 由脚本自动记。A 用了回复但不得不返工 → `python ~/.claude/ipc/ipc.py grade --task N --verdict rework --reason <tool|context|quota|misread|quality>`;某条 NEW 改变了 A 的决策状态(改了简报/结论/派单/规则、新开任务,或关掉 A 列为未决的事项)→ `grade --task N --used-new <brief|conclusion|routing|rule|task|scope-closed>`;只说"X 没问题"的不记。**复核类回复(2026-10-18 用户定)**:每条发现都核,不抽查;打开它引用的文件:行号,结论依赖它脚本输出的就重跑脚本;A 自己不再构造反例。核完记 `grade --task N --findings <成立>,<不成立>,<未核>`,`python ~/.claude/ipc/ipc_estimate.py accuracy` 按模型比准确率。
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
简报必含三行:`预算: N 分钟`、`完成判据:`、`类型:`(取数/脚本/写作/检索/评审/代码 选一,2026-10-17 起必填)。
写简报前先 `python ~/.claude/ipc/ipc.py board --type FAIL [--grep 关键词]`(2026-10-22,借自 Agensh 的共享上下文看板):列出本项目各回复里标 FAIL 的已证伪路径,相关的写进案情简报「已证伪路径」节(orchestration-and-dispatch.md 规则 6),简报引用该节,免得另一角色再烧预算去试。
**选角色**(2026-10-17):先 `python ~/.claude/ipc/ipc_quota.py --roles <候选>`(HOLD 的角色本轮不派),再
`python ~/.claude/ipc/ipc_estimate.py rank --type <类型> [--open]`。开放型(检索、评审、找根因、简报可能有错)加 `--open`;
封闭型(取数、机械脚本)不加,并在简报里写"只交付,不建议"。排名只是输入,由 A 选;`small-sample` 的角色只给短单(≤5 分钟),
`no basis` 的不给长单。各列含义见 multi-terminal-ipc 的 `references/command-details.md`。

**机械化(2026-08-30 起,用户要求「能用脚本固化的就固化,语义理解总会出错」)——这三条不靠记性:**
- 派单只经 `pwsh ~/.claude/ipc/ipc_dispatch.ps1 -Role <r> -BodyFile <brief>`,它先跑 `ipc_brief_lint.py`
  (标题角色==收件人、`预算: N 分钟` 必填且 ≤15、`完成判据:` 与 `类型:`(白名单)必填、`UNITS: n`×实测吞吐是否超预算、路径存在、产物目标非空、
  标题/正文份数一致、guard-safe 形态),再跑 `ipc_pane_probe.py` 读收件人的剩余上下文
  (2026-10-20 起先读 `ipc_state.py` 的状态文件 / rollout,读不到才退回 pane 状态行正则;横幅、提示、通知仍看 pane)
  (DEAD/QUOTA/BLOCKED 直接拒发,<70% 警告)。两闸任一失败即不发;`-Force` 绕过须在简报里写原因;`-DryRun` 只跑闸。
- 15 分钟回复时钟**内建于 `watch --me A`**(2026-10-18 起,DeepSeek 评审 #task3.D;`stall_watch.py` 已删除,不再起第二个 Monitor)。
  2026-10-20 起时钟由独立进程 `ipc_supervisor.py` 在会话外跑(编队启动脚本自动 start;`status`/`stop` 查停),
  它自己消 BLOCKED-NOTICE,其余要处置的行写进 `_supervisor_A.events`,A 的 watch 原样转发——A 只在真有事时花一个回合。
  它约每分钟查一次在途单,派出后 15/30/45 分钟各探一次收件人 pane;**正常工作时不出声**(静默延长),只在要 A 处置时打一行
  (`REPLY-CLOCK #id -> E 30m (since 11:45) | ctx 57% flags=- tail=SAME | ACT NOW: pane idle & unchanged ...`
  / `ACT NOW: DEAD-LOOP` / `PROBE-FAILED: ...` / `TWO EXTENSIONS USED -> reassign` / `STALLED TASK: #id ...` / `CLOCK-ERROR: ...`),A 只需照裁决处置。
  每单的里程碑与 pane 尾部基线存 `_clock_A.json`(信箱同目录),Monitor 30 分钟重挂不会重报,也不会重新计时。
  45 分钟后仍未结清的单每 30 分钟复查一次,只在有要处置的裁决时打 `STILL-OPEN #id ... | ACT NOW ...`,同一裁决只打一次;15/30/45 分钟里程碑判定时 pane 读不到,按 1、2、4、5 分钟退避重探,45 分钟后的复查读不到不退避、仍 30 分钟一次(#task22.D;#task24.E N1)。
  想看各在途单的时钟状态:`python ~/.claude/ipc/ipc.py clock --once --dry-run`(watch 在跑时也能用,不写状态、不回收租约;会真探到期的 pane,手动偶尔用)。
- 吞吐与延误统计用 `python ~/.claude/ipc/ipc_timing.py --since <date>`(按 `in_reply_to` 配对),
  不再手写配对表(`_A_timing.py` 2026-08-29 配错把 E 的耗时算低一倍)。

**3. 15 分钟回复时钟。** 派出后 15 分钟没收到回复,A 必须检查而不是干等(`watch --me A` 内建的时钟会替你探,下面是它的判据;它只在要处置时出声):
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

**5. 配额交接:限额将尽的角色先把工作落文件,别的角色接力(用户 2026-10-21 定)。**
worker 不自报额度(2026-08-29 决定不变);由时钟从文件读各角色窗口(`ipc_state.py`),每分钟一次。
- `QUOTA-HANDOFF #N -> E 5h=91%@19:47 | handoff request typed into the pane ...`:在途单的接收方 5h 窗口用到 90%、或 7d 窗口用到 98%(用户 2026-10-21 定:周额度剩 10% 还够做很多个 5h 窗口;重置还有 10 分钟以上),
  时钟已把交接要求打进它的 pane(回合中消息,不打断)。它写完 `<信箱目录>/handoff/task-N-E.md` 后以关联回复送达(`NEW MSG … [HANDOFF]`,单号自动结清,判 `handoff`,不计入 rank)。
  A 读交接文件,用 `ipc_quota.py` 挑有余量的角色,`ipc_dispatch.ps1` 改派剩余部分:原简报 + 交接文件路径,同 `--submit-id` 家族。
  10 分钟内没等到 `[HANDOFF]` 且 pane 已是配额横幅 → 按第 3 条改派,以产物文件现状为准。
- `... | handoff request NOT sent (pane BLOCKED-PROMPT)`:先按第 3 条解开 pane,时钟每轮自动重试。
- `... | queued, not started`:`cancel --task N --by A`,改派有余量的角色。
- `QUOTA-HANDOFF hub A 5h=86%@...`:A 自己的窗口到 85%(与用户会话共用):立即把 `_A_ledger.md` 补到最新(在途单、已定决策、下一步),不再派新单;worker 照常跑,回复留在信箱。
- 窗口来源:A/X 读 statusline 状态,C/E 读 Codex rollout,B(Kimi Code)、F(智谱)读厂商用量接口(2026-10-21 起,缓存 60 秒)。D(DeepSeek)按量计费、没有窗口,只能靠增量落盘规则,撞线后按第 3 条改派。
