# Codex recover adapter

> **Codex 适配**：`$multi-terminal-ipc recover` 的 Codex 形式（Claude 侧是 `/ipc-recover` → `claude-recover.md`；原独立技能 ipc-recover 于 2026-10-29 并入本 skill）。角色参数由用户在
> 调用 `$multi-terminal-ipc recover` 时给出，**角色一律以该参数为准**。⚠️ 不要拿
> `CODEX_THREAD_ID` 的前缀去对 `ipc_role.py status` 的 owner 列：codex thread id
> 是 UUIDv7，同一分钟内启动的线程前 8 位全相同（2026-08-31 pane C 据此把
> pane E 的合法 E 槽误读成"自己被错登记到 E"）；确需对账只能比对完整 id。**Codex 侧待命一律用
> 第 3 步的主动通知 supervisor(循环跑一次性 `recv --block` + `notify()`),
> 不用无限 `watch`**——无限 `watch` 会认领消息却唤不醒模型。`exec_command`
> 没有 `timeout_ms`:一轮多长由 `recv --timeout 580` 决定,进程没结束就用
> `write_stdin` 分段续取;同步调用不会因此变成后台任务。
>
> ⚠️ **实测限制(2026-08-06):supervisor 不保证越回合存活。** E 完成任务、回合
> 结束后,recv 循环随之消失(进程表零 recv),新消息躺信箱无人收。跨回合待命的
> **可靠路径是 hub 侧**:hub 每次 `send --to E` 后注入唤醒
> (`ipc_wake_pane.ps1 -Role E`);本 supervisor 只保证**当前回合内**即时收件。
> 收到唤醒指令时照做 `recv` 取件即可,不要以为自己"本来就在听"。
> **禁止**为"保持在线"自行拉起任何 keepalive/守护进程(2026-08-06 起
> `codex_ipc_worker.ps1` 已彻底删除):纯心跳会把 `status --watch` 撑成 ALIVE
> 而实际无人收件——制造"假活黑洞",比诚实的 DOWN 更有害。

你在**恢复本终端的 IPC 待命状态**(用于 `/clear`、上下文压缩、或 SessionStart 钩子没跑起来之后)。

> 本文所有 `ipc.py` / `ipc_role.py` 均指用户级安装:
> `python "C:\Users\<user>\.claude\ipc\ipc.py"` / `python "C:\Users\<user>\.claude\ipc\ipc_role.py"`。
> (遗留项目本地安装的项目才用项目根的 `ipc.py`;以项目 AGENTS.md 的 Deployment 说明为准。)

