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
the multi-terminal-ipc skill, NOT enforced by ipc.py (a neutral mailbox). A tiny on-disk registry
(ipc_roles.json next to this script's parent)
tracks who owns what, keyed by Claude's session_id. To add more worker slots,
extend ROLES below.

  python ipc_role.py enable   # onboard the CURRENT project: validate the
                              # user-level install + create .claude/ipc.enabled
                              # (refuses home dir / ~/.claude)
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
        f"you are the sole hub; workers don't talk to each other). Protocol details: the "
        f"project AGENTS.md's IPC section if present, else the multi-terminal-ipc skill. "
        f"You are the initiator/decider. Dispatch to one worker: "
        f"`python ipc.py send --from {hub} --to <worker> \"<task>\"`; to several at once: `--to B,C`; "
        f"broadcast to all live workers: `--to ALL`. [2026-08-21, user decision] Park your "
        f"watcher NOW as your first action (the hub is no longer watcher-less): start ONE "
        f"persistent Monitor (the Monitor tool, persistent=true, timeout_ms 3600000) running "
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
        f"talk only to {hub}, never to other workers). Protocol details: the project "
        f"AGENTS.md's IPC section if present, else the multi-terminal-ipc skill. Enter "
        f"standby immediately — WATCHER FIRST, then work: (1) IDLE PARK (the default since "
        f"2026-10-22): run `python ipc.py recv --me {role} --block --signal-only --timeout 3500` "
        f"as a BACKGROUND Bash (run_in_background) and end this turn; it costs no turn while "
        f"parked and exits when a message lands (exit 0: the `NEW MSG #id` signals are in its "
        f"output file) or times out (exit 2: re-park). After handling a message, re-park with "
        f"the same command. Alternative: ONE persistent Monitor running `python ipc.py watch "
        f"--me {role}`. Starting the watcher BEFORE touching any "
        f"backlog matters: the task-lease reaper AND-joins your heartbeat with the lease "
        f"ceiling, so work done without a beating watcher reads as stale and gets requeued "
        f"mid-flight. Any queued backlog arrives as signals within seconds — no separate "
        f"drain step. (2) Each task fires a tiny SIGNAL notification (`NEW MSG #id from ...`, "
        f"never the body — avoids truncation); on the signal, read the FULL task with "
        f"`python ipc.py peek --me {role} --tail 3`, execute, send the result back with "
        f"`python ipc.py send --from {role} --to {hub} --in-reply-to <task id> --body-file "
        f"<file>` — the file OPENS with one `RESULT: <absolute path>` line per product you "
        f"wrote (send verifies it on disk; CLEAN => the hub gets a one-line DONE summary and "
        f"never reads the body). A question, partial work or failure gets NO RESULT line so "
        f"the hub reads it in full. Optional after the RESULT lines: a `NEW:` line, then one "
        f"`- ` item per line, each an assertion plus the consequence of ignoring it (open it with "
        f"FAIL: for a falsified approach, FACT: for a confirmed one, OBSERVED: for an unconfirmed "
        f"one; first line under 200 chars, detail indented below), evidence "
        f"only (output, line number, two conflicting sources), never speculation; it counts "
        f"against the task budget, and a brief that says 只交付 gets none. Then end your turn; the same "
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
# Placeholder identity used when a terminal has no CLAUDE_SESSION_ID in its tool
# shell (the common case for a manually-issued take/claim). It is NOT an identity:
# several unrelated terminals can carry it at once, so anything that reasons about
# "the same session" must special-case it — see take().
MANUAL_SID = "manual"


def _short_sid(sid):
    """Display form of a session id that stays distinguishable. A bare [:8]
    collides for codex: its thread ids are UUIDv7 (first 48 bits = unix ms), so
    every thread started within the same 65.5 s window shares the first 8 hex
    (2026-08-31: C and E both showed as `01a056af`, and pane C read E's
    legitimate slot as its own mis-registration). Head + tail, ASCII only."""
    sid = sid or ""
    return sid if len(sid) <= 14 else f"{sid[:8]}..{sid[-4:]}"


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


def _busy_path(role):
    """Path of `role`'s worker-busy heartbeat, or None on an ipc.py too old to
    have the split (in which case liveness degrades to .alive only — the
    pre-2026-07-31 behaviour, never worse)."""
    if _IPC and hasattr(_IPC, "_busy_path"):
        return _IPC._busy_path(role)
    return None


def _fresh(path):
    """Has `path` been touched within WATCHER_MAX_AGE? False for absent/None."""
    if not path:
        return False
    try:
        return (time.time() - os.path.getmtime(path)) <= WATCHER_MAX_AGE
    except OSError:
        return False


def _liveness_age(role):
    """Seconds since `role` last showed ANY sign of life, or None if it showed none.

    Since the 2026-07-31 heartbeat split there are TWO files, and this module must
    honour both or it will evict people who are working:
      _watcher_<role>.alive : a consumer is parked, waiting for new messages
      _worker_<role>.busy   : the worker is executing a claimed task
    A worker on the bash-fallback path has only the SECOND one — `recv --block`
    deletes .alive the moment it hands the task over. Judging liveness on .alive
    alone therefore reads a busy worker as DORMANT once its claim passes
    CLAIM_GRACE (120s), and _sweep_dormant (which runs on EVERY new terminal's
    claim()) or `reclaim-dead` then hands its role to someone else while it is
    still mid-task — two sessions on one inbox, messages claimed by the wrong one.
    Found by worker B's review of the split (finding H1); the pre-existing warning
    that ipc.py and ipc_role.py judge liveness on different criteria is exactly
    the trap this walked into.
    Returns the age of the FRESHER of the two, so either signal keeps a role alive."""
    ages = []
    for path in (_heartbeat_path(role), _busy_path(role)):
        if not path:
            continue
        try:
            ages.append(time.time() - os.path.getmtime(path))
        except OSError:
            pass
    return min(ages) if ages else None


# Back-compat alias: this used to be the only liveness probe and is referenced by
# name in notes and older recipes.
_watcher_age = _liveness_age


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
    just started and hasn't beaten yet).
    The HUB slot is NEVER dormant: even though the hub parks a watcher at boot
    since 2026-08-21, a dead heartbeat still says nothing definitive about
    whether the hub is alive (it may be mid-turn with the watcher lost to a
    /clear). Auto-reclaiming it hands the hub role out by launch-order
    roulette — hub identity must change only deliberately
    (`take <hub>` / /main / IPC_ROLE)."""
    if role == HUB:
        return False
    claim = reg.get(role)
    if not claim or claim.get("session_id") == sid:
        return False
    age = _watcher_age(role)
    ca = _claim_age(claim)
    if age is not None and ca is not None and age > ca:
        # The heartbeat file predates this claim: it is the PREVIOUS owner's
        # remains (`recv --keep-heartbeat` leaves .alive behind by design and
        # nothing clears it on relaunch), not evidence about the current
        # claimant. Judge the new claim by its own grace window instead.
        # 2026-08-31: a 16h-old _watcher_C.alive made E's SessionStart sweep
        # null B/C/F's seconds-old claims before their watchers parked.
        age = None
    if age is not None:
        return age > WATCHER_MAX_AGE
    return ca is None or ca > CLAIM_GRACE


def _sweep_dormant(reg, sid):
    """Lazy registry GC: null EVERY dormant slot (dead worker claims; the hub is
    exempt via _is_dormant), not just the one a claimer needs — so ghost residue
    never outlives the next claim(). Mutates `reg`; returns the freed roles
    (caller saves iff non-empty)."""
    freed = [r for r in ROLES if _is_dormant(reg, r, sid)]
    for r in freed:
        reg[r] = None
    return freed


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


def _auto_claim_allowed():
    """User rule 2026-08-09: roles are auto-claimed ONLY inside a WezTerm pane
    (`WEZTERM_PANE`) or with an explicit `IPC_ROLE`; a bare terminal never claims.
    Until 2026-09-02 this guard lived only in Claude's settings.json hook line, so
    the Codex hooks (~/.codex/hooks.json → codex-compat.js, and project-level
    .codex/hooks.json) still auto-claimed a free role in any `ipc.enabled` project
    — a standalone `codex` in AI财务分析 silently became C. Enforced here so every
    host goes through the same gate. Child sessions (subagents) never claim."""
    if os.environ.get("CLAUDE_CODE_CHILD_SESSION"):
        return False
    return bool(os.environ.get("IPC_ROLE") or os.environ.get("WEZTERM_PANE"))


def claim():
    info = _read_stdin()
    sid = info.get("session_id") or "unknown"
    source = info.get("source", "")
    # Gate 0: pane-or-explicit-role only (see _auto_claim_allowed).
    if not _auto_claim_allowed():
        return                                # bare terminal: inject nothing
    # Gate: stay completely inert in projects that don't use IPC (lets the
    # user-level GLOBAL hook be registered once without claiming a role in every
    # project the user opens), and defer to a project-local hook if one also runs.
    if not _opted_in() or _is_redundant_global():
        return                                # inject nothing; not participating
    want = os.environ.get("IPC_ROLE")         # explicit launch-time intent, if any
    locked = _lock()
    try:
        reg = _load()
        if _sweep_dormant(reg, sid):          # lazy GC: ghost claims die at the
            _save(reg)                        # next claim, whoever it's from
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
        if role is None:
            # 兜底（2026-08-21）：MANUAL_SID 不是身份（见其定义处注释），拿真实 sid
            # 永远匹配不上，于是 manual 持有的角色在 SessionEnd 从不被释放；注册表
            # 锚等的 owner 变化就永远不发生，watcher 变成孤儿——心跳照跳、
            # status --watch 报 ALIVE、--require-watcher 放行，任务掉进没人读的信箱。
            # 判别依据用 pane 自己声明的 IPC_ROLE，不去猜 sid：按 sid 匹配 manual
            # 会重蹈 2026-07-31 那个 bug（worker 的 take 把 hub 的 A 清空）。
            env_role = (os.environ.get("IPC_ROLE") or "").strip()
            if env_role in ROLES:
                cur = reg.get(env_role)
                owner = cur.get("session_id") if isinstance(cur, dict) else None
                # 只释放：无主 / 本会话 / manual。别人真实会话持有的槽一律不碰。
                if owner in (None, sid, MANUAL_SID):
                    role = env_role
                    # 留痕：兜底释放的判据是环境变量而非身份，万一误释放，这行是唯一线索
                    # （X 在 2026-08-21 复核 #538 指出：误释放此前无迹可查）。
                    try:
                        with open(os.path.join(os.path.dirname(REGISTRY),
                                               "release_fallback.log"), "a",
                                  encoding="utf-8") as _lg:
                            _lg.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} "
                                      f"release-fallback role={env_role} "
                                      f"owner_was={owner!r} stdin_sid={sid!r} "
                                      f"reason={reason!r}\n")
                    except OSError:
                        pass  # 留痕失败不能挡住释放本身
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
        # Release any slot this session already holds — "one session, one role".
        # That invariant may only be enforced on a REAL identity. MANUAL_SID is a
        # shared placeholder used by every terminal whose CLAUDE_SESSION_ID is not
        # exported into its tool shell, so two unrelated terminals both carrying it
        # are not the same session and must not evict each other.
        # Observed 2026-07-31: the hub held A as "manual"; a worker then ran
        # `take B --session manual`; this loop matched and nulled A out from under
        # a live hub, whose watcher correctly retired itself on the ownership
        # change — leaving the hub deaf. Skipping the sweep for the placeholder
        # costs only a stale duplicate claim (visible in `status`, cleared by
        # reclaim-dead once the heartbeat dies), which is far cheaper than
        # silently unseating a working terminal.
        if sid != MANUAL_SID:
            for r in ROLES:
                if reg.get(r) and reg[r].get("session_id") == sid:
                    reg[r] = None
        prev = reg.get(role)
        reg[role] = {"session_id": sid, "ts": _now()}
        _save(reg)
        # 留痕（2026-08-31 #1431）：C 槽在 15:22:07 被写成 manual，全部 agent
        # 记录里找不到执行者。release 有 release_fallback.log，take 却无迹可查。
        # 同目录（每项目 ipc 状态目录，与 ipc_roles.json 同处，不跨项目共享）。
        try:
            with open(os.path.join(os.path.dirname(REGISTRY), "take.log"), "a",
                      encoding="utf-8") as _lg:
                _lg.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} take role={role} "
                          f"sid={sid!r} prev={(prev or {}).get('session_id')!r} "
                          f"pid={os.getpid()} ppid={os.getppid()} cwd={os.getcwd()!r}\n")
        except OSError:
            pass  # 留痕失败不能挡住 take 本身
    finally:
        if locked:
            _unlock()
    evicted = f" (evicted {_short_sid(prev['session_id'])})" if prev else ""
    print(f"OK {_short_sid(sid)} now holds {role}{evicted}")


def enable():
    """One-command project onboarding: create the `.claude/ipc.enabled` opt-in
    gate for the CURRENT project (cwd-derived root) after validating the
    user-level install. The gate is the ONLY per-project artifact — machinery
    stays at ~/.claude/ipc, per-project state auto-creates on first use. Refuses
    the home dir and ~/.claude (every launch dir becomes a registered project;
    user config must never be one — same guard as ipc.py's _project_root)."""
    root = os.path.abspath(PROJECT_ROOT)
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    user_claude = os.path.normcase(os.path.join(home, ".claude"))
    rn = os.path.normcase(root)
    if rn == home or rn == user_claude or rn.startswith(user_claude + os.sep):
        print(f"REFUSED  {root} is the home dir / user config, never a project "
              f"root. cd into the real project root and re-run.")
        sys.exit(2)
    warn = []
    if _IPC is None:
        warn.append("ipc.py not importable beside this hook — user-level install "
                    "incomplete? (fix: python install_user.py from the repo)")
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        # Parse, don't grep raw text: inside the JSON file quotes are escaped
        # (ipc_role.py\" claim), so a raw substring probe false-warns.
        with open(settings, encoding="utf-8") as f:
            cfg = json.load(f)
        cmds = [h.get("command", "")
                for g in (cfg.get("hooks", {}).get("SessionStart") or [])
                for h in g.get("hooks", [])]
        if not any("ipc_role.py" in c and "claim" in c for c in cmds):
            warn.append(f"claim hook not registered in {settings} — roles won't "
                        "auto-assign (fix: python install_user.py)")
    except (OSError, ValueError, AttributeError):
        warn.append(f"{settings} unreadable — hook registration unverified")
    gate = os.path.join(root, ".claude", "ipc.enabled")
    if os.path.exists(gate):
        print(f"ALREADY ENABLED  {gate}")
    else:
        os.makedirs(os.path.dirname(gate), exist_ok=True)
        with open(gate, "w", encoding="utf-8") as f:
            f.write("IPC opt-in gate — presence turns on the multi-terminal "
                    "mailbox for this project. See the multi-terminal-ipc skill "
                    "or the claude-star-ipc CLAUDE.md for the protocol.\n")
        print(f"ENABLED  {gate}")
    for w in warn:
        print(f"  ! {w}")
    print("Next: open terminals with cwd = this project root (first in = hub, "
          "rest = workers; pin with IPC_ROLE=X). Type one line in each worker "
          "window to park its watcher. Verify: ipc_role.py status, then one "
          "hub->worker round-trip.")


def reclaim_dead():
    """Free every WORKER slot whose watcher heartbeat is gone (dormant), so dead
    claims stop blocking slots. Safer than `reset`: live roles are kept, and the
    hub slot is exempt (the hub parks a watcher since 2026-08-21, but a dead
    heartbeat still proves nothing — reassign the hub deliberately via
    `take <hub> --session <sid>` or /main). Safe to run any time."""
    locked = _lock()
    try:
        reg = _load()
        freed = _sweep_dormant(reg, None)     # None never matches a real session_id
        if freed:
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
    print(f"{'role':4} {'owner':14} {'claim-age':>9} {'alive':>8} {'via':>6}  state")
    for r in ROLES:
        claim = reg.get(r)
        owner = (_short_sid(claim.get('session_id', '')) if claim else '-')
        ca = _claim_age(claim) if claim else None
        wa = _liveness_age(r)
        # Which of the two heartbeats is carrying this role right now. Without
        # this the age column is ambiguous after the split, and "parked" vs
        # "working" is precisely the distinction an operator needs before
        # deciding whether a quiet worker is stuck or busy.
        bp = _busy_path(r)
        parked = _fresh(_heartbeat_path(r))
        busy = _fresh(bp) if bp else False
        via = "park+busy" if (parked and busy) else ("park" if parked
              else ("busy" if busy else "-"))
        ca_s = f"{ca:.0f}s" if ca is not None else "?"
        wa_s = f"{wa:.0f}s" if wa is not None else "none"
        if not claim:
            state = "FREE" if wa is None or wa > WATCHER_MAX_AGE else "SQUATTER (watcher, no claim)"
        elif wa is not None and wa <= WATCHER_MAX_AGE:
            state = "live (working)" if (busy and not parked) else "live"
        elif r == HUB:
            # Since 2026-08-21 the hub parks a watcher too, so a dead heartbeat
            # is visible as down — but hub identity is still never auto-reclaimed.
            state = "hub (watcher down - never auto-reclaimed)"
        else:
            state = "DORMANT (reclaimable)"
        print(f"{r:4} {owner:14} {ca_s:>9} {wa_s:>8} {via:>6}  {state}")


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
        sid = _arg("--session") or os.environ.get("CLAUDE_SESSION_ID") or MANUAL_SID
        take(role, sid)
    elif cmd == "enable":
        enable()
    elif cmd in ("reclaim-dead", "reclaim_dead"):
        reclaim_dead()
    elif cmd == "status":
        status_view()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
