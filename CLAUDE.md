# 项目说明 / Project protocol

> 本文件是被 Claude Code 在 cwd=本项目时自动加载的「行为规范」。下面整段是多终端 IPC 协议，路径无关、可被 install_ipc.py 逐字复制到其他项目。
> This CLAUDE.md is the agent behavior spec auto-loaded by Claude Code. The section below is the multi-terminal IPC protocol (path-agnostic; install_ipc.py copies it verbatim).

## 多终端通信协议（IPC，星型拓扑）

本项目可同时开多个 Claude Code 终端协作。它们是独立会话、互不感知，靠
`ipc.py`（stdlib sqlite3，数据库 `_ipc.db`）传递消息。

**拓扑：星型，A 为唯一中枢。** A = 主终端，B、C、D… = worker 从终端。**worker 只跟 A
通信，彼此之间互不通信**——协作也经 A 转。这样把"防回声打转"的不变量（只有 A 决定是否
继续）保持为线性，而非 N² 网状（网状会回声打转、死锁）。worker 数由 `.claude/hooks/ipc_role.py`
的 `ROLES` 决定（当前 A,B,C,D；要加 worker 直接扩这个元组）。

> ⚠️ **星型是"约定"，非代码强制。** `ipc.py` 是中立邮箱，`send --from B --to C` 在代码层
> 完全能跑（有意为之：保留 ZZ 测试名与未来拓扑弹性）。"worker 只回 A、不互发"靠 SessionStart
> 注入的角色 prompt + 本协议纪律保证。所以每个 worker 必须遵守：收到的任何消息只回 A，绝不
> 主动 `send` 给另一个 worker——否则可能触发星型本要防的回声死锁。（名字限 `[A-Za-z0-9_]+`，
> 含路径分隔符的名字会被 ipc.py 在落地心跳文件前拒绝。）

**前提（硬性）：所有终端必须运行在同一个 Claude Code 项目下**，即启动 `claude` 时的 cwd
都是**本项目根目录（`ipc.py` 所在的那个目录）**。原因：`ipc.py` 把数据库定位在自己所在
目录的 `_ipc.db`，且本 `CLAUDE.md` 协议只在 cwd=本项目时自动加载。若某终端在别的目录启动，
它用的是另一份 `ipc.py` 和另一个 `_ipc.db`，永远互通不了。开终端前先确认 cwd 一致。
（本段及以下协议路径无关，可逐字复制到其他项目；用 `python install_ipc.py <目标项目>` 一键安装。）

**角色固定主从，避免回声打转：**
- **A = 主终端（中枢）**：发起方/决策方。只有 A 决定要不要继续对话；A 统筹派活、收口、对账。
- **B/C/D = worker 从终端**：执行方。被动响应，做完即停，不主动追问、不寒暄，不替 A 决定。

**命令（在项目根目录运行）：**
```
python ipc.py send --from A --to B "消息"     # 发给单个 worker
python ipc.py send --from A --to B,C "任务"   # 并发派给多个 worker（每人各一行，独立已读位）
python ipc.py send --from A --to ALL "任务"   # 广播给所有在线 worker（注册表里除 A 外的角色）
python ipc.py send --from A --to B "任务" --require-watcher  # 收件人盯哨没挂起则拒发(exit 3)、不入队（多收件人时逐个判，在线的入队、掉线的拒发，有任一拒发即 exit 3）
python ipc.py status --watch B               # 探 B 盯哨是否挂起：ALIVE(exit0)/DOWN(exit1)
python ipc.py recv --me B                    # 取「发给我的、未读的」新消息并标记已读
python ipc.py recv --me A --block            # 阻塞等到有新消息才返回（超时打印 NONE (timeout)）
python ipc.py peek --me A --tail 5           # 只看最近 5 条，不标记已读
python ipc.py archive --keep 50              # 清理已读旧消息，保留最近 50 条
```

**盯哨存活检测（心跳机制）：** `recv --block` 每轮(默认2秒)会 touch 一个
`_watcher_<me>.alive` 心跳文件，退出/超时即清掉；被 `/clear` 杀掉则文件变陈旧。
据此 A 能判断某 worker 的盯哨此刻是否真在听——注意 `.claude/ipc_roles.json` 的角色注册
**不能**用作判据（`/clear` 后角色仍保留、盯哨进程却已死）。**A 派任务给 worker 一律用
`send --require-watcher`**：盯哨没挂起就直接拒发(exit 3)、消息不入队，避免把任务发进
黑洞空等；拒发时去该 worker 窗口戳一句让它重挂盯哨，再重发。worker 回 A 用普通 `send`（不加
`--require-watcher`，A 未盯哨时也不该挡 worker 的回复）。

