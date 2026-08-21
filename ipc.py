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
    python ipc.py peek --me B [--tail 5]     # show recent thread WITHOUT marking read
    python ipc.py recv/peek/pending ... --json  # NDJSON envelopes instead of text lines:
                                             # recv/peek {id,ts,from,type,task,session,body};
                                             # pending {id,to,ts,state,attempts}. Machine-
                                             # readable outcome (type/state) + session echo
                                             # (grok-build headless-envelope + sessionId ideas:
                                             # reply session != task claimant => worker
                                             # /clear-ed, its context is gone, re-brief)
    python ipc.py archive [--keep 50]        # trim handled rows, keep the last N

Topology: STAR with A at the hub is a CONVENTION, not enforced by this script.
A = master (initiates/decides); B, C, D... = workers (respond, then stop). The
"workers talk only to A, never to each other" rule that keeps the anti-echo
invariant linear (not N-squared) is enforced by the role prompts injected at
SessionStart + the CLAUDE.md protocol — `send` itself is a neutral mailbox and
will deliver any sender->recipient pair (intentional: keeps test names and future
topologies open). Multi-recipient/ALL fan-out lets A dispatch or broadcast from
the hub; each recipient gets its own row with its own `handled` flag, so a
broadcast is never "consumed" by whoever reads first.

Names (sender/recipient/--me/--watch) must match [A-Za-z0-9_]+ : they become both
SQL values and on-disk heartbeat filenames (_watcher_<name>.alive), so the CLI
rejects path-separator/traversal characters before any filesystem touch.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_mod
import subprocess
import sys
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
                for m in ("CLAUDE.md", ".claude", ".git")):
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
    return conn.execute(
        "SELECT 1 FROM messages WHERE in_reply_to=? AND msg_type IN ('reply','ack') "
        "LIMIT 1", (tid,)
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


def _oldest_unanswered_task(conn, hub, worker):
    """(id, hop) of the oldest non-terminal, unreplied task hub->worker, else None.
    Lets a worker's reply be auto-linked to the task it answers WITHOUT the worker
    having to pass --in-reply-to, so 'who has replied' stays code-computable even
    when the worker is a different vendor's model that ignores the convention.
    Uses task_done (not its own NOT EXISTS) so 'answered' can't drift from done."""
    rows = conn.execute(
        "SELECT t.id, t.hop, t.tombstone FROM messages t "
        "WHERE t.sender=? AND t.recipient=? AND t.msg_type='task' "
        "AND t.tombstone IS NULL ORDER BY t.id", (hub, worker)).fetchall()
    for tid, hop, tomb in rows:
        if tomb is not None:
            continue
        if task_done(conn, tid):
            continue
        return (tid, hop)
    return None


def _dup_by_submit_id(conn, sender, recipient, submit_id):
    """Newest non-terminal row for this idempotency key, or None."""
    return conn.execute(
        "SELECT id FROM messages WHERE sender=? AND recipient=? AND submit_id=? "
        "AND tombstone IS NULL ORDER BY id DESC LIMIT 1",
        (sender, recipient, submit_id)).fetchone()


def send(sender, recipient, body, *, in_reply_to=None, msg_type=None, hop=None,
         ttl=4, require_watcher=False, max_age=8.0, lease=DEFAULT_LEASE,
         submit_id=None, no_requeue=False):
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
    with _conn() as conn:
        if msg_type is None:
            msg_type = "reply" if (sender != HUB and recipient == HUB) else "task"
        if in_reply_to is None and msg_type == "reply":
            parent = _oldest_unanswered_task(conn, recipient, sender)  # recipient=hub
            if parent:
                in_reply_to = parent[0]
                if hop is None:
                    hop = (parent[1] or 0) + 1
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
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(ts, sender, recipient, body, in_reply_to, msg_type, status, hop, ttl, "
            "lease_until, lease_secs, submit_id, no_requeue, session) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, sender, recipient, body, in_reply_to, msg_type, "sent", hop, ttl,
             lease_until, lease_secs, submit_id, 1 if no_requeue else 0,
             os.environ.get("CLAUDE_SESSION_ID") or None),
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
        "last_seen_ts=?, "
        "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
        "ELSE ? + lease_secs END "
        "WHERE id=("
        "  SELECT id FROM messages WHERE recipient=? AND handled=0 "
        "  ORDER BY id LIMIT 1"
        ") RETURNING id, ts, sender, body, msg_type, in_reply_to",
        (_now, _now, me),
    ).fetchone()


