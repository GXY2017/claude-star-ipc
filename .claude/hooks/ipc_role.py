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


def _import_ipc():
    """Import the ipc.py that owns this project's state, so the registry/heartbeat
    paths and the project root all match what ipc.py computes — whether this hook
    is the project-local copy (ipc.py in the project root) or the user-level copy
    (ipc.py beside it in ~/.claude/ipc). Bulletproof: returns None on any failure,
    and the caller falls back to legacy project-local paths so a hook never crashes."""
    here = os.path.dirname(os.path.abspath(__file__))
    # global copy: ipc.py beside us; local copy: ipc.py in the project root, which
    # is the parent of ROLE_DIR (.claude/) i.e. two levels up from this hook file.
    project_root = os.path.dirname(os.path.dirname(here))
    for cand in (here, project_root):
        if os.path.exists(os.path.join(cand, "ipc.py")):
            try:
                if cand not in sys.path:
                    sys.path.insert(0, cand)
                import ipc  # noqa: F401
                return ipc
            except Exception:
                return None
    return None


_IPC = _import_ipc()
# Registry + project root come from ipc.py when available (single source of truth),
# else legacy project-local defaults (unchanged behaviour).
REGISTRY = _IPC._REGISTRY if _IPC else os.path.join(ROLE_DIR, "ipc_roles.json")
PROJECT_ROOT = _IPC.PROJECT_ROOT if _IPC else os.path.dirname(ROLE_DIR)
# Command the injected contexts tell agents to run. When ipc.py is NOT in cwd
# (user-level install in ~/.claude/ipc), a bare `python ipc.py` fails, so inject
# the absolute path. Legacy: still absolute (harmless, more robust).
_IPC_CMD = f'python "{_IPC.__file__}"' if _IPC else "python ipc.py"
LOCK = REGISTRY + ".lock"
# Role universe + hub identity come from ipc.py — the SINGLE source of truth, so
# this module and ipc.py can never disagree about which roles exist or which one
# is the master/hub (the IPC_HUB-vs-hardcoded-A split-brain). Legacy fallback
# only when ipc.py couldn't be imported.
ROLES = _IPC.ROLES if _IPC else ("A", "B", "C", "D")  # hub + worker slots
HUB = _IPC.HUB if _IPC else os.environ.get("IPC_HUB", "A")  # the master/hub role


def _opted_in():
    """Does the CURRENT project opt into IPC? Gates the user-level GLOBAL hook so
    it stays inert in projects that don't use IPC (and never silently grabs a role
    in a random project the user opened). Opt-in signals, any one suffices:
      - IPC_ROLE env set (explicit launch intent)
      - a `.claude/ipc.enabled` marker file in the project root
      - a project-local install exists (.claude/hooks/ipc_role.py) — legacy opt-in
      - a mailbox already exists for this project (ipc.py's DB_PATH)"""
    if os.environ.get("IPC_ROLE"):
        return True
    if os.path.exists(os.path.join(PROJECT_ROOT, ".claude", "ipc.enabled")):
        return True
    if os.path.exists(os.path.join(PROJECT_ROOT, ".claude", "hooks", "ipc_role.py")):
        return True
    if _IPC and os.path.exists(_IPC.DB_PATH):
        return True
    return False


def _is_redundant_global():
    """True if I'm the user-level copy while a project-LOCAL hook also exists: defer
    to the local one so the role isn't double-claimed during the transition window."""
    local = os.path.join(PROJECT_ROOT, ".claude", "hooks", "ipc_role.py")
    return (os.path.exists(local)
            and os.path.normcase(os.path.abspath(__file__)) != os.path.normcase(os.path.abspath(local)))

# Behavior injected into each session so no manual /main or /sub is needed.
# Parameterized by the hub role so it tracks IPC_HUB (no hardcoded "A").
def _master_context(hub):
    return (
        f"[IPC role: you are master terminal {hub} (master / star hub)] This project uses "
        f"ipc.py(_ipc.db) to collaborate with worker terminals (star topology: "
        f"you are the sole hub; workers don't talk to each other). See CLAUDE.md for the "
        f"protocol. You are the initiator/decider. Dispatch to one worker: "
        f"`python ipc.py send --from {hub} --to <worker> \"<task>\"`; to several at once: `--to B,C`; "
        f"broadcast to all live workers: `--to ALL`. [LOCAL TRIAL 2026-06-27] To listen for "
        f"replies, start ONE persistent Monitor (the Monitor tool, persistent=true) running "
        f"`python ipc.py watch --me {hub}`: each reply fires a tiny SIGNAL notification "
        f"(`NEW MSG #id from ...`, never the body — avoids notification truncation of long "
        f"messages); on the signal, read the FULL message with `python ipc.py peek --me {hub} "
        f"--tail 3`. The watcher lives the whole session and idle costs ~zero turns (only "
        f"fires on a real message — no re-arming). Track multi-worker fan-out "
        f"completion (how many of N replied) in your harness task list (TaskCreate/TaskList), "
        f"which survives context compaction. FALLBACK if Monitor is unavailable: "
        f"`python ipc.py recv --me {hub} --block` as a background Bash (exits 0 = message, read it; "
        f"2 = empty timeout, re-arm without reading), or `--block --count N` to barrier-collect "
        f"N replies in one call. Only use watch/recv/peek to receive; never Read the whole "
        f"_ipc.db. This role is auto-assigned; no need to type /main."
    )