**给 Claude 的规则：**
1. **只用 `recv` 收消息，绝不 `Read` 整个 `_ipc.db`**——`recv` 只返回未读的新消息，
   历史不重复进上下文，token 成本不随消息数增长。需要回顾上下文时用 `peek --tail N`。
2. 发消息一律走 `send`，写清 `--from` / `--to`。
3. **worker（B/C/D，从终端）**：收到任务→执行→把结果 `send --from <自己> --to A`→**停止**，
   不要再主动 `recv` 等下一条（除非挂了 `/loop`），也绝不直接发给其他 worker（星型：只回 A）。
   **硬性：盯哨/`recv` 返回的任何 A→worker 消息都必须 `send` 一条回 A**，哪怕只是确认或
   "已收到、无需动作"——绝不允许"收掉（标记已读）却不回"。worker 一旦把消息 `recv` 走，A 侧
   就再也收不到了；不回则对 A 等同消息丢失、A 的 `--block` 盯哨会一直空等。不要把测试
   /寒暄类消息判定为"无需回复"而静默吞掉，先回一条再决定后续。
4. **A（主终端/中枢）**：正常对话；需要 worker 回复时再 `recv --me A`。**并发派给多个 worker**
   用 `--to B,C` 或 `--to ALL`；但回复是**逐条**唤醒 A（每条回复让后台 `--block` 退出一次），
   要"等齐 B、C、D 再汇总"需 A 自己记账：收一条、重挂一个后台 `--block`，直到预期的几方都报到。
   收口（对账、核覆盖、定档）始终在 A，不下放给 worker。
5. 纯确认（"收到"）尽量并进实质回复里，别为一句确认单发，省 token。
6. **A 自动等 worker 回复（轻量盯哨）**：A 发完任务后，把 `python ipc.py recv --me A --block`
   挂到**后台 Bash**（`run_in_background`）；A 立刻拿回控制权干别的，worker 一 `send` 回来该命令
   退出，harness 自动把回复喂回并唤醒 A 续上——这是 push，省去 A 自己轮询。盯哨完成只回报
   一次：要多轮你来我往（或等多个 worker），就每轮重发/重起一个后台 `--block`。打印 `NONE (timeout)`
   （默认 580 秒，卡在 Bash 600 秒上限内）表示还没等到，再起一个接着等即可。

**让 worker 自动接力：** 角色与盯哨指令已由 SessionStart 钩子自动注入，**worker 通常无需手敲
`/sub`**。机制：`.claude/settings.local.json` 挂的 `python .claude/hooks/ipc_role.py claim`
在终端一进项目时，按「先到先得 + `session_id` 注册表(`.claude/ipc_roles.json`)」分配角色
（第一个进的拿 A，其后依次拿 B、C、D… 最低空位，lockfile 防竞争，`/clear` 不释放角色、同
session 复用），并把对应角色行为（A=中枢派活/收口；worker=立即 `recv --me <自己>` 清积压 +
把 `recv --me <自己> --block` 挂后台 Bash 盯哨）作为 `additionalContext` 注进该终端上下文。

**唯一的人工下限**：钩子只能"注入指令"，不能替 worker 主动发第一次工具调用——Claude 在收到
第一条用户输入前不会自跑后台命令，而唤醒 worker 又依赖一次 Claude 的后台 Bash 盯哨（纯 OS
后台进程无法 push 唤醒 Claude）。所以 **每个 worker 开窗后都要随便回一句话（"待命"/"ok"皆可），
它才照注入指令自动清积压 + 挂盯哨**；"开窗即自驱、零键盘"做不到，这是 harness 架构下限，
不是配置缺陷。（A 在派活前应提示用户去每个待用 worker 窗口戳一句。）

之后即自驱：worker 平时挂起不耗 token，A 一派活盯哨退出、harness 自动唤醒 worker 执行并 `send`
回结果，然后 worker 重挂一个新盯哨，无需 `/loop`。`/sub` 仅作**手动备用**（钩子未生效的迁移期、
或 `/clear` 后想手动补挂盯哨时用；会先清积压未读任务再重挂）。只偶尔传几句时，手动 `recv` 更省。
