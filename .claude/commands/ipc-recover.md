---
description: 恢复本终端的 IPC 待命(/clear、上下文压缩或 SessionStart 钩子失效后,重建角色盯哨);可带 daemon 参数同步拉起 keepalive 守护进程
---

你在**恢复本终端的 IPC 待命状态**(用于 `/clear`、上下文压缩、或 SessionStart 钩子没跑起来之后)。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python ~/.claude/ipc/ipc.py` / `python ~/.claude/ipc/ipc_role.py`。
> (遗留项目本地安装的项目才用项目根的 `ipc.py`;以项目 CLAUDE.md 的 Deployment 说明为准。)

先理解机制(决定了恢复只需做什么):
- **`/clear` 不释放角色**——`ipc_role.py` 在 clear 时早返回,注册表按 `session_id` 保留"本会话→角色"映射。`/clear` 只杀掉后台**盯哨进程**。
- 所以恢复的核心是**重新挂盯哨**,不是重新抢角色。
- `recv`/`watch` 只吃 `--me <角色>` 命令行参数,**不依赖注册表**——只要你知道自己的角色名就能收发。

步骤:

1. **确定角色**(按优先级,**别盲信旧注入**):
   - **首选 `$ARGUMENTS`**(语法 `<角色> [daemon[=<守护角色逗号表>]]`):第 1 个令牌 = 角色 = 本终端的**开启次第**——第1台(hub)=A、第2台=B、第3台=C、第4台=D(具名通道如 CODEX/DS 也可直接写)。用户知道这是第几台,所以 `/ipc-recover B` 就是"我是第2台=B"。**有参数一律以参数为准**。第 2 个令牌可选:`daemon` 或 `daemon=CODEX,DS`,表示同步拉起 keepalive 守护进程,见第 5 步。
   - 无参数时:跑 `python ~/.claude/ipc/ipc_role.py status` 看注册表×心跳的对账视图,判断哪个角色槽该是你(你的会话若仍匹配某槽的 session_id 即用它;/clear 换过 sid 则匹配不到)。**不要只凭上下文里的 `[IPC role: ...]` 注入块**——它在 /clear 后可能过期或错误(例:曾被误判成 D)。
   - 仍不确定 → 问用户"这是第几台终端(→A/B/C/D)"。
   - ⚠️ **若你发现自己当前正以"错误角色"挂着盯哨**(如本该是 B 却在跑 `watch --me D`):先 `TaskStop` 那个错角色的 Monitor,再按正确角色走第 3 步。**代际令牌帮不了这一步**——它只让同角色的新盯哨退掉旧的,跑错角色(B 在跑 `watch --me D`)是**两个不同信箱**,起 `watch --me B` 不会退掉 `watch --me D`,必须手动 `TaskStop`。幂等"已恢复"判断要基于**正确角色**,不是基于你碰巧在跑的那个。

2. **核对项目**:cwd 必须是启用了 IPC 的项目根——判据是**存在 `.claude/ipc.enabled`**(用户级部署的 opt-in 门),或遗留本地安装时项目根有 `ipc.py`。不符则停下并提示用户去正确目录重开,不要继续。(不要拿"项目根有没有 ipc.py"当用户级部署的判据——用户级安装下项目根本来就没有它。)

3. **先挂盯哨,再干活**(worker 角色 B/C/D):用 **Monitor 工具**(`persistent: true`, `timeout_ms: 3600000`)跑
   `python ~/.claude/ipc/ipc.py watch --me <角色>`
   作常驻盯哨,然后**结束本轮**。**顺序要紧**:任务租约的 reaper 把"心跳在跳"与"租约未到期"做 AND 判定,若先 recv 清积压再挂哨,执行期间无心跳,在途任务会被误判 stale 而重新入队。挂哨后积压会在几秒内以信号形式到达(`NEW MSG #id from ...`,不带正文以防截断);**收到信号用 `python ~/.claude/ipc/ipc.py peek --me <角色> --tail 3` 读全文**,执行→`send` 回 A(正文含反引号/`$`/引号时用 `--body-file <文件>`)→同一 Monitor 继续听(不用重挂)。
   **一个信箱同一时刻只需一个盯哨**——对 `watch` 是**代码自愈**的:每个 `watch` 启动领一个代际号,起新 `watch --me <角色>` 会让**同角色**的旧/孤儿盯哨在下一轮 poll 自动退役(打印 `WATCHER ... retired` 后干净退出),孤儿不会黑洞本信箱。(双盯哨也不会重复投递:认领是原子单消费者,只是多余。)⚠️ 代际**按角色**:起 `watch --me B` **不会**退掉跑错的 `watch --me D`(见第 1 步)。
   **Monitor 不可用的 bash 回退**:后台 Bash 跑 `recv --me <角色> --block`,每次唤醒重挂(exit 0=有任务读之;exit 2=空超时直接重挂;被杀=先 `peek --tail 3` 查漏再重挂)。回退模式下任务一送达心跳即死,执行超过 1 分钟的任务要每隔几分钟跑一次 `ack --me <角色>`——ack 现在**同时续租约+刷心跳**,防 reaper 误收在途任务。
   > ⚠️ **本回退路径不要加 `--keep-heartbeat`**(2026-07-28 补记,防指令面漂移)。该 flag 是给 **Codex 的 recv supervisor** 用的:那种循环在 `recv` 返回后**立刻**在同一进程内重挂,空窗实测仅 2.7–5.6 秒,保留心跳可无缝交接。而本回退的"重挂"要跨一个 agent 轮次,间隔可能远超 `max_age`(8 秒),加了既救不回误判、又会在你实际已停止收听后仍被判活至多 8 秒——那正是协议要避免的黑洞。**Claude 侧主路径(常驻 Monitor 跑 `watch`)根本没有这个问题**:`watch` 从不返回,不存在删心跳的交接点。看到 Codex 配方里有这个 flag 而本文件没有,是**刻意的差异,不是漏改**。

