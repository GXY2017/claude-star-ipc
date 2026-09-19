# -*- coding: utf-8 -*-
"""Per-model task-duration statistics and pre-dispatch estimates (2026-09-19).

Data: ~/.claude/ipc/timing.jsonl — one line per first linked reply/fail, written by
ipc.py send() (source=live) or by `backfill` from every project mailbox (source=
backfill; those rows have no claimed_ts, so exec_min is None and total_min is used).
Metadata only, no bodies. Model = the launcher's $ModelTag for the role at the time
(live rows read it at write time; backfill uses ROLE_MODEL_HISTORY below).

  python ~/.claude/ipc/ipc_estimate.py backfill                 # (re)scan all mailboxes, dedupe
  python ~/.claude/ipc/ipc_estimate.py stats [--by model|role] [--since 2026-08-01]
  python ~/.claude/ipc/ipc_estimate.py estimate --role E --brief brief.md
  python ~/.claude/ipc/ipc_estimate.py estimate --role E --units 6 [--chars 3000]

Estimate = units x p80(min/unit) for the role's current model when >= MIN_N rows
with known units exist; else p80 of that model's total minutes; else all models;
n < MIN_N is printed as INSUFFICIENT and the fallback tier is named (review-
discipline rule 4: no rule from a single datapoint). ipc_brief_lint.py calls
rate_for(role) (p50 per unit) to replace its static DEFAULT_RATE when the ledger
has enough rows. Backfill rows measure dispatch->reply INCLUDING queue wait (no
claimed_ts before 2026-09-19), so their p80 is an upper bound on execution time;
live rows split queue_min / exec_min.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "timing.jsonl")
MIN_N = 5
UNITS = re.compile(r"UNITS\s*[:：]\s*(\d+)", re.I)
FEN = re.compile(r"(\d+)\s*份")
BUDGET = re.compile(r"(?:预算|时间预算|BUDGET)\s*[:：]\s*(\d+)\s*(?:分钟|min)?", re.I)

# role -> [(model_tag, from_date, to_date_exclusive)] — snapshot of the launcher's
# binding history (comments in ipc_wezterm_launch.ps1); backfill only.
ROLE_MODEL_HISTORY = {
    "A": [("Claude", "2026-01-01", None)],
    "B": [("Kimi", "2026-01-01", None)],
    "C": [("GLM-ollama", "2026-01-01", "2026-08-30"), ("Codex-sol", "2026-08-30", None)],
    "D": [("DeepSeek", "2026-01-01", None)],
    "E": [("Codex", "2026-01-01", None)],
    "F": [("GLM-Zhipu", "2026-08-14", None)],
    "CODEX": [("Codex", "2026-01-01", None)],
    "DS": [("DeepSeek", "2026-01-01", None)],
    "X": [("Claude-fleet2", "2026-01-01", None)],
}


def model_at(role, date):
    for tag, lo, hi in ROLE_MODEL_HISTORY.get(role.upper(), []):
        if date >= lo and (hi is None or date < hi):
            return tag
    return role


def current_model(role):
    sys.path.insert(0, HERE)
    import ipc  # noqa: E402  (reads $ModelTag from the launcher)
    return ipc._role_model(role)


def load():
    rows = []
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return rows


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def minutes(r):
    return r.get("exec_min") if r.get("exec_min") is not None else r.get("total_min")


def backfill():
    have = {(r.get("project"), r.get("task"), r.get("dispatch_ts")) for r in load()}
    added = 0
    with open(LOG, "a", encoding="utf-8") as out:
        for db in glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects",
                                         "*", "ipc", "_ipc.db")):
            project = os.path.basename(os.path.dirname(os.path.dirname(db)))
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            res_col = "result" if "result" in cols else "NULL"
            tasks = conn.execute(
                "SELECT id, recipient, ts, body, attempts FROM messages "
                "WHERE msg_type='task' AND sender='A' ORDER BY id").fetchall()
            for tid, role, ts, body, attempts in tasks:
                if (project, tid, ts) in have:
                    continue
                rep = conn.execute(
                    f"SELECT ts, msg_type, {res_col} FROM messages WHERE in_reply_to=? "
                    "AND sender=? AND msg_type IN ('reply','fail') ORDER BY id LIMIT 1",
                    (tid, role)).fetchone()
                if not rep:
                    continue
                d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                r = datetime.strptime(rep[0], "%Y-%m-%d %H:%M:%S")
                u = UNITS.search(body or "")
                fen = FEN.search((body or "").splitlines()[0] if body else "")
                b = BUDGET.search(body or "")
                out.write(json.dumps({
                    "ts": rep[0], "project": project, "task": tid, "role": role,
                    "model": model_at(role, ts[:10]), "dispatch_ts": ts,
                    "queue_min": None, "exec_min": None,
                    "total_min": round((r - d).total_seconds() / 60, 1),
                    "brief_chars": len(body or ""),
                    "units": int(u.group(1)) if u else (int(fen.group(1)) if fen else None),
                    "budget_min": int(b.group(1)) if b else None, "attempts": attempts,
                    "outcome": rep[1], "result": rep[2], "source": "backfill",
                }, ensure_ascii=False) + "\n")
                have.add((project, tid, ts))
                added += 1
            conn.close()
    print(f"backfill: +{added} rows -> {LOG} (total {len(load())})")


def stats(by="model", since=None):
    rows = [r for r in load() if minutes(r) is not None and (not since or r["ts"] >= since)]
    groups = {}
    for r in rows:
        groups.setdefault(r.get(by) or "?", []).append(r)
    print(f"{by:<14}{'n':>4}{'p50m':>7}{'p80m':>7}{'nU':>4}{'u/p50':>7}{'u/p80':>7}{'fail':>6}{'clean':>7}  (minutes; u/ = min per unit)")
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        mins = [minutes(r) for r in rs]
        per_u = [minutes(r) / r["units"] for r in rs if r.get("units")]
        fail = sum(r.get("outcome") == "fail" for r in rs) / len(rs)
        clean_known = [r for r in rs if r.get("result") is not None]
        clean = (sum(r["result"] == "CLEAN" for r in clean_known) / len(clean_known)) if clean_known else None
        print(f"{g:<14}{len(rs):>4}{pct(mins,50):>7.1f}{pct(mins,80):>7.1f}{len(per_u):>4}"
              f"{(pct(per_u,50) or 0):>7.2f}{(pct(per_u,80) or 0):>7.2f}{fail:>6.0%}"
              f"{('  -' if clean is None else f'{clean:>7.0%}')}")


def rate_for(role):
    """(p50 minutes per unit, n) for the role's current model, or None if n < MIN_N.
    p50 (typical rate) feeds the lint's hard "fits in 15 min" gate — the old static
    table was a typical rate too; p80 there would refuse briefs that used to pass,
    and backfill rows include queue wait. The p80 belongs to `estimate`'s 预算建议."""
    model = current_model(role)
    per_u = [minutes(r) / r["units"] for r in load()
             if r.get("model") == model and r.get("units") and minutes(r) is not None]
    if len(per_u) < MIN_N:
        return None
    return pct(per_u, 50), len(per_u)