先理解机制(决定了恢复只需做什么):
- **`/clear` 不释放角色**——`ipc_role.py` 在 clear 时早返回,注册表按 `session_id` 保留"本会话→角色"映射。`/clear` 只杀掉后台**盯哨进程**。
- 所以恢复的核心是**重新挂盯哨**,不是重新抢角色。
- `recv`/`watch` 只吃 `--me <角色>` 命令行参数,**不依赖注册表**——只要你知道自己的角色名就能收发。
- ⚠️ **但 A 的派单依赖注册表**(2026-08-17 起,#1186 修复):`send --require-watcher` 对"心跳新鲜但角色无 owner"直接 `REFUSED-SQUATTER` 拒发(孤儿接收器与合法未注册接收器无法区分,注册表 owner 是唯一判据)。所以恢复流程**必须保证注册表 claim 存在**。

步骤:

1. **确定角色**(按优先级,**别盲信旧注入**):
   - **首选 用户随 `$multi-terminal-ipc recover` 提供的角色**:角色 = 本终端的**开启次第**——第1台(hub)=A、第2台=B、第3台=C、第4台=D。用户知道这是第几台,所以 `$multi-terminal-ipc recover B` 就是"我是第2台=B"。**有参数一律以参数为准**。
   - 无参数时:跑 `python "C:\Users\<user>\.claude\ipc\ipc_role.py" status` 看注册表×心跳的对账视图,判断哪个角色槽该是你(你的会话若仍匹配某槽的 session_id 即用它;/clear 换过 sid 则匹配不到)。**不要只凭上下文里的 `[IPC role: ...]` 注入块**——它在 /clear 后可能过期或错误(例:曾被误判成 D)。
   - ⚠️ **别拿 session id 判断"我的槽被抢了"(2026-08-03 实测 wrong-turn 变种)**:/clear(或 `/new`)会换新会话,但环境变量和旧上下文里的 sid 是**过期旧值**。拿它对照注册表,会把钩子/占位**刚替你的新会话**重新认领的槽误判成"被另一个 live 会话抢走",进而转投别的空闲槽——实例:newcycle 批量重置后,B(Kimi)据 stale sid 误判 B 被抢,claim 了空闲的 E 还挂了 E 接收器,占掉 codex 的注册表占位。**判据永远是"角色该不该是你"(用户给的角色/开启次第),不是 sid 匹配**:即便 `status` 显示你的角色由"别的" live 会话持有,只要用户说你是 E,就直接按第 3 步给 E 起 supervisor,**绝不改投其他空闲槽**。起完 supervisor **核对 claim**:`ipc_role.py status` 里你的角色 owner 为 `-` 时必须补 `python "C:\Users\<user>\.claude\ipc\ipc_role.py" take <角色> --session "$env:CODEX_THREAD_ID"`——2026-08-17 起无 owner 的接收器会被 A 的 `--require-watcher` 以 `REFUSED-SQUATTER` 拒发(此前"不必重 take——`recv` 不依赖注册表"的说法已因该修复作废;`recv` 本身确实不查注册表,但 A 的派单闸查)。
   - 仍不确定 → 问用户"这是第几台终端(→A/B/C/D)"。
   - ⚠️ **若你发现自己当前正以"错误角色"挂着接收器**(如本该是 B 却在收 `--me D`):先终止那个错角色的 Codex execution cell,再按正确角色走第 3 步。**代际令牌帮不了这一步**——它只让同角色的新 `watch` 退掉旧 `watch`;跑错角色是**两个不同信箱**,而 Codex 侧的 supervisor 用 `recv` 根本不领代际号,任何旧接收器都只能手动终止。幂等"已恢复"判断要基于**正确角色**,不是基于你碰巧在跑的那个。

2. **核对项目**:cwd 必须是启用了 IPC 的项目根。任一即可:本会话环境带 **`IPC_ROLE`**(fleet 启动脚本按 pane 设置,`ipc_role.py:92` 以它为充分 opt-in;查 `$env:IPC_ROLE`,并核对 fleet pane 记录里本角色的 `project` 等于 cwd:fleet 1 `~/.claude/ipc/wezterm_panes.json` 里该角色条目的 `project`,fleet 2(X)`~/.claude/ipc/wezterm_fleet2.json` 顶层的 `project`;不等即是从别处继承来的 IPC_ROLE,停下);项目根**存在 `.claude/ipc.enabled`**(用户级部署的 opt-in 门);遗留本地安装时项目根有 `ipc.py`。home 目录建不了门(`ipc_role.py enable` 拒绝 home),fleet pane 在 home 只靠 `IPC_ROLE`,缺门不是停下的理由(2026-10-18:A 在 home 因缺门停下,盯哨没挂)。三者都不满足才停下并提示用户去正确目录重开,不要继续。(不要拿"项目根有没有 ipc.py"当用户级部署的判据——用户级安装下项目根本来就没有它。)

3. **先挂盯哨,再干活**(worker 角色 B/C/D):
   > 2026-10-22:Claude 侧 worker 的默认停泊改为后台 Bash `recv --me <角色> --block --signal-only --timeout 3500`(空闲不花回合,送达即退出唤醒会话;见 Claude 的 /ipc-recover 第 3 步)。Codex 没有越回合的后台通知,本技能的回合内 supervisor 不变;`--signal-only` 对 Codex 无用(它需要正文走 `--json`)。

   **Codex 用主动通知 supervisor,不要用无限 `watch`。** 无限 `watch` 交给
   yielded execution cell 后会**原子认领消息但唤不醒模型**:`status --watch`
   显示 `ALIVE`、`peek` 也看得到消息,可它的 stdout 不会在本轮结束后主动呈现,
   消息被标记已读却没人处理——对 A 表现为"已送达却永不回复"的黑洞
   (2026-07-28 隔离实测)。正确做法是把**一次性 `recv --block`** 放进循环,
   每轮用 `notify()` + `yield_control()` 主动叫醒模型。(Claude 侧的 persistent
   Monitor 跑 `watch` 语义不变,这是 Codex 专用实现,不动共享协议。)

   把 `<角色>` 替换为已确认的 B/C/D;在当前 Windows managed sandbox 中直接请求
   `require_escalated`,避免已知的 `CreateProcessAsUserW failed: 5` 重试浪费。
   第一行 pragma 是关键:

   ```javascript
   // @exec: {"yield_time_ms": 1000, "max_output_tokens": 1000}
   var ipcReceiveOnce = async () => {
     try {
       // tools.exec_command:2026-10-10 E 角色实测可用(启动后 status --watch E = ALIVE);
       // 2026-10-16 Codex 审查确认 tools.shell_command 已是 undefined。返回的是对象:
       // output(本段输出)、exit_code(进程已结束)、session_id(进程未结束:用
       // tools.write_stdin 继续取,直到返回里没有 session_id)。没有 timeout_ms 参数,
       // 等待按 yield_time_ms 分段;一轮多长由 recv 自己的 --timeout 580 决定。
       var r = await tools.exec_command({
         cmd: 'python "C:\\Users\\<user>\\.claude\\ipc\\ipc.py" recv --me <角色> --block --timeout 580 --keep-heartbeat --json --no-body',
         workdir: '<项目根>',  // 本终端启动时的项目目录(ipc_role.py status 打印的物理路径);信箱按项目分,写死别的项目会投错信箱(2026-09-05 修)
         sandbox_permissions: 'require_escalated',
         justification: '允许我在沙箱外启动本项目的 IPC 接收器以持续待命吗?',
         prefix_rule: ['python', 'C:\\Users\\<user>\\.claude\\ipc\\ipc.py'],
         yield_time_ms: 1000,
         max_output_tokens: 2000
       });
       var raw = r.output || '';
       while (r.session_id) {
         r = await tools.write_stdin({session_id: r.session_id, chars: '', yield_time_ms: 1000, max_output_tokens: 2000});
         raw += r.output || '';
       }
       // --json 模式:有消息 exit 0、每条一行 JSON;空超时 exit 2 且无输出(正常轮换,不是故障)。
       // 只有"exit 2 + 空输出"才算超时:argparse 参数错误也返回 2,但带错误文本,必须停下报告。
       var timedOut = r.exit_code === 2 && raw.trim() === '';
       return {ok: r.exit_code === 0 || timedOut, timedOut: timedOut, value: raw};
     } catch (error) {
       return {ok: false, value: String(error)};
     }
   };

   var ipcPending = ipcReceiveOnce();
   var ipcStopReason = '';
   while (true) {
     var ipcOutcome = await ipcPending;
     var ipcRaw = ipcOutcome.value;
     if (!ipcOutcome.ok) {
       ipcStopReason = `IPC receiver for <角色> failed: ${ipcRaw.slice(0, 300)}`;
       notify(ipcStopReason);
       await yield_control();
       break;
     }

     // recv --json --no-body:每条消息一行 JSON {id, ts, from, type, task, session, result, body:"", body_chars, paths, anchor, new_items}。
     // 只按对象字段分类,不从正文认协议(正文里引用的 "DONE #999" 曾被当成真回执,2026-10-16 Codex 审查);
     // 正文不进输出;但输出仍可能被 max_output_tokens 截断,所以 recv --json 同时把整批完整写进
     // 状态目录(_recv_<角色>_batch_<编号>.jsonl,按批编号保留最近 20 批(2026-10-17 前为 100),每条 envelope 带 batch 号),
     // peek --batch <编号> 原样读回,--batches 列清单;正文事后 peek --id 读。
     // type=reply 且 result=CLEAN 是干净回执(task 字段=结清的任务号);其余一律 NEW MSG。
     // 2026-10-17:new_items>0 的 CLEAN 回执在 DONE 行后加 "+NEW n: peek ...",hub 要 peek;anchor 供后续简报/结论引用。
     // 2026-10-22:信封字段 new_types(如 "FAIL 1, FACT 1")是条目类型计数,随 +NEW 一起打出;FAIL 条目供 `ipc.py board --type FAIL` 汇总。
     var ipcTag = (m) => (m.new_items ? ` +NEW ${m.new_items}${m.new_types ? ' (' + m.new_types + ')' : ''}: peek --me <角色> --id ${m.id}` : '') + (m.anchor ? `  anchor ${m.anchor}` : '');
     var ipcBatch = [];
     var ipcBatchNo = null;  // 本批编号,取自任一解析成功的 envelope
     var ipcBad = [];  // 解析不了的非空行 = 输出被截断或损坏;不能跳过,整批要报
     var ipcSaveFailed = null;  // recv 落盘失败(stderr 的 BATCH-SAVE-FAILED 行):本批没有文件,只能按 id 取正文
     for (var ipcLine of ipcRaw.split(/\r?\n/)) {
       if (!ipcLine.trim()) continue;
       if (ipcLine.indexOf('BATCH-SAVE-FAILED') === 0) { ipcSaveFailed = ipcLine.slice(0, 300); continue; }
       var ipcMsg = null;
       if (ipcLine.charAt(0) === '{') { try { ipcMsg = JSON.parse(ipcLine); } catch (e) { ipcMsg = null; } }
       if (!ipcMsg || typeof ipcMsg.id !== 'number') { ipcBad.push(ipcLine.slice(0, 160)); continue; }
       if (ipcBatchNo === null && typeof ipcMsg.batch === 'number') ipcBatchNo = ipcMsg.batch;
       if (ipcMsg.type === 'reply' && ipcMsg.result === 'CLEAN') {
         ipcBatch.push({id: ipcMsg.id, text: `DONE task #${ipcMsg.task} <- ${ipcMsg.from} RESULT CLEAN (#${ipcMsg.id})` + (Array.isArray(ipcMsg.paths) && ipcMsg.paths.length ? ` RESULT: ${ipcMsg.paths.join(', ')}` : '') + ipcTag(ipcMsg)});
       } else {
         ipcBatch.push({id: ipcMsg.id, text: `NEW MSG #${ipcMsg.id} from ${ipcMsg.from} (${ipcMsg.type}${ipcMsg.task ? ', task #' + ipcMsg.task : ''}, ${ipcMsg.body_chars} chars)` + ipcTag(ipcMsg)});
       }
     }
     var ipcTimedOut = ipcOutcome.timedOut;
     if (ipcSaveFailed !== null) {
       // 2026-10-17 审计:信封已认领、文件没存成——输出就是唯一副本,不能再指向 peek --batch。
       notify(`IPC batch for <角色> was NOT saved to disk (${ipcSaveFailed}) — the envelopes in this output are the only copy; ` +
              `read bodies with ipc.py peek --me <角色> --id <ids>, never peek --batch for this batch`);
     }
     if (ipcBad.length && ipcSaveFailed !== null) {
       notify(`IPC batch for <角色> damaged AND unsaved: ${ipcBad.length} unparsable line(s), ${ipcBatch.length} parsed — ` +
              `no batch file exists; the recv line above lists every claimed id: peek --me <角色> --id <those ids>; fragments: ` +
              ipcBad.join(' | ').slice(0, 400));
     } else if (ipcBad.length) {
       // recv 已把整批标为已读;恢复只能靠它落盘的完整批次(peek --batch),不按残留行数估 --tail
       // (2026-10-16 Codex 复现:100 条截到 2 条时 --tail 8 只找回最后 8 条)。报警后接收器继续跑。
       notify(
         `IPC batch for <角色> damaged: ${ipcBad.length} unparsable line(s) (truncated output?), ` +
         `${ipcBatch.length} message(s) parsed — recover the whole batch from the file recv saved: ` +
         (ipcBatchNo !== null
           ? `ipc.py peek --me <角色> --batch ${ipcBatchNo}`
           : 'ipc.py peek --me <角色> --batches (list), then --batch <N> for the newest') +
         ` (complete, one line per message; --json for raw), then peek --id for bodies; fragments: ` +
         ipcBad.join(' | ').slice(0, 400)
       );
     }
     if (!ipcBatch.length && !ipcBad.length && !ipcTimedOut) {
       ipcStopReason = `IPC receiver for <角色> stopped unexpectedly: ${ipcRaw.slice(0, 300)}`;
       notify(ipcStopReason);
       await yield_control();
       break;
     }

     // 关键顺序:先重挂下一次 recv,再通知模型。recv 返回时心跳已被 finally
     // 删除,先重挂才能把心跳空窗压到最短,免得在途任务被 reaper 误判 stale。
     ipcPending = ipcReceiveOnce();
     if (ipcBatch.length || ipcBad.length) {
       // 按 id 精确取正文:一次 recv 认领的是全部未读,固定 --tail 3 会漏掉前面的
       // (2026-10-16 Codex 审查复现:4 条只取回后 3 条)。
       var ipcIds = ipcBatch.map(function (m) { return m.id; }).join(' ');
       for (var item of ipcBatch) {
         notify(`${item.text} — read full: ipc.py peek --me <角色> --id ${ipcIds}`);
       }
       await yield_control();  // 损坏批次没有可通知的消息时也要叫醒模型去 peek
     }
     // 空超时(exit 2 + NONE (timeout))是正常轮换,静默继续。
   }
   text(ipcStopReason || 'IPC receiver stopped; run $multi-terminal-ipc recover again.');
   ```

   正常结果必须在约 1 秒内返回 `Script running with cell ID <id>`。记住
   `cell_id` 以备终止;**不要在前台等它退出**,也不要靠手工 `functions.wait`
   取信号——能收到 `notify()` 的短信号才算这套跑通了。

   启动后必须在 **3–5 秒内**用独立短命令检查:

   `python "C:\Users\<user>\.claude\ipc\ipc.py" status --watch <角色>`

   只有返回 `<角色> watcher: ALIVE` 才算恢复成功。若返回 `DOWN`、execution
   cell 已结束或未得到 `cell_id`,立即用 `functions.wait(cell_id=...,
   yield_time_ms=1000)` 取错误输出并报告;最多修正后重试一次,**不得继续静默
   等待数分钟**。若权限审批被拒绝,直接报告阻塞。

   ⚠️ **`--keep-heartbeat` 不是可选项,是本配方的必需项**(2026-07-28 加入)。
   `recv` 每次返回都会在 `finally` 里处置心跳,而 supervisor 只能在返回之后才启动
   下一次 `recv`——实测该重挂空窗为 **2.7–5.6 秒(中位 3.7 秒)**,不是亚秒级。
   不带该 flag 时,空窗内 `status --watch` 报 DOWN、`ipc_role.py` 报 DORMANT、
   hub 的 `send --require-watcher` 会**拒发一个其实活着的 worker**;而每收到一条
   消息同样会触发一次退出重挂,所以"刚回复完就被派活"极易撞中。带上该 flag 后,
   心跳在返回边界被刷新而非删除,继任者拿到完整的 8 秒窗口。**普通一次性
   `recv --block` 回退路径绝不要加它**——那会让角色在你已停止收听后仍被判活至多 8 秒。

   ⚠️ **从旧的无限 `watch` 切到本 supervisor 时,必须先终止旧的 execution
   cell**——`recv` 不领代际号,**代际令牌淘汰不了旧 `watch`**(它只让新 `watch`
   退掉同角色的旧 `watch`)。旧 `watch` 若还活着会继续抢认领消息,新 supervisor
   反而收不到。

   每轮 580 秒的空超时由 supervisor 自己重挂,属正常轮换;`exec_command`
   没有强杀参数,一轮多长由 `recv --timeout 580` 决定,不是永久化语义。cell 被杀或父进程退出后接收器即退役,
   仍需待命时按本步骤重挂。

   `ALIVE` 只证明心跳与收件进程存活,**不等于模型会被唤醒**——判据是能收到
   `notify()` 信号。心跳也不等于租约:长任务仍须按下文定期 `ack`。

   **顺序要紧**:任务租约的 reaper 把"心跳在跳"与"租约未到期"做 AND 判定,若先 recv 清积压再挂哨,执行期间无心跳,在途任务会被误判 stale 而重新入队。挂哨后积压会在几秒内以信号形式到达(`NEW MSG #id from ...`,不带正文以防截断);**收到信号用 `python "C:\Users\<user>\.claude\ipc\ipc.py" peek --me <角色> --id <信号里的 id>` 读全文**(一批多条就把通知里列出的 id 全部传给 `--id`;固定 `--tail 3` 会漏掉前面的)(`recv` 已把消息标记已读,只能靠 `peek` 读正文),读完**先 `ack --me <角色> --task N` 再开工**(2026-08-17 起 ack 刷新 last_seen_ts;不 ack 的在途任务超 30 分钟会在 A 的 pending 里标 `IN-PROGRESS-SILENT` 疑似未开工),然后执行→`send --from <角色> --to A --in-reply-to N --body-file <结果文件>` 回 A(N=本单任务号;带任务号的 reply 本身就结清任务,不用再 `done`;不要用 `--type note`——note 不结清,A 的时钟会一直报警;多单在手而不带 `--in-reply-to` 会被 REFUSED-LINK 拒发,2026-09-16)。**结果文件第一行写 `RESULT: <产物绝对路径>`**(多个产物多写几行,后面再接摘要;2026-09-19 起):`send` 会核盘上文件并打印 `RESULT CLEAN`/`UNVERIFIED: <原因>`,CLEAN 的回复 A 只收到一行 `DONE #N <- 角色 RESULT: 路径`、不读正文;有问题、只做了一部分、失败,就**不要写** RESULT 行,A 会全文读。RESULT 行之后可加**可选 `NEW:` 段**(2026-10-17):一行 `NEW:`,其下每条一行 `- `,写成"断言 + 不采纳的后果";只写有证据的(引输出、行号,或两个互相冲突的来源),不写推测和泛泛提醒;占本单预算;简报写了"只交付"的封闭型单不写 NEW 段。`send` 回显 `ANCHOR #task<N>.<角色>  NEW items=n`。supervisor 在通知前已自行重挂下一轮 recv,**不用手工重挂**。执行期间心跳只由每轮 recv 维持,**超过 1 分钟的任务须每隔几分钟跑 `ack --me <角色>`**——ack 同时续租约+刷心跳,防 reaper 误收在途任务。
   **一个信箱同一时刻只需一个接收器**——认领是原子单消费者,双接收器不会重复投递,但会互相抢消息、让本该唤醒你的那条落进另一个进程。⚠️ **代际令牌只覆盖 `watch`,不覆盖本 supervisor 的 `recv`**:起新 supervisor **不会**自动退掉任何旧接收器,无论旧的是同角色的 `watch` 还是另一个 supervisor,也不会退掉跑错角色的 `watch --me D`(见第 1 步)。所以重挂前**必须手动终止旧 execution cell**。
   **最后回退(无 `notify` / `yield_control` 时才用)**:单次阻塞 `recv --me <角色> --block`,每次唤醒手工重挂(exit 0=有任务读之;exit 2=空超时直接重挂;被杀=先 `peek --tail 10` 查漏再重挂(此时没有信号 id 可用))。⚠️ **单次 cell 跑完 ≠ 仍在待命**:它返回后心跳即死,不重挂就是彻底离线,不得据此宣称"已待命"。

   **配额交接(worker,2026-10-21 用户定)**:执行任务中收到一条插话 `IPC A: your 5h quota is N% used (resets HH:MM). Stop task #N ...`,是 A 的时钟打进 pane 的(Codex 的 steer,不是 recv 信号):做完当前这一步就停,不再开新单元;按它写的路径写交接文件,第一行 `HANDOFF: <该路径>`,其后写 (1) 已完成部分及产物路径 (2) 剩余步骤,下一步在前 (3) 已做的决定和假设 (4) 踩过的坑;再照它给的命令 `send --in-reply-to N --body-file <该文件>` 回 A,然后停。别的角色从这份文件接着做,写到对方不看对话也能接手。

