#!/usr/bin/env python3
"""Auto-assign IPC role (A=master / B=subordinate) on Claude Code session start.

Wired as two hooks in .claude/settings.local.json:
  SessionStart -> python ipc_role.py claim    (claims a free role, injects its
                                               behavior as additionalContext)
  SessionEnd   -> python ipc_role.py release   (frees the role so the next
                                               terminal can take it)

Role assignment is first-come-first-served in a STAR topology with A at the hub:
the first session becomes A (master), and each later session takes the next free
worker slot (B, C, D...). Workers respond to A and stop; they never message each
other — this star rule is a CONVENTION carried by the injected role prompts +
CLAUDE.md, NOT enforced by ipc.py (a neutral mailbox). A tiny on-disk registry
(ipc_roles.json next to this script's parent)
tracks who owns what, keyed by Claude's session_id. To add more worker slots,
extend ROLES below.

  python ipc_role.py reset    # wipe the registry (use after a hard-killed
                              # terminal left a stale claim)

Hook I/O contract (Claude Code):
  - stdin = JSON: SessionStart has {session_id, source}, SessionEnd has
    {session_id, reason}.
  - SessionStart context injection = stdout JSON
    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                            "additionalContext": "..."}}.
"""
import json
import os
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .claude/
REGISTRY = os.path.join(ROLE_DIR, "ipc_roles.json")
LOCK = REGISTRY + ".lock"
ROLES = ("A", "B", "C", "D")  # A = hub/master; B,C,D... = workers (extend freely)

# Behavior injected into each session so no manual /main or /sub is needed.
MASTER_CONTEXT = (
    "【IPC 角色：你是主终端 A（master / 星型中枢）】本项目用 ipc.py(_ipc.db) 与从终端 "
    "B、C、D… 协作（星型拓扑，你是唯一中枢，worker 之间互不通信），协议见 CLAUDE.md。"
    "你是发起方/决策方。派活给单个 worker：`python ipc.py send --from A --to B \"<任务>\"`；"
    "并发派给多个：`--to B,C`；广播给所有在线 worker：`--to ALL`。要等回复时，把 "
    "`python ipc.py recv --me A --block` 用后台 Bash(run_in_background) 挂起——worker 一回复"
    "该命令退出、harness 自动唤醒你读结果。多 worker 并发时回复是逐条唤醒，要等齐需自己计数、"
    "收一条重挂一次。只用 recv/peek 收消息，绝不 Read 整个 _ipc.db。本角色已自动指定，无需再敲 /main。"
)


def _worker_context(role):
    return (
        f"【IPC 角色：你是从终端 {role}（subordinate / worker）】本项目用 ipc.py(_ipc.db) 与主终端 A "
        f"协作（星型拓扑，你只跟 A 通信、不与其他 worker 通信），协议见 CLAUDE.md。立即进入从属待命："
        f"① 先跑一次 `python ipc.py recv --me {role}` 清积压未读任务，有就执行并用 "
        f"`python ipc.py send --from {role} --to A \"<结果摘要>\"` 发回；② 把 "
        f"`python ipc.py recv --me {role} --block` 用后台 Bash(run_in_background) 挂为阻塞盯哨，"
        f"然后结束本轮。每当盯哨完成被唤醒：有任务则执行→send 回 A→重挂新盯哨；输出 "
        f"NONE (timeout) 则直接重挂。做完即停，不寒暄、不替 A 决定。只用 recv/peek，"
        f"绝不 Read 整个 _ipc.db。本角色已自动指定，无需再敲 /sub。"
    )


NONE_CONTEXT = (
    "【IPC 角色：无】本项目 A 及所有 worker 协作角色均已被占用，此终端不参与多终端协作，"
    "按普通会话使用即可。若确认某终端已关闭却仍显示占用，运行 "
    "`python .claude/hooks/ipc_role.py reset` 清空角色登记后重开本终端。"
)


def _context_for(role):
    if role == "A":
        return MASTER_CONTEXT
    if role:
        return _worker_context(role)
    return NONE_CONTEXT


def _read_stdin():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _lock():
    """Crude cross-platform lock: exclusive-create a lockfile, retry briefly.
    Breaks a stale lock (>30s old) left behind by a process killed mid-claim,
    so a leftover LOCK file can't wedge every future claim into the unlocked path."""
    for _ in range(50):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK) > 30:
                    os.remove(LOCK)   # stale: owner died holding it; reclaim
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    return False  # give up after ~2.5s; proceed unlocked rather than hang a hook


def _unlock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def _load():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {r: data.get(r) for r in ROLES}


def _save(reg):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY)


def _owned_role(reg, session_id):
    for r in ROLES:
        if reg.get(r) and reg[r].get("session_id") == session_id:
            return r
    return None


def claim():
    info = _read_stdin()
    sid = info.get("session_id") or "unknown"
    locked = _lock()
    try:
        reg = _load()
        role = _owned_role(reg, sid)          # reuse on clear/resume/compact
        if role is None:
            for r in ROLES:                    # else take the lowest free slot
                if not reg.get(r):
                    role = r
                    reg[r] = {"session_id": sid,
                              "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    _save(reg)
                    break
    finally:
        if locked:
            _unlock()
    _inject(_context_for(role))


def release():
    info = _read_stdin()
    sid = info.get("session_id") or "unknown"
    reason = info.get("reason", "")
    if reason == "clear":
        return                                 # /clear continues the session; keep role
    locked = _lock()
    try:
        reg = _load()
        role = _owned_role(reg, sid)
        if role is not None:
            reg[role] = None
            _save(reg)
    finally:
        if locked:
            _unlock()


def reset():
    locked = _lock()
    try:
        _save({r: None for r in ROLES})
    finally:
        if locked:
            _unlock()
    print(f"OK reset {REGISTRY}")


def _inject(text):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }, ensure_ascii=False))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "claim"
    if cmd == "claim":
        claim()
    elif cmd == "release":
        release()
    elif cmd == "reset":
        reset()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