def _worker_context(role, hub):
    return (
        f"[IPC role: you are worker terminal {role} (subordinate / worker)] This project "
        f"uses ipc.py(_ipc.db) to collaborate with master terminal {hub} (star topology: you "
        f"talk only to {hub}, never to other workers). See CLAUDE.md for the protocol. Enter "
        f"standby immediately — WATCHER FIRST, then work: (1) start ONE persistent Monitor "
        f"(the Monitor tool, persistent=true) running `python ipc.py watch --me {role}` as "
        f"your standby watcher, then end this turn. Starting the watcher BEFORE touching any "
        f"backlog matters: the task-lease reaper AND-joins your heartbeat with the lease "
        f"ceiling, so work done without a beating watcher reads as stale and gets requeued "
        f"mid-flight. Any queued backlog arrives as signals within seconds — no separate "
        f"drain step. (2) Each task fires a tiny SIGNAL notification (`NEW MSG #id from ...`, "
        f"never the body — avoids truncation); on the signal, read the FULL task with "
        f"`python ipc.py peek --me {role} --tail 3`, execute, send the result back with "
        f"`python ipc.py send --from {role} --to {hub} \"<result summary>\"` (or --body-file "
        f"<file> for bodies containing backticks/$/quotes), then end your turn; the same "
        f"Monitor keeps listening (no re-arm), idle costs ~zero turns. Prefer ONE watcher "
        f"per inbox for clarity (double-delivery is structurally impossible either way: "
        f"claims are one atomic UPDATE...RETURNING). FALLBACK if Monitor is NOT in your "
        f"tool list (and no ToolSearch to load it — common on non-Anthropic bridges, e.g. "
        f"deepseek, which get a flattened toolset): do NOT hunt for Monitor — park "
        f"`python ipc.py recv --me {role} --block` as a background Bash and re-arm each "
        f"wake (exit 0 = task/read it; exit 2 = timeout/re-arm without reading; killed = "
        f"peek --tail 3 for a missed message, then re-arm). On the fallback your heartbeat "
        f"dies the moment a task is delivered, so during any task longer than ~1 minute run "
        f"`python ipc.py ack --me {role}` every few minutes — ack renews the lease AND beats "
        f"the heartbeat, keeping the reaper off your in-flight task. HARD RULE: send exactly "
        f"one reply to {hub} for EVERY message you receive — even a test, a greeting, or "
        f"'received, no action needed'; never consume (mark read) a message without "
        f"replying, or {hub}'s watcher waits forever. Stop when done; no chit-chat, don't "
        f"decide on {hub}'s behalf. Only use watch/recv/peek; never Read the whole _ipc.db. "
        f"This role is auto-assigned; no need to type /sub."
    )


_ROLE_CMD = f'python "{os.path.abspath(__file__)}"'

NONE_CONTEXT = (
    "[IPC role: none] A and all worker roles in this project are already taken; this "
    "terminal does not join the multi-terminal collaboration — use it as a normal "
    "session. If a terminal is confirmed closed but still shows as occupied, run "
    f"`{_ROLE_CMD} reclaim-dead` to free only the slots whose watcher heartbeat is "
    "gone (do NOT use `reset` — it wipes LIVE roles too), then reopen this terminal."
)


RECOVER_CONTEXT = (
    "[IPC role: recovery needed] This terminal was cleared/resumed/compacted and its "
    "previous IPC role could NOT be auto-matched (/clear changes the session_id, so the "
    "registry can't map you back). The role slot is still held under your old session, so "
    "the auto-assign deliberately did NOT grab you a new role (that's how a cleared B used "
    "to wrong-turn into D). Recover explicitly: if you are a worker, run `/ipc-recover B` "
    "(use your real letter), or manually start a persistent Monitor `python ipc.py watch "
    "--me B` FIRST (any backlog arrives as signals; read with peek; watcher-first keeps "
    "the lease reaper from requeueing work you do heartbeat-less). If you are the hub, you are A — continue as hub "
    "(or `/main`). Don't claim a different role."
)