4. **A(hub)角色也挂接收器(2026-08-21 起,用户决定;旧规"A 按需收、不挂常驻盯哨"作废)**:Codex 侧用第 3 步的 supervisor 挂 `--me A`(只在当前回合内有效,下一回合须重起;无 `notify` 时退回单次 `recv --me A --block`,`--block --count N` 可屏障式一次收齐 N 条),再按 `$multi-terminal-ipc main`「机械化」段起 `ipc.py clock --hub A --block` supervisor 执行 15 分钟回复时钟(2026-10-18 起取代已删除的 `stall_watch.py`;Claude 侧时钟内建于 `watch --me A`,Codex 的收信 supervisor 是 recv --json,不能混入时钟行,所以单起;`--block` 打出告警即退出 exit 0、超时 exit 2 无输出,与第 3 步 recv 循环同一套"退出后读输出"的接法)。挂好后核对注册表:`ipc_role.py status` 里 A 的 owner 为 `-` 则补 `python "C:\Users\<user>\.claude\ipc\ipc_role.py" take A --session "$env:CODEX_THREAD_ID"`。与 worker 的差异:A 收的是**回复不是任务**——不需要 ack/done,不受租约 reaper 影响;收完按 hub 职责(`$multi-terminal-ipc main`)综合裁决、继续派活。注册表对 hub 豁免:A 心跳死也**不会**被 reclaim-dead/claim 清扫回收,hub 身份只能显式 `take A` 转移。(A 也可用 `$multi-terminal-ipc main` 自声明 hub 身份——它会同步更新注册表归属。)

