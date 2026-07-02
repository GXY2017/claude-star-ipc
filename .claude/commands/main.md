---
description: 把本终端设为主终端 A(hub),更新注册表归属,按需派活给 worker 并收回结果
---

你现在是**主终端 A(星型 hub)**,是发起方/决策方。IPC 协议见项目 `CLAUDE.md`。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python "~/.claude/ipc/ipc.py"` / `python "~/.claude/ipc/ipc_role.py"`(shell 不展开 `~` 时写绝对路径)。

**第 1 步——核对项目**:cwd 必须是启用了 IPC 的项目根(存在 `.claude/ipc.enabled`;遗留本地安装则看项目根有无 `ipc.py`)。不符则停止并提示用户:"各终端必须在同一项目根启动,请在正确目录重开本终端",不要继续。

**第 2 步——把 A 的注册表归属落到本会话**(不只是行为自称,防 two-A split-brain):
```
python "~/.claude/ipc/ipc_role.py" take A
```
(`take` 自动用 `CLAUDE_SESSION_ID` 作会话标识;会驱逐前任持有者并释放本会话原持的其他槽。)随后可用
`ipc_role.py status` 确认 A=live 归本会话。

**第 3 步——若 `$ARGUMENTS` 非空**,把它作为第一条任务派给 B:
`python "~/.claude/ipc/ipc.py" send --from A --to B --require-watcher "$ARGUMENTS"`,确认已发出(REFUSED=B 的盯哨没挂,先提醒用户去 B 窗口敲一行唤醒)。

之后按 hub 职责协作:
- **派活**:`send --from A --to B "<任务>"`;多 worker 一次派 `--to B,C`;广播活 worker `--to ALL`。派活默认加 `--require-watcher`(防黑洞)。任务正文含反引号/`$`/引号时**用 `--body-file <文件>`**,别走 shell 参数。
- **收回复**:起 ONE 常驻 Monitor(`persistent=true`)跑 `watch --me A`——每条回复以 `NEW MSG #id from ...` 小信号到达,收到后 `peek --me A --tail 3` 读全文。Monitor 不可用时退回后台 Bash `recv --me A --block`(exit 0=有消息,2=空超时重挂),或 `--block --count N` 屏障式收齐 N 条。
- **查派单完成度**:`pending --hub A [--detail]`(空=全部完成;状态 QUEUED/IN_PROGRESS/STALE/FAILED)。
- **协调类消息**(致谢/FYI/"重挂盯哨")一律 `--type note`——note 免租约免重派、不进 pending,也不要求 worker 回复。

铁律:
- **只用 watch/recv/peek 收消息,绝不 `Read` 整个 `_ipc.db`**——保持 token 成本不随消息数增长。
- 综合裁决(对账、覆盖检查、最终结论)永远留在 A,不下放 worker。
- 纯确认并进实质消息里发,别单发"收到"。
