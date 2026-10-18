#!/usr/bin/env python3
"""Lightweight SQLite mailbox for two Claude Code terminals in one project.

Two shells on the same machine can't share session context, so they exchange
messages through a tiny SQLite table. Each message has a single recipient and a
`handled` flag, so `recv` only ever returns the new messages addressed to you —
history never re-enters the context, which keeps token cost flat as the log grows.

CLI:
    python ipc.py init                       # create the DB (auto-runs on first use)
    python ipc.py send --from A --to B "msg" # queue a message
    python ipc.py send --from A --to B,C "m" # fan out to several workers (one row each)
    python ipc.py send --from A --to ALL "m" # broadcast to every other live role
    python ipc.py send --from A --to B --body-file m.md  # body from file (shell-metachar-safe)
    python ipc.py recv --me B                # print NEW messages for B, mark them read
    python ipc.py recv --me A --block        # wait until a message arrives, then print it
    python ipc.py recv --me A --block --count 3  # BARRIER: wait until 3 replies arrive (parallel fan-out)
    # recv --block exit code: 0 = returned message(s); 2 = empty timeout (lets a
    # backgrounded watcher skip re-reading output — shows as status=failed but is
    # a normal timeout, not an error). Non-block recv always exits 0.
    python ipc.py watch --me B               # run under the Monitor tool (persistent): emit a tiny SIGNAL
                                             # per new message (never the body — avoids notification
                                             # truncation); read full content with `peek`. One long-lived
                                             # watcher, ~zero idle turns, survives turns/user input (TRIAL)
    python ipc.py send --from A --to B "msg" --require-watcher  # refuse if B neither parked nor busy; QUEUED-BUSY if mid-task
    python ipc.py send --from A --to B "msg" --submit-id fx7    # idempotent: same key never double-queues (safe re-dispatch)
    python ipc.py send --from A --to B "msg" --no-requeue       # fail-closed: stale lease parks as NEEDS-REVIEW, never auto-requeued
    python ipc.py status --watch B           # is B's --block watcher parked? ALIVE/DOWN
    python ipc.py status --watch B --json    # one JSON object: alive/busy/owner_valid/parked/
                                             # reason — scripts gate on `parked`, never on
                                             # substring-matching the text line (2026-10-15)
    python ipc.py task --id 123 [--json]     # lifecycle state of ONE task by id: QUEUED |
                                             # IN_PROGRESS | DONE | FAILED | CANCELLED |
                                             # NEEDS-REVIEW | STALE | UNKNOWN. Read-only (no
                                             # reap, no archive); exit 1 when UNKNOWN
    python ipc.py peek --me B [--tail 5]     # show recent thread WITHOUT marking read
    python ipc.py recv/peek/pending ... --json  # NDJSON envelopes instead of text lines:
                                             # recv/peek {id,ts,from,type,task,session,body};
                                             # pending {id,to,ts,state,attempts}. Machine-
                                             # readable outcome (type/state) + session echo
                                             # (grok-build headless-envelope + sessionId ideas:
                                             # reply session != task claimant => worker
                                             # /clear-ed, its context is gone, re-brief)
    python ipc.py archive [--keep 50]        # move handled rows to messages_archive, keep the last N
    python ipc.py grade --task N --verdict rework --reason quality [--used-new brief]
                                             # hub's hand on the timing ledger: mark a used-but-
                                             # reworked reply (or undo with --verdict pass); see grade()

Topology: STAR with A at the hub is a CONVENTION, not enforced by this script.
A = master (initiates/decides); B, C, D... = workers (respond, then stop). The
"workers talk only to A, never to each other" rule that keeps the anti-echo
invariant linear (not N-squared) is enforced by the role prompts injected at
SessionStart + the multi-terminal-ipc skill — `send` itself is a neutral mailbox and
will deliver any sender->recipient pair (intentional: keeps test names and future
topologies open). Multi-recipient/ALL fan-out lets A dispatch or broadcast from
the hub; each recipient gets its own row with its own `handled` flag, so a
broadcast is never "consumed" by whoever reads first.

Names (sender/recipient/--me/--watch) must match [A-Za-z0-9_]+ : they become both
SQL values and on-disk heartbeat filenames (_watcher_<name>.alive), so the CLI
rejects path-separator/traversal characters before any filesystem touch.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_mod
import subprocess
import sys
import threading
import time
from datetime import datetime

# Print UTF-8 so CJK never hits the Windows console's GBK codec.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))  # where THIS ipc.py sits


# --- State location (Topic 1: user-level install + per-project isolation) -------
# Code location and state location used to be the same dir (everything resolved
# relative to __file__). That breaks once the machinery moves to ~/.claude (one
# shared dir would cross-wire every project). So state is resolved SEPARATELY:
#
#   * Legacy / project-local install -> a `_ipc.db` already sits next to this
#     script: keep using the script dir, so existing installs NEVER change.
#   * User-level install (script in ~/.claude/ipc, no db beside it) -> a per-cwd
#     dir ~/.claude/projects/<key>/ipc, key = deterministic hash of the project
#     root. We hash the NORMALIZED absolute path (never parse Claude's projects/
#     dir name — that encoding is inconsistent AND lossy: distinct CJK paths can
#     collapse to the same dashes and collide).
def _project_root():
    """Authoritative project root, independent of where the code lives.
    CLAUDE_PROJECT_DIR (exported by Claude Code) first; else walk up from cwd to
    a project marker; else cwd."""
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if root:
        return os.path.abspath(root)
    # cwd-walk heuristic (only when CLAUDE_PROJECT_DIR is absent, e.g. a manual
    # shell). The home dir and the user-level ~/.claude tree are NEVER a project
    # root: ~/.claude/CLAUDE.md and the ~/.claude dir both look like markers but
    # are user config. Matching them silently cross-wires unrelated terminals
    # into one mailbox (or drops a real-project terminal into ~/.claude) with no
    # error — the worst failure mode for cross-model interop. Skip them and keep
    # walking up. (For manual cross-terminal use, set CLAUDE_PROJECT_DIR.)
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    user_claude = os.path.normcase(os.path.join(home, ".claude"))
    d = os.path.abspath(os.getcwd())
    while True:
        dn = os.path.normcase(d)
        is_user_config = (dn == home or dn == user_claude
                          or dn.startswith(user_claude + os.sep))
        if not is_user_config and any(
                os.path.exists(os.path.join(d, m))
                for m in ("AGENTS.md", "CLAUDE.md", ".claude", ".git")):  # AGENTS.md: 2026-10-04 rule
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(os.getcwd())
        d = parent


def _project_key(root):
    """Stable, filesystem-safe, collision-resistant key for an absolute cwd."""
    norm = os.path.normcase(os.path.abspath(root))
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    base = os.path.basename(root.rstrip("\\/")) or "root"
    slug = "".join(c if c.isalnum() else "-" for c in base)[:32]  # ASCII slug, cosmetic
    return f"{slug}-{h}"


def _resolve_state_dir():
    # NB: compute the path only — do NOT create it here. This runs at import, and
    # the user-level global hook imports this in EVERY project; creating the dir
    # eagerly would litter ~/.claude/projects with empty ipc/ dirs for projects
    # that never opt in. Creation is lazy (see _ensure_state_dir), on first use.
    if os.path.exists(os.path.join(_HERE, "_ipc.db")):
        return _HERE                                  # legacy project-local: unchanged
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        _project_key(_project_root()), "ipc")


def _ensure_state_dir():
    """Create the state dir on first actual use (DB/registry/heartbeat write)."""
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        pass


_STATE_DIR = _resolve_state_dir()
_LEGACY = (_STATE_DIR == _HERE)
# The current project's root (where a `.claude/` and the IPC opt-in marker live).
# Legacy: the script dir IS the project root; user-level: derived from cwd/env.
PROJECT_ROOT = _HERE if _LEGACY else _project_root()
DB_PATH = os.path.join(_STATE_DIR, "_ipc.db")


_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def valid_name(name):
    """A terminal name is safe only if it is plain alnum/underscore: it becomes a
    heartbeat filename, so '..', '/', '\\' etc. must never reach the filesystem."""
    return bool(name) and _NAME_RE.match(name) is not None


def _require_valid(name, label):
    if not valid_name(name):
        print(f"BAD NAME  {label}={name!r} (must match [A-Za-z0-9_]+)")
        sys.exit(2)


def _heartbeat_path(who):
    return os.path.join(_STATE_DIR, f"_watcher_{who}.alive")


def watcher_alive(who, max_age, verify_owner=False):
    """True if `who`'s --block watcher refreshed its heartbeat within max_age
    seconds. Absent or stale file => not listening.

    verify_owner (used by require-watcher): also cross-check the heartbeat's
    session against the role registry's claimant, so a squatter/ghost process
    refreshing the heartbeat for a slot it doesn't own can't read as ALIVE.
    Best-effort: enforced ONLY when BOTH the heartbeat and the registry carry a
    session id. The watcher often has no CLAUDE_SESSION_ID in its env (heartbeat
    session = ""), and that case must NOT false-refuse a real watcher, so it
    falls back to liveness-only."""
    try:
        if (time.time() - os.path.getmtime(_heartbeat_path(who))) > max_age:
            return False
    except OSError:
        return False
    if verify_owner:
        hb_sess = (watcher_identity(who) or {}).get("session") or ""
        reg_sess = _registry_session(who)
        if hb_sess and reg_sess and hb_sess != reg_sess:
            return False
    return True


def _beat(who):
    """Refresh `who`'s heartbeat. Stamps identity (pid + session) alongside the
    timestamp so a stale/ghost watcher is DISTINGUISHABLE from the real owner:
    `status --watch` can show which pid/session is parked on a slot, and the role
    hook can cross-check the registry's claimant against who is actually beating.
    Body format is JSON; `watcher_alive` still uses mtime only, so this stays
    backward-compatible with the old plain-timestamp file."""
    try:
        _ensure_state_dir()
        payload = json.dumps({
            "ts": time.time(),
            "pid": os.getpid(),
            "session": os.environ.get("CLAUDE_SESSION_ID", ""),
        })
        with open(_heartbeat_path(who), "w") as f:
            f.write(payload)
    except OSError:
        pass  # heartbeat is best-effort; never let it break the watcher


def watcher_identity(who):
    """Return {'ts','pid','session'} of the process beating `who`'s heartbeat,
    or None if the file is absent/unreadable. Tolerates the legacy plain-float
    format (returns just the ts)."""
    try:
        with open(_heartbeat_path(who)) as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if isinstance(d, dict):
        return d
    # legacy plain-float heartbeat (a bare number is valid JSON -> float, not dict)
    try:
        return {"ts": float(raw), "pid": None, "session": ""}
    except ValueError:
        return None


# ---- worker-busy heartbeat: "alive and executing a claimed task" ------------
# Deliberately a SECOND file, not a reuse of _watcher_<who>.alive. The two answer
# different questions, and for a worker that is correctly busy they are OPPOSITE
# states:
#   _watcher_<who>.alive : a consumer is PARKED, ready to receive new work
#                          (gates send --require-watcher and --to ALL)
#   _worker_<who>.busy   : the worker is ALIVE and executing a claimed task
#                          (feeds the reaper's liveness test, _lease_alive)
# Conflating them is what let a worker doing 11 minutes of real work read as dead
# and have its in-flight task requeued underneath it (2026-07-31 incident: the
# bash-fallback `recv --block` deletes the heartbeat on delivery, the worker acked
# once and then executed heartbeat-less). The 2026-07-28 `--keep-heartbeat` patch
# closed the narrow version of this seam (a sub-8s re-arm gap); this closes the
# wide one (minutes of actual work).
# require-watcher gates on .alive first; since 2026-08-01 a fresh .busy is an
# accepted SECOND form of liveness evidence — the message is then QUEUED (printed
# as QUEUED-BUSY) instead of refused, because the busy beater is code-forked at
# claim and expires at done/fail/cancel/lease-ceiling, so it cannot lie about the
# worker existing. Refusal (WatcherDown) is reserved for neither-parked-nor-busy:
# no code-knowable evidence the recipient exists => still don't queue into a
# black hole. (Previously busy ALSO refused, which made a mid-task worker
# unreachable for new dispatch and forced the hub into poll-retry loops.)
_BUSY_DAEMON_MAX = 4 * 3600  # runaway guard; the daemon normally exits far sooner
_BUSY_POLL = 2.0
# Freshness window for "is a beater already running?". MUST be a small multiple of
# the poll interval, NOT the daemon lifetime: a beater that died (killed, machine
# slept) leaves a stale .busy behind, and if this guard were generous the next
# claim would decline to spawn a replacement while _lease_alive — which judges on
# _LEASE_MARGIN, 24s — has long since called the worker dead. The task would then
# be requeued with nothing beating for it. Two beaters racing into existence is
# harmless (both beat the same file, both exit when the tasks close); a missing
# beater is not, so this errs toward spawning.
_BUSY_FRESH = 3 * _BUSY_POLL
# Consecutive DB read failures after which the beater stops asserting liveness.
# Beating through a transient lock is right; beating forever on the strength of an
# error we cannot interpret is not — that would hide a dead worker indefinitely.
_BUSY_DB_ERR_LIMIT = 5


def _busy_path(who):
    return os.path.join(_STATE_DIR, f"_worker_{who}.busy")


def worker_busy(who, max_age):
    """True if a busy-beater refreshed `who`'s task heartbeat within max_age."""
    try:
        return (time.time() - os.path.getmtime(_busy_path(who))) <= max_age
    except OSError:
        return False


def _beat_busy(who):
    """Refresh `who`'s busy heartbeat. Same JSON shape as _beat, separate file."""
    try:
        _ensure_state_dir()
        with open(_busy_path(who), "w") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "pid": os.getpid(),
                "session": os.environ.get("CLAUDE_SESSION_ID", ""),
            }))
    except OSError:
        pass  # best-effort, exactly like _beat


def _busy_identity(who):
    """{'ts','pid','session'} of the process beating `who`'s busy heartbeat, or
    None. Concurrent beaters can interleave writes here, so treat a malformed
    payload as 'unknown pid' rather than an error — liveness itself never depends
    on this file's CONTENT, only on its mtime (see worker_busy)."""
    try:
        with open(_busy_path(who)) as f:
            d = json.loads(f.read().strip())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _clear_busy(who):
    try:
        os.remove(_busy_path(who))
    except OSError:
        pass


def _spawn_busy_beater(me):
    """Detach a child that beats _worker_<me>.busy until `me` has no open claimed
    task left. Spawned at CLAIM time by recv(), so liveness-while-working is a
    CONSEQUENCE of taking the work rather than something the worker must remember
    to do. That distinction is the whole point: this protocol exists to drive
    workers on other vendors' harnesses, whose behaviour is precisely what cannot
    be relied upon. No-op if a beater is already live for this role."""
    if worker_busy(me, _BUSY_FRESH):
        return
    _beat_busy(me)  # take the slot synchronously; the child refreshes from here
    argv = [sys.executable, os.path.abspath(__file__), "busy-daemon", "--me", me]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # Double fork (2026-09-21, #1521): the Monitor host kills its command as a
        # process TREE by walking ParentProcessId, so a direct child - even
        # DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB - dies with the watcher
        # (measured: beater pid 42112 died at TaskStop; a grandchild via this
        # launcher, pid 20708, kept beating). The launcher exits at once, so the
        # daemon's parent pid is dead before any tree walk can reach it.
        launcher = ("import subprocess,sys;subprocess.Popen(sys.argv[1:],"
                    "creationflags=0x00000208,stdin=subprocess.DEVNULL,"
                    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                    "close_fds=True)")
        argv = [sys.executable, "-c", launcher] + argv
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)
    except OSError:
        pass  # worst case we degrade to the pre-fix behaviour, never break recv


def _busy_daemon(me):
    """The detached beater. Beats while `me` holds work that is still within its
    lease, and exits otherwise. Four exit conditions, each closing a distinct hole
    found in worker B's review of the split (2026-07-31):

    (a) no open claimed task           -> the worker finished (done/fail) or the
        task was cancelled/requeued; stop looking busy within a poll or two.
    (b) every open task's hard ceiling has already fallen (M2) -> the reaper has
        judged them stale anyway, so continuing to beat would only disguise a DEAD
        worker as a live one and delay redelivery. Bounds the orphan window to the
        lease instead of to the runaway cap.
    (c) runaway cap reached AND nothing has runway left (M1) -> the cap must NOT
        kill a legitimately long task, so it is extended while some open task is
        still inside its lease. Tasks sent with `--lease 0` (lease_until NULL)
        have no runway to check and so stay bounded by the cap: that is the
        deliberate trade-off, since an unbounded beater on a lease-less task is a
        permanent black hole, which is strictly worse than a late requeue.
    (d) the DB stays unreadable -> a transient lock must not false-kill live work,
        but claiming liveness forever on the strength of an error is worse: after
        _BUSY_DB_ERR_LIMIT consecutive failures we stop asserting it.

    Ordering is beat -> evaluate -> sleep, and a quiescent round must be seen
    TWICE before exiting (M3): a task claimed in the gap between "no open tasks"
    and _clear_busy would otherwise find a still-fresh .busy, have its spawn
    suppressed by the freshness guard, and end up with no beater at all."""
    hard_cap = time.monotonic() + _BUSY_DAEMON_MAX
    errors = 0
    quiet_rounds = 0
    try:
        while True:
            _beat_busy(me)  # beat FIRST: never leave a gap before evaluating
            conn = None
            open_tasks = None
            try:
                conn = _conn()
                rows = conn.execute(
                    "SELECT id, lease_until FROM messages WHERE recipient=? "
                    "AND handled=1 AND tombstone IS NULL AND msg_type='task'",
                    (me,)).fetchall()
                open_tasks = [(r[0], r[1]) for r in rows if not task_done(conn, r[0])]
                errors = 0
            except sqlite3.Error:
                errors += 1
                if errors >= _BUSY_DB_ERR_LIMIT:
                    break                                   # (d)
            finally:
                # MUST close explicitly: `with conn:` is a TRANSACTION context
                # manager in sqlite3, not a closing one. Harmless everywhere else
                # in this file (short-lived CLI processes exit at once); in a loop
                # that can run for hours it would leak one connection per poll and
                # hold the DB file open against cleanup.
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

            if open_tasks is not None:
                if not open_tasks:
                    quiet_rounds += 1
                    if quiet_rounds >= 2:
                        break                               # (a)
                else:
                    quiet_rounds = 0
                    now = time.time()
                    if all(lu is not None and now >= lu for _tid, lu in open_tasks):
                        break                               # (b)
                    if time.monotonic() >= hard_cap:
                        if any(lu is not None and now < lu for _tid, lu in open_tasks):
                            hard_cap = time.monotonic() + _BUSY_DAEMON_MAX
                        else:
                            break                           # (c)
            elif time.monotonic() >= hard_cap:
                break
            time.sleep(_BUSY_POLL)
    finally:
        _clear_busy(me)
        # Close the residual window worker C found (finding C1): between this
        # daemon's last "no open tasks" read and the _clear_busy above, a task can
        # be claimed. That claim's _spawn_busy_beater would have seen a .busy file
        # still fresh from our final beat, suppressed itself — and then we deleted
        # the file, leaving the new task with no beater at all. The reaper's 24s
        # backstop would recover it, but by requeueing work mid-flight, which is
        # the exact disease this whole mechanism exists to cure. Now that the file
        # is gone the guard cannot misfire, so re-check and hand over.
        # The handover condition must mirror this daemon's OWN exit conditions,
        # not merely "a task row is open". Handing over on any open task respawns
        # a successor that immediately re-evaluates, stands down for the same
        # reason, hands over again... — a spawn loop every poll until the reaper
        # requeues the row. (Caught by test 8's stand-down time moving from 22s to
        # 48s while the assertion still passed.) Only work that still has runway
        # deserves a beater; a task whose ceiling has already fallen is exactly
        # what we just, deliberately, stopped beating for.
        try:
            conn = _conn()
            try:
                now = time.time()
                rows = conn.execute(
                    "SELECT id, lease_until FROM messages WHERE recipient=? "
                    "AND handled=1 AND tombstone IS NULL AND msg_type='task'",
                    (me,)).fetchall()
                if any(not task_done(conn, tid) and (lu is None or now < lu)
                       for tid, lu in rows):
                    _spawn_busy_beater(me)
            finally:
                conn.close()
        except sqlite3.Error:
            pass  # best-effort handover; the reaper remains the backstop