def pending(hub):
    """Tasks `hub` dispatched that are not yet done -> the workers still absent.
    Fan-out is COMPLETE when this is empty. Uses task_done (so 'answered' matches
    done exactly) and derives a lifecycle state per row so the hub can read
    QUEUED / IN_PROGRESS / STALE / FAILED at a glance. Lazily reaps first.
    Returns [(id, recipient, ts, state, attempts)]."""
    with _conn() as conn:
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


_ARCHIVE_THRESHOLD = 300   # start trimming once the table exceeds this many rows
_ARCHIVE_KEEP = 150        # rows always kept (same guard as `archive --keep`)

# Shared delete condition for archive()/_auto_archive so the two can't drift.
# handled=1 normally means history, but an UNRESOLVED --no-requeue task is
# handled=1 too (the fail-closed claim stays in place while it awaits review) —
# active work, never archivable. The NOT EXISTS mirrors task_done()'s
# reply/ack predicate in SQL (kept adjacent by this comment: change both
# together) so a RESOLVED no-requeue task does age out normally.
_ARCHIVE_DELETE = (
    "DELETE FROM messages WHERE (handled=1 OR tombstone IS NOT NULL) AND id<=? "
    "AND NOT (no_requeue=1 AND tombstone IS NULL AND msg_type='task' "
    "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.in_reply_to=messages.id "
    "AND r.msg_type IN ('reply','ack')))"
)


def _auto_archive(conn, threshold=_ARCHIVE_THRESHOLD, keep=_ARCHIVE_KEEP):
    """Lazy self-trim, same philosophy as the lazy reaper: piggybacks on
    recv/watch/pending instead of needing a cron. Once the table exceeds
    `threshold` rows, delete handled/terminal rows older than the newest `keep`
    (identical condition to archive(): unread requeued rows are active work and
    are never touched). Done-dropped rows are handled=1, so they age out here."""
    if conn.execute("SELECT count(*) FROM messages").fetchone()[0] <= threshold:
        return
    row = conn.execute(
        "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
    ).fetchone()
    if row:
        conn.execute(_ARCHIVE_DELETE, (row[0],))


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
        non-idempotent tasks from infinite re-runs — CLAUDE.md concern)
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
    with _conn() as conn:
        _reap_stale(conn, me=me)
        _now = time.time()
        rows = conn.execute(
            "UPDATE messages SET handled=1, status='delivered', attempts=attempts+1, "
            "last_seen_ts=?, "
            "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
            "ELSE ? + lease_secs END "
            "WHERE recipient=? AND handled=0 "
            "RETURNING id, ts, sender, body, msg_type, in_reply_to, session",
            (_now, _now, me),
        ).fetchall()
        # Done-drop: claimed (handled=1, so archive can sweep it) but not handed
        # to the caller. task_done is False for ordinary replies in a hub's inbox
        # (nothing links to a reply), so only requeued-done tasks are dropped.
        rows = [r for r in rows if not task_done(conn, r[0])]
    # Handing over a task starts the busy heartbeat. Placed here (the claim path
    # used by one-shot recv AND by the bash-fallback recv --block) rather than in
    # watch(), which never returns and therefore never stops beating .alive — the
    # Monitor path was never exposed to this failure. msg_type is column 4 of the
    # RETURNING tuple.
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
    probes/re-dispatches those (see CLAUDE.md). count<=1 keeps the original
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


def _emit(line):
    """Print one watcher line; False if stdout is dead (EPIPE/EINVAL/closed).
    Never raises: emission failure is a liveness signal, not an error."""
    try:
        print(line, flush=True)
        return True
    except (OSError, ValueError):  # ValueError: I/O operation on closed file
        return False


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
    while True:
        try:
            with _conn() as conn:
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
                    mid, ts, sender, body, mtype, in_reply_to = row
                    if task_done(conn, mid):
                        # Done-drop (same as recv): a requeued-but-already-
                        # answered task is finished work; claim it silently,
                        # never re-announce it.
                        continue
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
                    if not _emit(
                            f"NEW MSG #{mid} from {sender} ({len(body)} chars) "
                            f'— read full: python "{os.path.abspath(__file__)}" '
                            f"peek --me {me} --tail 3"):
                        raise _StdoutDead(mid)
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


