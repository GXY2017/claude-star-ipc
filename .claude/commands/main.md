---
description: 把本终端设为主终端 A，发起任务给从终端 B 并收回结果
---

你现在是**主终端 A**（IPC 协议见项目 `CLAUDE.md`），是发起方/决策方。

**先核对项目**：本终端的 cwd 必须是本项目根目录
`<项目根目录>`（与从终端 B 同一项目）。若 cwd 不符或当前目录
没有 `ipc.py`，立即停止并提示用户："A/B 终端必须在同一项目下启动，请在
`<项目根目录>` 重开本终端"，不要继续。

如果 `$ARGUMENTS` 非空，把它作为派给 B 的第一条任务：立即运行
`python ipc.py send --from A --to B "$ARGUMENTS"`，确认已发出。

之后按需协作：
- **派活**：`python ipc.py send --from A --to B "<任务>"`。
- **收 B 的回复**：`python ipc.py recv --me A`（只取未读新消息，`NONE` 表示 B 还没回）。
- **回顾上下文**：`python ipc.py peek --me A --tail 5`。

铁律：
- **只用 `recv` 收消息，绝不 `Read` 整个 `_ipc.db`**——保持 token 成本不随消息数增长。
- 只有 A 决定要不要继续对话；收到 B 的结果后由你（和用户）判断下一步，别和 B 互相寒暄。
- 纯确认并进实质消息里发，别单发"收到"。

你不必挂 `/loop`：正常对话，需要 B 的回复时再手动 `recv --me A` 即可。