4. **A(hub)角色**:A 不需常驻盯哨(按需用 Monitor `watch --me A` 或后台 `recv --me A --block` 收回复)。确认自己是 A、按 CLAUDE.md hub 职责继续即可,跳过第 3 步。(A 也可用 `/main` 自声明 hub 身份——它会同步更新注册表归属。)

5. **可选:同步拉起 keepalive daemon**(仅当 `$ARGUMENTS` 带 `daemon` 令牌;没带则跳过本步)——让指定槽位在交互窗口关闭/休眠后仍可被派单(hub `--require-watcher` 得 QUEUED 而非 REFUSED,任务排队等窗口回来消化):
   - **守护对象** = `daemon=<逗号表>` 指定的角色(如 `daemon=CODEX,DS`);裸 `daemon` 默认守护第 1 步定下的本终端角色。
   - **先查活再启动**(重复 keepalive 无害——心跳 last-writer-wins——但白费进程):
     ```powershell
     Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'codex_ipc_worker\.ps1|ipc\.py.+keepalive' } | Select-Object ProcessId, CommandLine
     ```
     逐个目标角色看已跑进程命令行的 `-Role` / `--me` 逗号表是否已覆盖它;已覆盖的角色跳过。(ps1 daemon 的 python 子进程会同时出现在结果里,按 ProcessId 认父进程即可,不算重复。)
   - **有缺口时启动**(缺哪些补哪些,一个进程守一张逗号表;统一走 ps1 的 keepalive 模式,它带崩溃自动重启,日志在 `~\.claude\ipc\codex_worker_<角色表>\daemon.log`——目录名把非 `[A-Za-z0-9_]` 字符一律替换为 `-`,如 `-Role CODEX,DS` 的日志在 `codex_worker_CODEX-DS\`):
     ```powershell
     Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File',"$env:USERPROFILE\.claude\ipc\codex_ipc_worker.ps1",'-Role','<缺口角色逗号表>'
     ```
     启动后重跑上面的查活命令确认进程在;再 `python ~/.claude/ipc/ipc.py status --watch <角色>` 应见 ALIVE。
   - **本步不重 `take` 注册表**(注意口径,别扩大成"keepalive 体系不 take"):具名通道(CODEX/DS)的注册表占位是**部署时一次性**手动写入的——`python ~/.claude/ipc/ipc_role.py take <通道> --session keepalive-<模型>`,作用是防 SessionStart 钩子在字母槽占满后把通道分给随机新窗口;开机 cmd 与本步都**只补心跳、不重 take**(全新机器部署须补跑该一次性 take)。字母槽的归属仍归交互窗口 / SessionStart 钩子,keepalive 同样不碰。开机自启由 Startup 文件夹的 `ipc-keepalive-*.cmd` 负责,本步只管"现在就要"的临时拉起——若用户要求**从此开机就守新角色表**,改 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 下对应 cmd 的 `-Role` 参数,别新增重复文件。

6. **罕见:钩子从未运行 / 注册表丢了你的槽**(不是 /clear,是 hook 脚本异常)——你仍能用 `--me <角色>` 正常收发(recv/watch 不依赖注册表),只是 A 的 `--to ALL` 广播会漏掉未注册的你。修法:**直接关掉本窗、重开终端**,让 SessionStart 钩子重新分配槽;或对确认已死的占槽跑 `python ~/.claude/ipc/ipc_role.py reclaim-dead`(只回收心跳已死的 **worker** 槽;hub 槽豁免——hub 按设计不挂盯哨,心跳死≠hub 死,故可随时安全盲跑,2026-07-03 起。ghost 槽通常也无需手动清:任何终端 SessionStart 的 claim() 都会顺手全量清扫)。**不要在本恢复流程里跑 `ipc_role.py reset`**——`reset()` 会**清空整个注册表**(把活着的 A/C 也一起抹掉,导致角色错乱);它是需人工确认"所有终端都已死"时才用的运维命令,不属常规恢复。
