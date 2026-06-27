---
description: 恢复本终端的 IPC 待命(/clear、上下文压缩或 SessionStart 钩子失效后,重建角色盯哨)
---

你在**恢复本终端的 IPC 待命状态**(用于 `/clear`、上下文压缩、或 SessionStart 钩子没跑起来之后)。

先理解机制(决定了恢复只需做什么):
- **`/clear` 不释放角色**——`.claude/hooks/ipc_role.py` 在 clear 时早返回,注册表按 `session_id` 保留"本会话→角色"映射。`/clear` 只杀掉后台**盯哨进程**。
- 所以恢复的核心是**重新挂盯哨**,不是重新抢角色。
- `recv`/`watch` 只吃 `--me <角色>` 命令行参数,**不依赖注册表**——只要你知道自己的角色名就能收发。

步骤:

1. **确定角色**(按优先级):
   - `$ARGUMENTS` 给了角色(A/B/C/D)→ 用它(如 `/ipc-recover B`);
   - 否则读上下文里的 `[IPC role: ...]` 注入块(实际措辞是 `you are master terminal A` / `you are worker terminal B` 等),从中取你的角色字母;
   - 都没有 → 问用户"这是哪个终端(A/B/C/D)"。

2. **核对项目**:cwd 必须是本项目根(目录下有 `ipc.py`)。不符则停下并提示用户去正确目录重开,不要继续。

3. **清积压**:跑 `python ipc.py recv --me <角色>`(取回 /clear 间隙 A 可能已派的活)。若有任务,逐条执行后 `python ipc.py send --from <角色> --to A "<结果摘要>"` 发回。输出 `NONE` 则无积压。

4. **挂盯哨**(仅 worker 角色 B/C/D):用 **Monitor 工具**(`persistent: true`, `timeout_ms: 3600000`)跑
   `python ipc.py watch --me <角色>`
   作常驻盯哨,然后**结束本轮**。此后每条消息以极短信号 `NEW MSG #id from ... ` 到达(不带正文,避免通知截断);**收到信号用 `python ipc.py peek --me <角色> --tail 3` 读全文**,执行→`send` 回 A→同一 Monitor 继续听(不用重挂)。
   **铁律:一个信箱同一时刻只挂一个盯哨**——`watch` 运行期间别再对本信箱跑 `recv`/`recv --block`(两者抢收会**重复投递**)。

5. **A(hub)角色**:A 不需常驻盯哨(按需用后台 `recv --me A --block` 或 Monitor `watch --me A` 收回复)。确认自己是 A、按 CLAUDE.md hub 职责继续即可,跳过第 4 步。(A 也可直接用 `/main` 自声明 hub 身份。)

6. **罕见:钩子从未运行 / 注册表丢了你的槽**(不是 /clear,是 hook 脚本异常)——你仍能用 `--me <角色>` 正常收发(recv/watch 不依赖注册表),只是 A 的 `--to ALL` 广播会漏掉未注册的你。修法:**直接关掉本窗、重开终端**,让 SessionStart 钩子重新分配槽。**不要在本恢复流程里跑 `ipc_role.py reset`**——`reset()` 会**清空整个注册表**(把活着的 A/C 也一起抹掉,导致角色错乱);它是需人工确认"所有终端都已死"时才用的运维命令,不属常规恢复。
