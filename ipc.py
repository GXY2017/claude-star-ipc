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
    python ipc.py send --from A --to B "msg" --require-watcher  # refuse if B not listening
    python ipc.py status --watch B           # is B's --block watcher parked? ALIVE/DOWN
    python ipc.py peek --me B [--tail 5]     # show recent thread WITHOUT marking read
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
            tombstone   TEXT                           -- NULL=active; 'cancelled'|'failed'=terminal, excluded from active set
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
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
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
    return conn


class WatcherDown(Exception):
    """Raised by send() when require_watcher is set but the recipient's
    --block watcher is not parked/listening."""


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
_BASE_ROLES = ("A", "B", "C", "D")  # extend to add worker slots (E, F, ...)
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
    lands between polls isn't mis-reaped. See DEFAULT_LEASE comment."""
    if not watcher_alive(recipient, margin):
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


def task_state(conn, tid, recipient, handled, lease_until, tombstone):
    """Derive the lifecycle state of a task row. No state column — everything is
    computed from (handled, lease_until, attempts, tombstone, in_reply_to, now,
    heartbeat). tombstone takes precedence over done/in_progress so cancelled/failed
    display correctly even if a stray late reply lands. Order matters:
        CANCELLED > FAILED > DONE > QUEUED(handled=0) > IN_PROGRESS(lease alive) > STALE."""
    if tombstone == "cancelled":
        return "CANCELLED"
    if tombstone == "failed":
        return "FAILED"
    if task_done(conn, tid):
        return "DONE"
    if handled == 0:
        return "QUEUED"  # attempts>0 means requeued; caller shows attempts separately
    if _lease_alive(recipient, lease_until):
        return "IN_PROGRESS"
    return "STALE"


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


def send(sender, recipient, body, *, in_reply_to=None, msg_type=None, hop=None,
         ttl=4, require_watcher=False, max_age=8.0, lease=DEFAULT_LEASE):
    """Insert one message for a SINGLE recipient.
    - StarViolation if a non-hub addresses another non-hub, or hop > ttl.
    - WatcherDown if require_watcher is set and the recipient isn't parked.
    Classifies hub->worker as 'task' and worker->hub as 'reply' by default, and
    auto-links a reply to the sender's oldest unanswered task (setting hop =
    parent.hop + 1) so fan-out completion is computable in code.
    lease: hard lease seconds. 0/None => pure-heartbeat lease (lease_until NULL,
    alive iff recipient's watcher beats); >0 => a hard ceiling that catches
    alive-but-stuck. Stored as lease_secs (the sender's intent) and pre-set as
    lease_until=now+lease for the queued phase; claim RESETS lease_until to
    claim-time + lease_secs so the runway starts when the work starts (a task
    that waited out its lease in the queue no longer arrives pre-expired, and a
    requeued task retries under a fresh ceiling); ack() pushes it out."""
    if sender != HUB and recipient != HUB:
        raise StarViolation(f"star topology forbids {sender}->{recipient} "
                            f"(hub={HUB}; set IPC_HUB to change)")
    if require_watcher and not watcher_alive(recipient, max_age, verify_owner=True):
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
        cur = conn.execute(
            "INSERT INTO messages "
            "(ts, sender, recipient, body, in_reply_to, msg_type, status, hop, ttl, "
            "lease_until, lease_secs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, sender, recipient, body, in_reply_to, msg_type, "sent", hop, ttl,
             lease_until, lease_secs),
        )
        return cur.lastrowid


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
        raw = [r for r in _claimed_roles()
               if r != sender and watcher_alive(r, max_age)]
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
    return conn.execute(
        "UPDATE messages SET handled=1, status='delivered', attempts=attempts+1, "
        "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
        "ELSE ? + lease_secs END "
        "WHERE id=("
        "  SELECT id FROM messages WHERE recipient=? AND handled=0 "
        "  ORDER BY id LIMIT 1"
        ") RETURNING id, ts, sender, body, msg_type, in_reply_to",
        (time.time(), me),
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
            "t.attempts FROM messages t "
            "WHERE t.sender=? AND t.msg_type='task' AND t.tombstone IS NULL "
            "ORDER BY t.id", (hub,)).fetchall()
        out = []
        for rid, recipient, ts, handled, lease_until, tombstone, attempts in rows:
            if task_done(conn, rid):
                continue
            st = task_state(conn, rid, recipient, handled, lease_until, tombstone)
            out.append((rid, recipient, ts, st, attempts))
        return out