def _context_for(role):
    if role == HUB:
        return _master_context(HUB)
    if role:
        return _worker_context(role, HUB)
    return NONE_CONTEXT


def _read_stdin():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _ensure_registry_dir():
    """Create the registry's dir on first use (user-level per-cwd dir may not exist
    yet). Only ever called on a participating project, so non-IPC projects stay clean."""
    try:
        os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    except OSError:
        pass


def _lock():
    """Crude cross-platform lock: exclusive-create a lockfile, retry briefly.
    Breaks a stale lock (>30s old) left behind by a process killed mid-claim,
    so a leftover LOCK file can't wedge every future claim into the unlocked path."""
    _ensure_registry_dir()
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


_CONTINUATION = ("clear", "resume", "compact")  # same terminal continuing, NOT a new one

# Liveness reconciliation: the registry says who OWNS a slot, the heartbeat says
# who is actually LISTENING. They drift (a claim survives /clear and hard-kills;
# a watcher can be a ghost). These thresholds let claim() treat a registry entry
# whose watcher is gone as reclaimable, so a dead claim (e.g. a 2-day-old A) no
# longer permanently blocks the slot.
WATCHER_MAX_AGE = 60     # a heartbeat older than this => the watcher is gone
CLAIM_GRACE = 120        # a claim with NO heartbeat yet is dormant only after this
                         # (gives a just-started owner time to park its watcher)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _heartbeat_path(role):
    """Heartbeat file for `role`, resolved by ipc.py (so it matches whether state
    is project-local or in the user-level per-cwd dir); legacy fallback otherwise."""
    if _IPC:
        return _IPC._heartbeat_path(role)
    return os.path.join(PROJECT_ROOT, f"_watcher_{role}.alive")


def _watcher_age(role):
    """Seconds since `role`'s watcher last beat, or None if there is no heartbeat."""
    try:
        return time.time() - os.path.getmtime(_heartbeat_path(role))
    except OSError:
        return None


