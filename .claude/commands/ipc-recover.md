---
description: 恢复本终端的 IPC 待命(/clear、上下文压缩或 SessionStart 钩子失效后,重建角色盯哨)
---

你在**恢复本终端的 IPC 待命状态**(用于 `/clear`、上下文压缩、或 SessionStart 钩子没跑起来之后)。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python ~/.claude/ipc/ipc.py` / `python ~/.claude/ipc/ipc_role.py`。
> (遗留项目本地安装的项目才用项目根的 `ipc.py`;以项目 CLAUDE.md 的 Deployment 说明为准。)

先理解机制(决定了恢复只需做什么):
- **`/clear` 不释放角色**——`ipc_role.py` 在 clear 时早返回,注册表按 `session_id` 保留"本会话→角色"映射。`/clear` 只杀掉后台**盯哨进程**。
- 所以恢复的核心是**重新挂盯哨**,不是重新抢角色。
- `recv`/`watch` 只吃 `--me <角色>` 命令行参数,**不依赖注册表**——只要你知道自己的角色名就能收发。
- ⚠️ **但 A 的派单依赖注册表**(2026-08-17 起,#1186 修复):`send --require-watcher` 对"心跳新鲜但角色无 owner"直接 `REFUSED-SQUATTER` 拒发(孤儿盯哨与合法未注册盯哨无法区分,注册表 owner 是唯一判据)。所以恢复流程**必须保证注册表 claim 存在**,不再是"可有可无"。

步骤:

1. **确定角色**(按优先级,**别盲信旧注入**):
   - **首选 `$ARGUMENTS`**(语法 `<角色>`):角色 = 本终端的**开启次第**——第1台(hub)=A、第2台=B、第3台=C、第4台=D(具名通道如 CODEX/DS 也可直接写)。用户知道这是第几台,所以 `/ipc-recover B` 就是"我是第2台=B"。**有参数一律以参数为准**。
   - 无参数时:跑 `python ~/.claude/ipc/ipc_role.py status` 看注册表×心跳的对账视图,判断哪个角色槽该是你(你的会话若仍匹配某槽的 session_id 即用它;/clear 换过 sid 则匹配不到)。**不要只凭上下文里的 `[IPC role: ...]` 注入块**——它在 /clear 后可能过期或错误(例:曾被误判成 D)。
   - ⚠️ **别拿 session id 判断"我的槽被抢了"(2026-08-03 实测 wrong-turn 变种)**:/clear 会换新 `session_id`,但环境变量(`CLAUDE_SESSION_ID`)和旧注入块里的 sid 是**过期旧值**。拿它对照注册表,会把 SessionStart 钩子**刚替你的新会话**重新认领的槽(WezTerm pane 预置 `IPC_ROLE` 时钩子按它确定性认领)误判成"被另一个 live 会话抢走",进而转投别的空闲槽——实例:newcycle 批量 /clear 后,B(Kimi)据 stale sid 误判 B 被抢,claim 了空闲的 E 还挂了 E 盯哨,占掉 codex 的注册表占位,直到 `/ipc-recover B` 执行时才自我纠正。**判据永远是"角色该不该是你"(参数/开启次第),不是 sid 匹配**:即便 `status` 显示你的角色由"别的" live 会话持有,只要参数/次第说你是 B,就直接按第 3 步给 B 挂盯哨(同角色旧盯哨会被代际令牌自动退役),**绝不改投其他空闲槽**。挂完盯哨**核对 claim**:`ipc_role.py status` 里你的角色 owner 为 `-` 时必须补 `python ~/.claude/ipc/ipc_role.py take <角色>`——2026-08-17 起无 owner 的盯哨会被 A 的 `--require-watcher` 以 `REFUSED-SQUATTER` 拒发(此前"不必重 take"的说法已因该修复作废)。
   - 仍不确定 → 问用户"这是第几台终端(→A/B/C/D)"。
   - ⚠️ **若你发现自己当前正以"错误角色"挂着盯哨**(如本该是 B 却在跑 `watch --me D`):先 `TaskStop` 那个错角色的 Monitor,再按正确角色走第 3 步。**代际令牌帮不了这一步**——它只让同角色的新盯哨退掉旧的,跑错角色(B 在跑 `watch --me D`)是**两个不同信箱**,起 `watch --me B` 不会退掉 `watch --me D`,必须手动 `TaskStop`。幂等"已恢复"判断要基于**正确角色**,不是基于你碰巧在跑的那个。

2. **核对项目**:cwd 必须是启用了 IPC 的项目根——判据是**存在 `.claude/ipc.enabled`**(用户级部署的 opt-in 门),或遗留本地安装时项目根有 `ipc.py`。不符则停下并提示用户去正确目录重开,不要继续。(不要拿"项目根有没有 ipc.py"当用户级部署的判据——用户级安装下项目根本来就没有它。)

3. **先挂盯哨,再干活**(worker 角色 B/C/D):用 **Monitor 工具**(`persistent: true`, `timeout_ms: 3600000`)跑
   `python ~/.claude/ipc/ipc.py watch --me <角色>`
   作常驻盯哨,然后**结束本轮**。
   ⛔ **`watch` 的宿主只准 Monitor,禁止 Bash `run_in_background`**(2026-08-17 #1188 实测:后台 bash 的输出全部写进文件,harness 只在进程退出时通知会话——盯哨认领了任务、信号在文件里躺了 1h55m,会话毫不知情,任务在 `pending` 里假装 IN_PROGRESS)。后台 bash 只在第 3 步末的 `recv --block` 回退里合法(它送达即退出,退出即触发通知,机制上成立;`watch` 永不退出,机制上必然黑洞)。
   挂好盯哨后**核对注册表 claim**(见第 1 步末尾):owner 为 `-` 则 `ipc_role.py take <角色>`,否则 A 派单会被 `REFUSED-SQUATTER` 拒掉。**顺序要紧**:任务租约的 reaper 把"心跳在跳"与"租约未到期"做 AND 判定,若先 recv 清积压再挂哨,执行期间无心跳,在途任务会被误判 stale 而重新入队。挂哨后积压会在几秒内以信号形式到达(`NEW MSG #id from ...`,不带正文以防截断);**收到信号用 `python ~/.claude/ipc/ipc.py peek --me <角色> --tail 3` 读全文**,读完**先 `ack --me <角色> --task N` 再开工**(2026-08-17 起 ack 刷新 last_seen_ts;不 ack 的在途任务超 30 分钟会在 A 的 pending 里标 `IN-PROGRESS-SILENT` 疑似未开工),然后执行→`send` 回 A(正文含反引号/`$`/引号时用 `--body-file <文件>`)→同一 Monitor 继续听(不用重挂)。
   **一个信箱同一时刻只需一个盯哨**——对 `watch` 是**代码自愈**的:每个 `watch` 启动领一个代际号,起新 `watch --me <角色>` 会让**同角色**的旧/孤儿盯哨在下一轮 poll 自动退役(打印 `WATCHER ... retired` 后干净退出),孤儿不会黑洞本信箱。(双盯哨也不会重复投递:认领是原子单消费者,只是多余。)⚠️ 代际**按角色**:起 `watch --me B` **不会**退掉跑错的 `watch --me D`(见第 1 步)。
   **Monitor 不可用的 bash 回退**:后台 Bash 跑 `recv --me <角色> --block`,每次唤醒重挂(exit 0=有任务读之;exit 2=空超时直接重挂;被杀=先 `peek --tail 3` 查漏再重挂)。回退模式下任务一送达心跳即死,执行超过 1 分钟的任务要每隔几分钟跑一次 `ack --me <角色>`——ack 现在**同时续租约+刷心跳**,防 reaper 误收在途任务。
   > ⚠️ **本回退路径不要加 `--keep-heartbeat`**(2026-07-28 补记,防指令面漂移)。该 flag 是给 **Codex 的 recv supervisor** 用的:那种循环在 `recv` 返回后**立刻**在同一进程内重挂,空窗实测仅 2.7–5.6 秒,保留心跳可无缝交接。而本回退的"重挂"要跨一个 agent 轮次,间隔可能远超 `max_age`(8 秒),加了既救不回误判、又会在你实际已停止收听后仍被判活至多 8 秒——那正是协议要避免的黑洞。**Claude 侧主路径(常驻 Monitor 跑 `watch`)根本没有这个问题**:`watch` 从不返回,不存在删心跳的交接点。看到 Codex 配方里有这个 flag 而本文件没有,是**刻意的差异,不是漏改**。