_ARCHIVE_THRESHOLD = 300   # start trimming once the table exceeds this many rows
_ARCHIVE_KEEP = 150        # rows always kept (same guard as `archive --keep`)


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
        conn.execute(
            "DELETE FROM messages WHERE (handled=1 OR tombstone IS NOT NULL) "
            "AND id<=?", (row[0],))


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
    non-idempotent tasks still use --max-attempts 1 (first stick -> failed).
    Finishes with the lazy auto-archive (size-gated) so the DB self-trims
    without a maintenance cron."""
    if me is not None:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts FROM messages "
            "WHERE recipient=? AND handled=1 AND tombstone IS NULL "
            "AND msg_type='task'", (me,)).fetchall()
    elif hub is not None:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts FROM messages "
            "WHERE sender=? AND handled=1 AND tombstone IS NULL "
            "AND msg_type='task'", (hub,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, recipient, lease_until, attempts FROM messages "
            "WHERE handled=1 AND tombstone IS NULL AND msg_type='task'").fetchall()
    requeued, failed = [], []
    for rid, recipient, lease_until, attempts in rows:
        if task_done(conn, rid):
            continue
        if _lease_alive(recipient, lease_until):
            continue
        if attempts < MAX_ATTEMPTS:
            conn.execute("UPDATE messages SET handled=0, lease_until=NULL WHERE id=?", (rid,))
            requeued.append(rid)
        else:
            conn.execute("UPDATE messages SET tombstone='failed' WHERE id=?", (rid,))
            failed.append(rid)
    _auto_archive(conn)
    return requeued, failed


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
        rows = conn.execute(
            "UPDATE messages SET handled=1, status='delivered', attempts=attempts+1, "
            "lease_until=CASE WHEN lease_secs IS NULL THEN lease_until "
            "ELSE ? + lease_secs END "
            "WHERE recipient=? AND handled=0 "
            "RETURNING id, ts, sender, body",
            (time.time(), me),
        ).fetchall()
        # Done-drop: claimed (handled=1, so archive can sweep it) but not handed
        # to the caller. task_done is False for ordinary replies in a hub's inbox
        # (nothing links to a reply), so only requeued-done tasks are dropped.
        rows = [r for r in rows if not task_done(conn, r[0])]
    return sorted(rows, key=lambda r: r[0])  # RETURNING order is unspecified


def recv_block(me, timeout, interval, count=1):
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
        # Best-effort cleanup on normal exit/timeout so the heartbeat goes stale
        # immediately rather than waiting out max_age. A killed watcher skips
        # this, but staleness still ages it out.
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


def watch(me, interval):
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
    with _conn() as conn:
        my_gen = _bump_gen(conn, me)
    print(f"WATCHER #{my_gen} for {me} online", flush=True)
    while True:
        try:
            with _conn() as conn:
                cur = _current_gen(conn, me)
                if cur > my_gen:
                    print(
                        f"WATCHER for {me} retired: superseded by gen {cur} "
                        f"(was #{my_gen})",
                        flush=True,
                    )
                    return  # clean exit; the Monitor task ends — no orphan black hole
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
                    try:
                        # SIGNAL ONLY (never the body): a tiny line that can never be
                        # truncated by the harness notification layer. Read the full
                        # message with `peek` (see below). A bodyless done-ack
                        # (msg_type='ack', empty body) is a lifecycle marker, not
                        # content — surface it as "TASK #N DONE" instead of a noisy
                        # "0 chars" NEW MSG. Display-layer only; claim/done semantics
                        # unchanged (done still derived from in_reply_to in task_done).
                        if mtype == "ack" and not body:
                            tgt = in_reply_to if in_reply_to is not None else "?"
                            print(
                                f"TASK #{tgt} DONE (ack from {sender})",
                                flush=True,
                            )
                            continue
                        # Absolute path in the hint: under the user-level install
                        # a bare `python ipc.py` copy-pasted from this signal
                        # fails (ipc.py is not in the project cwd).
                        print(
                            f"NEW MSG #{mid} from {sender} ({len(body)} chars) "
                            f'— read full: python "{os.path.abspath(__file__)}" '
                            f"peek --me {me} --tail 3",
                            flush=True,
                        )
                    except Exception as e:  # noqa: BLE001 — never kill the loop
                        sys.stderr.write(f"[watch] signal failed for #{mid}: {e}\n")
        except Exception as e:  # noqa: BLE001 — transient DB error: log, keep polling
            sys.stderr.write(f"[watch] poll error: {e}\n")
        time.sleep(interval)


def peek(me, tail):
    """Show the last `tail` messages involving `me` without marking read."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, sender, recipient, body, handled, msg_type, "
            "in_reply_to FROM messages "
            "WHERE recipient=? OR sender=? ORDER BY id DESC LIMIT ?",
            (me, me, tail),
        ).fetchall()
    return list(reversed(rows))