def _conn():
    _ensure_state_dir()  # lazy: create the per-cwd dir only when actually used
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # WAL lets one terminal read while the other writes without blocking.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            sender      TEXT NOT NULL,
            recipient   TEXT NOT NULL,
            body        TEXT NOT NULL,
            handled     INTEGER NOT NULL DEFAULT 0,
            in_reply_to INTEGER,                       -- task id this answers; NULL = originating
            msg_type    TEXT NOT NULL DEFAULT 'task',  -- task | reply | ack | broadcast
            status      TEXT NOT NULL DEFAULT 'sent',  -- sent | delivered  (code-set lifecycle)
            hop         INTEGER NOT NULL DEFAULT 0,    -- echo/relay counter
            ttl         INTEGER NOT NULL DEFAULT 4,    -- hop ceiling (see send())
            lease_until REAL,                          -- hard lease deadline (epoch s); NULL=pure-heartbeat lease
            lease_secs  INTEGER,                       -- sender's lease duration; claim resets lease_until=now+lease_secs
            attempts    INTEGER NOT NULL DEFAULT 0,    -- claim count; caps requeue at MAX_ATTEMPTS
            tombstone   TEXT,                          -- NULL=active; 'cancelled'|'failed'=terminal, excluded from active set
            submit_id   TEXT,                          -- idempotent dispatch key: same (sender,recipient,submit_id) never double-queues (tutti clientSubmitID idea)
            no_requeue  INTEGER NOT NULL DEFAULT 0,    -- 1 = fail-closed task: reaper never requeues/fails it, stale => NEEDS-REVIEW (tutti fail-closed idea)
            session     TEXT                           -- sender's CLAUDE_SESSION_ID at send time (grok-build sessionId-echo idea): reply session == original claimant => worker still holds task context (terse follow-up OK); differs => /clear happened, re-brief
        )"""
    )
    # Idempotent migration for a pre-existing DB created before these columns.
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, ddl in (
        ("in_reply_to", "INTEGER"),
        ("msg_type", "TEXT NOT NULL DEFAULT 'task'"),
        ("status", "TEXT NOT NULL DEFAULT 'sent'"),
        ("hop", "INTEGER NOT NULL DEFAULT 0"),
        ("ttl", "INTEGER NOT NULL DEFAULT 4"),
        ("lease_until", "REAL"),
        ("lease_secs", "INTEGER"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("tombstone", "TEXT"),
        ("submit_id", "TEXT"),
        ("no_requeue", "INTEGER NOT NULL DEFAULT 0"),
        ("session", "TEXT"),
        # 2026-09-19 RESULT-line protocol: a worker's linked reply whose body
        # opens with `RESULT: <absolute path>` lines is verified at send time
        # (path exists, is a file, mtime >= the task's dispatch ts) and stamped
        # result='CLEAN' / 'UNVERIFIED: <why>' / 'UNSOLVABLE: <why>' /
        # 'REPLAN: <why>' (NULL = no status line). The hub
        # watcher holds CLEAN replies (announced=0) instead of signalling
        # `NEW MSG` and flushes them as one-line `DONE #task <- X RESULT: path`
        # summaries — the hub never has to peek the body. announced defaults
        # to 1 so every legacy/non-CLEAN row is already "announced".
        ("result", "TEXT"),
        ("announced", "INTEGER NOT NULL DEFAULT 1"),
        # 2026-09-19 timing ledger: claimed_ts = first claim (epoch; queue wait vs
        # execution split), budget_min = `预算: N 分钟` from the brief (or
        # --budget) so the hub watcher can tell "may finish soon" from "long".
        ("claimed_ts", "REAL"),
        ("budget_min", "INTEGER"),
        # 2026-10-17 routing ledger: task_type = the brief's `类型:` value
        # (whitelist TASK_TYPES, else 未分类; tasks only); new_items = count of
        # the reply's NEW: items; anchor = `#task<N>.<sender>` on a linked
        # reply/fail, quotable in later briefs. NULL on older rows.
        ("task_type", "TEXT"),
        ("new_items", "INTEGER"),
        ("anchor", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
    # Race-free idempotency: the partial UNIQUE index makes the duplicate check a
    # DB invariant, not a SELECT-then-INSERT convention two concurrent senders
    # could slip past. Tombstoned rows leave the index, so an explicit
    # cancel/fail re-opens the key for a deliberate retry.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_submit_id "
        "ON messages(sender, recipient, submit_id) "
        "WHERE submit_id IS NOT NULL AND tombstone IS NULL"
    )
    # Generation tokens for orphan-watcher retirement (#3): each watch() startup
    # bumps its role's gen; a superseded watcher reads a higher gen next poll and
    # retires itself — no pid-kill (avoids Windows os.kill(pid,0) mis-fire), no
    # daemon. Pure new table, no migration needed (IF NOT EXISTS).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watcher_gen (
            role TEXT PRIMARY KEY,
            gen  INTEGER NOT NULL
        )"""
    )
    # Lazy column migration (2026-08-17, #1188 F2): CREATE TABLE IF NOT EXISTS
    # never extends an EXISTING table, so a new column must be ALTERed in.
    # last_seen_ts = epoch of the recipient's last code-knowable sign of life
    # on this task (stamped at claim, on ack, and when a linked reply/fail/ack
    # row arrives). NULL on pre-migration rows and after redeliver = unknown,
    # never judged. One PRAGMA per connection; idempotent.
    if not any(r[1] == "last_seen_ts"
               for r in conn.execute("PRAGMA table_info(messages)")):
        conn.execute("ALTER TABLE messages ADD COLUMN last_seen_ts REAL")
    return conn


class WatcherDown(Exception):
    """Raised by send() when require_watcher is set but the recipient's
    --block watcher is not parked/listening."""


class SquatterHeartbeat(WatcherDown):
    """Raised by send() when require_watcher is set, the recipient's heartbeat
    is FRESH, but the role has NO owner in the registry (and no busy beat): the
    beater is an ownerless squatter — e.g. an orphan watcher surviving a hard
    fleet kill (2026-08-17 #1186 incident: orphan pid beat B's slot for 2h47m
    after its session died at 07:22, passed the gate at 10:08:57, then died on
    its first signal print and rolled back the claim — task sat QUEUED
    attempts=0 with no consumer and no alarm). A fresh heartbeat alone cannot
    distinguish that orphan from a live worker; registry ownership is the
    tie-breaker, so parked-evidence now requires BOTH. args = (role, pid)."""


class LinkRefused(Exception):
    """A worker->hub reply/ack/fail could not be bound to a task: either the
    worker has several open tasks and passed no --in-reply-to (guessing linked
    F's #1568 'reply to #1566' onto #1567 on 2026-09-15, because #1566 had been
    auto-failed and the oldest-open heuristic skipped it), or the explicit
    --in-reply-to names a message that is not a task dispatched to this sender.
    Refused at send time (exit 3) so the hub never sees a silently mis-closed or
    never-closed task (E review 2026-09-16: four F results sent as unlinked
    notes left #1592/#1594/#1596/#1598 open -> stale REPLY-CLOCK alarms and
    requeues until auto-fail)."""


class StarViolation(Exception):
    """Raised by send() when a message breaks the star topology (worker->worker)
    or exceeds the echo/relay ceiling (hop > ttl). The star and 'only A decides'
    rules used to be prose convention; this makes them code-enforced so a worker
    driven by ANY model literally cannot relay to a peer or echo a loop."""


# The hub is the sole star center: workers may talk only to it, never to each
# other. Configurable so the topology isn't hard-coded to the literal "A".
HUB = os.environ.get("IPC_HUB", "A")

# Canonical role universe — the SINGLE source of truth for "which roles exist",
# consumed by ipc_role.py (registry/slot assignment) so the two modules can't
# disagree about the valid roles or which one is the hub. HUB is always part of
# it (a custom IPC_HUB gets a slot). send() itself stays a NEUTRAL mailbox and
# does NOT reject names outside ROLES (by design — keeps test names / ad-hoc
# topologies open, see module docstring); ROLES governs role ASSIGNMENT, not
# delivery.
# Letters = generic interactive model windows (hook assigns lowest free, in order).
# Letters FIRST so the hook never hands a named channel to a random new window.
# E = codex CLI slot since 2026-08-03 (registry placeholder-owned — codex has no
# SessionStart hook to claim it; keep the one-time `ipc_role.py take E` in place
# or the hook hands E to the 5th random interactive window). CODEX/DS named
# channels predate the WezTerm lineup (B=kimi C=glm D=ds E=codex) and are
# redundant since the 2026-08-03 daemon decommission — kept only for old queues.
# X = second-formation slot (user rule 2026-08-09): a SECOND wezterm window may
# join the fleet with exactly ONE pane holding exactly this role. Deliberately
# LAST so _take_auto's launch-order roulette never reaches it — X is claimed
# only via explicit IPC_ROLE=X. Plain worker otherwise (star topology: talks
# to the hub only).
_BASE_ROLES = ("A", "B", "C", "D", "E", "F", "CODEX", "DS", "X")
ROLES = _BASE_ROLES if HUB in _BASE_ROLES else (HUB,) + _BASE_ROLES


# --- Task lifecycle knobs (core-only round: 3 cols, no progress/resume layer) ---
# Lease = a claimed task must show progress before this hard deadline, counted from
# SEND time (send() pre-sets lease_until = now + lease). --lease 0 opts out to a
# pure-heartbeat lease (lease_until NULL -> alive iff the recipient's watcher beats).
# Two INDEPENDENT staleness judgments, do not conflate:
#   * REAP_MARGIN (3x max_age ~24s) guards the heartbeat race so an alive worker whose
#     beat falls between polls is NOT mis-reaped.  (防误判活 worker)
#   * LEASE        guards "alive but stuck" — the Monitor watch process keeps beating
#     even when the Claude session is frozen/compacted, so heartbeat alone can't see
#     stuck; the hard ceiling can.  (测卡死)
DEFAULT_LEASE = 1800
_LEASE_MARGIN = 24.0  # 3 * default max_age(8.0); reaper liveness tolerance
MAX_ATTEMPTS = int(os.environ.get("IPC_MAX_ATTEMPTS", "3"))  # module-level, env-overridable; per-task cap deferred (no 4th col)


def _lease_alive(recipient, lease_until, margin=_LEASE_MARGIN):
    """Is the lease on a claimed task still alive? AND-joined: alive iff the
    recipient's watcher is beating (process alive) AND the hard ceiling hasn't
    fallen. EITHER trip => stale, so the two failure modes are INDEPENDENT signals:
      * watcher dead            -> process died (heartbeat signal)
      * lease_until past        -> alive-but-stuck (Monitor keeps beating while the
        Claude session is frozen/compacted, so heartbeat alone can't see this; only
        the hard ceiling can — this is why --lease is a dispatch default)
    For pure-heartbeat tasks (lease_until None) the ceiling term is vacuously True,
    so alive reduces to watcher_alive — unchanged from pre-lifecycle behavior.
    margin=3x max_age guards the heartbeat poll race so an alive worker whose beat
    lands between polls isn't mis-reaped. See DEFAULT_LEASE comment.

    The liveness term is an OR over the two heartbeats (see the worker-busy block
    above): a worker PARKED on a watcher and a worker BUSY executing the claimed
    task are both alive, but they are mutually exclusive on the bash-fallback path
    — `recv --block` deletes .alive the moment it hands the task over. ANDing on
    .alive alone therefore declared a correctly-working worker dead and requeued
    its task mid-flight. The hard ceiling below is untouched and remains the only
    detector of alive-but-stuck, so this loosening cannot mask a frozen session."""
    if not (watcher_alive(recipient, margin) or worker_busy(recipient, margin)):
        return False
    if lease_until is not None and time.time() >= lease_until:
        return False  # hard ceiling fell: alive-but-stuck
    return True


def task_done(conn, tid):
    """Single authority for 'is task tid done?'. A task is done iff it has NO
    tombstone AND exists a reply/ack linked to it. msg_type='fail' replies do NOT
    count (a fail explains a failure, it doesn't complete the task) — this is the
    R3 line that keeps a failed task from mis-reading as done. pending,
    _oldest_unanswered_task and _reap_stale all call THIS so the predicate can't
    drift across sites."""
    row = conn.execute("SELECT tombstone FROM messages WHERE id=?", (tid,)).fetchone()
    if not row or row[0] is not None:
        return False
    # 2026-09-05: the closing reply/ack must come from the task's own recipient
    # (Codex design review, overall item 3: a reply from another worker that
    # named this task id used to complete it). Mirrored in _ARCHIVE_WHERE.
    return conn.execute(
        "SELECT 1 FROM messages r JOIN messages t ON t.id=r.in_reply_to "
        "WHERE r.in_reply_to=? AND r.msg_type IN ('reply','ack') "
        "AND r.sender=t.recipient LIMIT 1", (tid,)
    ).fetchone() is not None


# A QUEUED task nobody has claimed for this long, while its recipient is not
# even busy, is a black-hole alarm (#1186 incident: sent 10:08:57, consumer died
# 10:08:59, sat QUEUED attempts=0 with no signal anywhere until a human noticed
# at ~10:33). Surfaced by pending as QUEUED-STALLED — display-layer only, the
# reaper and gates are untouched.
_STALL_AFTER = 300
# A CLAIMED task whose recipient has shown no code-knowable sign of life
# (claim, ack, linked reply/fail — see last_seen_ts) for this long is flagged
# IN-PROGRESS-SILENT (#1188 type-2 incident: claimed at 10:14, the signal sat
# in a background-shell file for 1h55m, busy heartbeat fresh throughout —
# "working for two hours" and "never started" were indistinguishable).
# Display-layer only: legit long tasks look identical, so this NEVER requeues
# — the cure for a false flag is the worker's `ack --task N` (protocol: first
# action on receiving a task). Deliberately NOT merged into NEEDS-REVIEW,
# which is a reaper/lease lifecycle state; SILENT is a display inference.
_SILENT_AFTER = 1800


def task_state(conn, tid, recipient, handled, lease_until, tombstone,
               no_requeue=0, ts=None, last_seen_ts=None):
    """Derive the lifecycle state of a task row. No state column — everything is
    computed from (handled, lease_until, attempts, tombstone, in_reply_to, now,
    heartbeat). tombstone takes precedence over done/in_progress so cancelled/failed
    display correctly even if a stray late reply lands. Order matters:
        CANCELLED > FAILED > DONE > QUEUED(handled=0) > IN_PROGRESS(lease alive)
        > NEEDS-REVIEW(no_requeue) > STALE.
    NEEDS-REVIEW is the fail-closed terminal-of-attention for --no-requeue tasks:
    lease dead but the reaper deliberately leaves the claim in place (tutti:
    'the Run remains running and reconciliation is scheduled'). Exits via hub
    cancel, worker done/fail, or worker ack (revives to IN_PROGRESS)."""
    if tombstone == "cancelled":
        return "CANCELLED"
    if tombstone == "failed":
        return "FAILED"
    if task_done(conn, tid):
        return "DONE"
    if handled == 0:
        # QUEUED-STALLED: unclaimed past _STALL_AFTER while the recipient is
        # not busy either. A parked watcher claims within one poll interval, so
        # old-and-unclaimed means NO working consumer exists (dead, orphaned,
        # or beating-without-claiming) — the exact state that needs a human/hub
        # eye. A busy recipient legitimately queues work until it re-parks, so
        # busy suppresses the alarm (same margin as the reaper's liveness).
        if ts is not None and not worker_busy(recipient, _LEASE_MARGIN):
            try:
                age = time.time() - time.mktime(
                    time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            except (ValueError, OverflowError):
                age = 0
            if age > _STALL_AFTER:
                return "QUEUED-STALLED"
        return "QUEUED"  # attempts>0 means requeued; caller shows attempts separately
    if _lease_alive(recipient, lease_until):
        # NULL last_seen_ts = unknown (pre-migration claim or post-redeliver):
        # never judged — a wrong SILENT is noise, an absent one costs nothing
        # extra over the pre-F2 world.
        if (last_seen_ts is not None
                and (time.time() - last_seen_ts) > _SILENT_AFTER):
            return "IN-PROGRESS-SILENT"
        return "IN_PROGRESS"
    return "NEEDS-REVIEW" if no_requeue else "STALE"


def _open_tasks(conn, hub, worker):
    """[(id, hop)] of every non-terminal, unreplied task hub->worker, oldest first.
    send() auto-links a worker's reply ONLY when exactly one is open; with several
    it refuses (LinkRefused) instead of guessing, because the old oldest-first
    guess bound replies to the wrong task once an older one had been auto-failed
    (#1568 -> #1567, 2026-09-15). Uses task_done so 'answered' can't drift."""
    rows = conn.execute(
        "SELECT t.id, t.hop FROM messages t "
        "WHERE t.sender=? AND t.recipient=? AND t.msg_type='task' "
        "AND t.tombstone IS NULL ORDER BY t.id", (hub, worker)).fetchall()
    return [(tid, hop) for tid, hop in rows if not task_done(conn, tid)]


def _oldest_unanswered_task(conn, hub, worker):
    """Kept for callers that only need 'the' open task; None when zero or many."""
    opened = _open_tasks(conn, hub, worker)
    return opened[0] if len(opened) == 1 else None


def _dup_by_submit_id(conn, sender, recipient, submit_id):
    """Newest non-terminal row for this idempotency key, or None."""
    return conn.execute(
        "SELECT id FROM messages WHERE sender=? AND recipient=? AND submit_id=? "
        "AND tombstone IS NULL ORDER BY id DESC LIMIT 1",
        (sender, recipient, submit_id)).fetchone()


def send(sender, recipient, body, *, in_reply_to=None, msg_type=None, hop=None,
         ttl=4, require_watcher=False, max_age=8.0, lease=DEFAULT_LEASE,
         submit_id=None, no_requeue=False, budget=None):
    """Insert one message for a SINGLE recipient. Returns (id, created, queued_busy):
    created=False means submit_id matched an existing non-terminal row, which was
    reused instead of queued again (idempotent resend, tutti clientSubmitID idea).
    queued_busy=True means require_watcher was set, the recipient's watcher was
    NOT parked, but its busy heartbeat (_worker_<who>.busy, beaten by the
    code-forked claim daemon) was fresh — the recipient is provably alive and
    executing, so the message was QUEUED for delivery when it next parks/recvs
    (2026-08-01: previously this case was REFUSED, which made a busy worker
    unreachable for new work and forced the hub into poll-retry loops).
    - StarViolation if a non-hub addresses another non-hub, or hop > ttl.
    - WatcherDown if require_watcher is set and the recipient is neither parked
      nor busy (no code-knowable evidence it exists => don't queue into a black
      hole; that remains this flag's job).
    Classifies hub->worker as 'task' and worker->hub as 'reply' by default, and
    auto-links a reply to the sender's oldest unanswered task (setting hop =
    parent.hop + 1) so fan-out completion is computable in code.
    lease: hard lease seconds. 0/None => pure-heartbeat lease (lease_until NULL,
    alive iff recipient's watcher beats); >0 => a hard ceiling that catches
    alive-but-stuck. Stored as lease_secs (the sender's intent) and pre-set as
    lease_until=now+lease for the queued phase; claim RESETS lease_until to
    claim-time + lease_secs so the runway starts when the work starts (a task
    that waited out its lease in the queue no longer arrives pre-expired, and a
    requeued task retries under a fresh ceiling); ack() pushes it out.
    submit_id: idempotency key scoped to (sender, recipient). A resend with the
    same key is a no-op returning the existing row — this is what makes
    re-dispatch after a barrier timeout SAFE instead of a double-run risk. The
    dup fast-path runs BEFORE the watcher gate: an already-queued task must not
    be refused (or double-queued) just because the watcher blinked. Enforced by
    a partial UNIQUE index, so two racing senders can't both insert; an explicit
    cancel/fail tombstones the row out of the index and re-opens the key.
    no_requeue: fail-closed task (see _reap_stale): a stale claim is NEVER
    auto-requeued or auto-failed — it parks as NEEDS-REVIEW in pending until the
    hub cancels or the worker done/fail/ack-revives it. Use for non-idempotent
    work where a phantom second run is worse than waiting."""
    if sender != HUB and recipient != HUB:
        raise StarViolation(f"star topology forbids {sender}->{recipient} "
                            f"(hub={HUB}; set IPC_HUB to change)")
    if submit_id is not None:
        with _conn() as conn:
            dup = _dup_by_submit_id(conn, sender, recipient, submit_id)
        if dup:
            return dup[0], False, False
    queued_busy = False
    if require_watcher:
        parked = watcher_alive(recipient, max_age, verify_owner=True)
        # Ownerless-squatter gate (2026-08-17, #1186 incident): a fresh
        # heartbeat whose role has NO registry owner is NOT parked-evidence.
        # An orphan watcher surviving a hard fleet kill beats indistinguishably
        # from a live worker (its own anchors can all fail: parent pins an
        # intermediate shell, a hard kill runs no SessionEnd so the registry
        # never changes, and no successor bumps the gen), then dies on its
        # first signal print and rolls the claim back — the task black-holes
        # AFTER the gate approved. Registry ownership ("manual" counts) is the
        # code-knowable tie-breaker every recovered worker has; requiring it
        # turns that silent black hole into an actionable refusal. The busy
        # fallback below is untouched, so a mid-task worker that never claimed
        # the registry still queues as QUEUED-BUSY rather than refusing.
        squatter = parked and _registry_session(recipient) is None
        if squatter or not parked:
            # Not parked (or parked-but-ownerless). A fresh busy heartbeat is
            # code-forked at claim time and expires at
            # done/fail/cancel/lease-ceiling, so it is strong (not perfect: an
            # orphan daemon whose session died keeps beating until the lease
            # ceiling — a BOUNDED window, C review B-2 2026-08-01) evidence
            # the worker exists — queue instead of refusing. Only
            # neither-parked-nor-busy (provably absent) still refuses.
            # Same max_age as the .alive check, NOT _BUSY_FRESH: status --watch
            # judges busy on max_age too, and the two gates disagreeing (status
            # says BUSY, send says REFUSED) is observable boundary jitter (C
            # review B-1). _BUSY_FRESH stays what it was built for: the spawn
            # guard.
            if worker_busy(recipient, max_age):
                queued_busy = True
            elif squatter:
                raise SquatterHeartbeat(
                    recipient, (watcher_identity(recipient) or {}).get("pid"))
            else:
                raise WatcherDown(recipient)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lease_secs = None if (lease is None or lease <= 0) else int(lease)
    lease_until = None if lease_secs is None else time.time() + lease_secs
    with _txn() as conn:
        if msg_type is None:
            msg_type = "reply" if (sender != HUB and recipient == HUB) else "task"
        worker_to_hub = (sender != HUB and recipient == HUB
                         and msg_type in ("reply", "ack", "fail"))
        if worker_to_hub and in_reply_to is None and msg_type == "reply":
            opened = _open_tasks(conn, recipient, sender)  # recipient=hub
            if len(opened) > 1:
                ids = ", ".join(f"#{t}" for t, _h in opened)
                raise LinkRefused(
                    f"{sender} has {len(opened)} open tasks from {recipient} "
                    f"({ids}); pass --in-reply-to <task id> so the result closes "
                    f"the right one")
            if opened:
                in_reply_to = opened[0][0]
                if hop is None:
                    hop = (opened[0][1] or 0) + 1
        elif worker_to_hub and in_reply_to is not None:
            pr = conn.execute("SELECT recipient, msg_type FROM messages WHERE id=?",
                              (in_reply_to,)).fetchone()
            if not pr:
                raise LinkRefused(f"--in-reply-to {in_reply_to}: no such message")
            if pr[1] != "task":
                raise LinkRefused(f"--in-reply-to {in_reply_to}: #{in_reply_to} is a "
                                  f"{pr[1]}, not a task")
            if pr[0] != sender:
                raise LinkRefused(f"--in-reply-to {in_reply_to}: task #{in_reply_to} "
                                  f"was dispatched to {pr[0]}, not {sender}")
        if hop is None:
            if in_reply_to is not None:
                pr = conn.execute("SELECT hop FROM messages WHERE id=?",
                                  (in_reply_to,)).fetchone()
                hop = ((pr[0] if pr else 0) or 0) + 1
            else:
                hop = 0
        if hop > ttl:
            raise StarViolation(f"echo ceiling hit: hop {hop} > ttl {ttl} "
                                f"({sender}->{recipient})")
        # OR IGNORE closes the fast-path TOCTOU: if a racing sender inserted the
        # same submit_id between our check and here, the unique index swallows
        # this insert and we return the winner's row.
        result = None
        if worker_to_hub and in_reply_to is not None and msg_type == "reply":
            result = _verify_result(conn, body, in_reply_to)
        budget_min = task_type = new_items = anchor = None
        if msg_type == "task":
            budget_min = budget if budget is not None else _extract_budget(body)
            task_type = task_type_of(body)
        if worker_to_hub and in_reply_to is not None and msg_type in ("reply", "fail"):
            anchor = _anchor(in_reply_to, sender)
            if msg_type == "reply":
                new_items = count_new_items(body)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(ts, sender, recipient, body, in_reply_to, msg_type, status, hop, ttl, "
            "lease_until, lease_secs, submit_id, no_requeue, session, result, budget_min, "
            "task_type, new_items, anchor) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, sender, recipient, body, in_reply_to, msg_type, "sent", hop, ttl,
             lease_until, lease_secs, submit_id, 1 if no_requeue else 0,
             os.environ.get("CLAUDE_SESSION_ID") or None, result, budget_min,
             task_type, new_items, anchor),
        )
        if cur.rowcount == 0 and submit_id is not None:
            dup = _dup_by_submit_id(conn, sender, recipient, submit_id)
            if dup:
                return dup[0], False, False
        # A linked reply/fail/ack from the worker is a sign of life on the
        # parent task: refresh its last_seen_ts so IN-PROGRESS-SILENT clears
        # (2026-08-17 F2). Tasks/notes linked via in_reply_to (hub follow-ups)
        # say nothing about the WORKER's liveness, so they don't stamp.
        if in_reply_to is not None and msg_type in ("reply", "fail", "ack"):
            conn.execute("UPDATE messages SET last_seen_ts=? WHERE id=?",
                         (time.time(), in_reply_to))
        if worker_to_hub and in_reply_to is not None and msg_type in ("reply", "fail"):
            _log_timing(conn, cur.lastrowid, in_reply_to, sender, msg_type, result, ts,
                        new_items=new_items or 0)
        return cur.lastrowid, True, queued_busy