def _claim_age(claim):
    try:
        return time.time() - time.mktime(time.strptime(claim["ts"], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, ValueError, TypeError):
        return None


def _is_dormant(reg, role, sid):
    """True if `role` is claimed by someone OTHER than `sid` whose watcher is dead.
    Heartbeat is the liveness truth: a fresh heartbeat => live (not dormant); a
    stale heartbeat => dormant; no heartbeat => dormant only once the claim is old
    enough that the owner should have parked a watcher (avoids evicting a peer that
    just started and hasn't beaten yet)."""
    claim = reg.get(role)
    if not claim or claim.get("session_id") == sid:
        return False
    age = _watcher_age(role)
    if age is not None:
        return age > WATCHER_MAX_AGE
    ca = _claim_age(claim)
    return ca is None or ca > CLAIM_GRACE


def _take_auto(reg, sid):
    """Lowest truly-free slot; if none, reclaim the lowest DORMANT slot so a
    terminal is never permanently locked out by dead claims. Does NOT auto-evict
    a live peer (use `take` for deliberate reassignment)."""
    for r in ROLES:
        if not reg.get(r):
            reg[r] = {"session_id": sid, "ts": _now()}
            _save(reg)
            return r
    for r in ROLES:
        if _is_dormant(reg, r, sid):
            reg[r] = {"session_id": sid, "ts": _now()}
            _save(reg)
            return r
    return None


def claim():
    info = _read_stdin()
    sid = info.get("session_id") or "unknown"
    source = info.get("source", "")
    # Gate: stay completely inert in projects that don't use IPC (lets the
    # user-level GLOBAL hook be registered once without claiming a role in every
    # project the user opens), and defer to a project-local hook if one also runs.
    if not _opted_in() or _is_redundant_global():
        return                                # inject nothing; not participating
    want = os.environ.get("IPC_ROLE")         # explicit launch-time intent, if any
    locked = _lock()
    try:
        reg = _load()
        role = _owned_role(reg, sid)          # reuse if this session already owns a role
        if role is None and want in ROLES:
            # Explicit intent overrides launch-order roulette: this window claims
            # exactly `want` (evicting a stale/foreign holder). The user is telling
            # us which window is the hub; identity-stamped heartbeats + `status`
            # surface any remaining live conflict to resolve.
            for r in ROLES:                   # don't hold two slots
                if reg.get(r) and reg[r].get("session_id") == sid:
                    reg[r] = None
            reg[want] = {"session_id": sid, "ts": _now()}
            _save(reg)
            role = want
        elif role is None and source not in _CONTINUATION:
            # Genuinely NEW terminal: lowest free slot, else reclaim a dormant one.
            # On clear/resume/compact the terminal is CONTINUING and /clear changes
            # the session_id, so we deliberately do NOT grab a fresh slot here (that
            # was the bug that turned a cleared B into D); recover via /ipc-recover.
            role = _take_auto(reg, sid)
    finally:
        if locked:
            _unlock()
    if role is not None:
        _inject(_context_for(role))
    elif source in _CONTINUATION:
        _inject(RECOVER_CONTEXT)              # cleared/resumed but role unmatched: recover explicitly
    else:
        _inject(NONE_CONTEXT)                 # genuinely no free role for a new terminal


def release():
    info = _read_stdin()
    sid = info.get("session_id") or "unknown"
    reason = info.get("reason", "")
    if reason == "clear":
        return                                 # /clear continues the session; keep role
    if _is_redundant_global():
        return                                 # the project-local hook owns release here
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


def take(role, sid):
    """Deliberately (re)assign `role` to session `sid`, evicting any prior holder
    and freeing whatever slot `sid` currently holds. This is the registry-level
    counterpart to /main and /sub: a slash command can call it so asserting a role
    actually updates ownership instead of only changing behaviour (the split-brain
    that caused this round's two-A mess). `sid` should be the real session_id when
    available (--session / CLAUDE_SESSION_ID); a 'manual' fallback still occupies
    the slot correctly, it just won't auto-reconnect across /clear."""
    if role not in ROLES:
        print(f"unknown role: {role!r} (valid: {','.join(ROLES)})", file=sys.stderr)
        sys.exit(1)
    locked = _lock()
    try:
        reg = _load()
        for r in ROLES:                       # release any slot this session holds
            if reg.get(r) and reg[r].get("session_id") == sid:
                reg[r] = None
        prev = reg.get(role)
        reg[role] = {"session_id": sid, "ts": _now()}
        _save(reg)
    finally:
        if locked:
            _unlock()
    evicted = f" (evicted {prev['session_id'][:8]})" if prev else ""
    print(f"OK {sid[:8]} now holds {role}{evicted}")


def reclaim_dead():
    """Free every slot whose watcher heartbeat is gone (dormant), so dead claims
    stop blocking slots. Safer than `reset`: live roles are kept."""
    locked = _lock()
    freed = []
    try:
        reg = _load()
        for r in ROLES:
            if _is_dormant(reg, r, None):     # None never matches a real session_id
                freed.append(r)
                reg[r] = None
        _save(reg)
    finally:
        if locked:
            _unlock()
    print("reclaimed dormant: " + (",".join(freed) if freed else "(none)"))


def status_view():
    """Reconciled view: registry ownership X heartbeat liveness, the one place
    that shows both truths together so a ghost (heartbeat with no/old claim) or a
    dormant claim (claim with no heartbeat) is obvious at a glance."""
    reg = _load()
    print(f"{'role':4} {'owner':10} {'claim-age':>9} {'watcher':>8}  state")
    for r in ROLES:
        claim = reg.get(r)
        owner = (claim.get('session_id', '')[:8] if claim else '-')
        ca = _claim_age(claim) if claim else None
        wa = _watcher_age(r)
        ca_s = f"{ca:.0f}s" if ca is not None else "?"
        wa_s = f"{wa:.0f}s" if wa is not None else "none"
        if not claim:
            state = "FREE" if wa is None or wa > WATCHER_MAX_AGE else "SQUATTER (watcher, no claim)"
        elif wa is not None and wa <= WATCHER_MAX_AGE:
            state = "live"
        else:
            state = "DORMANT (reclaimable)"
        print(f"{r:4} {owner:10} {ca_s:>9} {wa_s:>8}  {state}")


def _inject(text):
    text = text.replace("python ipc.py", _IPC_CMD)  # absolute path for user-level
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }, ensure_ascii=False))


def _arg(flag):
    """Return the value following `flag` in argv, or None."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "claim"
    if cmd == "claim":
        claim()
    elif cmd == "release":
        release()
    elif cmd == "reset":
        reset()
    elif cmd == "take":
        role = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else ""
        sid = _arg("--session") or os.environ.get("CLAUDE_SESSION_ID") or "manual"
        take(role, sid)
    elif cmd in ("reclaim-dead", "reclaim_dead"):
        reclaim_dead()
    elif cmd == "status":
        status_view()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