def estimate(role, units=None, chars=None, brief=None):
    if brief:
        with open(brief, encoding="utf-8") as f:
            text = f.read()
        m = UNITS.search(text) or FEN.search(text.splitlines()[0] if text else "")
        units = units or (int(m.group(1)) if m else None)
        chars = chars or len(text)
    model = current_model(role)
    rows = [r for r in load() if minutes(r) is not None]
    mine = [r for r in rows if r.get("model") == model]
    tiers = []
    if units:
        per_u = [minutes(r) / r["units"] for r in mine if r.get("units")]
        tiers.append(("per-unit/" + model, [units * v for v in per_u]))
    tiers.append(("total/" + model, [minutes(r) for r in mine]))
    tiers.append(("total/all-models", [minutes(r) for r in rows]))
    for name, vals in tiers:
        if len(vals) >= MIN_N:
            p50, p80 = pct(vals, 50), pct(vals, 80)
            print(f"EST role={role} model={model} units={units or '?'} chars={chars or '?'} "
                  f"basis={name} n={len(vals)} p50={p50:.0f}m p80={p80:.0f}m -> 预算建议: {max(1, round(p80))} 分钟"
                  + ("" if name.startswith("per-unit") else "  (no per-unit history for this model yet)"))
            return
        print(f"  {name}: n={len(vals)} INSUFFICIENT (< {MIN_N}), falling back")
    print(f"EST role={role} model={model}: no history -> use the 15-minute default and slice")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill")
    s = sub.add_parser("stats")
    s.add_argument("--by", choices=("model", "role", "project"), default="model")
    s.add_argument("--since", default=None)
    e = sub.add_parser("estimate")
    e.add_argument("--role", required=True)
    e.add_argument("--brief", default=None)
    e.add_argument("--units", type=int, default=None)
    e.add_argument("--chars", type=int, default=None)
    a = ap.parse_args()
    if a.cmd == "backfill":
        backfill()
    elif a.cmd == "stats":
        stats(a.by, a.since)
    else:
        estimate(a.role, a.units, a.chars, a.brief)


if __name__ == "__main__":
    main()
