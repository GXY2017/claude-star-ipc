# -*- coding: utf-8 -*-
"""Hub-side 15-minute reply clock + stall watchdog (role A). Run under the Monitor tool.

User rule 2026-08-29 (memory hub-manage-not-operate-15min-slices): no reply 15 min
after dispatch -> A inspects; working normally -> one more 15 min; stalled/abnormal ->
act now; two extensions max. This script does the inspection mechanically so nobody
has to remember to look: at 15/30/45 min after dispatch it probes the recipient's pane
(ipc_pane_probe.py: context-left %, recv-loop, permission prompt, quota banner, tail
change since the previous milestone) and emits ONE verdict line per milestone:

  REPLY-CLOCK #1138 -> E 15m | ctx 31% flags=working tail=CHANGED | WORKING -> extend 15m
  REPLY-CLOCK #1138 -> E 30m | ctx 0% flags=recv_loop tail=SAME  | ACT NOW: DEAD-LOOP
  REPLY-CLOCK #1138 -> E 45m | ...                                  | TWO EXTENSIONS USED -> reassign

It also keeps the older one-shot alarms from `pending` (QUEUED-STALLED /
IN-PROGRESS-SILENT / NEEDS-REVIEW). Each (task, milestone) is reported once.

Usage:  python ~/.claude/ipc/stall_watch.py [--poll 60] [--hub A] [--cwd <project root>] [--once]
Run from the project root (the mailbox is cwd-resolved) — Monitor inherits A's cwd.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ipc_pane_probe as probe  # noqa: E402

IPC = os.path.join(HERE, "ipc.py")
BAD = ("QUEUED-STALLED", "IN-PROGRESS-SILENT", "NEEDS-REVIEW")
MILESTONES = (15, 30, 45)
ACT_FLAGS = ("recv_loop", "permission_prompt", "quota_banner")


def pending_json(cwd, hub):
    r = subprocess.run([sys.executable, IPC, "pending", "--hub", hub, "--json"],
                       cwd=cwd, capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s`'\"()（）,，。;；|<>]+")   # same shape as ipc_brief_lint


def task_body(tid):
    """Body of message #tid, read-only, never raises (returns '' on any failure)."""
    try:
        import ipc  # noqa: WPS433 - resolves DB_PATH from cwd exactly like the CLI does
        conn = sqlite3.connect(f"file:{ipc.DB_PATH}?mode=ro", uri=True, timeout=5)
        row = conn.execute("SELECT body FROM messages WHERE id=?", (tid,)).fetchone()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


def artifact_truth(tid, now):
    """Newest existing file named (absolute path) in the brief: 'artifact=<name> mtime=HH:MM:SS (Nm ago) <bytes>B'.
    Inputs (reference PDFs) are old, products are fresh, so 'newest wins' picks the product.
    A directory token contributes its newest direct child. Wezterm-free: readable when panes are not."""
    best = None
    for tok in set(ABS_PATH.findall(task_body(tid))):
        p = tok.rstrip("\\.:")
        cands = []
        if os.path.isfile(p):
            cands = [p]
        elif os.path.isdir(p):
            try:
                cands = [os.path.join(p, f) for f in os.listdir(p)]
            except OSError:
                cands = []
        for c in cands:
            try:
                st = os.stat(c)
            except OSError:
                continue
            if not os.path.isfile(c):
                continue
            if best is None or st.st_mtime > best[1]:
                best = (c, st.st_mtime, st.st_size)
    if not best:
        return "no-artifact"
    age = (now.timestamp() - best[1]) / 60
    return f"artifact={os.path.basename(best[0])} mtime={datetime.fromtimestamp(best[1]).strftime('%H:%M:%S')} ({age:.0f}m ago) {best[2]}B"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=60)
    ap.add_argument("--hub", default="A")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    seen_bad = set()     # (task, state)
    reported = set()     # (task, milestone)
    changed = {}         # task -> tail changed at least once since the last milestone
    last_hash = {}       # task -> most recent tail_hash
    announced = set()    # tasks whose clock anchor has been printed once
    print(f"stall-watch: online hub={a.hub} cwd={a.cwd} poll={a.poll}s milestones={MILESTONES}", flush=True)

    while True:
        try:
            rows = pending_json(a.cwd, a.hub)
        except Exception as exc:
            print(f"stall-watch: poll failed: {exc}", flush=True)
            if a.once:
                return
            time.sleep(a.poll)
            continue

        live = set()
        now = datetime.now()
        for row in rows:
            tid, to, ts, state = row.get("id"), row.get("to"), row.get("ts", ""), row.get("state", "")
            live.add(tid)
            if state in BAD and (tid, state) not in seen_bad:
                seen_bad.add((tid, state))
                print(f"STALLED TASK: #{tid} -> {to} {state} attempts={row.get('attempts')}", flush=True)
            try:
                age = (now - datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            except ValueError:
                continue
            if tid not in announced:
                # The anchor is the mailbox INSERT time (messages.ts, write-once; ack/lease touch only
                # last_seen_ts/lease_until). Printing it once makes a wrong mental clock visible at once
                # (2026-08-30 #1271: hub remembered 14:52, the row said 16:18).
                announced.add(tid)
                print(f"CLOCK-START #{tid} -> {to} since {ts[:19]} (age {age:.0f}m, state {state}) - milestones at 15/30/45m from this anchor", flush=True)
            p = probe.probe(to) if to else None
            if p and p["tail_hash"]:
                if tid in last_hash and last_hash[tid] != p["tail_hash"]:
                    changed[tid] = True
                last_hash[tid] = p["tail_hash"]
            due = [m for m in MILESTONES if age >= m and (tid, m) not in reported]
            if not due:
                continue
            m = max(due)
            for d in due:
                reported.add((tid, d))
            ctx = "?" if not p or p["ctx_left"] is None else f'{p["ctx_left"]}%'
            flags = ",".join(p["flags"]) if p else "probe-failed"
            tail = "CHANGED" if changed.get(tid) else "SAME"
            if not p or p.get("error") or p["tail_hash"] is None:
                # 2026-08-30 (X, #1175): probe failure is NOT data. Emitting an idle/unchanged
                # verdict here made A interrupt a healthy B. Say why the pane is unreadable
                # (socket hijack / no such pane / timeout) and decide nothing.
                why = (p.get("error") if p else "probe returned nothing") or "no pane text"
                action = f"PROBE-FAILED: cannot read pane ({why}) - no verdict; use the artifact ground truth on this line"
            elif p["verdict"].startswith(("DEAD", "QUOTA", "BLOCKED")):
                action = f"ACT NOW: {p['verdict']}"
            elif p and any(f in p["flags"] for f in ACT_FLAGS):
                action = "ACT NOW: " + ",".join(f for f in p["flags"] if f in ACT_FLAGS)
            elif m >= 45:
                action = "TWO EXTENSIONS USED -> reassign (cancel + same --submit-id family)"
            elif not changed.get(tid) and not (p and "working" in p["flags"]):
                action = "ACT NOW: pane idle & unchanged for the whole window (wake / redeliver)"
            else:
                action = "WORKING -> extend 15m"
            print(f"REPLY-CLOCK #{tid} -> {to} {m}m | ctx {ctx} flags={flags} tail={tail} | {action} | {artifact_truth(tid, now)}", flush=True)
            changed[tid] = False

        for tid in list(last_hash):
            if tid not in live:
                last_hash.pop(tid, None)
                changed.pop(tid, None)

        if a.once:
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
