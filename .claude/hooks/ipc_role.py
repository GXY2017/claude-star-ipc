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
    "[IPC role: you are master terminal A (master / star hub)] This project uses "
    "ipc.py(_ipc.db) to collaborate with worker terminals B, C, D… (star topology: "
    "you are the sole hub; workers don't talk to each other). See CLAUDE.md for the "
    "protocol. You are the initiator/decider. Dispatch to one worker: "
    "`python ipc.py send --from A --to B \"<task>\"`; to several at once: `--to B,C`; "
    "broadcast to all live workers: `--to ALL`. To wait for a reply, run "
    "`python ipc.py recv --me A --block` as a background Bash(run_in_background) — when "
    "a worker replies, that command exits and the harness wakes you to read the result. "
    "To wait for several at once, use the barrier `recv --me A --block --count N`: one "
    "background call returns when all N have replied (the tally lives in the process, "
    "surviving context compaction). The watcher exits 0 when it returns message(s) — read "
    "them — and 2 on an empty timeout — just re-arm WITHOUT re-reading the output (the "
    "harness shows exit 2 as status=failed; that is a normal timeout, not an error). Only "
    "use recv/peek to receive; never Read the whole _ipc.db. This role is auto-assigned; "
    "no need to type /main."
)


def _worker_context(role):
    return (
        f"[IPC role: you are worker terminal {role} (subordinate / worker)] This project "
        f"uses ipc.py(_ipc.db) to collaborate with master terminal A (star topology: you "
        f"talk only to A, never to other workers). See CLAUDE.md for the protocol. Enter "
        f"standby immediately: (1) first run `python ipc.py recv --me {role}` to drain any "
        f"backlog; if there is a task, do it and send the result back with "
        f"`python ipc.py send --from {role} --to A \"<result summary>\"`; (2) park "
        f"`python ipc.py recv --me {role} --block` as a background Bash(run_in_background) "
        f"blocking watcher, then end this turn. Each time the watcher wakes you: if there "
        f"is a task, execute → send back to A → re-arm a new watcher; on NONE (timeout) the "
        f"watcher exits 2 (harness shows status=failed — a normal timeout, not an error), so "
        f"just re-arm WITHOUT re-reading the output; if instead it was killed (neither exit 0 "
        f"nor 2, e.g. /clear), peek --tail 3 to check for a missed message, then re-arm. HARD "
        f"RULE: send exactly one reply to A for EVERY message you recv — "
        f"for EVERY message you recv — "
        f"even a test, a greeting, or 'received, no action needed'; never consume (mark "
        f"read) a message without replying, or A's --block watcher waits forever. Stop when "
        f"done; no chit-chat, don't decide on A's behalf. Only use recv/peek; never Read the "
        f"whole _ipc.db. This role is auto-assigned; no need to type /sub."
    )


NONE_CONTEXT = (
    "[IPC role: none] A and all worker roles in this project are already taken; this "
    "terminal does not join the multi-terminal collaboration — use it as a normal "
    "session. If a terminal is confirmed closed but still shows as occupied, run "
    "`python .claude/hooks/ipc_role.py reset` to clear the role registry, then reopen "
    "this terminal."
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