5. **罕见:钩子从未运行 / 注册表丢了你的槽**(不是 /clear,是 hook 脚本异常)——你仍能用 `--me <角色>` 正常收发(recv/watch 不依赖注册表),但后果不止"`--to ALL` 广播漏掉你":**A 的 `--require-watcher` 派单会对无 owner 的你直接 `REFUSED-SQUATTER`**(2026-08-17 起)。首选轻修法:`python "C:\Users\<user>\.claude\ipc\ipc_role.py" take <角色> --session "$env:CODEX_THREAD_ID"` 一条命令补上 claim(placeholder "manual" 也算有效 owner)。重修法:**直接关掉本窗、重开终端**,让 SessionStart 钩子重新分配槽;或对确认已死的占槽跑 `python "C:\Users\<user>\.claude\ipc\ipc_role.py" reclaim-dead`(只回收心跳已死的 **worker** 槽;hub 槽豁免——hub 心跳死≠hub 死(盯哨可能到期未重挂,见第 4 步),故可随时安全盲跑,2026-07-03 起。ghost 槽通常也无需手动清:任何终端 SessionStart 的 claim() 都会顺手全量清扫)。**不要在本恢复流程里跑 `ipc_role.py reset`**——`reset()` 会**清空整个注册表**(把活着的 A/C 也一起抹掉,导致角色错乱);它是需人工确认"所有终端都已死"时才用的运维命令,不属常规恢复。