4. **A(hub)角色**:A 不需常驻盯哨(按需用 Monitor `watch --me A` 或后台 `recv --me A --block` 收回复)。确认自己是 A、按 CLAUDE.md hub 职责继续即可,跳过第 3 步。(A 也可用 `/main` 自声明 hub 身份——它会同步更新注册表归属。)

5. **罕见:钩子从未运行 / 注册表丢了你的槽**(不是 /clear,是 hook 脚本异常)——你仍能用 `--me <角色>` 正常收发(recv/watch 不依赖注册表),但后果不止"`--to ALL` 广播漏掉你":**A 的 `--require-watcher` 派单会对无 owner 的你直接 `REFUSED-SQUATTER`**(2026-08-17 起)。首选轻修法:`python ~/.claude/ipc/ipc_role.py take <角色>` 一条命令补上 claim(placeholder "manual" 也算有效 owner)。重修法:**直接关掉本窗、重开终端**,让 SessionStart 钩子重新分配槽;或对确认已死的占槽跑 `python ~/.claude/ipc/ipc_role.py reclaim-dead`(只回收心跳已死的 **worker** 槽;hub 槽豁免——hub 按设计不挂盯哨,心跳死≠hub 死,故可随时安全盲跑,2026-07-03 起。ghost 槽通常也无需手动清:任何终端 SessionStart 的 claim() 都会顺手全量清扫)。**不要在本恢复流程里跑 `ipc_role.py reset`**——`reset()` 会**清空整个注册表**(把活着的 A/C 也一起抹掉,导致角色错乱);它是需人工确认"所有终端都已死"时才用的运维命令,不属常规恢复。