# Star-topology fan-out: A may address several workers at once. Recipients can be
# a comma list ("B,C") or the literal "ALL" (every currently-claimed role except
# the sender, read from the role registry the SessionStart hook maintains).
_REGISTRY = (os.path.join(_HERE, ".claude", "ipc_roles.json") if _LEGACY
             else os.path.join(_STATE_DIR, "ipc_roles.json"))


def _claimed_roles():
    """Roles currently held by a live session, per .claude/ipc_roles.json."""
    try:
        with open(_REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
        return [r for r, v in data.items() if v]
    except (OSError, ValueError):
        return []


def _registry_session(role):
    """session_id that owns `role` in the registry, or None. Lets require-watcher
    cross-check the heartbeat's stamped session against the claimed owner."""
    try:
        with open(_REGISTRY, encoding="utf-8") as f:
            v = json.load(f).get(role)
        return v.get("session_id") if isinstance(v, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def expand_recipients(recipient, sender, max_age=8.0):
    """Resolve a --to value into an ordered, de-duplicated recipient list.
    'ALL' -> every LIVE worker except the sender; otherwise split on commas.

    'ALL' filters by heartbeat liveness, NOT registry truthiness: a claim
    survives /clear and hard-kill (the watcher dies, the claim lingers), so a
    stale claim would otherwise be a broadcast target and the message would
    black-hole while fan-out never completes. Liveness is code-knowable
    (watcher_alive), so 'ALL' self-corrects regardless of whether the caller
    remembered --require-watcher. Explicit comma lists are NOT liveness-filtered:
    the caller named those roles deliberately, and --require-watcher is the right
    per-recipient gate there (a dead one is REFUSED, not silently dropped)."""
    if recipient.strip().upper() == "ALL":
        # parked OR busy both count as live: a worker mid-task has no parked
        # watcher but its code-forked busy beater proves it exists; excluding it
        # would silently drop it from broadcasts (2026-08-01, same rationale as
        # the send --require-watcher busy-queue path).
        raw = [r for r in _claimed_roles()
               if r != sender and (watcher_alive(r, max_age)
                                   or worker_busy(r, max_age))]
    else:
        raw = [x.strip() for x in recipient.split(",")]
    seen, out = set(), []
    for r in raw:
        if r and r != sender and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _claim_one(conn, me):
    """Atomically claim the single oldest unhandled message for `me`, or None.
    ONE UPDATE...RETURNING runs under SQLite's write lock, so concurrent consumers
    can never both claim the same row. Used by watch(). Claim = lease: handled=1,
    status=delivered, attempts=attempts+1 in the same atomic statement; lease_until
    is RESET to now + lease_secs (claim-time lease: the runway starts when the work
    starts, see send()). Takes the caller's conn so watch() can reap and claim
    within one connection."""
    _now = time.time()
    return conn.execute(
        "UPDATE messages SET handled=1, status='delivered', attempts=attempts+1, "
        "last_seen_ts=?, claimed_ts=COALESCE(claimed_ts, ?), "
        "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
        "ELSE ? + lease_secs END "
        "WHERE id=("
        "  SELECT id FROM messages WHERE recipient=? AND handled=0 "
        "  ORDER BY id LIMIT 1"
        ") RETURNING id, ts, sender, body, msg_type, in_reply_to, result, new_items",
        (_now, _now, _now, me),
    ).fetchone()


# ---- RESULT-line protocol (2026-09-19, user decision: protocol first, no model) --
# Worker reply bodies open with one or more `RESULT: <absolute path>` lines naming
# the products. send() verifies them against the filesystem and the parent task's
# dispatch time; the hub watcher then treats a CLEAN reply as "done, product at
# <path>" and never signals the body. Everything else (no RESULT line, a missing or
# stale path) keeps the old NEW MSG signal so the hub reads it in full. Ground truth
# is the disk, not the worker's prose.
_RESULT_LINE = re.compile(r"^\s*RESULT\s*[:：]\s*(.+?)\s*$")   # single line, anchored; see _result_paths
# 2026-10-02 (book "Build an AI Agent" §2.4.2 is_solvable / §7.4 need_replan): a reply
# may instead OPEN with `UNSOLVABLE: <why>` (cannot be done; no guessing from memory)
# or `REPLAN: <which premise of the brief is wrong + evidence>` (the brief must be
# rewritten, not re-sent). Both stay non-CLEAN, so the hub reads the full body; the
# status is stored in `result` and tagged on the NEW MSG signal.
_STATUS_LINE = re.compile(r"^\s*(UNSOLVABLE|REPLAN)\s*[:：]\s*(.*?)\s*$", re.I)
_HOLD_SECS = float(os.environ.get("IPC_HOLD_SECS", "900"))   # max wait for a CLEAN reply = the 15-min slice rule


def _result_paths(body):
    """Paths from the RESULT lines that OPEN the body: leading blank lines are
    skipped, then consecutive RESULT lines are collected, and the first other
    line ends the header. A RESULT line buried in prose is prose, not a claim.
    (2026-09-19 review item 1: the earlier re.M finditer matched anywhere, so a
    "尚未完成、需要澄清" reply that quoted a valid path was marked CLEAN and the
    hub only ever saw the one-line DONE summary.)"""
    out = []
    for line in (body or "").splitlines():
        if not line.strip():
            if out:
                break
            continue
        m = _RESULT_LINE.match(line)
        if not m:
            break
        out.append(m.group(1).strip("`'\" "))
    return out


def _verify_result(conn, body, task_id):
    """Return 'CLEAN', 'UNVERIFIED: <why>' or None (no RESULT line) for a linked
    reply body. CLEAN iff the body OPENS with >=1 RESULT line and every path is
    absolute, an existing regular file, and modified at/after the parent task's
    dispatch ts. A body opening with UNSOLVABLE:/REPLAN: returns that status."""
    first = next((l for l in (body or "").splitlines() if l.strip()), "")
    ms = _STATUS_LINE.match(first)
    if ms:
        return f"{ms.group(1).upper()}: {ms.group(2) or '(no reason given)'}"
    paths = _result_paths(body)
    if not paths:
        return None
    row = conn.execute("SELECT ts FROM messages WHERE id=?", (task_id,)).fetchone()
    try:
        dispatched = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        dispatched = 0.0
    for p in paths:
        if not os.path.isabs(p):
            return f"UNVERIFIED: not an absolute path: {p}"
        if not os.path.isfile(p):
            return f"UNVERIFIED: missing: {p}"
        if os.path.getmtime(p) < dispatched - 60:   # 60 s clock/rounding slack
            return f"UNVERIFIED: older than task #{task_id}: {p}"
    return "CLEAN"


# ---- Routing ledger fields (2026-10-17, memory ipc-record-dedup-plan-20261017) ----
# Brief field `类型: <type>` (also `类型：`, `type:`, `TYPE:`), one value from the
# whitelist below; anything else is recorded as 未分类. Single authority for the
# whitelist: ipc_brief_lint.py (rule T) and ipc_estimate.py import it from here.
TASK_TYPES = ("取数", "脚本", "写作", "检索", "评审", "代码")
UNTYPED = "未分类"
# Both spellings anchored at line start; optional prefix of blanks, list markers,
# quote marks and heading hashes (`## 类型: 取数`, `- **类型:** 取数`).
_TYPE_CN = re.compile(r"^[ \t>*\-#]*类型[ \t*]*[:：][ \t*]*`?([^\s`*，,;；。（(]*)", re.M)
_TYPE_EN = re.compile(r"^[ \t>*\-#]*type[ \t*]*[:：][ \t*]*`?([^\s`*，,;；。（(]*)", re.M | re.I)
_TYPE_SCAN_LINES = 40     # the field belongs in the brief's head, never in its tail


def _unfenced(lines):
    """`lines` without fenced code blocks (``` / ~~~). A fence still open at the
    end hides nothing: its opening marker is taken as stray and the lines after
    it are kept. Shared by parse_task_type and count_new_items."""
    keep, fence_at = [], None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence_at = i if fence_at is None else None
            continue
        if fence_at is None:
            keep.append(line)
    if fence_at is not None:                    # unclosed: do not swallow the rest
        keep += lines[fence_at + 1:]            # no marker can follow an unclosed fence
    return keep


def parse_task_type(body):
    """Raw value of the brief's first `类型:` line within its first 40 lines,
    fenced code blocks removed first (_unfenced), so a YAML `type: object` in an
    example never reads as the field. The English `type:` spelling counts only
    when no 类型 line exists. None when absent, "" when present but empty."""
    text = "\n".join(_unfenced((body or "").splitlines()[:_TYPE_SCAN_LINES]))
    m = _TYPE_CN.search(text) or _TYPE_EN.search(text)
    return m.group(1) if m else None


def task_type_of(body):
    """The whitelisted type of a brief, else UNTYPED (a force-sent brief)."""
    t = parse_task_type(body)
    return t if t in TASK_TYPES else UNTYPED


# Worker reply: optional NEW section, placed by protocol after the RESULT lines but
# counted wherever it appears in the body (the RESULT header itself cannot hold
# one: its first non-RESULT line ends it). A line `NEW:` (also `**NEW:**`,
# `## NEW`) opens the section and each following `- ` / `* ` / `1. ` line is one
# item until the first other non-blank line; a `NEW: <text>` line is one item by
# itself. Uppercase NEW only, so prose "new:" never counts. Lenient by design:
# zero when absent, never an error. Parsed ONCE, in send(); readers use the
# stored `new_items` column.
_NEW_HEAD = re.compile(r"^\s*(?:#+\s*)?\**\s*NEW\s*\**\s*[:：]\s*\**\s*(.*?)\s*$")
_NEW_HEADING = re.compile(r"^\s*#+\s*NEW\s*$")
_NEW_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")


def count_new_items(body):
    """Items of the NEW section(s). The one rule (also in operational-cautions
    caution 6 and the ipc_dispatch.ps1 footer): items are `- ` bullets under a
    `NEW:` line; text after `NEW:` on that same line counts as one item only when
    no bullet follows. Lenient details: `* ` and `1. ` bullets count too; a bullet
    deeper than the shallowest bullet seen so far in the section, or any indented
    non-bullet line, continues the current item; the section ends at a
    non-indented, non-empty, non-bullet line. Fenced code blocks are removed first
    (_unfenced), so a quoted `NEW:` example in a fence counts nothing."""
    count, in_sec, base, inline, bullets = 0, False, None, 0, 0

    def close():
        return inline if not bullets else 0

    for line in _unfenced((body or "").splitlines()):
        m = _NEW_HEAD.match(line)
        if m or _NEW_HEADING.match(line):
            if in_sec:
                count += close()
            in_sec, base, bullets = True, None, 0
            inline = 1 if (m and m.group(1)) else 0
            continue
        if not in_sec or not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        bullet = _NEW_ITEM.match(line) is not None
        if bullet and (base is None or indent <= base):
            base = indent if base is None else min(base, indent)
            bullets += 1
            count += 1
        elif indent == 0 and not bullet:
            count += close()
            in_sec, inline, bullets = False, 0, 0
    if in_sec:
        count += close()
    return count


def _anchor(task_id, sender):
    """Quotable reference to one worker's reply on one task (`#task12.B`)."""
    return f"#task{task_id}.{sender}"


def _summary_suffix(task_id, sender, n_new, mid, me):
    """Tail of the hub's one-line DONE summary (watch flush and recv): the reply's
    anchor, and for a reply carrying NEW items (stored count) the count plus the
    peek that shows them (a CLEAN reply's body is otherwise never read)."""
    if task_id is None:
        return ""
    new = f" +NEW {n_new}: peek --me {me} --id {mid}" if n_new else ""
    return f"{new}  anchor {_anchor(task_id, sender)}"


def _open_hub_tasks(conn, hub):
    rows = conn.execute(
        "SELECT id FROM messages WHERE sender=? AND msg_type='task' "
        "AND tombstone IS NULL", (hub,)).fetchall()
    return [r[0] for r in rows if not task_done(conn, r[0])]


_BUDGET_LINE = re.compile(r"(?:预算|时间预算|BUDGET)\s*[:：]\s*(\d+)\s*(?:分钟|min)?", re.I)
_UNITS_LINE = re.compile(r"UNITS\s*[:：]\s*(\d+)", re.I)
_FEN = re.compile(r"(\d+)\s*份")
_DEFAULT_BUDGET_MIN = 15          # user rule 2026-08-29: every dispatch <= 15 min
# user-level (~/.claude/ipc): models are cross-project. IPC_TIMING_PATH points a
# test run at a copy; ipc_estimate.py honours the same variable.
TIMING_LOG = os.environ.get("IPC_TIMING_PATH") or os.path.join(_HERE, "timing.jsonl")


def _extract_budget(body):
    m = _BUDGET_LINE.search(body or "")
    return int(m.group(1)) if m else None


def _ts_epoch(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def _hold_needed(conn, hub, deadline, now=None):
    """True while some open task is expected to finish before `deadline` (the
    oldest held reply's ts + _HOLD_SECS): its budget end (dispatch + budget_min,
    default 15 min) lies in [now, deadline]. A long task whose budget ends after
    the deadline, or an overdue one, never delays sibling summaries (user
    2026-09-19: the 40-minute-task objection). Budgets come from the brief's
    `预算: N 分钟` line or `send --budget`; ipc_estimate.py suggests them."""
    now = now or time.time()
    rows = conn.execute(
        "SELECT id, ts, budget_min FROM messages WHERE sender=? AND msg_type='task' "
        "AND tombstone IS NULL", (hub,)).fetchall()
    for tid, ts, budget in rows:
        if task_done(conn, tid):
            continue
        end = (_ts_epoch(ts) or now) + 60 * (budget or _DEFAULT_BUDGET_MIN)
        if now <= end <= deadline:
            return True
    return False


_MODEL_TAG_CACHE = {}


def _role_model(role):
    """Current model tag for a role, read from the launcher's `$ModelTag` table
    (the single authority for role->model; parsed, never copied). Falls back to
    the role name itself."""
    if not _MODEL_TAG_CACHE:
        launcher = os.path.join(_HERE, "ipc_wezterm_launch.ps1")
        try:
            with open(launcher, encoding="utf-8") as f:
                text = f.read()
            block = re.search(r"\$ModelTag\s*=\s*@\{(.*?)\n\}", text, re.S)
            for m in re.finditer(r"^\s*(\w+)\s*=\s*'([^']+)'", block.group(1) if block else "", re.M):
                _MODEL_TAG_CACHE[m.group(1).upper()] = m.group(2)
        except OSError:
            pass
        # Roles outside fleet 1's table: X lives in fleet 2 (Claude, its own
        # launcher). Keep these in step with ipc_estimate.ROLE_MODEL_HISTORY.
        _MODEL_TAG_CACHE.setdefault("X", "Claude-fleet2")
        _MODEL_TAG_CACHE.setdefault("A", "Claude")
    return _MODEL_TAG_CACHE.get(role.upper(), role)


_LAUNCH_CMD_CACHE = {}


def _role_variant(role):
    """Model AND parameters the role was launched with, e.g. 'Codex/gpt-5.6-sol/xhigh',
    'Claude/claude-opus-5[1m]', 'Kimi/k3[1m]'. Parsed from the launcher's
    `$LaunchCmd` line (`--model X`, a quoted positional model, and
    `model_reasoning_effort=Y`); a bare `codex` falls back to ~/.codex/config.toml's
    `model` / `model_reasoning_effort` read at write time; a bare claude wrapper
    records 'default'. Best effort by construction: an in-pane /model switch or a
    usage-limit downgrade is invisible here (2026-09-19 review item 3 — $ModelTag
    alone lumped every E model and effort under 'Codex')."""
    tag = _role_model(role)
    if not _LAUNCH_CMD_CACHE:
        try:
            with open(os.path.join(_HERE, "ipc_wezterm_launch.ps1"), encoding="utf-8") as f:
                text = f.read()
            block = re.search(r"\$LaunchCmd\s*=\s*@\{(.*?)\n\}", text, re.S)
            for m in re.finditer(r"^\s*(\w+)\s*=\s*(['\"])(.*?)\2", block.group(1) if block else "", re.M):
                _LAUNCH_CMD_CACHE[m.group(1).upper()] = m.group(3)
        except OSError:
            pass
        _LAUNCH_CMD_CACHE.setdefault("_", "")   # sentinel: parsed once even if empty
    cmd = _LAUNCH_CMD_CACHE.get(role.upper(), "")
    mm = (re.search(r"--model\s+['\"]?([^\s'\"]+)", cmd)
          or re.match(r"^\S+\s+'([^']+)'", cmd))          # claude-kimi 'k3[1m]' positional
    me = re.search(r"model_reasoning_effort=['\"]?(\w+)", cmd)
    model = mm.group(1) if mm else None
    effort = me.group(1) if me else None
    if cmd.split()[:1] == ["codex"] and not (model and effort):
        try:
            with open(os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
                      encoding="utf-8") as f:
                cfg = f.read()
            cm = re.search(r'^model\s*=\s*"([^"]+)"', cfg, re.M)
            ce = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', cfg, re.M)
            model = model or (cm.group(1) if cm else None)
            effort = effort or (ce.group(1) if ce else None)
        except OSError:
            pass
    return "/".join([tag, model or "default"] + ([effort] if effort else []))


GRADE_REASONS = ("tool", "context", "quota", "misread", "quality")
USED_NEW = ("brief", "conclusion", "routing", "rule", "task", "scope-closed", "none")
_GRADE_FIELDS = ("verdict", "reason", "used_new", "findings")
# findings (2026-10-18, user: review results are checked in full "这样可以比较哪个模型更准确"):
# A's per-finding check of a review reply, {"held": h, "false": f, "unchecked": u};
# ipc_estimate.py accuracy aggregates it per model.


def _findings_arg(text):
    """argparse type for --findings HELD,FALSE,UNCHECKED (three non-negative ints, sum > 0)."""
    try:
        h, f, u = (int(x) for x in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected HELD,FALSE,UNCHECKED, e.g. 12,2,1")
    if min(h, f, u) < 0 or h + f + u == 0:
        raise argparse.ArgumentTypeError("counts must be >= 0 and not all zero")
    return {"held": h, "false": f, "unchecked": u}

# Ledger keys (2026-10-17; the one statement every reader and writer follows):
#   * a BASE row is identified by (project, task, dispatch_ts) and at most one is
#     ever appended per task row (ledger_append_new skips when the key exists).
#     The dispatch_ts part keeps a re-created mailbox, whose ids restart at 1,
#     from colliding with an old row of the same project. Consequence: redispatch
#     is recorded from attempts > 1 only; a reply / rework / second reply cycle on
#     one task leaves one row and relies on `grade --verdict rework`.
#   * `grade` looks a task up by (project, task), takes the newest base row and
#     APPENDS a correction row {"grade": true, project, task, dispatch_ts, + only
#     the fields given}. The file is append-only: nothing rewrites it, so an
#     append from another project's send() can never be lost.
#   * readers fold corrections into their base row (fold_ledger): matched on the
#     base key, applied in file order, field by field, the latest wins. EVERY
#     reader must fold: a raw grep / jq / wc over timing.jsonl counts correction
#     rows (`"grade": true`) as tasks and shows pre-grade verdicts.
#   * every read-dedupe-append runs under _ledger_lock (sidecar `<ledger>.lock`),
#     so a hub cancel and a worker reply on the same task cannot both append a
#     base row. The dedupe scan reads the whole file: O(n) per append, ~1 ms per
#     1000 rows, acceptable at a few appends per minute. If the lock is not won
#     within 10 s, rows are appended WITHOUT the dedupe and flagged
#     `"lock_timeout": true`; fold_ledger keeps the first base row per key, so the
#     worst case is a flagged, ignored duplicate, never a silent one. A flush whose
#     append fails (OSError) parks its rows in `<ledger stem>.pending.jsonl`; the
#     next ledger_append_new drains that file first.
#     Known cost / TODO: every append reads the whole ledger for the dedupe; once
#     it passes ~20k rows, read only the tail or keep a key sidecar.
#   * no ledger I/O while the SQLite write lock is held: rows are built inside a
#     transaction (_ledger_build defers them) and appended by _ledger_flush only
#     after the commit (_txn wraps every such `with _conn()`), dropped on rollback.
#     A row built outside any _txn (a caller bug) is appended at once with a
#     stderr note rather than lost or left waiting.


_LEDGER_LOCK_TIMEOUT = 10.0   # seconds to wait for the ledger lock before the flagged fallback


@contextlib.contextmanager
def _ledger_lock(timeout=None):
    """Exclusive lock on the sidecar `<ledger>.lock` (msvcrt on Windows, flock
    elsewhere) around a read-dedupe-append. Yields True when held. After
    `timeout` seconds it yields False with a stderr warning (the ledger must never
    block or break a send); ledger_append_new then skips the dedupe and flags its
    rows `lock_timeout: true` instead of racing unlocked."""
    fh, locked = None, False
    try:
        fh = open(TIMING_LOG + ".lock", "a+b")
        deadline = time.monotonic() + (_LEDGER_LOCK_TIMEOUT if timeout is None else timeout)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    sys.stderr.write("[timing] ledger lock timed out; rows appended "
                                     "flagged lock_timeout, no dedupe\n")
                    break
                time.sleep(0.05)
    except OSError as e:
        sys.stderr.write(f"[timing] ledger lock unavailable ({e}); rows appended "
                         f"flagged lock_timeout, no dedupe\n")
    try:
        yield locked
    finally:
        if fh is not None:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


def _ledger_project():
    """Ledger `project` key: ~/.claude/projects/<project-key>/ipc -> the key."""
    return (os.path.basename(_HERE) if _LEGACY else
            os.path.basename(os.path.dirname(_STATE_DIR.rstrip("/\\"))))


def _ledger_key(r):
    return (r.get("project"), r.get("task"), r.get("dispatch_ts"))


def _ledger_rows():
    """Every parseable ledger line, file order (newest last), base rows and grade
    corrections alike; [] when absent. Read once per operation."""
    out = []
    try:
        with open(TIMING_LOG, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        out.append(r)
    except OSError:
        pass
    return out


def fold_ledger(rows):
    """Base rows with their grade corrections applied (see the key comment above).
    ipc_estimate.load() and grade() read the ledger through this. Only the FIRST
    base row per key is kept: a later one can only be a `lock_timeout` duplicate."""
    base, corr, seen = [], {}, set()
    for r in rows:
        if r.get("grade"):
            corr.setdefault(_ledger_key(r), []).append(r)
        elif _ledger_key(r) not in seen:
            seen.add(_ledger_key(r))
            base.append(r)
    for b in base:
        for c in corr.get(_ledger_key(b), ()):
            b.update({k: c[k] for k in _GRADE_FIELDS if k in c})
    return base


def _auto_verdict(outcome, attempts, result):
    """Code-set verdict, most decisive first: fail (fail reply, cancel, reaper
    auto-fail) > redispatch (claimed more than once) > unsolvable / replan (the
    reply's opening status line, as already parsed into `result` by
    _verify_result) > pass. Only `rework` needs the hub's hand (grade); a missed
    grade records an extra pass."""
    if outcome in ("fail", "cancel"):
        return "fail"
    if (attempts or 0) > 1:
        return "redispatch"
    status = (result or "").split(":", 1)[0]
    return status.lower() if status in ("UNSOLVABLE", "REPLAN") else "pass"


def _ledger_append(rec):
    """Raw append of one line. Callers hold _ledger_lock."""
    with open(TIMING_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_append_new(recs):
    """Append the base rows whose key (project, task, dispatch_ts) the ledger does
    not hold yet: one locked read for the whole batch, then one append. Returns
    the rows written. send/cancel/reap pass one row; ipc_estimate backfill many.
    Without the lock (timeout) every row is appended with `lock_timeout: true`
    and no dedupe: the worst case is a flagged possible duplicate, which
    fold_ledger drops (first base row per key wins), never a silent one. Rows
    parked in the spill file by a failed flush are drained first, through the
    same dedupe, and the spill file is removed once they are written."""
    spill = _ledger_spill_path()
    with _ledger_lock() as locked:
        parked = []
        if os.path.exists(spill):
            with open(spill, encoding="utf-8") as f:
                for line in f:
                    try:
                        parked.append(json.loads(line))
                    except ValueError:
                        pass
        batch = [r for r in parked if isinstance(r, dict)] + list(recs)
        if locked:
            have = {_ledger_key(r) for r in _ledger_rows() if not r.get("grade")}
            out = []
            for rec in batch:
                if _ledger_key(rec) not in have:
                    have.add(_ledger_key(rec))
                    out.append(rec)
        else:
            out = [dict(rec, lock_timeout=True) for rec in batch]
        if out:
            with open(TIMING_LOG, "a", encoding="utf-8") as f:
                f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
        if parked:
            os.remove(spill)
    return out


def _ledger_spill_path():
    """`<ledger stem>.pending.jsonl` beside the ledger: rows whose append failed."""
    return os.path.splitext(TIMING_LOG)[0] + ".pending.jsonl"


_LEDGER_PENDING = []   # base rows built inside a DB transaction, appended after its commit
_TXN_DEPTH = [0]       # open _txn blocks in this process; _ledger_build refuses at 0


def _ledger_flush():
    """Append the deferred base rows (ledger_append_new skips any key the ledger
    already holds: a reply after a cancel or reaper-fail row writes nothing, an
    existing verdict is never changed). Called by _txn after the commit, never
    under the SQLite write lock. Never raises."""
    if not _LEDGER_PENDING:
        return
    recs = list(_LEDGER_PENDING)
    del _LEDGER_PENDING[:]
    try:
        ledger_append_new(recs)
    except OSError as e:
        # Ledger not writable right now: park the rows; the next successful
        # ledger_append_new drains them (2026-10-17 review).
        spill = _ledger_spill_path()
        try:
            with _ledger_lock():
                with open(spill, "a", encoding="utf-8") as f:
                    f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
            sys.stderr.write(f"[timing] ledger append failed ({e}); {len(recs)} row(s) parked "
                             f"in {spill}, drained by the next append\n")
        except OSError as e2:
            sys.stderr.write(f"[timing] not logged: {e}; parking failed too: {e2}\n")
    except Exception as e:  # noqa: BLE001 — the ledger must never break the caller
        try:
            sys.stderr.write(f"[timing] not logged: {e}\n")
        except (OSError, ValueError):
            pass


@contextlib.contextmanager
def _txn():
    """`with _conn() as conn` plus the ledger rule: rows deferred inside the block
    are appended only after the commit (by the outermost _txn), and dropped when
    it rolls back."""
    _TXN_DEPTH[0] += 1
    try:
        with _conn() as conn:
            yield conn
    except BaseException:
        del _LEDGER_PENDING[:]
        raise
    finally:
        _TXN_DEPTH[0] -= 1
    if _TXN_DEPTH[0] == 0:
        _ledger_flush()


def _ledger_build(conn, task_id, worker, outcome, result, end_ts, new_items=0):
    """Defer the base row for task_id ended at end_ts (reads only; the append
    happens in _ledger_flush after the caller's commit). Returns the row.
    Outside a _txn block the row is appended at once through ledger_append_new
    (never left waiting for an unrelated later command), with a stderr note that
    the caller should use _txn."""
    t = conn.execute("SELECT ts, claimed_ts, body, budget_min, attempts, task_type "
                     "FROM messages WHERE id=?", (task_id,)).fetchone()
    if not t:
        return None
    d_ts, claimed, brief, budget, attempts, task_type = t
    project = _ledger_project()
    d = _ts_epoch(d_ts)
    r = _ts_epoch(end_ts) or time.time()
    units = _UNITS_LINE.search(brief or "")
    fen = _FEN.search((brief or "").splitlines()[0] if brief else "")
    rec = {
        "ts": end_ts, "project": project,
        "task": task_id, "role": worker, "model": _role_model(worker),
        "variant": _role_variant(worker),   # model + params; stats/estimate prefer it
        "dispatch_ts": d_ts,
        "queue_min": round((claimed - d) / 60, 1) if (claimed and d) else None,
        "exec_min": round((r - (claimed or d)) / 60, 1) if (claimed or d) else None,
        "total_min": round((r - d) / 60, 1) if d else None,
        "brief_chars": len(brief or ""),
        "units": int(units.group(1)) if units else (int(fen.group(1)) if fen else None),
        "budget_min": budget, "attempts": attempts,
        "outcome": outcome, "result": result, "source": "live",
        # 2026-10-17 routing fields (ipc_estimate.py rank): type from the brief,
        # verdict set by code (grade appends rework), NEW count, anchor for briefs.
        # a task dispatched before the upgrade has no task_type column value:
        # read the type from its brief
        "type": task_type or task_type_of(brief),
        "verdict": _auto_verdict(outcome, attempts, result),
        "reason": None, "new_items": new_items, "used_new": "none",
        "anchor": _anchor(task_id, worker),
    }
    if _TXN_DEPTH[0] == 0:
        sys.stderr.write(f"[timing] ledger row for task #{task_id} built outside _txn: "
                         f"appended now; the caller should use _txn\n")
        ledger_append_new([rec])
        return rec
    _LEDGER_PENDING.append(rec)
    return rec


def grade(task_id, verdict=None, reason=None, used_new=None, findings=None):
    """The hub's hand on the ledger (2026-10-17): mark the newest row for task_id in
    THIS project `rework` with a reason (a reply A used but had to redo), undo that
    with `pass`, and/or record `used_new` (how A used the reply's NEW items), by
    APPENDING a correction row (see the key comment). rework/pass apply only over
    pass/rework: fail, redispatch, replan and unsolvable come from code and stand.
    Only fields that would change are written; when none would, nothing is
    appended. Returns (folded row, correction row or None for a no-op); raises
    LookupError (no row) or ValueError (verdict refused, or the ledger lock not
    won in time: grading never reads and appends on an unlocked snapshot)."""
    project = _ledger_project()
    with _ledger_lock() as locked:
        if not locked:
            raise ValueError(f"ledger lock not acquired within {_LEDGER_LOCK_TIMEOUT:g} s; "
                             f"nothing appended, retry the grade")
        rows = [r for r in fold_ledger(_ledger_rows())
                if r.get("project") == project and r.get("task") == task_id]
        if not rows:
            raise LookupError(f"no ledger row for task #{task_id} in project {project} "
                              f"({TIMING_LOG}); rows are written when a linked reply/fail "
                              f"lands, or when a claimed task is cancelled or auto-failed")
        target = rows[-1]
        current = target.get("verdict") or "pass"
        if verdict is not None and current not in ("pass", "rework"):
            raise ValueError(f"task #{task_id} verdict is {current!r}, set by code "
                             f"(attempts / outcome / status line); --verdict rework|pass "
                             f"applies only to a pass or rework row (--used-new still works)")
        want = {}
        if verdict is not None:
            want["verdict"] = verdict
            want["reason"] = reason if verdict == "rework" else None
        if used_new is not None:
            want["used_new"] = used_new
        if findings is not None:
            want["findings"] = findings
        current_vals = {"verdict": current, "reason": target.get("reason"),
                        "used_new": target.get("used_new") or "none",
                        "findings": target.get("findings")}
        changes = {k: v for k, v in want.items() if current_vals[k] != v}
        if not changes:
            return target, None
        corr = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "project": project,
                "task": task_id, "dispatch_ts": target.get("dispatch_ts"), "grade": True,
                **changes}
        _ledger_append(corr)
    target.update(changes)
    return target, corr


def _log_terminal(conn, task_id, outcome):
    """Ledger row for a task that ended WITHOUT a linked reply/fail: the hub
    cancelled it (outcome cancel) or the reaper failed it after MAX_ATTEMPTS
    (outcome fail); verdict fail, and its minutes never enter a duration sample
    (ipc_estimate.timed). Only a claimed task gets one: an unclaimed cancel says
    nothing about the worker. When the task already has a row (it was answered,
    or failed by its worker) nothing is written: a cancel never overwrites an
    existing verdict. Never raises."""
    try:
        row = conn.execute("SELECT recipient, claimed_ts, attempts, msg_type "
                           "FROM messages WHERE id=?", (task_id,)).fetchone()
        if not row or row[3] != "task" or (row[1] is None and not row[2]):
            return
        _ledger_build(conn, task_id, row[0], outcome, None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:  # noqa: BLE001 — the ledger must never break cancel/reap
        try:
            sys.stderr.write(f"[timing] terminal row not logged: {e}\n")
        except (OSError, ValueError):
            pass


def _log_timing(conn, reply_id, task_id, worker, outcome, result, reply_ts,
                new_items=0):
    """Append one metadata-only line to the user-level timing ledger when a
    worker's linked reply/fail lands: the durable input for ipc_estimate.py
    (the mailbox self-archives past 300 rows, so it cannot be the history).
    Only the FIRST linked reply/fail per task is logged, and none when the task
    row already has a ledger row (cancel / reaper fail; checked at flush). The row
    is deferred and appended after the caller's commit (_txn). Never raises."""
    try:
        prior = conn.execute(
            "SELECT 1 FROM messages WHERE in_reply_to=? AND sender=? "
            "AND msg_type IN ('reply','fail') AND id<?", (task_id, worker, reply_id)).fetchone()
        if prior:
            return
        _ledger_build(conn, task_id, worker, outcome, result, reply_ts, new_items=new_items)
    except Exception as e:  # noqa: BLE001 — the ledger must never break a send
        try:
            sys.stderr.write(f"[timing] not logged: {e}\n")
        except (OSError, ValueError):
            pass


def _flush_held(conn, me, force=False):
    """Emit one `DONE #task <- sender RESULT: path` line per held CLEAN reply and
    mark it announced. Flushes when forced (a non-CLEAN signal is about to go
    out), when the hub has no open task left (fan-out complete), or when the
    oldest held reply has waited _HOLD_SECS. Durable: announced=0 lives in the
    DB, so a watcher restart resumes the hold instead of losing the summary.
    Returns False if stdout died (rows stay held; retried next poll)."""
    held = conn.execute(
        "SELECT id, ts, sender, body, in_reply_to, new_items FROM messages "
        "WHERE recipient=? AND announced=0 ORDER BY id", (me,)).fetchall()
    if not held:
        return True
    if not force:
        oldest = _ts_epoch(held[0][1]) or 0.0
        deadline = oldest + _HOLD_SECS
        if time.time() < deadline and _hold_needed(conn, me, deadline):
            return True
    for mid, _ts, sender, body, task, n_new in held:
        paths = ", ".join(_result_paths(body)) or "?"
        tail = _summary_suffix(task, sender, n_new, mid, me)
        note = "" if n_new else ", no peek needed"
        if not _emit(f"DONE #{task} <- {sender} RESULT: {paths} (#{mid}{note}){tail}"):
            return False
        conn.execute("UPDATE messages SET announced=1 WHERE id=?", (mid,))
    return True


def pending(hub):
    """Tasks `hub` dispatched that are not yet done -> the workers still absent.
    Fan-out is COMPLETE when this is empty. Uses task_done (so 'answered' matches
    done exactly) and derives a lifecycle state per row so the hub can read
    QUEUED / IN_PROGRESS / STALE / FAILED at a glance. Lazily reaps first.
    Returns [(id, recipient, ts, state, attempts)]."""
    with _txn() as conn:
        _reap_stale(conn, hub=hub)
        rows = conn.execute(
            "SELECT t.id, t.recipient, t.ts, t.handled, t.lease_until, t.tombstone, "
            "t.attempts, t.no_requeue, t.last_seen_ts FROM messages t "
            "WHERE t.sender=? AND t.msg_type='task' AND t.tombstone IS NULL "
            "ORDER BY t.id", (hub,)).fetchall()
        out = []
        for (rid, recipient, ts, handled, lease_until, tombstone, attempts,
             norq, seen) in rows:
            if task_done(conn, rid):
                continue
            st = task_state(conn, rid, recipient, handled, lease_until, tombstone,
                            norq, ts=ts, last_seen_ts=seen)
            out.append((rid, recipient, ts, st, attempts))
        return out


# Lifecycle states that end a task: nothing further will happen to the row.
_TERMINAL_STATES = frozenset({"DONE", "FAILED", "CANCELLED"})


def task_status(tid):
    """State of ONE task by id, as a dict (2026-10-15 efficiency review P1).
    Why a per-task query: ipc_dispatch.ps1 used to confirm a claim by grepping
    `pending` for `#id .*IN_PROGRESS`, but pending omits done tasks, so a task
    finished before the first check could never match and was reported as
    "queued but not yet claimed" (exit 1) — a false alarm on the fastest path.
    Same derivation as pending (task_state over the row), but READ-ONLY: no
    _reap_stale, no _auto_archive, so a probe loop never mutates the mailbox.
    STALE here means "lease dead, not yet reaped"; the next recv/pending/reap
    turns it into a requeue or FAILED. Returns state UNKNOWN (with reason) for
    a missing id or a non-task row; `terminal` is True for DONE/FAILED/CANCELLED."""
    if not os.path.exists(DB_PATH):
        return _task_info(tid, "UNKNOWN", reason=f"no mailbox at {DB_PATH}")
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, sender, recipient, ts, handled, lease_until, tombstone, "
            "attempts, no_requeue, last_seen_ts, msg_type, result, claimed_ts "
            "FROM messages WHERE id=?", (tid,)).fetchone()
        if not row:
            arch = _archived_task_status(conn, tid)
            if arch is not None:
                return arch
            return _task_info(tid, "UNKNOWN", reason="no such message id")
        (rid, sender, recipient, ts, handled, lease_until, tombstone, attempts,
         norq, seen, msg_type, result, claimed_ts) = row
        if msg_type != "task":
            return _task_info(tid, "UNKNOWN", reason=f"message #{tid} is a {msg_type}, not a task",
                              sender=sender, recipient=recipient, ts=ts)
        st = task_state(conn, rid, recipient, handled, lease_until, tombstone,
                        norq, ts=ts, last_seen_ts=seen)
        replies = conn.execute(
            "SELECT id, sender, msg_type FROM messages WHERE in_reply_to=? "
            "ORDER BY id", (rid,)).fetchall()
    return _task_info(rid, st, sender=sender, recipient=recipient, ts=ts, attempts=attempts,
                      result=result, claimed_ts=claimed_ts, replies=replies)


def _task_info(tid, state, reason=None, archived=False, sender=None, recipient=None,
               ts=None, attempts=None, result=None, claimed_ts=None, replies=()):
    """The one shape every `task --id` answer has (text printer and --json alike):
    all keys present, null where unknown (2026-10-17 review)."""
    return {
        "id": tid, "state": state, "terminal": state in _TERMINAL_STATES,
        "from": sender, "to": recipient, "ts": ts, "attempts": attempts,
        "result": result, "claimed_ts": claimed_ts,
        "replies": [{"id": i, "from": s, "type": t} for i, s, t in replies],
        "reason": reason, "archived": archived,
    }


def _archived_task_status(conn, tid):
    """task_status for an id that left `messages` for `messages_archive`, or None.
    Only finished work is ever archived (see _ARCHIVE_WHERE), so the state is
    CANCELLED / FAILED / DONE; replies are read from both tables."""
    if not _has_archive(conn):
        return None
    row = conn.execute(
        "SELECT id, sender, recipient, ts, tombstone, attempts, msg_type, result, "
        "claimed_ts FROM messages_archive WHERE id=? ORDER BY archived_ts DESC LIMIT 1",
        (tid,)).fetchone()   # newest archived version of this id
    if not row:
        return None
    rid, sender, recipient, ts, tombstone, attempts, msg_type, result, claimed_ts = row
    if msg_type != "task":
        return _task_info(tid, "UNKNOWN", archived=True,
                          reason=f"message #{tid} is an archived {msg_type}, not a task",
                          sender=sender, recipient=recipient, ts=ts)
    replies = sorted(conn.execute(
        "SELECT id, sender, msg_type FROM messages WHERE in_reply_to=? UNION "
        "SELECT id, sender, msg_type FROM messages_archive WHERE in_reply_to=?",
        (rid, rid)).fetchall())
    closed = any(t in ("reply", "ack") and s == recipient for _i, s, t in replies)
    st = {"cancelled": "CANCELLED", "failed": "FAILED"}.get(
        tombstone, "DONE" if closed else "UNKNOWN")
    return _task_info(rid, st, archived=True,
                      reason=None if st != "UNKNOWN" else "archived task without a closing reply",
                      sender=sender, recipient=recipient, ts=ts, attempts=attempts,
                      result=result, claimed_ts=claimed_ts, replies=replies)


_ARCHIVE_THRESHOLD = 300   # start trimming once the table exceeds this many rows
_ARCHIVE_KEEP = 150        # rows always kept (same guard as `archive --keep`)

# Shared archive condition for archive()/_auto_archive so the two can't drift
# (2026-10-17: rows matching it are moved to messages_archive, no longer deleted).
# handled=1 normally means history, but an UNRESOLVED --no-requeue task is
# handled=1 too (the fail-closed claim stays in place while it awaits review) —
# active work, never archivable. The NOT EXISTS mirrors task_done()'s
# reply/ack predicate in SQL (kept adjacent by this comment: change both
# together) so a RESOLVED no-requeue task does age out normally.
# 2026-09-05: protection widened from no_requeue tasks to EVERY unfinished
# task (tombstone NULL, no reply/ack from its own recipient). A plain claimed
# task mid-lease is handled=1 too and could be deleted once 150 newer rows
# accumulated (Codex design review, overall item 2). Code-reachable: the
# reaper skips a task whose worker heartbeat is fresh and lease valid, then
# this archive runs on the same path (Codex re-test 2026-09-05: 310 rows
# during one live task deleted it). Production frequency unmeasured.
_ARCHIVE_WHERE = (
    "(handled=1 OR tombstone IS NOT NULL) AND id<=? "
    "AND announced=1 "   # 2026-09-19: a held CLEAN reply (announced=0) is unsummarised work, never history
    "AND NOT (tombstone IS NULL AND msg_type='task' "
    "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.in_reply_to=messages.id "
    "AND r.msg_type IN ('reply','ack') AND r.sender=messages.recipient)) "
    # 2026-10-17 review: a reply/ack stays while its task is QUEUED (requeued
    # before the late reply landed). The task cannot be archived then, and the
    # worker's next claim must still see the reply to done-drop it (task_done
    # reads `messages` only); moving the reply would get the task redone.
    "AND NOT (msg_type IN ('reply','ack') AND EXISTS (SELECT 1 FROM messages t "
    "WHERE t.id=messages.in_reply_to AND t.handled=0 AND t.tombstone IS NULL))"
)


def _has_archive(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='messages_archive'").fetchone() is not None


def _ensure_archive(conn):
    """Create messages_archive lazily (2026-10-17: trimmed rows are moved, not
    deleted) and add any column `messages` gained since; returns the column list
    the move copies. Same columns as messages plus archived_ts; primary key
    (id, archived_ts), so an id archived again (a re-created mailbox restarts
    ids) is a new version, never a conflict. An archive made by the first
    2026-10-17 cut (id alone as key) is migrated once, rows kept."""
    cols = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(messages)")]
    info = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(messages_archive)")}
    if info and not info.get("archived_ts"):          # old key: id alone
        conn.execute("ALTER TABLE messages_archive RENAME TO messages_archive_v1")
        info = {}
    if not info:
        conn.execute("CREATE TABLE messages_archive (id INTEGER NOT NULL, "
                     "archived_ts TEXT NOT NULL, PRIMARY KEY (id, archived_ts))")
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages_archive)")}
    for name, typ in cols:
        if name not in have:
            conn.execute(f"ALTER TABLE messages_archive ADD COLUMN {name} {typ or ''}")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='messages_archive_v1'").fetchone():
        old = [r[1] for r in conn.execute("PRAGMA table_info(messages_archive_v1)")]
        have = {r[1] for r in conn.execute("PRAGMA table_info(messages_archive)")}  # after ADD COLUMN
        keep = [c for c in old if c in have and c != "archived_ts"]
        conn.execute(f"INSERT INTO messages_archive ({', '.join(keep)}, archived_ts) "
                     f"SELECT {', '.join(keep)}, "
                     f"{'COALESCE(archived_ts, ?)' if 'archived_ts' in old else '?'} "
                     f"FROM messages_archive_v1",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("DROP TABLE messages_archive_v1")
    return [name for name, _typ in cols]


_ARCHIVE_CHUNK = 500       # ids per statement, well under SQLite's variable limit


def _archive_move(conn, cutoff):
    """Move history rows (id <= cutoff, _ARCHIVE_WHERE) from messages to
    messages_archive inside the caller's transaction: collect the matching ids,
    INSERT exactly those rows (one new version each, archived_ts = now), DELETE
    exactly those ids. No per-row comparison, so the recv/watch/pending write
    transaction stays short. The archive is unbounded and keeps bodies (it is the
    durable record for analysis; a manual prune would be a future command).
    The write lock is taken FIRST (a no-op UPDATE): the id scan, copy and delete
    then run under it, so another process's commit cannot land in between (in
    WAL that would make the INSERT fail on a stale snapshot and roll the move
    back; ids themselves are never reused, AUTOINCREMENT). Returns rows moved."""
    conn.execute("UPDATE messages SET id=id WHERE 0")   # take RESERVED before reading
    cols = ", ".join(_ensure_archive(conn))
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM messages WHERE {_ARCHIVE_WHERE}", (cutoff,))]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    moved = 0
    for k in range(0, len(ids), _ARCHIVE_CHUNK):
        chunk = ids[k:k + _ARCHIVE_CHUNK]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"INSERT INTO messages_archive ({cols}, archived_ts) "
                     f"SELECT {cols}, ? FROM messages WHERE id IN ({marks})",
                     (stamp, *chunk))
        moved += conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk).rowcount
    return moved


def _archive_guarded(conn, cutoff, fatal=False):
    """_archive_move inside a SAVEPOINT: any error rolls back only the move. The
    automatic trim (fatal=False) warns on stderr and returns 0, so a trim problem
    never fails recv / watch / pending (the pre-2026-10-17 one-statement DELETE
    never did); the manual `archive` command (fatal=True) re-raises after the
    rollback so the operator sees a non-zero exit."""
    try:
        conn.execute("SAVEPOINT ipc_archive")
    except sqlite3.Error as e:
        if fatal:
            raise
        sys.stderr.write(f"[archive] skipped: {type(e).__name__}: {e}\n")
        return 0
    moved, err = 0, None
    try:
        moved = _archive_move(conn, cutoff)
    except Exception as e:  # noqa: BLE001 — a trim must never break the caller
        err = e
        try:
            conn.execute("ROLLBACK TO ipc_archive")
        except sqlite3.Error:
            pass
        if not fatal:
            sys.stderr.write(f"[archive] skipped: {type(e).__name__}: {e}\n")
    try:
        conn.execute("RELEASE ipc_archive")
    except sqlite3.Error:
        pass
    if fatal and err is not None:
        raise err
    return moved


def _auto_archive(conn, threshold=_ARCHIVE_THRESHOLD, keep=_ARCHIVE_KEEP):
    """Lazy self-trim, same philosophy as the lazy reaper: piggybacks on
    recv/watch/pending instead of needing a cron. Once the table exceeds
    `threshold` rows, move handled/terminal rows older than the newest `keep`
    into messages_archive (identical condition to archive(): unread requeued rows
    are active work and are never touched). Done-dropped rows are handled=1, so
    they age out here. `peek --id` / `task --id` read the archive back."""
    if conn.execute("SELECT count(*) FROM messages").fetchone()[0] <= threshold:
        return
    row = conn.execute(
        "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
    ).fetchone()
    if row:
        _archive_guarded(conn, row[0])


def _reap_stale(conn, me=None, hub=None):
    """Lazy reaper — no daemon. Callers: recv/watch (me=worker, reap that worker's
    own stale claimed tasks so a restarted worker re-exposes its orphaned work) and
    pending (hub=A, reap all of A's dispatched tasks). status does NOT call this
    (stays a pure heartbeat file probe, no DB).
    Reaps rows where msg_type='task' AND tombstone IS NULL AND handled=1 AND NOT
    task_done AND lease dead:
      attempts < MAX_ATTEMPTS -> requeue: handled=0, lease_until=NULL (re-exposed;
        attempts kept, will ++ on next claim)
      attempts >= MAX_ATTEMPTS -> tombstone='failed' (stop re-running; guards
        non-idempotent tasks from infinite re-runs — multi-terminal-ipc skill concern)
    Runs as a plain UPDATE under the write lock; the atomic claim UPDATE...RETURNING
    is a separate, later statement, so the single-consumer invariant holds (R4).
    R5 (BUG 1 fix): the msg_type='task' filter is mandatory — send() tags EVERY row
    (including worker->hub reply/ack/fail rows) with lease_until=now+lease. Without
    this filter, A's already-read reply rows would be reaped+requeued at lease
    expiry and redelivered to A's watcher as phantom "NEW MSG" — breaking the
    "history doesn't re-enter the inbox" invariant. Only tasks are reapable.
    Requeue drops lease_until -> NULL only transiently: the next claim resets it
    to claim-time + lease_secs (see _claim_one/recv), so a retried --lease task
    runs under a FRESH hard ceiling instead of the old pre-expired one. For
    non-idempotent tasks use send --no-requeue (stale claim parks as NEEDS-REVIEW,
    never auto-requeued; there is no per-message --max-attempts flag).
    Finishes with the lazy auto-archive (size-gated) so the DB self-trims
    without a maintenance cron."""
    if me is not None:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts, no_requeue FROM messages "
            "WHERE recipient=? AND handled=1 AND tombstone IS NULL "
            "AND msg_type='task'", (me,)).fetchall()
    elif hub is not None:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts, no_requeue FROM messages "
            "WHERE sender=? AND handled=1 AND tombstone IS NULL "
            "AND msg_type='task'", (hub,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts, no_requeue FROM messages "
            "WHERE handled=1 AND tombstone IS NULL AND msg_type='task'").fetchall()
    requeued, failed, review = [], [], []
    for rid, recipient, lease_until, attempts, norq in rows:
        if task_done(conn, rid):
            continue
        if _lease_alive(recipient, lease_until):
            continue
        if norq:
            # Fail-closed (--no-requeue): NEVER auto-requeue or auto-fail a
            # non-idempotent task on a stale lease — a phantom second run (or a
            # premature 'failed' while the worker is still mid-write) is worse
            # than waiting. The claim stays in place; pending shows NEEDS-REVIEW
            # until the hub cancels or the worker done/fail/ack-revives it.
            # (tutti issue-execution.md: ambiguous identity is fail-closed —
            # 'the Run remains running and reconciliation is scheduled'.)
            review.append(rid)
            continue
        if attempts < MAX_ATTEMPTS:
            conn.execute("UPDATE messages SET handled=0, lease_until=NULL WHERE id=?", (rid,))
            requeued.append(rid)
        else:
            conn.execute("UPDATE messages SET tombstone='failed' WHERE id=?", (rid,))
            failed.append(rid)
            _log_terminal(conn, rid, "fail")   # verdict fail in the timing ledger
    _auto_archive(conn)
    return requeued, failed, review


def recv(me):
    """Atomically claim + return every unhandled message addressed to `me`.
    ONE UPDATE...RETURNING runs under SQLite's write lock, so two concurrent
    consumers can't both claim the same row (the old SELECT-then-UPDATE was a
    TOCTOU that double-delivered). This is what makes 'one watcher per inbox'
    unnecessary: the DB is now the single-consumer authority.
    Lifecycle: lazily reaps this inbox's stale claimed tasks first (so a worker
    that died and came back re-exposes its own orphaned work and can re-claim it),
    then claims unhandled rows as a LEASE — handled=1, status=delivered,
    attempts=attempts+1, lease_until reset to now + lease_secs (claim-time lease,
    see send()), all in the same atomic UPDATE (single-consumer invariant
    preserved). Rows that already have a reply/ack linked (finished work the
    reaper requeued while the worker executed watcher-less) are claimed but NOT
    returned — redelivering them would redo a completed task."""
    with _txn() as conn:
        _reap_stale(conn, me=me)
        _now = time.time()
        rows = conn.execute(
            "UPDATE messages SET handled=1, status='delivered', attempts=attempts+1, "
            "last_seen_ts=?, claimed_ts=COALESCE(claimed_ts, ?), "
            "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
            "ELSE ? + lease_secs END "
            "WHERE recipient=? AND handled=0 "
            "RETURNING id, ts, sender, body, msg_type, in_reply_to, session, result, "
            "new_items",
            (_now, _now, _now, me),
        ).fetchall()
        # Done-drop: claimed (handled=1, so archive can sweep it) but not handed
        # to the caller. task_done is False for ordinary replies in a hub's inbox
        # (nothing links to a reply), so only requeued-done tasks are dropped.
        rows = [r for r in rows if not task_done(conn, r[0])]
    # Handing over a task starts the busy heartbeat. Same in watch() since
    # 2026-09-21 (#1521): the Monitor host kills the watcher at its 30-min cap,
    # so "watch never returns" no longer holds and the Monitor path IS exposed.
    # msg_type is column 4 of the RETURNING tuple.
    if any(r[4] == "task" for r in rows):
        _spawn_busy_beater(me)
    return sorted(rows, key=lambda r: r[0])  # RETURNING order is unspecified


def recv_block(me, timeout, interval, count=1, keep_heartbeat=False):
    """Like recv(), but if fewer than `count` messages are waiting, poll until
    `count` messages for `me` have arrived (accumulated across polls) or
    `timeout` seconds elapse. Returns the rows (fewer than `count`, possibly
    empty, on timeout).

    This is the push primitive for the lightweight watcher pattern: A sends a
    task to B, then runs this as a BACKGROUND bash command. The process stays
    parked until B replies (or timeout), at which point it exits and the harness
    re-invokes A with the reply — no polling loop in the agent itself.

    count>1 is the BARRIER primitive for parallel fan-out: A dispatches to N
    workers (`--to B,C,D`), then a SINGLE `recv --count N --block` parks until
    all N have replied and returns them together. The tally lives inside this
    one blocking process, NOT across A's turns — so it survives context
    compression, unlike A re-arming N separate watchers and counting replies by
    hand (which silently breaks when the count is lost on compaction). On
    timeout it returns the k<N collected so far; A diffs the senders it got
    against the recipients it fanned out to, to find who is absent, then
    probes/re-dispatches those (see the multi-terminal-ipc skill). count<=1 keeps the original
    "return on first message" behaviour unchanged.
    """
    deadline = time.monotonic() + timeout
    collected = []
    try:
        while True:
            _beat(me)  # tell the other terminal this watcher is parked & listening
            rows = recv(me)
            if rows:
                collected.extend(rows)
                if len(collected) >= count:
                    return collected
            if time.monotonic() >= deadline:
                return collected
            # Sleep, but never overshoot the deadline.
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    finally:
        if keep_heartbeat:
            # Smooth handoff for a supervisor that re-arms recv immediately (the
            # Codex receiver loop: it can only start the next recv AFTER this one
            # returns). Deleting the beat here opens a TOCTOU window in which
            # status --watch reports DOWN, ipc_role reports DORMANT, and
            # send --require-watcher REFUSES a worker that is in fact alive.
            # Measured on the Codex bridge (2026-07-28, 10ms sampling): the
            # re-arm gap runs 2.7-5.6s (median 3.7s), i.e. seconds, not
            # sub-second — well inside max_age but far too long to sample past.
            # Refreshing AT the return boundary (rather than merely skipping the
            # delete) hands the successor a full max_age window instead of
            # whatever was left of the last ordinary beat.
            # Failure mode is bounded and already part of the protocol: if the
            # supervisor dies right here, this beat ages out under the same
            # max_age as a watcher killed immediately after its last beat.
            _beat(me)
        else:
            # Best-effort cleanup on normal exit/timeout so the heartbeat goes
            # stale immediately rather than waiting out max_age. A killed watcher
            # skips this, but staleness still ages it out.
            try:
                os.remove(_heartbeat_path(me))
            except OSError:
                pass


def _bump_gen(conn, role):
    """Atomically increment and return the new generation token for `role`.
    A new watch() calls this on startup to become the latest generation; any
    older watcher still polling this role will see gen > its own and retire.
    INSERT ... ON CONFLICT DO UPDATE ... RETURNING runs under SQLite's write
    lock, so two concurrent bumps can't both win — the higher gen is unique."""
    return conn.execute(
        "INSERT INTO watcher_gen(role, gen) VALUES(?, 1) "
        "ON CONFLICT(role) DO UPDATE SET gen = gen + 1 "
        "RETURNING gen",
        (role,),
    ).fetchone()[0]


def _current_gen(conn, role):
    """The latest generation token for `role`, or 0 if no watcher ever started.
    A watcher whose own gen < current has been superseded and must retire."""
    row = conn.execute(
        "SELECT gen FROM watcher_gen WHERE role=?", (role,)
    ).fetchone()
    return row[0] if row else 0


# Liveness anchors (#4, orphan prevention): a watcher must die with whatever
# launched it. Harness-managed watchers are killed by the Monitor, and a NEW
# same-role watcher retires the old one via gen tokens — but a manually launched
# watcher whose role nobody re-takes had NO anchor and could squat "live" for
# days (seen 2026-07-03: a bare `watch --me A` beat for 20h after its purpose
# ended). Two cheap per-poll checks close that class:
#   * anchor-process death — at startup pin the process whose lifetime this
#     watcher should share and grab a SYNCHRONIZE handle to it (the handle pins
#     the process object, immune to PID reuse); each poll a 0-timeout wait tells
#     us if it exited. The pinned process is NOT the direct parent: review #537
#     (2026-08-21) proved the direct parent is a shell-snapshot bash wrapper
#     chain that OUTLIVES the TUI (python <- bash x3 <- claude.exe), which let a
#     watcher survive its dead TUI for 30+ min while the same surviving bash
#     chain also held the stdout pipe open (so _StdoutDead stayed silent too).
#     Pin order: IPC_ANCHOR_PID env (bringup scripts pass the TUI pid
#     explicitly) > nearest TUI-host ancestor (claude/codex/node, via a
#     Toolhelp32 walk) > direct parent (legacy fallback). POSIX keeps the
#     original getppid()-changed detection. Anchor gone -> retire.
#   * registry owner change — snapshot this role's owner in ipc_roles.json at
#     startup; if a later poll sees a DIFFERENT owner (another session took the
#     role, or SessionEnd released it), this watcher is obsolete -> retire.
#     Read errors return _REG_ERR and skip the check (never retire on a
#     transient failure; missing registry = anchor off). Startup exception
#     (#537 bring-up ordering trap): see watch() — an unclaimed/manual owner
#     turning into a real claim moments after this watcher started is our own
#     session's take, not an eviction, and is adopted instead of retired on.

_REG_ERR = object()  # sentinel: registry unreadable this poll (skip, don't retire)
_REG_ANCHOR_GRACE = 45   # s after watch start in which None/manual -> claimed
                         # is adopted as "my own take" instead of retiring

_WAIT_OBJECT_0 = 0
_SYNCHRONIZE = 0x00100000

# Executables that host an interactive agent session ("the TUI"). The nearest
# ancestor with one of these names is what a pane's watcher should die with.
_TUI_EXES = {"claude.exe", "codex.exe", "node.exe"}


def _process_table():
    """{pid: (ppid, exe_name_lowercase)} for every live process, via a
    Toolhelp32 snapshot (Windows only). Raises on API failure — callers fall
    back to pinning the direct parent, the pre-#537 behaviour."""
    import ctypes
    from ctypes import wintypes
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p,
                                   ctypes.POINTER(PROCESSENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    table = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            table[int(e.th32ProcessID)] = (int(e.th32ParentProcessID),
                                           e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return table


def _anchor_target():
    """(pid, label) of the process whose death should retire this watcher.
    Windows: IPC_ANCHOR_PID env > nearest TUI-host ancestor > direct parent.
    POSIX: always the direct parent (getppid-changed detection unchanged)."""
    ppid = os.getppid()
    if os.name != "nt":
        return ppid, f"ppid:{ppid}"
    env = (os.environ.get("IPC_ANCHOR_PID") or "").strip()
    if env.isdigit():
        return int(env), f"env:{env}"
    try:
        table = _process_table()
        pid, hops = ppid, 0
        while pid and pid in table and hops < 12:
            parent, exe = table[pid]
            if exe in _TUI_EXES:
                return pid, f"{exe}:{pid}"
            if parent == pid:
                break
            pid, hops = parent, hops + 1
    except Exception:
        pass  # snapshot unavailable: legacy direct-parent pinning
    return ppid, f"ppid:{ppid}"


def _parent_anchor():
    """(handle, pid, label) pinning the anchor process for death-detection.
    Windows: a SYNCHRONIZE handle (or None if OpenProcess failed -> anchor off).
    POSIX: (None, ppid, label) — death detected by getppid() changing."""
    pid, label = _anchor_target()
    if os.name != "nt":
        return None, pid, label
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    return (h or None), pid, label


def _parent_dead(handle, start_ppid):
    """Has the anchor process (see _anchor_target) exited?"""
    if os.name == "nt":
        if handle is None:
            return False  # couldn't pin the anchor; anchor off, never false-fire
        import ctypes
        return (ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
                == _WAIT_OBJECT_0)
    return os.getppid() != start_ppid


def _registry_owner(role):
    """session_id owning `role` in the registry: a string, None (unclaimed /
    role absent), or _REG_ERR (registry unreadable — treat as no-signal)."""
    try:
        with open(_REGISTRY, encoding="utf-8") as f:
            v = json.load(f).get(role)
        return v.get("session_id") if isinstance(v, dict) else None
    except (OSError, ValueError):
        return _REG_ERR


class _StdoutDead(Exception):
    """watch()-internal: the signal pipe (stdout) is gone — the Monitor/session
    that owns this watcher is dead, so this process is an orphan. Raised INSIDE
    the claim transaction so the sqlite connection context manager rolls the
    claim back (the un-signalled message returns to QUEUED for the next real
    consumer instead of being swallowed handled=1), then handled by watch() as:
    drop heartbeat, leave a disk trace, clean exit. Before 2026-08-17 this case
    was an ACCIDENT: print raised, the except handler's own sys.stderr.write
    raised again (stderr equally dead), the escape rolled back the claim and
    killed the process — right outcome, but the heartbeat file stayed looking
    fresh for max_age and nothing on disk said why (#1186 incident)."""


def _stdout_is_regular_file():
    """True iff stdout is a plain on-disk file — the signature of a watch()
    hosted in a background Bash task (its output is redirected to a task file
    the harness only surfaces on process EXIT, never per line). A Monitor and a
    foreground shell both give a pipe/console. Probe failure returns False:
    never refuse on an inconclusive probe (a false refusal is worse than
    letting an exotic host through)."""
    try:
        return stat_mod.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
    except (OSError, ValueError, AttributeError):
        return False


_emit_lock = threading.Lock()  # the hub's reply-clock thread prints too: keep lines whole


def _emit(line):
    """Print one watcher line; False if stdout is dead (EPIPE/EINVAL/closed).
    Never raises: emission failure is a liveness signal, not an error."""
    try:
        with _emit_lock:
            print(line, flush=True)
        return True
    except (OSError, ValueError):  # ValueError: I/O operation on closed file
        return False


def _gen_still(role, gen):
    """True while no newer watcher generation exists for `role`."""
    with _conn() as conn:
        return _current_gen(conn, role) <= gen


def _orphan_exit(me, reason):
    """This watcher's consumer is gone: stop advertising the slot as parked
    (remove the heartbeat NOW instead of letting it read fresh for another
    max_age) and leave a trace in the state dir — stdout/stderr are dead, so
    disk is the only place a post-mortem can read. Narrow race accepted: if a
    NEW live watcher just took this role, deleting the shared per-role
    heartbeat blanks its beat for under one poll interval (it re-beats next
    poll) — a brief false DOWN, far cheaper than an orphan's evergreen file."""
    try:
        os.remove(_heartbeat_path(me))
    except OSError:
        pass
    try:
        with open(os.path.join(_STATE_DIR, f"_watcher_{me}.exit.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} pid={os.getpid()} {reason}\n")
    except OSError:
        pass


# --- hub reply clock (2026-10-18: stall_watch.py folded in; DeepSeek review
# #task3.D, user "按DS 方案修改"; v2 after the Codex review #task5.E). The hub's
# watch() starts clock_tick about once a minute on a daemon thread, so A keeps
# ONE Monitor and a slow wezterm probe (25 s timeout) never stalls the hub's
# heartbeat or claims; a Codex hub (recv --json supervisor, whose parser must
# not see clock lines) runs the same tick as `ipc.py clock --block`.
# Verdicts are stall_watch's, but quiet: only lines the hub must act on are
# printed (STALLED TASK, ACT NOW, PROBE-FAILED, TWO EXTENSIONS USED,
# CLOCK-ERROR); a healthy milestone extends silently. The pane is probed once
# for a baseline and once per due milestone (stall_watch probed every 60 s);
# activity between two checks = a different tail hash. The newest artifact is
# printed on the line as ground truth but never counts as activity: any writer
# (backup, sync, the hub editing a file the brief names) would mask a stall
# (DeepSeek #task7.D); a tail that went a -> b -> a therefore reads idle, which
# costs one look, not a missed stall. A milestone whose probe failed prints
# PROBE-FAILED once and stays due, so a pane that comes back DEAD after
# unreadable checks is still judged (#task7.D). Per-task state lives in _clock_<hub>.json:
# stall_watch kept it in memory, so every 30-minute Monitor re-arm repeated
# CLOCK-START and passed milestones and could call a working pane "idle &
# unchanged". A tick whose watcher was superseded (generation) neither prints
# nor saves; a failed print sets _clock_stdout_dead so watch() orphan-exits.
_CLOCK_EVERY = 60.0
_CLOCK_MILESTONES = (15, 30, 45)
_CLOCK_BAD = ("QUEUED-STALLED", "IN-PROGRESS-SILENT", "NEEDS-REVIEW")
_CLOCK_ACT_FLAGS = ("recv_loop", "permission_prompt", "quota_banner", "notice_overlay")
_CLOCK_ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s`'\"()（）,，。;；|<>]+")  # same shape as ipc_brief_lint
_clock_last_error = None
_clock_stdout_dead = threading.Event()


class _ClockAbort(Exception):
    """Stop this tick without saving: stdout died or the watcher was superseded."""


def _clock_path(hub):
    return os.path.join(_STATE_DIR, f"_clock_{hub}.json")


def _clock_load(hub):
    try:
        with open(_clock_path(hub), encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return tasks if isinstance(tasks, dict) else {}
    except (OSError, ValueError):
        return {}


def _clock_entry(raw):
    """One task's clock state, normalized: a malformed entry restarts that
    task's clock instead of raising for the whole batch."""
    t = raw if isinstance(raw, dict) else {}

    def milestones(v):
        return sorted({m for m in v if isinstance(m, int) and m in _CLOCK_MILESTONES}) if isinstance(v, list) else []
    bad = t.get("bad")
    out = {"reported": milestones(t.get("reported")),   # milestones with a verdict
           "pf": milestones(t.get("pf")),               # milestones whose PROBE-FAILED was printed
           "bad": [b for b in bad if isinstance(b, str)] if isinstance(bad, list) else []}
    for k in ("hash", "to"):
        if isinstance(t.get(k), str):
            out[k] = t[k]
    return out


def _clock_save(hub, tasks):
    path = _clock_path(hub)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f)
    os.replace(tmp, path)


def _clock_owner_path(hub):
    return os.path.join(_STATE_DIR, f"_clock_{hub}.owner")


def _clock_mark_owner(hub):
    """watch()'s main loop marks that it runs the hub clock, once a tick. The
    main loop, not the tick thread: a tick slowed by unreadable panes must not
    age the mark (#task9.D N2). Not the heartbeat: recv --block beats it too."""
    try:
        with open(_clock_owner_path(hub), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
    except OSError:
        pass


def _clock_watch_owns(hub):
    """True if a hub watch() marked the clock within three ticks. A mark from
    the future (clock set back) does not count (#task9.D N3)."""
    try:
        age = time.time() - os.path.getmtime(_clock_owner_path(hub))
    except OSError:
        return False
    return 0 <= age < 3 * _CLOCK_EVERY


def _artifact_best(tid):
    """(path, mtime, size) of the newest existing file named by absolute path in
    task #tid's brief, or None. Inputs (reference PDFs) are old, products are
    fresh, so 'newest wins' picks the product; a directory token contributes its
    newest direct child. Wezterm-free: readable when panes are not."""
    try:
        with _conn() as conn:
            row = conn.execute("SELECT body FROM messages WHERE id=?", (tid,)).fetchone()
        body = row[0] if row and row[0] else ""
    except Exception:  # noqa: BLE001 — ground truth is advisory; never break the clock
        body = ""
    best = None
    for tok in set(_CLOCK_ABS_PATH.findall(body)):
        p = tok.rstrip("\\.:")
        if os.path.isfile(p):
            cands = [p]
        elif os.path.isdir(p):
            try:
                cands = [os.path.join(p, f) for f in os.listdir(p)]
            except OSError:
                cands = []
        else:
            cands = []
        for c in cands:
            try:
                st = os.stat(c)
            except OSError:
                continue
            if os.path.isfile(c) and (best is None or st.st_mtime > best[1]):
                best = (c, st.st_mtime, st.st_size)
    return best


def _artifact_text(best, now):
    if not best:
        return "no-artifact"
    age = (now.timestamp() - best[1]) / 60
    return (f"artifact={os.path.basename(best[0])} "
            f"mtime={datetime.fromtimestamp(best[1]).strftime('%H:%M:%S')} "
            f"({age:.0f}m ago) {best[2]}B")


def _clock_action(p, m, changed):
    """Verdict for one due milestone; None = healthy (extend silently).
    changed=None means "no baseline yet": only verdicts that need no history
    (probe failure, DEAD/QUOTA/BLOCKED, act flags, 45 min) can fire."""
    if not p or p.get("error") or p.get("tail_hash") is None:
        # 2026-08-30 (X, #1175): probe failure is NOT data. Say why the pane is
        # unreadable and decide nothing.
        why = (p.get("error") if p else "probe returned nothing") or "no pane text"
        return f"PROBE-FAILED: cannot read pane ({why}) - no verdict; use the artifact ground truth on this line"
    if p["verdict"].startswith(("DEAD", "QUOTA", "BLOCKED")):
        action = f"ACT NOW: {p['verdict']}"
        if p["verdict"] == "BLOCKED-NOTICE":
            action += (" -> dismiss: wezterm cli --no-auto-start send-text --no-paste "
                       "--pane-id <pane> $'\\r' (Codex update prompt: Esc), then re-probe")
        return action
    if any(f in p["flags"] for f in _CLOCK_ACT_FLAGS):
        return "ACT NOW: " + ",".join(f for f in p["flags"] if f in _CLOCK_ACT_FLAGS)
    if m >= 45:
        return "TWO EXTENSIONS USED -> reassign (cancel + same --submit-id family)"
    if changed is False and "working" not in p["flags"]:
        return "ACT NOW: pane idle & unchanged since the last check (wake / redeliver)"
    return None


def _clock_still_open(rid):
    """The task is still dispatched and unanswered (a slow probe may outlive it)."""
    with _conn() as conn:
        row = conn.execute("SELECT tombstone FROM messages WHERE id=?", (rid,)).fetchone()
        return bool(row) and row[0] is None and not task_done(conn, rid)


def _clock_task(rid, to, ts, st, attempts, t, probe_fn, now, emit):
    if t.get("to") not in (None, to):
        t.pop("hash", None)   # redelivered to another role: its pane needs its own baseline
    if to:
        t["to"] = to
    bad_key = f"{st}@{attempts}"   # a requeued task re-entering the same bad state reports again
    if st in _CLOCK_BAD and bad_key not in t["bad"]:
        emit(f"STALLED TASK: #{rid} -> {to} {st} attempts={attempts}")
        t["bad"].append(bad_key)
    try:
        age = (now - datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
    except (TypeError, ValueError):
        return
    due = [m for m in _CLOCK_MILESTONES if age >= m and m not in t["reported"]]
    if not due and "hash" in t:
        return  # between milestones: no probe
    p = probe_fn(to) if to else None
    h = p.get("tail_hash") if p else None
    if "hash" in t:
        changed = h is not None and h != t["hash"]
    elif h:
        changed = None  # baseline now: only history-free verdicts may fire this tick
    elif not due:
        return  # baseline probe failed before any milestone: retry next tick
    else:
        changed = False  # no baseline and still unreadable at a milestone -> PROBE-FAILED
    if due:
        m = max(due)
        action = _clock_action(p, m, changed)
        failed = bool(action) and action.startswith("PROBE-FAILED")
        if action and not (failed and m in t["pf"]) and _clock_still_open(rid):
            ctx = "?" if not p or p.get("ctx_left") is None else f'{p["ctx_left"]}%'
            flags = ",".join(p["flags"]) if p and p.get("flags") else ("-" if p else "probe-failed")
            tail = "?" if changed is None else ("CHANGED" if changed else "SAME")
            emit(f"REPLY-CLOCK #{rid} -> {to} {m}m (since {ts[11:16]}) | ctx {ctx} "
                 f"flags={flags} tail={tail} | {action} | {_artifact_text(_artifact_best(rid), now)}")
        if failed:
            t["pf"] = sorted(set(t["pf"]) | {m})   # said once; the milestone stays due for a real verdict
        elif action or changed is not None:
            # a baseline-only pass with a healthy pane leaves the milestone due: judged next tick
            t["reported"] = sorted(set(t["reported"]) | set(due))
    if h:
        t["hash"] = h


def clock_tick(hub, probe_fn=None, now=None, is_current=None):
    """One pass of the hub reply clock; returns the lines it printed. Never
    raises (the hub's watch thread calls it). is_current() False (watcher
    superseded) aborts the tick before any print or save. A failed print sets
    _clock_stdout_dead. Errors print one CLOCK-ERROR line per distinct message;
    a task whose own step fails does not stop the others."""
    global _clock_last_error
    printed = []

    def emit(line):
        if is_current is not None and not is_current():
            raise _ClockAbort("superseded")
        if not _emit(line):
            _clock_stdout_dead.set()
            raise _ClockAbort("stdout dead")
        printed.append(line)

    errors = []
    try:
        if probe_fn is None:
            import ipc_pane_probe  # noqa: WPS433 — lives next to ipc.py; lazy so workers never load it
            probe_fn = ipc_pane_probe.probe
        now = now or datetime.now()
        tasks = _clock_load(hub)
        live = {}
        for rid, to, ts, st, attempts in pending(hub):
            t = _clock_entry(tasks.get(str(rid)))
            live[str(rid)] = t
            try:
                _clock_task(rid, to, ts, st, attempts, t, probe_fn, now, emit)
            except _ClockAbort:
                raise
            except Exception as e:  # noqa: BLE001 — one task's failure must not blind the rest
                errors.append(f"#{rid} {type(e).__name__}: {e}")
        if is_current is not None and not is_current():
            return printed
        _clock_save(hub, live)  # finished tasks drop out here
    except _ClockAbort:
        return printed
    except Exception as e:  # noqa: BLE001 — the clock must never take the hub's watch down
        errors.append(f"{type(e).__name__}: {e}")
    msg = "; ".join(errors) or None
    if msg and msg != _clock_last_error:
        # On stdout: a quiet clock that fails silently would read exactly like
        # "every task healthy".
        try:
            emit(f"CLOCK-ERROR: {msg} - the reply clock is blind there until this clears")
        except _ClockAbort:
            return printed
    _clock_last_error = msg
    return printed


def clock_loop(hub, interval, once=False, block=False, timeout=580):
    """`ipc.py clock`: the reply clock as its own process, for a hub whose
    receiver is not watch() (the Codex recv --json supervisor). Claude's watch
    --me <hub> runs it built in. --block follows recv --block's contract for
    that supervisor: exit 0 right after a tick that printed, exit 2 with no
    output at --timeout. Exit 3 = superseded by a newer clock process (one
    line, do not restart); exit 1 = refused (the hub's watch already runs the
    clock) or stdout dead. Returns the exit code."""
    with _conn() as conn:
        my_gen = _bump_gen(conn, f"clock-{hub}")  # a newer clock process retires this one

    def current():
        with _conn() as c:
            return _current_gen(c, f"clock-{hub}") <= my_gen

    if not (once or block) and not _emit(f"CLOCK for {hub} online (quiet: prints only lines the hub must act on)"):
        return 1
    deadline = time.time() + timeout
    while True:
        if _clock_watch_owns(hub):
            # Two clocks on one mailbox overwrite each other's state (#task7.D);
            # checked every tick, so a watch started later takes over (#task9.D N1).
            _emit(f"CLOCK REFUSED for {hub}: `watch --me {hub}` runs the reply clock built in")
            return 1
        lines = clock_tick(hub, is_current=current)
        if _clock_stdout_dead.is_set():
            return 1
        if once or (block and lines):
            return 0
        if not current():
            # not exit 0: a read-at-exit supervisor would wake the model for nothing (#task7.D)
            _emit(f"CLOCK for {hub} superseded by a newer clock process - not restarting")
            return 3
        if block and time.time() >= deadline:
            return 2
        time.sleep(interval)


def watch(me, interval, allow_file_stdout=False):
    """Poll forever, printing a tiny SIGNAL for each new message for `me`.
    Designed to run under the Monitor tool with persistent=true: each printed
    line becomes an event/notification, so ONE long-lived Monitor replaces
    re-arming a `recv --block` bash watcher every ~580s. Wins over the bash
    watcher (LOCAL TRIAL 2026-06-27): ~zero idle agent turns (only fires on a
    real message, no spurious timeout wakes), survives across turns AND user
    input (Monitor is session-scoped; a backgrounded bash watcher gets killed by
    a new turn), reachability intact (polls every `interval`s). Lifetime is owned
    by the Monitor (TaskStop or session end); this loop never exits. Refreshes the
    same heartbeat file as recv_block so `status --watch` still reports live.

    SIGNAL-ONLY (2026-06-27, fixes the truncation problem): the printed line is
    just `NEW MSG #id from SENDER (N chars) — read full: ipc.py peek ...`, never
    the body. The harness notification layer truncates long event text, so putting
    the body inline silently lost the tail of long messages; a tiny fixed-size
    signal can never be truncated. On the notification, the agent reads the FULL
    message with `peek --me <me>` (peek shows handled rows too). One short read per
    message — same cost as the old bash watcher's Read — in exchange for guaranteed
    no truncation.

    Atomic claim-then-signal: each row is claimed by ONE `_claim_one` UPDATE
    (under SQLite's write lock) before its signal is printed, so a watch and a
    stray recv on the same inbox can never both announce/consume the same message
    — this is what lets us drop the old "one watcher per inbox" discipline. The
    narrow cost: a crash in the microsecond between claim and print loses a
    SIGNAL (not the body — `peek` still shows it), far cheaper than the
    double-delivery the old non-atomic select-then-mark allowed. `handled` means
    "signalled" (a watch restart won't re-announce); the body stays for `peek`.
    Per-message and per-poll try/except so one bad message or a transient DB error
    can't kill the loop. If a burst arrives, several signals may batch into one
    notification — fine, each carries its own `#id`; peek `--tail` enough to cover
    them. Keep `interval` < `status --max-age` (default 8s) or the heartbeat ages
    out and `status` reports DOWN while watch is running.

    Generation tokens (#3, orphan retirement): on startup this watcher bumps its
    role's gen and becomes the latest. Any older watcher still polling this role
    reads gen > its own next poll and RETIRES (clean return, no pid-kill — avoids
    Windows os.kill(pid,0) mis-fire). The gen is re-checked before each inner claim
    so a superseded watcher stops claiming the instant it's overtaken, narrowing
    the black-hole window to < one claim. recv_block is intentionally NOT gen-gated
    — it has a timeout and also serves as A's --count barrier; bumping gen there
    would let a barrier retire the long-lived watch. Only the infinite watch() loop
    is gen-gated. On retirement we do NOT remove the heartbeat file: it is shared
    per-role (one _watcher_<me>.alive), so removing it would delete the NEW live
    watcher's heartbeat and briefly false-report DOWN; the retired watcher simply
    stops touching it and the new watcher's _beat owns its mtime."""
    # Host gate (2026-08-17, #1188 type-2 incident): a watch whose stdout is a
    # regular file is hosted in a background Bash task — its signals land in a
    # file the harness surfaces only on process EXIT, so a claimed task's
    # signal can sit unread for hours while status/busy/reaper all say fine
    # (observed: 1h55m delivery gap). Refusing HERE is self-correcting: in a
    # background bash the immediate exit fires the task-notification at once,
    # so the agent reads this message seconds later and re-arms under Monitor.
    if _stdout_is_regular_file() and not allow_file_stdout:
        print(f"WATCH REFUSED for {me}: stdout is a regular file — this watch "
              f"is hosted in a background shell, whose output is only "
              f"surfaced when the process exits. Signals would black-hole "
              f"(incident #1188: 1h55m delivery gap). Host the watcher under "
              f"the Monitor tool (persistent) instead: "
              f'python "{os.path.abspath(__file__)}" watch --me {me} . '
              f"If file-stdout hosting is genuinely intended, re-run with "
              f"--allow-file-stdout.", flush=True)
        sys.exit(4)
    with _conn() as conn:
        my_gen = _bump_gen(conn, me)
    parent_h, parent_pid, anchor_label = _parent_anchor()
    t0 = time.time()              # registry anchor's bring-up grace window base
    owner0 = _registry_owner(me)  # snapshot; owner drift => this watcher is obsolete
    anchors = (
        f"anchors: parent={anchor_label if (parent_h or os.name != 'nt') else 'off'}, "
        f"registry={'off' if owner0 is _REG_ERR else 'on'}"
    )
    if not _emit(f"WATCHER #{my_gen} for {me} online ({anchors})"):
        return  # stdout dead before we ever advertised: nothing to clean up
    clock_at, clock_thread = 0.0, None  # hub only: reply clock ticks from this loop (2026-10-18)
    while True:
        claimed_task = False
        try:
            with _txn() as conn:
                cur = _current_gen(conn, me)
                if cur > my_gen:
                    _emit(
                        f"WATCHER for {me} retired: superseded by gen {cur} "
                        f"(was #{my_gen})"
                    )
                    return  # clean exit; the Monitor task ends — no orphan black hole
                if _parent_dead(parent_h, parent_pid):
                    _emit(
                        f"WATCHER for {me} retired: parent process gone "
                        f"(was #{my_gen})"
                    )
                    return
                owner = _registry_owner(me)
                if (owner0 is not _REG_ERR and owner is not _REG_ERR
                        and owner != owner0):
                    if (owner is not None
                            and owner0 in (None, "manual")  # ipc_role.MANUAL_SID
                            and time.time() - t0 <= _REG_ANCHOR_GRACE):
                        # Bring-up ordering trap (#537): "take the role, THEN
                        # park the watcher" inverted — a claim landing moments
                        # after this watcher started is this pane's own take,
                        # not a foreign eviction. Adopt the new owner. A change
                        # to None (a release) never gets this grace: that is
                        # exactly the orphan-retirement signal the anchor is for.
                        _emit(
                            f"WATCHER for {me}: adopted bring-up claim "
                            f"{owner0!r} -> {owner!r} (startup grace)"
                        )
                        owner0 = owner
                    else:
                        _emit(
                            f"WATCHER for {me} retired: registry owner changed "
                            f"{owner0!r} -> {owner!r} (was #{my_gen})"
                        )
                        return
                _beat(me)
                _reap_stale(conn, me=me)  # re-expose this worker's orphaned claims first
                while True:
                    # Re-check gen before each claim: a superseded watcher stops
                    # claiming the moment it's overtaken (black-hole window < 1 claim).
                    cur = _current_gen(conn, me)
                    if cur > my_gen:
                        print(
                            f"WATCHER for {me} retired: superseded by gen {cur} "
                            f"(was #{my_gen})",
                            flush=True,
                        )
                        return
                    row = _claim_one(conn, me)  # atomic: each row to exactly one consumer
                    if row is None:
                        break
                    mid, ts, sender, body, mtype, in_reply_to, result, n_new = row
                    if task_done(conn, mid):
                        # Done-drop (same as recv): a requeued-but-already-
                        # answered task is finished work; claim it silently,
                        # never re-announce it.
                        continue
                    if mtype == "reply" and result == "CLEAN":
                        # RESULT-line protocol: product verified on disk. Hold
                        # (announced=0) instead of signalling; _flush_held
                        # emits a one-line DONE summary when the fan-out
                        # completes, a non-CLEAN signal goes out, or the hold
                        # times out. The hub never peeks this body.
                        conn.execute("UPDATE messages SET announced=0 WHERE id=?",
                                     (mid,))
                        continue
                    # A non-CLEAN signal is going out: flush held summaries
                    # first so the hub sees fan-out state in order.
                    if not _flush_held(conn, me, force=True):
                        raise _StdoutDead(mid)
                    # SIGNAL ONLY (never the body): a tiny line that can never be
                    # truncated by the harness notification layer. Read the full
                    # message with `peek` (see below). A bodyless done-ack
                    # (msg_type='ack', empty body) is a lifecycle marker, not
                    # content — surface it as "TASK #N DONE" instead of a noisy
                    # "0 chars" NEW MSG. Display-layer only; claim/done semantics
                    # unchanged (done still derived from in_reply_to in task_done).
                    # Emission failure = our consumer is dead: raise INSIDE the
                    # claim transaction so the claim rolls back (message stays
                    # QUEUED, never swallowed), then orphan-exit below.
                    if mtype == "ack" and not body:
                        tgt = in_reply_to if in_reply_to is not None else "?"
                        if not _emit(f"TASK #{tgt} DONE (ack from {sender})"):
                            raise _StdoutDead(mid)
                        continue
                    # Absolute path in the hint: under the user-level install
                    # a bare `python ipc.py` copy-pasted from this signal
                    # fails (ipc.py is not in the project cwd).
                    tag = ""
                    if result and result.split(":", 1)[0] in ("UNSOLVABLE", "REPLAN"):
                        tag = f" [{result.split(':', 1)[0]}]"
                    if mtype in ("reply", "fail") and in_reply_to is not None:
                        # 2026-10-17: stored NEW item count + the quotable anchor
                        tag += ((f" NEW {n_new}" if n_new else "")
                                + f" anchor {_anchor(in_reply_to, sender)}")
                    if not _emit(
                            f"NEW MSG #{mid} from {sender} ({len(body)} chars){tag} "
                            f'— read full: python "{os.path.abspath(__file__)}" '
                            f"peek --me {me} --id {mid}"):
                        raise _StdoutDead(mid)
                    if mtype == "task":
                        claimed_task = True
                # Held CLEAN replies: flush when the fan-out is complete or the
                # hold timed out (stdout death here just retries next poll —
                # the hold is durable in the DB).
                _flush_held(conn, me)
            if _clock_stdout_dead.is_set():
                raise _StdoutDead("reply-clock line")
            if me == HUB and time.time() - clock_at >= _CLOCK_EVERY:
                clock_at = time.time()
                _clock_mark_owner(HUB)  # from this loop, so a slow tick cannot age it
                if not (clock_thread and clock_thread.is_alive()):
                    # A daemon thread, outside the claim transaction (pending()
                    # opens its own): a probe stuck on its 25 s wezterm timeout
                    # must not stall this loop's heartbeat and claims (#task5.E).
                    # The tick checks the generation before printing or saving.
                    clock_thread = threading.Thread(
                        target=clock_tick, args=(HUB,), daemon=True,
                        kwargs={"is_current": lambda: _gen_still(me, my_gen)})
                    clock_thread.start()
            if claimed_task:
                # 2026-09-21 (#1521 root cause): the Monitor host caps a watcher
                # at 30 min (Claude Code 2.1.278 clamps timeout_ms to 1800000)
                # and kills it regardless of the task in flight. The worker
                # cannot re-arm until its turn ends, so a Monitor-path worker
                # mid-task executes with NO heartbeat: .alive stale, .busy never
                # written (this path used to skip the beater on the premise that
                # "watch never returns"). _lease_alive then fails at +24s, the
                # hub's pending() requeues the row (QUEUED-STALLED attempts=1),
                # and the re-armed watcher re-claims it (attempts=2, #1488/#1494/
                # #1499). Fork the same detached beater recv() forks: it is
                # DETACHED_PROCESS, outlives this watcher, and exits on its own
                # once the task is done/failed/cancelled or its lease has fallen.
                _spawn_busy_beater(me)
        except _StdoutDead as e:
            # Leaving the `with _conn()` block via this exception rolled the
            # claim back: the un-signalled message is QUEUED again, attempts
            # unchanged. Make the death VISIBLE (heartbeat gone at once, trace
            # on disk) instead of camouflaged (#1186: fresh-looking heartbeat
            # for max_age after death, no trace anywhere).
            _orphan_exit(me, f"gen #{my_gen}: stdout dead on signal for "
                             f"#{e.args[0]}; claim rolled back")
            return
        except Exception as e:  # noqa: BLE001 — transient DB error: log, keep polling
            try:
                sys.stderr.write(f"[watch] poll error: {e}\n")
            except (OSError, ValueError):
                pass  # stderr dead too; never let the handler itself raise
        time.sleep(interval)


_BATCH_KEEP = 20  # numbered recv --json batches kept per role (oldest pruned); 100 until 2026-10-17, cut once peek --id read the archive table
_BATCH_RE = re.compile(r"^_recv_(?P<me>[A-Za-z0-9_]+)_batch_(?P<n>\d+)\.jsonl$")


def _batch_path(me, n):
    return os.path.join(_STATE_DIR, f"_recv_{me}_batch_{n:06d}.jsonl")


def _batch_numbers(me):
    """Sorted batch numbers saved for `me` (empty when none)."""
    out = []
    try:
        names = os.listdir(_STATE_DIR)
    except OSError:
        return out
    for name in names:
        m = _BATCH_RE.match(name)
        if m and m.group("me") == me:
            out.append(int(m.group("n")))
    return sorted(out)


def _batch_complete(me, n):
    """False for a number _reserve_batch took but _write_batch never filled (the
    receiver died between the two): the file exists and is empty. A complete
    batch always holds at least one line (an empty recv saves nothing)."""
    try:
        return os.path.getsize(_batch_path(me, n)) > 0
    except OSError:
        return False


def _next_batch(me):
    nums = _batch_numbers(me)
    return (nums[-1] + 1) if nums else 1


def _reserve_batch(me):
    """Atomically claim the next batch number for `me`: create the numbered file
    with O_EXCL and move on when another receiver already took that number
    (2026-10-16 review: two interleaved receivers both computed 1 and the
    second overwrote the first). The reserved file stays empty until
    _write_batch fills it, so a reader sees either nothing or the full batch."""
    n = _next_batch(me)
    while True:
        try:
            os.close(os.open(_batch_path(me, n), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return n
        except FileExistsError:
            n += 1


def _write_batch(me, n, lines):
    """Complete copy of one `recv --json` batch for `me`: one envelope per line,
    into the file reserved by _reserve_batch (never another number's file).
    Called BEFORE the envelopes are printed: the rows are already claimed and
    the caller's output channel may break or truncate; the file does not, and
    `peek --batch [N]` / `--batches` read it back. When the save itself fails
    (OSError) the caller still prints the envelopes with batch=null and names the
    claimed ids on stderr (BATCH-SAVE-FAILED): then the printed output is the only
    copy and bodies come from `peek --id`. Files beyond _BATCH_KEEP per role are
    pruned, oldest first."""
    path = _batch_path(me, n)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    os.replace(tmp, path)
    for old in _batch_numbers(me)[:-_BATCH_KEEP]:
        try:
            os.remove(_batch_path(me, old))
        except OSError:
            pass


def peek(me, tail, ids=None):
    """Show the last `tail` messages involving `me` without marking read.
    With `ids`, show exactly those rows (still only ones involving `me`) in id
    order: the per-message signals name an id, and a batch recv can claim more
    rows than any fixed --tail covers (2026-10-16 Codex review). Each row ends
    with an `archived` element: None for a live row; for an id no longer in
    `messages` (2026-10-17) the row comes from messages_archive and the element is
    (archived_ts, versions): the newest archived version is shown, and versions
    > 1 says the archive holds that id more than once (a re-created mailbox)."""
    cols = ("SELECT id, ts, sender, recipient, body, handled, msg_type, "
            "in_reply_to, session, result")
    with _conn() as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            where = f" WHERE (recipient=? OR sender=?) AND id IN ({marks})"
            rows = [r + (None,) for r in conn.execute(
                cols + " FROM messages" + where, (me, me, *ids)).fetchall()]
            if len(rows) < len(set(ids)) and _has_archive(conn):
                seen = {r[0] for r in rows}
                versions = {}
                for r in conn.execute(cols + ", archived_ts FROM messages_archive" + where
                                      + " ORDER BY id, archived_ts", (me, me, *ids)):
                    if r[0] not in seen:
                        versions.setdefault(r[0], []).append(r)
                rows += [vs[-1][:-1] + ((vs[-1][-1], len(vs)),) for vs in versions.values()]
            return sorted(rows, key=lambda r: r[0])
        rows = conn.execute(
            cols + " FROM messages WHERE recipient=? OR sender=? ORDER BY id DESC LIMIT ?",
            (me, me, tail),
        ).fetchall()
    return [r + (None,) for r in reversed(rows)]


def archive(keep):
    """Move terminal/handled messages except the most recent `keep` rows into
    messages_archive (2026-10-17; it used to delete them). Condition is
    (handled=1 OR tombstone IS NOT NULL) so failed/cancelled rows go even when
    handled=1; requeued rows (handled=0, tombstone NULL) are protected — they're
    active work, not history. R2 line. Unresolved --no-requeue tasks are likewise
    protected (see _ARCHIVE_WHERE): their claim is handled=1 by design while they
    await review, but they're active work. Returns rows moved."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
        ).fetchone()
        if row:
            return _archive_guarded(conn, row[0], fatal=True)
    return 0


def main():
    p = argparse.ArgumentParser(description="SQLite mailbox for two terminals")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", dest="recipient", required=True)
    s.add_argument("body", nargs="?", default=None)
    s.add_argument("--body-file", dest="body_file", default=None,
                   help="read the message body from this file (UTF-8). Use for "
                        "bodies containing backticks/$()/quotes: a body passed "
                        "as a shell argument gets expanded/mangled by the shell "
                        "(observed twice in practice); a file never touches the "
                        "shell")
    s.add_argument("--require-watcher", action="store_true",
                   help="refuse to queue unless the recipient's --block watcher "
                        "is parked & listening (use for A->B task dispatch)")
    s.add_argument("--max-age", type=float, default=8.0,
                   help="max seconds since the recipient's last heartbeat to "
                        "count as alive (default 8 = 4 missed 2s beats)")
    s.add_argument("--in-reply-to", type=int, default=None,
                   help="task id this message answers (usually auto-linked for "
                        "worker->hub replies; pass explicitly to override)")
    s.add_argument("--type", dest="msg_type", default=None,
                   help="message type override: task|reply|ack|broadcast "
                        "(default: hub->worker=task, worker->hub=reply)")
    s.add_argument("--lease", type=int, default=DEFAULT_LEASE,
                   help="hard lease seconds from send time; 0=pure-heartbeat lease "
                        f"(default {DEFAULT_LEASE}). Two independent staleness "
                        "signals: heartbeat-death vs lease-ceiling (stuck).")
    s.add_argument("--submit-id", dest="submit_id", default=None,
                   help="idempotency key scoped to (sender, recipient): a resend "
                        "with the same key reuses the existing row (prints DUP) "
                        "instead of queuing a duplicate. Makes re-dispatch after "
                        "a barrier timeout safe. cancel/fail re-opens the key.")
    s.add_argument("--budget", type=int, default=None,
                   help="task budget in minutes (default: the brief's `预算: N 分钟` "
                        "line, else none). Stored as budget_min; the hub watcher "
                        "holds CLEAN summaries only for tasks that may finish "
                        "inside the hold window, and ipc_estimate.py learns from it.")
    s.add_argument("--no-requeue", dest="no_requeue", action="store_true",
                   help="fail-closed task: on a dead lease the reaper neither "
                        "requeues nor fails it — it parks as NEEDS-REVIEW in "
                        "pending until the hub cancels or the worker "
                        "done/fail/ack-revives it. Use for non-idempotent work.")

    r = sub.add_parser("recv")
    r.add_argument("--me", required=True)
    r.add_argument("--block", action="store_true",
                   help="wait for a message instead of returning NONE immediately")
    r.add_argument("--timeout", type=int, default=580,
                   help="max seconds to wait in --block mode (stay under bash's 600s cap)")
    r.add_argument("--interval", type=float, default=2.0,
                   help="seconds between polls in --block mode")
    r.add_argument("--keep-heartbeat", action="store_true",
                   help="on return, refresh the heartbeat instead of deleting it, "
                        "so a supervisor that immediately re-arms recv does not "
                        "flicker to DOWN/DORMANT in between. ONLY use it when the "
                        "caller really does re-arm at once (the Codex receiver "
                        "loop); a bare one-shot recv must NOT set it, or the role "
                        "reads alive for up to max_age after you stopped listening.")
    r.add_argument("--count", type=int, default=1,
                   help="BARRIER: in --block mode, wait until this many messages "
                        "have arrived (accumulated) before returning. Use after a "
                        "fan-out (--to B,C,D) so one blocking call collects all N "
                        "replies; the tally lives in the process, not across A's "
                        "context. On timeout returns however many arrived (k<N).")
    r.add_argument("--no-body", action="store_true",
                   help="with --json: body is emitted as \"\" and body_chars carries its length, so a "
                        "receiver that only routes signals never sees message text (bodies can hold "
                        "lines that look like protocol output, and long bodies push ids past an "
                        "output cap); read the text afterwards with peek --id (2026-10-16)")
    r.add_argument("--json", action="store_true",
                   help="emit one JSON envelope per message (NDJSON) instead of "
                        "the plain text line: id/ts/from/type/task/session/body. "
                        "type is the machine-readable outcome (reply|ack|fail|"
                        "task|note); task echoes the in_reply_to link; session "
                        "identifies the sender session (differs from the task's "
                        "claimant => the worker /clear-ed, context is gone).")

    w = sub.add_parser("watch")
    w.add_argument("--me", required=True)
    w.add_argument("--allow-file-stdout", action="store_true",
                   help="escape hatch for the background-shell host gate: run "
                        "even though stdout is a regular file (signals will "
                        "only surface when this process exits — incident "
                        "#1188). Only for deliberate log-to-file setups.")
    w.add_argument("--interval", type=float, default=3.0,
                   help="seconds between polls (default 3). Keep it < status "
                        "--max-age (default 8) or the heartbeat ages out and "
                        "status reports DOWN while watch is running.")

    ck = sub.add_parser(
        "clock",
        help="hub reply clock (15/30/45-min checks of open tasks, quiet unless the hub must act). "
             "Claude's `watch --me <hub>` runs it built in; start this only for a hub whose "
             "receiver is not watch (the Codex recv --json supervisor)")
    ck.add_argument("--hub", default=HUB)
    ck.add_argument("--interval", type=float, default=_CLOCK_EVERY,
                    help=f"seconds between ticks (default {_CLOCK_EVERY:.0f})")
    ck.add_argument("--once", action="store_true", help="one tick, then exit")
    ck.add_argument("--block", action="store_true",
                    help="for a supervisor that reads output at process exit (Codex): tick every "
                         "--interval, exit 0 right after a tick that printed, exit 2 with no output "
                         "at --timeout (recv --block's contract); state persists between runs")
    ck.add_argument("--timeout", type=int, default=580,
                    help="max seconds in --block mode (default 580)")

    k = sub.add_parser("peek")
    k.add_argument("--me", required=True)
    k.add_argument("--tail", type=int, default=5)
    k.add_argument("--batch", nargs="?", const=-1, type=int, default=None, metavar="N",
                   help="print one complete recv --json batch for --me from the state dir: the latest "
                        "without N, batch N otherwise (one line per message; --json for the raw "
                        "envelopes). The recovery path when a receiver's tool output was truncated; "
                        "every envelope carries its batch number")
    k.add_argument("--batches", action="store_true",
                   help="list the saved recv --json batches for --me (number, count, id range)")
    k.add_argument("--id", type=int, nargs="+", dest="ids",
                   help="show exactly these message ids (from NEW MSG / DONE signals) instead of --tail")
    k.add_argument("--json", action="store_true",
                   help="emit one JSON envelope per message (NDJSON): "
                        "id/ts/from/to/type/task/session/unread/body")

    a = sub.add_parser("archive",
                       help="move handled/terminal rows except the newest --keep into "
                            "messages_archive (peek --id / task --id still read them)")
    a.add_argument("--keep", type=int, default=50)

    gr = sub.add_parser(
        "grade",
        help="hub's correction of the timing ledger row for one task of THIS project "
             "(run from the project root): --verdict rework --reason R marks a reply "
             "A used but had to rework; --verdict pass undoes it; --used-new records "
             "how A used the reply's NEW items. Automatic verdicts (fail, redispatch, "
             "replan, unsolvable) are set by code and are not overridden. Appends a "
             "correction row for the newest row of the task; the ledger is never "
             "rewritten and every reader folds corrections in (latest wins per field).")
    gr.add_argument("--task", type=int, required=True)
    gr.add_argument("--verdict", choices=("rework", "pass"), default=None)
    gr.add_argument("--reason", choices=GRADE_REASONS, default=None,
                    help="required with --verdict rework, rejected otherwise")
    gr.add_argument("--used-new", dest="used_new", choices=USED_NEW, default=None,
                    help="brief/conclusion/routing/rule/task = A's decision state changed "
                         "because of a NEW item; scope-closed = it closed an item A had "
                         "listed as open; none = default (a bare 'X is fine' stays none)")
    gr.add_argument("--findings", type=_findings_arg, default=None, metavar="HELD,FALSE,UNCHECKED",
                    help="A's check of every finding in a review reply (2026-10-18): how many held, "
                         "did not hold, were not checked; ipc_estimate.py accuracy compares models")

    st = sub.add_parser("status")
    st.add_argument("--watch", required=True,
                    help="terminal whose --block watcher to probe, e.g. B")
    st.add_argument("--max-age", type=float, default=8.0)
    st.add_argument("--json", action="store_true",
                    help="emit one JSON object instead of the text line: "
                         "role/state/alive/busy/squatter/owner_valid/parked/"
                         "reason/pid/session/busy_pid/claimed. `parked` is "
                         "the single dispatch gate (alive, not busy, owned). "
                         "Exit code unchanged.")

    tk = sub.add_parser("task",
                        help="lifecycle state of one task by id (read-only: "
                             "no reap, no archive). Exit 1 when UNKNOWN.")
    tk.add_argument("--id", type=int, required=True, dest="task")
    tk.add_argument("--json", action="store_true",
                    help="emit one JSON object: id/state/terminal/from/to/ts/"
                         "attempts/result/claimed_ts/replies")

    pd = sub.add_parser("pending",
                        help="list tasks the hub dispatched that have no reply yet "
                             "(empty = fan-out complete)")
    pd.add_argument("--hub", default=HUB,
                    help=f"hub whose outstanding dispatches to list (default {HUB})")
    pd.add_argument("--detail", action="store_true",
                    help="show attempts per task alongside the lifecycle state")
    pd.add_argument("--json", action="store_true",
                    help="emit one JSON envelope per outstanding task (NDJSON): "
                         "id/to/ts/state/attempts — state is machine-readable "
                         "(QUEUED|IN_PROGRESS|STALE|NEEDS-REVIEW|...)")

    # --- task lifecycle verbs (core round: lifecycle + weak-rollback lease) ---
    ak = sub.add_parser("ack",
                        help="renew the lease on a claimed task (push lease_until out)")
    ak.add_argument("--me", required=True)
    ak.add_argument("--task", type=int, default=None,
                    help="task id to renew; omit to renew ALL of your claimed tasks")

    dn = sub.add_parser("done",
                        help="mark a task done (sends a bodyless ack reply linked to it)")
    dn.add_argument("--me", required=True)
    dn.add_argument("--task", type=int, required=True)

    fl = sub.add_parser("fail",
                        help="mark a task failed (tombstone=failed + a fail reply)")
    fl.add_argument("--me", required=True)
    fl.add_argument("--task", type=int, required=True)
    fl.add_argument("--reason", default="",
                    help="short reason recorded on the fail reply (peekable by the hub)")

    cn = sub.add_parser("cancel",
                        help="hub retracts a task (tombstone=cancelled)")
    cn.add_argument("--task", type=int, required=True)
    cn.add_argument("--by", required=True,
                    help="caller role; must equal the hub (IPC_HUB, default A)")

    rd = sub.add_parser(
        "redeliver",
        help="reset a claimed-but-never-started task back to QUEUED for a "
             "fresh delivery (2026-08-17 #1188: a claim swallowed by a "
             "mis-hosted watcher left recv returning NONE while the "
             "documented same-submit-id resend hit DUP — the only recovery "
             "was cancel + NEW submit-id + manual wake). Keeps the same row: "
             "submit_id idempotency and attempts history intact (next claim "
             "increments attempts as usual). Refused for done/tombstoned "
             "rows. Pair with ipc_wake_pane.ps1 if the worker is not parked.")
    rd.add_argument("--task", type=int, required=True)
    rd.add_argument("--by", required=True,
                    help="caller role; must equal the task's sender (the hub)")

    rp = sub.add_parser("reap",
                        help="manually run the lazy reaper and print what was harvested")
    rp.add_argument("--me", default=None, help="reap only this worker's stale tasks")
    rp.add_argument("--hub", default=None, help="reap all tasks this hub dispatched")

    # Internal: spawned detached by recv() at task-claim time. Not for humans —
    # it is the mechanism that makes "working without a heartbeat" impossible.
    bd = sub.add_parser("busy-daemon",
                        help=argparse.SUPPRESS)
    bd.add_argument("--me", required=True)

    ka = sub.add_parser("keepalive",
                        help="park liveness only: keep the watcher heartbeat fresh "
                             "WITHOUT consuming messages, so the hub can dispatch "
                             "(require-watcher passes) and rows queue unclaimed until "
                             "a real consumer parks recv/watch. For daemon-kept slots "
                             "whose executor is an interactive window opened later.")
    ka.add_argument("--me", required=True)
    ka.add_argument("--interval", type=float, default=3.0,
                    help="seconds between heartbeat touches (default 3.0; "
                         "require-watcher freshness window is 8s)")

    args = p.parse_args()

    if args.cmd == "busy-daemon":
        _require_valid(args.me, "--me")
        _busy_daemon(args.me)
    elif args.cmd == "keepalive":
        ka_roles = [r.strip() for r in args.me.split(",") if r.strip()]
        for r in ka_roles:
            _require_valid(r, "--me")
        print(f"KEEPALIVE {','.join(ka_roles)} beating every {args.interval}s "
              f"(pid {os.getpid()}); messages queue for a later consumer",
              flush=True)
        while True:
            for r in ka_roles:
                _beat(r)
            time.sleep(args.interval)
    elif args.cmd == "init":
        _conn().close()
        print(f"OK  db={DB_PATH}")
    elif args.cmd == "send":
        _require_valid(args.sender, "--from")
        if args.body_file is not None:
            if args.body is not None:
                print("BODY  pass either a positional body or --body-file, not both")
                sys.exit(2)
            try:
                with open(args.body_file, encoding="utf-8") as f:
                    args.body = f.read().strip()
            except OSError as e:
                print(f"BODY  cannot read --body-file: {e}")
                sys.exit(2)
        elif args.body is None:
            print("BODY  missing: pass a positional body or --body-file")
            sys.exit(2)
        targets = expand_recipients(args.recipient, args.sender, args.max_age)
        if not targets:
            print("NO RECIPIENTS  (--to ALL matched no other live role, "
                  "or the list was empty / only the sender)")
            sys.exit(2)
        for tgt in targets:
            _require_valid(tgt, "--to")  # reject path-traversal names before any FS touch
        any_refused = False
        for tgt in targets:
            try:
                mid, created, queued_busy = send(
                    args.sender, tgt, args.body,
                    in_reply_to=args.in_reply_to, msg_type=args.msg_type,
                    require_watcher=args.require_watcher, max_age=args.max_age,
                    lease=args.lease, submit_id=args.submit_id,
                    no_requeue=args.no_requeue, budget=args.budget)
                if created and queued_busy and args.msg_type == "note":
                    print(f"QUEUED-BUSY #{mid}  {args.sender}->{tgt}  "
                          f"({tgt} is executing a claimed task; the note delivers "
                          f"when it next parks/recvs. CAUTION: notes are NOT in "
                          f"`pending` and exempt from the reaper — fire-and-forget "
                          f"FYI; use a task if delivery must be guaranteed.)")
                elif created and queued_busy:
                    print(f"QUEUED-BUSY #{mid}  {args.sender}->{tgt}  "
                          f"({tgt} is executing a claimed task — busy heartbeat "
                          f"fresh; the row stays QUEUED in `pending` until {tgt} "
                          f"next parks/recvs, the reaper does not touch unclaimed "
                          f"rows. Don't re-dispatch while BUSY — probe with "
                          f"`status --watch {tgt}` first; if {tgt}'s busy beat "
                          f"expired and it never re-parked, nudge its window.)")
                elif created:
                    # Print what send() stored (msg_type as it inferred it, the
                    # parsed type, the RESULT verdict), never a second inference.
                    with _conn() as conn:
                        row = conn.execute("SELECT msg_type, task_type, result, in_reply_to, "
                                           "anchor, new_items FROM messages WHERE id=?",
                                           (mid,)).fetchone()
                    print(f"SENT #{mid}  {args.sender}->{tgt}"
                          + (f"  type={row[1]}" if row and row[0] == "task" else ""))
                    if args.sender != HUB and tgt == HUB:
                        r = row[2:] if row and row[0] == "reply" else None
                        if r and r[0]:
                            print(f"RESULT {r[0]}  (task #{r[1]}; CLEAN = hub gets a "
                                  f"one-line DONE summary, no body read)")
                        elif r and r[1] is not None:
                            print(f"RESULT none  (body does not OPEN with a `RESULT: "
                                  f"<absolute path>` line — hub will read the full body; "
                                  f"put one on the first line when the task produced a file, "
                                  f"`UNSOLVABLE: <why>` when it cannot be done, "
                                  f"`REPLAN: <wrong premise>` when the brief itself is wrong)")
                        if row and row[0] in ("reply", "fail") and row[4]:
                            print(f"ANCHOR {row[4]}  NEW items={row[5] or 0}  (the hub can "
                                  f"quote the anchor in later briefs)")
                else:
                    print(f"DUP #{mid}  {args.sender}->{tgt}  "
                          f"(submit-id {args.submit_id!r} already queued/answered; "
                          f"not re-queued — check `pending`, or cancel/fail #{mid} "
                          f"first to force a true retry)")
            except StarViolation as e:
                any_refused = True
                print(f"REJECTED  {e}")
            except LinkRefused as e:
                any_refused = True
                print(f"REFUSED-LINK  {e}  (NOT queued; `pending --hub A` lists "
                      f"open task ids)")
            except SquatterHeartbeat as e:
                any_refused = True
                pid = e.args[1] if len(e.args) > 1 else "?"
                print(f"REFUSED-SQUATTER  {tgt}'s heartbeat is fresh but the "
                      f"role has NO registry owner (beater pid={pid}) — likely "
                      f"an orphan watcher from a dead session, not dispatch "
                      f"evidence. NOT queued to {tgt}. If {tgt}'s pane is "
                      f"really parked and alive, claim the role there: "
                      f'python "{os.path.join(_HERE, "ipc_role.py")}" take '
                      f"{tgt} — else run /ipc-recover in {tgt}'s pane. Then "
                      f"re-send.")
            except WatcherDown:
                any_refused = True
                print(f"REFUSED  {tgt} is neither parked nor busy "
                      f"(no fresh watcher heartbeat <{args.max_age:g}s AND no fresh "
                      f"busy heartbeat). NOT queued to {tgt}. "
                      f"Nudge {tgt} to park its watcher first.")
            except (sqlite3.Error, OSError) as e:  # DB lock / disk full: skip, keep rest
                any_refused = True
                print(f"ERROR  could not queue to {tgt}: {type(e).__name__}: {e}")
        if any_refused:
            sys.exit(3)  # at least one recipient down/errored; live ones still queued
    elif args.cmd == "recv":
        _require_valid(args.me, "--me")
        if args.block:
            rows = recv_block(args.me, args.timeout, args.interval, args.count,
                              keep_heartbeat=args.keep_heartbeat)
        else:
            rows = recv(args.me)
        if not rows:
            if not args.json:  # --json: empty output = nothing; exit code unchanged
                print("NONE (timeout)" if args.block else "NONE")
            if args.block:
                # Two-state exit code for the backgrounded watcher: exit 2 on an
                # empty timeout, exit 0 when messages were returned (the else
                # branch below). This lets the agent tell "nothing arrived" from
                # "got a message" straight from the task-notification exit code
                # and SKIP re-reading the output on every idle timeout — the
                # top token sink for a long-parked hub/worker. The harness shows
                # a non-zero background exit as status=failed/"exit code 2": that
                # is a NORMAL park timeout, NOT an error (verified: no auto-retry,
                # no permission prompt). Non-block recv keeps exit 0 so existing
                # drain scripts/hooks stay safe. (A future non-consuming partial
                # barrier could add exit 4; 3 stays reserved for send REFUSED.)
                sys.exit(2)
        else:
            if args.json:
                # Reserve the number, persist the complete batch, THEN print: the
                # rows are already claimed, and a broken or capped output channel
                # must not be the only copy (2026-10-16 review: BrokenPipeError
                # while printing left no file).
                # The rows are claimed (handled=1 committed) before any of this
                # runs, so a save failure (permissions, disk full, locked file)
                # must never swallow the envelopes: print them regardless and put
                # the failure plus the claimed ids on stderr (2026-10-17 audit).
                ids = " ".join(str(mid) for mid, *_ in rows)

                def envelopes(batch_no):
                    return [
                        json.dumps({"id": mid, "ts": ts, "from": sender, "type": mtype,
                                    "task": irt, "session": sess, "result": result,
                                    "body": "" if args.no_body else body,
                                    "body_chars": len(body or ""), "batch": batch_no,
                                    # RESULT paths travel with --no-body too, so a hub reading
                                    # envelopes can log DONE without a peek (2026-10-17 audit)
                                    "paths": _result_paths(body) if (mtype == "reply" and result == "CLEAN") else [],
                                    # 2026-10-17: quotable anchor + NEW item count of a linked reply
                                    "anchor": _anchor(irt, sender) if (irt is not None and mtype in ("reply", "fail")) else None,
                                    "new_items": n_new or 0},
                                   ensure_ascii=False)
                        for mid, ts, sender, body, mtype, irt, sess, result, n_new in rows]

                def save_failed(stage, e):
                    # One line, every claimed id spelled out (a range cannot say which
                    # ids belong to the batch), no batch number to recover from.
                    print(f"BATCH-SAVE-FAILED {stage}: {type(e).__name__}: {e}; the envelopes "
                          f"printed below are the only copy (batch=null); bodies via "
                          f"peek --me {args.me} --id {ids}", file=sys.stderr)

                try:
                    batch_no = _reserve_batch(args.me)
                except OSError as e:
                    batch_no = None
                    save_failed("reserve", e)
                batch_lines = envelopes(batch_no)
                if batch_no is not None:
                    try:
                        _write_batch(args.me, batch_no, batch_lines)
                    except OSError as e:
                        save_failed(f"write batch {batch_no}", e)
                        batch_lines = envelopes(None)  # no file carries this number
                for line in batch_lines:
                    print(line)
                rows = []  # printed above; the text loop below has nothing left to do
            for mid, ts, sender, body, mtype, irt, sess, result, n_new in rows:
                if mtype == "reply" and result == "CLEAN":
                    # RESULT-line protocol on the recv path (bash-fallback /
                    # codex hub): same one-line summary as the watch path, the
                    # body stays available via peek. announced stays at its
                    # default 1 (only watch() sets 0), so a later watch never
                    # re-summarises a row recv already handed over.
                    paths = ", ".join(_result_paths(body)) or "?"
                    note = "" if n_new else ", body via peek if needed"
                    print(f"DONE #{irt} <- {sender} RESULT: {paths} (#{mid}{note})"
                          + _summary_suffix(irt, sender, n_new, mid, args.me))
                else:
                    # Linked reply/fail: same NEW count + anchor tags as watch's
                    # NEW MSG line (the body follows in full, so no peek hint).
                    tag = ""
                    if irt is not None and mtype in ("reply", "fail"):
                        tag = (f" NEW {n_new}" if n_new else "") + f" anchor {_anchor(irt, sender)}"
                    print(f"#{mid} [{ts}] {sender}{tag}: {body}")
    elif args.cmd == "peek" and (args.batch is not None or args.batches):
        _require_valid(args.me, "--me")
        nums = _batch_numbers(args.me)
        if args.batches:
            if not nums:
                print("NONE (no saved batch: recv --json has not run for this role here)")
            for n in nums:
                if not _batch_complete(args.me, n):
                    print(f"batch {n}: reserved, never written (recv --json interrupted)")
                    continue
                path = _batch_path(args.me, n)
                try:
                    with open(path, encoding="utf-8") as f:
                        lines = [ln for ln in f if ln.strip()]
                    first = json.loads(lines[0]) if lines else {}
                    last = json.loads(lines[-1]) if lines else {}
                    print(f"batch {n}: {len(lines)} message(s) ids {first.get('id')}..{last.get('id')} "
                          f"first ts {first.get('ts')}")
                except (OSError, ValueError) as e:
                    print(f"batch {n}: unreadable ({type(e).__name__})")
        else:
            # Default = the latest COMPLETE batch; a number _reserve_batch took but
            # _write_batch never filled is reported, never returned as an empty
            # success (2026-10-17 audit: exit 0 with empty stdout hid the real batch).
            err = sys.stderr if args.json else sys.stdout
            complete = [n for n in nums if _batch_complete(args.me, n)]
            n = args.batch if args.batch >= 0 else (complete[-1] if complete else None)
            if not nums or n not in nums:
                print("NONE (no saved batch" + ("" if nums else ": recv --json has not run for this role here")
                      + (f"; available: {nums[0]}..{nums[-1]}" if nums else "") + ")", file=err)
                sys.exit(1)
            if n not in complete:
                print(f"NONE (batch {n} reserved but never written: recv --json was interrupted"
                      + (f"; latest complete is {complete[-1]}" if complete else "") + ")", file=err)
                sys.exit(1)
            with open(_batch_path(args.me, n), encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            for line in lines:
                if args.json:
                    print(line)
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    print(line)
                    continue
                print(f"#{o['id']} [{o['ts']}] {o['from']}: {o['type']} task={o.get('task')} "
                      f"result={o.get('result')} chars={o.get('body_chars')}")
            # --json keeps stdout pure NDJSON (an automated recovery parses every
            # line); the human summary goes to stderr there (2026-10-16 review).
            print(f"BATCH {n} ({len(lines)} message(s), saved by recv --json; latest complete is {complete[-1]}); "
                  f"bodies via peek --id", file=err)
    elif args.cmd == "clock":
        _require_valid(args.hub, "--hub")
        sys.exit(clock_loop(args.hub, args.interval, once=args.once,
                            block=args.block, timeout=args.timeout))
    elif args.cmd == "watch":
        _require_valid(args.me, "--me")
        watch(args.me, args.interval,
              allow_file_stdout=args.allow_file_stdout)  # never returns; Monitor owns the lifetime
    elif args.cmd == "peek":
        _require_valid(args.me, "--me")
        rows = peek(args.me, args.tail, args.ids)
        if not rows:
            if not args.json:
                print("NONE")
        else:
            for (mid, ts, sender, recipient, body, handled, mtype, in_reply_to,
                 sess, result, archived) in rows:
                if args.json:
                    print(json.dumps(
                        {"id": mid, "ts": ts, "from": sender, "to": recipient,
                         "type": mtype, "task": in_reply_to, "session": sess,
                         "unread": not handled, "result": result, "body": body,
                         "archived": archived is not None,
                         "archived_ts": archived[0] if archived else None,
                         "archived_versions": archived[1] if archived else 0},
                        ensure_ascii=False))
                    continue
                flag = "" if handled else "  (unread)"
                if result:
                    flag += f"  [RESULT {result}]"
                if archived:
                    flag += ("  [archived]" if archived[1] == 1 else
                             f"  [archived: newest of {archived[1]} versions, {archived[0]}]")
                # A bodyless done-ack is a lifecycle marker; show what it marks
                # instead of a blank body. Display-layer only.
                if mtype == "ack" and not body:
                    tgt = in_reply_to if in_reply_to is not None else "?"
                    body = f"[done-marker -> task #{tgt}]"
                print(f"#{mid} [{ts}] {sender}->{recipient}: {body}{flag}")
    elif args.cmd == "archive":
        try:
            n = archive(args.keep)
        except Exception as e:  # noqa: BLE001 — manual trim: fail loudly, nothing moved
            print(f"ARCHIVE FAILED  {type(e).__name__}: {e} (rolled back, nothing moved)",
                  file=sys.stderr)
            sys.exit(1)
        print(f"ARCHIVED {n} rows  (kept last {args.keep}; moved to messages_archive, "
              f"peek --id / task --id still read them)")
    elif args.cmd == "grade":
        if args.verdict is None and args.used_new is None and args.findings is None:
            print("GRADE  pass --verdict rework|pass, --used-new and/or --findings")
            sys.exit(2)
        if args.verdict == "rework" and not args.reason:
            print(f"GRADE  --verdict rework needs --reason ({'|'.join(GRADE_REASONS)})")
            sys.exit(2)
        if args.reason and args.verdict != "rework":
            print("GRADE  --reason goes only with --verdict rework")
            sys.exit(2)
        try:
            rec, corr = grade(args.task, args.verdict, args.reason, args.used_new, args.findings)
        except (LookupError, ValueError) as e:
            print(f"GRADE REFUSED  {e}")
            sys.exit(2)
        except OSError as e:
            print(f"GRADE ERROR  {type(e).__name__}: {e} (no correction appended)")
            sys.exit(1)
        if corr is None:
            print(f"GRADE NO-OP  #{args.task} already verdict={rec.get('verdict')} "
                  f"reason={rec.get('reason')} used_new={rec.get('used_new')}; nothing appended")
            sys.exit(0)
        print(f"GRADED #{args.task}  verdict={rec.get('verdict')} reason={rec.get('reason')} "
              f"used_new={rec.get('used_new')} findings={rec.get('findings')}  "
              f"(correction row appended; readers fold it in)")
        print(json.dumps(corr, ensure_ascii=False))
        print(json.dumps(rec, ensure_ascii=False))
    elif args.cmd == "status":
        _require_valid(args.watch, "--watch")
        alive = watcher_alive(args.watch, args.max_age)
        busy = worker_busy(args.watch, args.max_age)
        # DB-derived busy (2026-08-08): a Monitor-driven worker consumes via
        # watch()+peek and never touches recv, so no busy-beater is ever forked
        # and the .busy file stays absent while it is mid-task (observed: B
        # worked 25 min on a claimed task, status showed plain ALIVE, the hub
        # misread that as idle and cancelled 14s before the reply landed).
        # The claim row itself is authoritative evidence of work in flight, so
        # fall back to the DB with the SAME predicate _busy_daemon exits on:
        # claimed (handled=1), not done, lease not yet fallen. Two guards:
        # (1) DB file must exist - status must never CREATE a mailbox
        #     (littering invariant, see _resolve_state_dir);
        # (2) the .busy FILE must be ABSENT - absence is the Monitor-path
        #     signature (watch never forks a beater), which is exactly the
        #     blind spot this fallback covers. A PRESENT-but-stale .busy is
        #     the recv-path ghost (beater killed, no finally cleanup): there
        #     the 2026-08-01 window-consistency invariant must hold - status
        #     and the send gate read the same file with the same max-age and
        #     agree the worker is gone (regression group 12 enforces this;
        #     first cut of this patch broke it and was caught by that group).
        # Display-only: exit code and the send()/require-watcher gate are
        # untouched.
        claimed = []
        if (not busy and not os.path.exists(_busy_path(args.watch))
                and os.path.exists(DB_PATH)):
            try:
                conn = _conn()
                try:
                    _now = time.time()
                    _rows = conn.execute(
                        "SELECT id, lease_until FROM messages WHERE recipient=? "
                        "AND handled=1 AND tombstone IS NULL AND msg_type='task'",
                        (args.watch,)).fetchall()
                    claimed = [(tid, lu) for tid, lu in _rows
                               if not task_done(conn, tid)
                               and (lu is None or _now < lu)]
                finally:
                    conn.close()
            except sqlite3.Error:
                pass  # never let a DB hiccup break a read-only status
            busy = bool(claimed)
        ident = watcher_identity(args.watch)
        who = ""
        if ident and (ident.get("pid") or ident.get("session")):
            sess = (ident.get("session") or "")[:8]
            who = f"  [pid={ident.get('pid')} session={sess or '?'}]"
        # Three states, not two. BUSY is the one the old two-state view could not
        # express, and it is exactly the state A most needs to tell apart from
        # DOWN: the worker is alive and working, but is NOT parked to accept new
        # work — so do not re-dispatch, and do not treat it as a black hole.
        state = "ALIVE" if alive else ("BUSY" if busy else "DOWN")
        if alive and busy:
            state = "ALIVE+BUSY"
        # Show WHICH process is beating .busy. Without this the identity shown is
        # always the parked watcher's, so an orphaned beater — a detached process
        # outliving a dead session, the one failure mode that can disguise a dead
        # worker as a live one — could not be traced back to a pid to kill.
        if busy:
            bident = _busy_identity(args.watch) or {}
            if bident.get("pid"):
                who += f"  [busy-beater pid={bident['pid']}]"
            elif claimed:
                _now = time.time()
                frag = ", ".join(
                    f"#{tid}" + (f" lease {int(lu - _now)}s left" if lu else "")
                    for tid, lu in claimed[:3])
                who += f"  [claimed: {frag}]"
        # Squatter surfacing (2026-08-17, #1186): a fresh heartbeat with no
        # registry owner is the signature of an orphan watcher from a dead
        # session. send --require-watcher now refuses that as parked-evidence
        # (SquatterHeartbeat), so status must agree — the exit code gates "can
        # I dispatch?", and ALIVE/exit-0 here with REFUSED at send would be the
        # two-gates-disagree jitter C review B-1 banned. Busy still exits 0:
        # dispatch to a busy worker queues (QUEUED-BUSY), same as the gate.
        squatter = alive and _registry_session(args.watch) is None
        if squatter:
            state += " [SQUATTER: no registry owner — require-watcher will " \
                     "refuse; run /ipc-recover in this role's pane]"
        if args.json:
            # Structured verdict (2026-10-15 efficiency review P1): the text
            # line carries compound states (ALIVE+BUSY, ALIVE [SQUATTER...])
            # that ipc_dispatch.ps1 used to substring-match as plain ALIVE,
            # so a busy or ownerless worker passed its "is it parked?" gate.
            # `parked` is the one field scripts gate on; the rest explains why.
            # Exit code below is identical to the text path.
            parked = bool(alive and not busy and not squatter)
            if parked:
                reason = "parked: heartbeat fresh, no task in flight, registry owner present"
            elif not alive and busy:
                reason = "busy: no parked watcher, task in flight (queue only)"
            elif alive and busy:
                reason = "busy: watcher alive but a task is in flight (do not dispatch a second one)"
            elif squatter:
                reason = "squatter: heartbeat fresh but no registry owner (run /ipc-recover in the pane)"
            else:
                reason = "down: no fresh heartbeat and no task in flight (re-park)"
            bident = _busy_identity(args.watch) if busy else None
            print(json.dumps({
                "role": args.watch, "state": state, "alive": bool(alive),
                "busy": bool(busy), "squatter": bool(squatter),
                "owner_valid": not squatter, "parked": parked, "reason": reason,
                "pid": (ident or {}).get("pid"),
                "session": (ident or {}).get("session") or None,
                "busy_pid": (bident or {}).get("pid"),
                "claimed": [tid for tid, _lu in claimed],
            }, ensure_ascii=False))
        else:
            print(f"{args.watch} watcher: {state}{who}")
        # Exit code: as before (alive -> 0) EXCEPT a squatter now exits 1, so
        # scripts gating on it agree with the send gate. Busy-only stays 1,
        # unchanged from the original semantics.
        sys.exit(0 if (alive and not squatter) else 1)
    elif args.cmd == "task":
        info = task_status(args.task)
        if args.json:
            print(json.dumps(info, ensure_ascii=False))
        elif info["state"] == "UNKNOWN":
            print(f"UNKNOWN  #{args.task} ({info['reason']})")
        else:
            line = (f"#{info['id']} [{info['ts']}] {info['from']}->{info['to']}  "
                    f"[{info['state']}]  attempts={info['attempts']}")
            if info["result"]:
                line += f"  result={info['result']}"
            if info.get("archived"):
                line += "  [archived]"
            print(line)
        sys.exit(0 if info["state"] != "UNKNOWN" else 1)
    elif args.cmd == "pending":
        _require_valid(args.hub, "--hub")
        rows = pending(args.hub)
        if not rows:
            if not args.json:
                print(f"NONE  (every task {args.hub} dispatched is done/cancelled/"
                      f"failed — fan-out complete)")
        else:
            for tid, recipient, ts, state, attempts in rows:
                if args.json:
                    print(json.dumps(
                        {"id": tid, "to": recipient, "ts": ts, "state": state,
                         "attempts": attempts}, ensure_ascii=False))
                    continue
                line = f"#{tid} [{ts}] {args.hub}->{recipient}  [{state}]"
                if args.detail:
                    line += f"  attempts={attempts}"
                print(line)
            sys.exit(1)  # non-empty => incomplete, usable in scripts
    elif args.cmd == "ack":
        _require_valid(args.me, "--me")
        # ack also beats the heartbeat: _lease_alive AND-joins heartbeat with the
        # lease ceiling, so a watcher-less worker mid-task (the bash-fallback
        # recv --block exits on delivery and removes its heartbeat) would read as
        # stale no matter how it renewed the lease. Beating here makes periodic
        # `ack` a genuine keep-alive for that path.
        # Also beats the busy heartbeat, so a manual `ack` still rescues a worker
        # whose spawned beater died (or never spawned, e.g. an older client) —
        # belt and braces, since the beater is now the primary mechanism.
        _beat(args.me)
        _beat_busy(args.me)
        # Lease renewal semantics deliberately UNCHANGED by the 2026-08-17 F2
        # work (A review on #1253/#1254): liveness is now carried by the
        # explicit last_seen_ts stamp below, so ack keeps its single original
        # job (flat DEFAULT_LEASE renewal) instead of doubling as the
        # life-sign clock via lease arithmetic — coupling the two meant any
        # future change to either would silently break the other.
        _now = time.time()
        new_lease = _now + DEFAULT_LEASE
        with _conn() as conn:
            if args.task is not None:
                cur = conn.execute(
                    "UPDATE messages SET lease_until=?, last_seen_ts=? "
                    "WHERE id=? AND recipient=? AND handled=1 AND tombstone IS NULL",
                    (new_lease, _now, args.task, args.me))
                n = cur.rowcount
            else:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE recipient=? AND handled=1 "
                    "AND tombstone IS NULL AND msg_type='task'", (args.me,)).fetchall()
                n = 0
                for (rid,) in rows:
                    if task_done(conn, rid):
                        continue  # done tasks have no lease to renew
                    conn.execute(
                        "UPDATE messages SET lease_until=?, last_seen_ts=? "
                        "WHERE id=?", (new_lease, _now, rid))
                    n += 1
        print(f"ACK  renewed {n} task(s); lease_until -> now+{DEFAULT_LEASE}s")
    elif args.cmd == "done":
        _require_valid(args.me, "--me")
        with _conn() as conn:
            row = conn.execute("SELECT recipient, tombstone FROM messages WHERE id=?",
                               (args.task,)).fetchone()
        if not row:
            print(f"NO TASK  #{args.task}")
            sys.exit(2)
        if row[0] != args.me:
            print(f"NOT OWNER  task #{args.task} belongs to {row[0]}, not {args.me}")
            sys.exit(2)
        # A bodyless ack reply linked to the task; task_done() then derives DONE
        # from it (msg_type='ack' counts as answering). No state column written.
        send(args.me, HUB, "", in_reply_to=args.task, msg_type="ack")
        print(f"DONE  task #{args.task} (ack reply sent to {HUB})")
    elif args.cmd == "fail":
        _require_valid(args.me, "--me")
        with _conn() as conn:
            row = conn.execute("SELECT recipient, tombstone FROM messages WHERE id=?",
                               (args.task,)).fetchone()
            if not row:
                print(f"NO TASK  #{args.task}")
                sys.exit(2)
            if row[0] != args.me:
                print(f"NOT OWNER  task #{args.task} belongs to {row[0]}, not {args.me}")
                sys.exit(2)
            if row[1] is not None:
                print(f"ALREADY TERMINAL  task #{args.task} tombstone={row[1]}")
                sys.exit(2)
            conn.execute(
                "UPDATE messages SET tombstone='failed', lease_until=NULL WHERE id=?",
                (args.task,))
        # fail reply explains the failure but does NOT count as answering
        # (task_done excludes msg_type='fail') — R3 line.
        send(args.me, HUB, args.reason or "", in_reply_to=args.task, msg_type="fail")
        print(f"FAILED  task #{args.task} tombstone=failed  ANCHOR {_anchor(args.task, args.me)}")
    elif args.cmd == "cancel":
        if args.by != HUB:
            print(f"FORBIDDEN  only the hub ({HUB}) may cancel; --by was {args.by}")
            sys.exit(2)
        with _txn() as conn:
            row = conn.execute("SELECT sender, tombstone FROM messages WHERE id=?",
                               (args.task,)).fetchone()
            if not row:
                print(f"NO TASK  #{args.task}")
                sys.exit(2)
            if row[0] != HUB:
                print(f"NOT HUB TASK  task #{args.task} sender={row[0]} != {HUB}")
                sys.exit(2)
            if row[1] is not None:
                print(f"ALREADY TERMINAL  task #{args.task} tombstone={row[1]}")
                sys.exit(2)
            conn.execute(
                "UPDATE messages SET tombstone='cancelled', handled=1, lease_until=NULL "
                "WHERE id=?", (args.task,))
            _log_terminal(conn, args.task, "cancel")   # ledger verdict fail (claimed tasks only)
        print(f"CANCELLED  task #{args.task}")
    elif args.cmd == "redeliver":
        with _conn() as conn:
            row = conn.execute(
                "SELECT sender, recipient, msg_type, tombstone, lease_secs "
                "FROM messages WHERE id=?", (args.task,)).fetchone()
            if not row:
                print(f"NO TASK  #{args.task}")
                sys.exit(2)
            sender, recipient, mtype, tomb, lease_secs = row
            if sender != args.by:
                print(f"FORBIDDEN  task #{args.task} sender={sender} != --by "
                      f"{args.by}; only the dispatching hub may redeliver")
                sys.exit(2)
            if mtype != "task":
                print(f"NOT A TASK  #{args.task} is msg_type={mtype!r}")
                sys.exit(2)
            if tomb is not None:
                print(f"ALREADY TERMINAL  task #{args.task} tombstone={tomb} "
                      f"— send a fresh task instead")
                sys.exit(2)
            if task_done(conn, args.task):
                print(f"ALREADY DONE  task #{args.task} has an answer; "
                      f"nothing to redeliver")
                sys.exit(2)
            # Back to the queued phase: same lease semantics as send() (a hard
            # lease restarts as a queue-phase ceiling and is reset again at
            # claim; pure-heartbeat stays NULL). last_seen_ts=NULL — the old
            # claim's life-signs are void, and unknown is never judged SILENT.
            new_lu = (time.time() + lease_secs) if lease_secs else None
            conn.execute(
                "UPDATE messages SET handled=0, status='sent', lease_until=?, "
                "last_seen_ts=NULL WHERE id=?", (new_lu, args.task))
        print(f"REDELIVERED #{args.task}  back to QUEUED for {recipient} "
              f"(same row: submit-id/attempts kept; its old busy beater "
              f"exits within ~{int(2 * _BUSY_POLL + 2)}s once it sees no open claim). "
              f"If {recipient} is not parked, wake it: "
              f"pwsh ~/.claude/ipc/ipc_wake_pane.ps1 -Role {recipient}")
    elif args.cmd == "reap":
        if args.me and args.hub:
            print("REAP  pass either --me or --hub, not both")
            sys.exit(2)
        if args.me:
            _require_valid(args.me, "--me")
            with _txn() as conn:
                rq, fl, rv = _reap_stale(conn, me=args.me)
        elif args.hub:
            _require_valid(args.hub, "--hub")
            with _txn() as conn:
                rq, fl, rv = _reap_stale(conn, hub=args.hub)
        else:
            with _txn() as conn:
                rq, fl, rv = _reap_stale(conn)
        if not rq and not fl and not rv:
            print("REAP  nothing stale")
        else:
            print(f"REAP  requeued={rq}  failed={fl}  needs-review={rv}")


if __name__ == "__main__":
    main()