def archive(keep):
    """Delete terminal/handled messages except the most recent `keep` rows.
    Condition is (handled=1 OR tombstone IS NOT NULL) so failed/cancelled rows are
    reaped even when handled=1; requeued rows (handled=0, tombstone NULL) are
    protected — they're active work, not history. R2 line."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM messages WHERE (handled=1 OR tombstone IS NOT NULL) "
                "AND id<=?", (row[0],)
            )
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

    r = sub.add_parser("recv")
    r.add_argument("--me", required=True)
    r.add_argument("--block", action="store_true",
                   help="wait for a message instead of returning NONE immediately")
    r.add_argument("--timeout", type=int, default=580,
                   help="max seconds to wait in --block mode (stay under bash's 600s cap)")
    r.add_argument("--interval", type=float, default=2.0,
                   help="seconds between polls in --block mode")
    r.add_argument("--count", type=int, default=1,
                   help="BARRIER: in --block mode, wait until this many messages "
                        "have arrived (accumulated) before returning. Use after a "
                        "fan-out (--to B,C,D) so one blocking call collects all N "
                        "replies; the tally lives in the process, not across A's "
                        "context. On timeout returns however many arrived (k<N).")

    w = sub.add_parser("watch")
    w.add_argument("--me", required=True)
    w.add_argument("--interval", type=float, default=3.0,
                   help="seconds between polls (default 3). Keep it < status "
                        "--max-age (default 8) or the heartbeat ages out and "
                        "status reports DOWN while watch is running.")

    k = sub.add_parser("peek")
    k.add_argument("--me", required=True)
    k.add_argument("--tail", type=int, default=5)

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

    rp = sub.add_parser("reap",
                        help="manually run the lazy reaper and print what was harvested")
    rp.add_argument("--me", default=None, help="reap only this worker's stale tasks")
    rp.add_argument("--hub", default=None, help="reap all tasks this hub dispatched")

    args = p.parse_args()

    if args.cmd == "init":
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
                mid = send(args.sender, tgt, args.body,
                           in_reply_to=args.in_reply_to, msg_type=args.msg_type,
                           require_watcher=args.require_watcher, max_age=args.max_age,
                           lease=args.lease)
                print(f"SENT #{mid}  {args.sender}->{tgt}")
            except StarViolation as e:
                any_refused = True
                print(f"REJECTED  {e}")
            except WatcherDown:
                any_refused = True
                print(f"REFUSED  {tgt}'s watcher is not listening "
                      f"(no fresh heartbeat <{args.max_age:g}s). NOT queued to {tgt}. "
                      f"Nudge {tgt} to park its recv --block watcher first.")
            except (sqlite3.Error, OSError) as e:  # DB lock / disk full: skip, keep rest
                any_refused = True
                print(f"ERROR  could not queue to {tgt}: {type(e).__name__}: {e}")
        if any_refused:
            sys.exit(3)  # at least one recipient down/errored; live ones still queued
    elif args.cmd == "recv":
        _require_valid(args.me, "--me")
        if args.block:
            rows = recv_block(args.me, args.timeout, args.interval, args.count)
        else:
            rows = recv(args.me)
        if not rows:
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
            for mid, ts, sender, body in rows:
                print(f"#{mid} [{ts}] {sender}: {body}")
    elif args.cmd == "watch":
        _require_valid(args.me, "--me")
        watch(args.me, args.interval)  # never returns; Monitor owns the lifetime
    elif args.cmd == "peek":
        _require_valid(args.me, "--me")
        rows = peek(args.me, args.tail)
        if not rows:
            print("NONE")
        else:
            for mid, ts, sender, recipient, body, handled, mtype, in_reply_to in rows:
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
        ident = watcher_identity(args.watch)
        who = ""
        if ident and (ident.get("pid") or ident.get("session")):
            sess = (ident.get("session") or "")[:8]
            who = f"  [pid={ident.get('pid')} session={sess or '?'}]"
        print(f"{args.watch} watcher: {'ALIVE' if alive else 'DOWN'}{who}")
        sys.exit(0 if alive else 1)
    elif args.cmd == "pending":
        _require_valid(args.hub, "--hub")
        rows = pending(args.hub)
        if not rows:
            print(f"NONE  (every task {args.hub} dispatched is done/cancelled/failed "
                  f"— fan-out complete)")
        else:
            for tid, recipient, ts, state, attempts in rows:
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
        _beat(args.me)
        new_lease = time.time() + DEFAULT_LEASE
        with _conn() as conn:
            if args.task is not None:
                cur = conn.execute(
                    "UPDATE messages SET lease_until=? WHERE id=? AND recipient=? "
                    "AND handled=1 AND tombstone IS NULL",
                    (new_lease, args.task, args.me))
                n = cur.rowcount
            else:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE recipient=? AND handled=1 "
                    "AND tombstone IS NULL AND msg_type='task'", (args.me,)).fetchall()
                n = 0
                for (rid,) in rows:
                    if task_done(conn, rid):
                        continue  # done tasks have no lease to renew
                    conn.execute("UPDATE messages SET lease_until=? WHERE id=?",
                                 (new_lease, rid))
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
    elif args.cmd == "reap":
        if args.me and args.hub:
            print("REAP  pass either --me or --hub, not both")
            sys.exit(2)
        if args.me:
            _require_valid(args.me, "--me")
            with _conn() as conn:
                rq, fl = _reap_stale(conn, me=args.me)
        elif args.hub:
            _require_valid(args.hub, "--hub")
            with _conn() as conn:
                rq, fl = _reap_stale(conn, hub=args.hub)
        else:
            with _conn() as conn:
                rq, fl = _reap_stale(conn)
        if not rq and not fl:
            print("REAP  nothing stale")
        else:
            print(f"REAP  requeued={rq}  failed={fl}")


if __name__ == "__main__":
    main()