def peek(me, tail):
    """Show the last `tail` messages involving `me` without marking read."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, sender, recipient, body, handled, msg_type, "
            "in_reply_to, session FROM messages "
            "WHERE recipient=? OR sender=? ORDER BY id DESC LIMIT ?",
            (me, me, tail),
        ).fetchall()
    return list(reversed(rows))


def archive(keep):
    """Delete terminal/handled messages except the most recent `keep` rows.
    Condition is (handled=1 OR tombstone IS NOT NULL) so failed/cancelled rows are
    reaped even when handled=1; requeued rows (handled=0, tombstone NULL) are
    protected — they're active work, not history. R2 line. Unresolved
    --no-requeue tasks are likewise protected (see _ARCHIVE_DELETE): their claim
    is handled=1 by design while they await review, but they're active work."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
        ).fetchone()
        if row:
            conn.execute(_ARCHIVE_DELETE, (row[0],))
            return conn.total_changes
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

    k = sub.add_parser("peek")
    k.add_argument("--me", required=True)
    k.add_argument("--tail", type=int, default=5)
    k.add_argument("--json", action="store_true",
                   help="emit one JSON envelope per message (NDJSON): "
                        "id/ts/from/to/type/task/session/unread/body")

    a = sub.add_parser("archive")
    a.add_argument("--keep", type=int, default=50)

    st = sub.add_parser("status")
    st.add_argument("--watch", required=True,
                    help="terminal whose --block watcher to probe, e.g. B")
    st.add_argument("--max-age", type=float, default=8.0)

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
                    no_requeue=args.no_requeue)
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
                    print(f"SENT #{mid}  {args.sender}->{tgt}")
                else:
                    print(f"DUP #{mid}  {args.sender}->{tgt}  "
                          f"(submit-id {args.submit_id!r} already queued/answered; "
                          f"not re-queued — check `pending`, or cancel/fail #{mid} "
                          f"first to force a true retry)")
            except StarViolation as e:
                any_refused = True
                print(f"REJECTED  {e}")
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
            for mid, ts, sender, body, mtype, irt, sess in rows:
                if args.json:
                    print(json.dumps(
                        {"id": mid, "ts": ts, "from": sender, "type": mtype,
                         "task": irt, "session": sess, "body": body},
                        ensure_ascii=False))
                else:
                    print(f"#{mid} [{ts}] {sender}: {body}")
    elif args.cmd == "watch":
        _require_valid(args.me, "--me")
        watch(args.me, args.interval,
              allow_file_stdout=args.allow_file_stdout)  # never returns; Monitor owns the lifetime
    elif args.cmd == "peek":
        _require_valid(args.me, "--me")
        rows = peek(args.me, args.tail)
        if not rows:
            if not args.json:
                print("NONE")
        else:
            for (mid, ts, sender, recipient, body, handled, mtype, in_reply_to,
                 sess) in rows:
                if args.json:
                    print(json.dumps(
                        {"id": mid, "ts": ts, "from": sender, "to": recipient,
                         "type": mtype, "task": in_reply_to, "session": sess,
                         "unread": not handled, "body": body},
                        ensure_ascii=False))
                    continue
                flag = "" if handled else "  (unread)"
                # A bodyless done-ack is a lifecycle marker; show what it marks
                # instead of a blank body. Display-layer only.
                if mtype == "ack" and not body:
                    tgt = in_reply_to if in_reply_to is not None else "?"
                    body = f"[done-marker -> task #{tgt}]"
                print(f"#{mid} [{ts}] {sender}->{recipient}: {body}{flag}")
    elif args.cmd == "archive":
        n = archive(args.keep)
        print(f"ARCHIVED {n} rows  (kept last {args.keep})")
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
        print(f"{args.watch} watcher: {state}{who}")
        # Exit code: as before (alive -> 0) EXCEPT a squatter now exits 1, so
        # scripts gating on it agree with the send gate. Busy-only stays 1,
        # unchanged from the original semantics.
        sys.exit(0 if (alive and not squatter) else 1)
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
        print(f"FAILED  task #{args.task} tombstone=failed")
    elif args.cmd == "cancel":
        if args.by != HUB:
            print(f"FORBIDDEN  only the hub ({HUB}) may cancel; --by was {args.by}")
            sys.exit(2)
        with _conn() as conn:
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
            with _conn() as conn:
                rq, fl, rv = _reap_stale(conn, me=args.me)
        elif args.hub:
            _require_valid(args.hub, "--hub")
            with _conn() as conn:
                rq, fl, rv = _reap_stale(conn, hub=args.hub)
        else:
            with _conn() as conn:
                rq, fl, rv = _reap_stale(conn)
        if not rq and not fl and not rv:
            print("REAP  nothing stale")
        else:
            print(f"REAP  requeued={rq}  failed={fl}  needs-review={rv}")


if __name__ == "__main__":
    main()
