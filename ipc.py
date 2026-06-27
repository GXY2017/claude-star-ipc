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
    python ipc.py recv --me B                # print NEW messages for B, mark them read
    python ipc.py recv --me A --block        # wait until a message arrives, then print it
    python ipc.py recv --me A --block --count 3  # BARRIER: wait until 3 replies arrive (parallel fan-out)
    # recv --block exit code: 0 = returned message(s); 2 = empty timeout (lets a
    # backgrounded watcher skip re-reading output — shows as status=failed but is
    # a normal timeout, not an error). Non-block recv always exits 0.
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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ipc.db")

# Liveness heartbeat: a --block watcher touches this file every poll, so the
# other terminal can tell whether the watcher is actually parked and listening
# (a registered role in ipc_roles.json does NOT prove the watcher is running —
# it survives /clear, the watcher process does not). Staleness, not presence,
# is the signal: a killed watcher stops refreshing and the mtime ages out.
_HERE = os.path.dirname(os.path.abspath(__file__))


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
    return os.path.join(_HERE, f"_watcher_{who}.alive")


def watcher_alive(who, max_age):
    """True if `who`'s --block watcher refreshed its heartbeat within max_age
    seconds. Absent or stale file => not listening."""
    try:
        return (time.time() - os.path.getmtime(_heartbeat_path(who))) <= max_age
    except OSError:
        return False


def _beat(who):
    try:
        with open(_heartbeat_path(who), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass  # heartbeat is best-effort; never let it break the watcher


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # WAL lets one terminal read while the other writes without blocking.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            sender    TEXT NOT NULL,
            recipient TEXT NOT NULL,
            body      TEXT NOT NULL,
            handled   INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return conn


class WatcherDown(Exception):
    """Raised by send() when require_watcher is set but the recipient's
    --block watcher is not parked/listening."""


def send(sender, recipient, body, require_watcher=False, max_age=8.0):
    """Insert one message for a SINGLE recipient. Raises WatcherDown if
    require_watcher is set and that recipient's --block watcher isn't parked."""
    if require_watcher and not watcher_alive(recipient, max_age):
        raise WatcherDown(recipient)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (ts, sender, recipient, body) VALUES (?,?,?,?)",
            (ts, sender, recipient, body),
        )
        return cur.lastrowid


# Star-topology fan-out: A may address several workers at once. Recipients can be
# a comma list ("B,C") or the literal "ALL" (every currently-claimed role except
# the sender, read from the role registry the SessionStart hook maintains).
_REGISTRY = os.path.join(_HERE, ".claude", "ipc_roles.json")


def _claimed_roles():
    """Roles currently held by a live session, per .claude/ipc_roles.json."""
    try:
        with open(_REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
        return [r for r, v in data.items() if v]
    except (OSError, ValueError):
        return []


def expand_recipients(recipient, sender):
    """Resolve a --to value into an ordered, de-duplicated recipient list.
    'ALL' -> every claimed role except the sender; otherwise split on commas."""
    if recipient.strip().upper() == "ALL":
        raw = [r for r in _claimed_roles() if r != sender]
    else:
        raw = [x.strip() for x in recipient.split(",")]
    seen, out = set(), []
    for r in raw:
        if r and r != sender and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def recv(me):
    """Return and mark-as-read every unhandled message addressed to `me`."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, sender, body FROM messages "
            "WHERE recipient=? AND handled=0 ORDER BY id",
            (me,),
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            conn.executemany(
                "UPDATE messages SET handled=1 WHERE id=?", [(i,) for i in ids]
            )
        return rows


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


def peek(me, tail):
    """Show the last `tail` messages involving `me` without marking read."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, sender, recipient, body, handled FROM messages "
            "WHERE recipient=? OR sender=? ORDER BY id DESC LIMIT ?",
            (me, me, tail),
        ).fetchall()
    return list(reversed(rows))


def archive(keep):
    """Delete handled messages except the most recent `keep` rows."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM messages WHERE handled=1 AND id<=?", (row[0],)
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
    s.add_argument("body")
    s.add_argument("--require-watcher", action="store_true",
                   help="refuse to queue unless the recipient's --block watcher "
                        "is parked & listening (use for A->B task dispatch)")
    s.add_argument("--max-age", type=float, default=8.0,
                   help="max seconds since the recipient's last heartbeat to "
                        "count as alive (default 8 = 4 missed 2s beats)")

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

    k = sub.add_parser("peek")
    k.add_argument("--me", required=True)
    k.add_argument("--tail", type=int, default=5)

    a = sub.add_parser("archive")
    a.add_argument("--keep", type=int, default=50)

    st = sub.add_parser("status")
    st.add_argument("--watch", required=True,
                    help="terminal whose --block watcher to probe, e.g. B")
    st.add_argument("--max-age", type=float, default=8.0)

    args = p.parse_args()

    if args.cmd == "init":
        _conn().close()
        print(f"OK  db={DB_PATH}")
    elif args.cmd == "send":
        _require_valid(args.sender, "--from")
        targets = expand_recipients(args.recipient, args.sender)
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
                           require_watcher=args.require_watcher, max_age=args.max_age)
                print(f"SENT #{mid}  {args.sender}->{tgt}")
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
    elif args.cmd == "peek":
        _require_valid(args.me, "--me")
        rows = peek(args.me, args.tail)
        if not rows:
            print("NONE")
        else:
            for mid, ts, sender, recipient, body, handled in rows:
                flag = "" if handled else "  (unread)"
                print(f"#{mid} [{ts}] {sender}->{recipient}: {body}{flag}")
    elif args.cmd == "archive":
        n = archive(args.keep)
        print(f"ARCHIVED {n} rows  (kept last {args.keep})")
    elif args.cmd == "status":
        _require_valid(args.watch, "--watch")
        alive = watcher_alive(args.watch, args.max_age)
        print(f"{args.watch} watcher: {'ALIVE' if alive else 'DOWN'}")
        sys.exit(0 if alive else 1)


if __name__ == "__main__":
    main()
