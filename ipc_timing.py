# -*- coding: utf-8 -*-
"""Dispatch -> first-reply timing per task and per worker, paired by `in_reply_to`
(never by hand: A's `_A_timing.py` on 2026-08-29 paired #1079 with #1082 — the reply
to a different task — and halved E's real turnaround). Metadata only, no bodies.

Usage (from the project root, or pass --cwd):
  python ~/.claude/ipc/ipc_timing.py --since 2026-08-29 [--hub A] [--cwd <project root>]
Columns: task, to, dispatched, reply(min), units (from `UNITS: n` or "N 份" in the
title line), min/unit, flag. Flags: NO-REPLY (open), CANCELLED/FAILED, FAST? (< 1 min/unit
with >= 5 units — a completion faster than any real per-unit work; 2026-08-29 C did
"11 份" in 5 min and the batch was sent back for redo).
"""
import argparse
import os
import re
import statistics
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UNITS = re.compile(r"UNITS\s*[:：]\s*(\d+)", re.I)
FEN = re.compile(r"(\d+)\s*份")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--hub", default="A")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--fast-threshold", type=float, default=1.0)
    a = ap.parse_args()
    os.chdir(a.cwd)
    sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".claude", "ipc"))
    import ipc  # resolves DB_PATH from cwd
    import sqlite3
    conn = sqlite3.connect(ipc.DB_PATH)
    tasks = conn.execute(
        "SELECT id, recipient, ts, tombstone, substr(body,1,200) FROM messages "
        "WHERE sender=? AND msg_type='task' AND ts>=? ORDER BY id", (a.hub, a.since)).fetchall()
    rows, per_role = [], {}
    for tid, to, ts, tomb, head in tasks:
        # LAST linked reply, not the first: workers send interim receipts ("已收到") that
        # are also linked. Workers also sometimes reply UNLINKED (E #1108, #1120 on
        # 2026-08-29) — those cannot be paired; the per-role unlinked count below shows
        # how much of the picture is missing.
        rep = conn.execute(
            "SELECT ts, length(body) FROM messages WHERE in_reply_to=? AND msg_type='reply' AND sender=? "
            "ORDER BY id DESC LIMIT 1", (tid, to)).fetchone()
        t0 = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        title = head.splitlines()[0][:48] if head else ""
        mu = UNITS.search(head or "")
        mf = FEN.search(title)
        units = int(mu.group(1)) if mu else (int(mf.group(1)) if mf else None)
        if rep:
            mins = (datetime.strptime(rep[0], "%Y-%m-%d %H:%M:%S") - t0).total_seconds() / 60
        else:
            mins = None
        flag = ""
        if tomb:
            flag = tomb.upper()
        elif mins is None:
            flag = "NO-REPLY"
        elif rep[1] is not None and rep[1] < 150:
            flag = "ACK-ONLY?"     # last linked reply is a receipt; real completion probably unlinked
        elif units and units >= 5 and mins / units < a.fast_threshold:
            flag = "FAST?"
        pu = f"{mins/units:.1f}" if (mins is not None and units) else "-"
        rows.append((tid, to, ts[5:16], "-" if mins is None else f"{mins:.0f}", units or "-", pu, flag, title))
        if mins is not None:
            per_role.setdefault(to, []).append((mins, units))
    print(f"{'task':>6} {'to':<3} {'dispatched':<11} {'min':>5} {'units':>5} {'min/u':>6} {'flag':<10} title")
    for r in rows:
        print(f"{r[0]:>6} {r[1]:<3} {r[2]:<11} {r[3]:>5} {str(r[4]):>5} {r[5]:>6} {r[6]:<10} {r[7]}")
    print()
    print(f"{'role':<4} {'n':>3} {'median':>7} {'mean':>6} {'max':>6}  min/unit(median, n)  unlinked-replies")
    for role, v in sorted(per_role.items()):
        m = [x[0] for x in v]
        pu = [x[0] / x[1] for x in v if x[1]]
        pus = f"{statistics.median(pu):.1f} ({len(pu)})" if pu else "-"
        unl = conn.execute(
            "SELECT count(*) FROM messages WHERE sender=? AND recipient=? AND msg_type='reply' "
            "AND (in_reply_to IS NULL OR in_reply_to='') AND ts>=?", (role, a.hub, a.since)).fetchone()[0]
        print(f"{role:<4} {len(m):>3} {statistics.median(m):>7.1f} {statistics.mean(m):>6.1f} {max(m):>6.0f}  {pus:<18} {unl}")
    print("\nNote: pairing = last reply linked via in_reply_to; an unlinked completion (worker forgot the link) "
          "leaves its task at the receipt time (ACK-ONLY?) — count in the last column.")


if __name__ == "__main__":
    main()
