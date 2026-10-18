# -*- coding: utf-8 -*-
"""Per-model task-duration statistics and pre-dispatch estimates (2026-09-19).

Data: ~/.claude/ipc/timing.jsonl — one line per first linked reply/fail, written by
ipc.py send() (source=live) or by `backfill` from every project mailbox (source=
backfill; those rows have no claimed_ts, so exec_min is None and total_min is used).
Metadata only, no bodies. Model = the launcher's $ModelTag for the role at the time
(live rows read it at write time; backfill uses ROLE_MODEL_HISTORY below). Live rows
since 2026-09-19 also carry `variant` = model + parameters parsed from $LaunchCmd
(ipc._role_variant, e.g. Codex/gpt-5.6-sol/xhigh); rate_for/estimate try the
variant tier before the vendor tag, and `fail` rows never enter a duration sample.

  python ~/.claude/ipc/ipc_estimate.py backfill                 # (re)scan all mailboxes, dedupe
  python ~/.claude/ipc/ipc_estimate.py stats [--by model|role|type|cell] [--since 2026-08-01]
  python ~/.claude/ipc/ipc_estimate.py estimate --role E --brief brief.md [--type 取数]
  python ~/.claude/ipc/ipc_estimate.py estimate --role E --units 6 [--chars 3000]
  python ~/.claude/ipc/ipc_estimate.py rank --type 取数 [--open] [--roles B,D,E]

2026-10-17 routing (memory ipc-record-dedup-plan-20261017): rows carry `type` (the
brief's 类型: field), `verdict` (pass|rework|redispatch|fail|replan|unsolvable),
`new_items` and `used_new`. `rank` prints one line per candidate role for one task
type; the role level is that role's rows under its CURRENT model ($ModelTag), the
cell is the same rows of that type, and thin cells are shrunk toward the role
level: est = (n_cell*v_cell + k*v_role) / (n_cell + k), k = 10. Rows written
before 2026-10-17 have no verdict and count as pass; they still count for timing.
IPC_TIMING_PATH points every command at another ledger file (tests use a copy).

Correction rows: `ipc.py grade` appends {"grade": true, project, task, dispatch_ts,
+fields}. Every reader must fold them into their base row (ipc.fold_ledger; load()
here does): a raw grep / jq / wc over timing.jsonl counts corrections as tasks and
shows pre-grade verdicts. Cancel rows (outcome cancel) are the hub's decision, not
a role failure: stats, rank and the estimates leave them out.

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
sys.path.insert(0, HERE)
try:
    import ipc  # noqa: E402  (fold, type whitelist, verdict rules; computes paths only, creates nothing)
except Exception as _e:  # noqa: BLE001 — fail loudly, never with a bare traceback
    print(f"ipc import failed: {type(_e).__name__}: {_e}")
    print("ERROR kind=env next=python ~/.claude/ipc/ipc.py --help to see the import error")
    sys.exit(1)
LOG = os.environ.get("IPC_TIMING_PATH") or os.path.join(HERE, "timing.jsonl")
MIN_N = 5
K_SHRINK = 10        # shrinkage strength: a cell with 10 rows weighs as much as the role level
SMALL_CELL = 3       # below this many rows in (role, type): short pieces only (<= 5 min)
OPEN_WEIGHT = 0.3    # rank --open: weight of the NEW component (provide + adoption)
NEW_NEUTRAL = 0.5    # NEW component of a role with no NEW data at all: neither rewarded nor penalised
UNTYPED = ipc.UNTYPED   # a row without `type` (older rows) falls here
SHRINK_HELP = ("est = (n_cell*v_cell + k*v_role) / (n_cell + k), k = %d: the role level "
               "(the role's rows under its current model) when the (role, type) cell is "
               "empty, sliding to the cell's own value as its rows accumulate. rank --open: "
               "NEW component = mean(provide, adopt), %.1f (neutral) for a role with no NEW "
               "data at all" % (K_SHRINK, NEW_NEUTRAL))
UNITS = re.compile(r"UNITS\s*[:：]\s*(\d+)", re.I)
FEN = re.compile(r"(\d+)\s*份")
BUDGET = re.compile(r"(?:预算|时间预算|BUDGET)\s*[:：]\s*(\d+)\s*(?:分钟|min)?", re.I)

# role -> [(model_tag, from_date, to_date_exclusive)] — snapshot of the launcher's
# binding history (comments in ipc_wezterm_launch.ps1); backfill only.
ROLE_MODEL_HISTORY = {
    "A": [("Claude", "2026-01-01", None)],
    "B": [("Kimi", "2026-01-01", None)],
    "C": [("GLM-ollama", "2026-01-01", "2026-08-30"), ("Codex-sol", "2026-08-30", "2026-09-19"),
          ("Codex-luna", "2026-09-19", "2026-10-04"), ("Codex-gpt6-luna", "2026-10-04", None)],
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
    return ipc._role_model(role)   # reads $ModelTag from the launcher


def load():
    """Base rows with `grade` correction rows folded in (ipc.fold_ledger: matched
    on (project, task, dispatch_ts), latest correction wins field by field)."""
    rows = []
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
    return ipc.fold_ledger(rows)   # single authority for the fold


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def minutes(r):
    return r.get("exec_min") if r.get("exec_min") is not None else r.get("total_min")


def _col(have, name):
    return name if name in have else f"NULL AS {name}"


def backfill(dbs=None):
    """(Re)scan project mailboxes (`dbs`, default every ~/.claude/projects/*/ipc/
    _ipc.db, read-only; messages and messages_archive) and append a base row for
    each answered A-task the ledger lacks. Rows carry the 2026-10-17 fields (type,
    verdict, new_items, anchor; reason null, used_new none). A task that already
    has a row is skipped, so its verdict and grade corrections are never touched:
    the pre-filter reads the folded ledger, ipc.ledger_append_new re-checks every
    key under the ledger lock."""
    have = {(r.get("project"), r.get("task"), r.get("dispatch_ts")) for r in load()}
    recs = []
    for db in dbs or glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects",
                                            "*", "ipc", "_ipc.db")):
        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(db))))
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = ["messages"] + (["messages_archive"] if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_archive'"
            ).fetchone() else [])
            parts = []
            for tbl in tables:
                h = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
                parts.append(f"SELECT id, ts, sender, recipient, body, msg_type, in_reply_to, "
                             f"attempts, {_col(h, 'result')}, {_col(h, 'task_type')}, "
                             f"{_col(h, 'new_items')} FROM {tbl}")
            src = "(" + " UNION ALL ".join(parts) + ")"
            tasks = conn.execute(
                f"SELECT id, recipient, ts, body, attempts, task_type FROM {src} "
                "WHERE msg_type='task' AND sender='A' ORDER BY id").fetchall()
            for tid, role, ts, body, attempts, ttype in tasks:
                if (project, tid, ts) in have:
                    continue
                rep = conn.execute(
                    f"SELECT ts, msg_type, result, body, new_items FROM {src} "
                    "WHERE in_reply_to=? AND sender=? AND msg_type IN ('reply','fail') "
                    "AND ts >= ? "                     # a reply never predates its task
                    "ORDER BY id LIMIT 1", (tid, role, ts)).fetchone()
                if not rep:
                    continue
                rep_ts, outcome, result, rep_body, n_new = rep
                d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                r = datetime.strptime(rep_ts, "%Y-%m-%d %H:%M:%S")
                u = UNITS.search(body or "")
                fen = FEN.search((body or "").splitlines()[0] if body else "")
                b = BUDGET.search(body or "")
                recs.append({
                    "ts": rep_ts, "project": project, "task": tid, "role": role,
                    "model": model_at(role, ts[:10]), "dispatch_ts": ts,
                    "queue_min": None, "exec_min": None,
                    "total_min": round((r - d).total_seconds() / 60, 1),
                    "brief_chars": len(body or ""),
                    "units": int(u.group(1)) if u else (int(fen.group(1)) if fen else None),
                    "budget_min": int(b.group(1)) if b else None, "attempts": attempts,
                    "outcome": outcome, "result": result, "source": "backfill",
                    # older mailboxes have no task_type / new_items column: parse instead
                    "type": ttype or ipc.task_type_of(body),
                    "verdict": ipc._auto_verdict(outcome, attempts, result),
                    "reason": None,
                    "new_items": n_new if n_new is not None else (
                        ipc.count_new_items(rep_body) if outcome == "reply" else 0),
                    "used_new": "none", "anchor": ipc._anchor(tid, role),
                })
                have.add((project, tid, ts))
        finally:
            conn.close()
    written = ipc.ledger_append_new(recs)
    print(f"backfill: +{len(written)} rows -> {LOG} (total {len(load())})")


def timed(r):
    """A row that may feed a duration sample: has minutes AND did not fail.
    (2026-09-19 review item 2: five fast `fail` rows formed a valid throughput
    rate and drove the budget below what real work needs.) Fail rows still
    count toward `fail%` in stats. Cancel rows (a claimed task the hub cancelled,
    2026-10-17) measure dispatch->cancel, not work, and are excluded the same way."""
    return minutes(r) is not None and r.get("outcome") not in ("fail", "cancel")


def row_type(r):
    return r.get("type") or UNTYPED


def verdict_of(r):
    """Code- or hub-set verdict; rows written before 2026-10-17 count as pass."""
    return r.get("verdict") or "pass"


def _group_key(r, by):
    if by == "type":
        return row_type(r)
    if by == "cell":
        return f"{r.get('role') or '?'}×{row_type(r)}"
    return r.get(by) or "?"


def not_cancel(r):
    """Cancel rows record the hub's decision, not a role outcome: out of every
    rate, count and duration here (the ledger keeps them)."""
    return r.get("outcome") != "cancel"


def stats(by="model", since=None):
    rows = [r for r in load() if minutes(r) is not None and not_cancel(r)
            and (not since or r["ts"] >= since)]
    groups = {}
    for r in rows:
        groups.setdefault(_group_key(r, by), []).append(r)
    print(f"{by:<28}{'n':>4}{'p50m':>7}{'p80m':>7}{'nU':>4}{'u/p50':>7}{'u/p80':>7}{'fail':>6}{'clean':>7}  (minutes; u/ = min per unit; durations exclude fail rows)")
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ok = [r for r in rs if timed(r)]
        mins = [minutes(r) for r in ok]
        per_u = [minutes(r) / r["units"] for r in ok if r.get("units")]
        fail = sum(r.get("outcome") == "fail" for r in rs) / len(rs)
        clean_known = [r for r in rs if r.get("result") is not None]
        clean = (sum(r["result"] == "CLEAN" for r in clean_known) / len(clean_known)) if clean_known else None
        print(f"{g:<28}{len(rs):>4}{(pct(mins,50) or 0):>7.1f}{(pct(mins,80) or 0):>7.1f}{len(per_u):>4}"
              f"{(pct(per_u,50) or 0):>7.2f}{(pct(per_u,80) or 0):>7.2f}{fail:>6.0%}"
              f"{('  -' if clean is None else f'{clean:>7.0%}')}")


def current_variant(role):
    return ipc._role_variant(role)


def _keys(role):
    """[(field, value)] tiers, most specific first: exact model+params, then vendor tag."""
    return [("variant", current_variant(role)), ("model", current_model(role))]


def rate_for(role):
    """(p50 minutes per unit, n) for the role's current model+params, falling back
    to the vendor tag; None if no tier reaches MIN_N. p50 (typical rate) feeds the
    lint's hard "fits in 15 min" gate — the old static table was a typical rate
    too; p80 there would refuse briefs that used to pass, and backfill rows include
    queue wait. The p80 belongs to `estimate`'s 预算建议. Fail rows excluded."""
    rows = [r for r in load() if timed(r) and r.get("units")]
    for field, key in _keys(role):
        per_u = [minutes(r) / r["units"] for r in rows if r.get(field) == key]
        if len(per_u) >= MIN_N:
            return pct(per_u, 50), len(per_u)
    return None


def shrink(n_cell, v_cell, v_role, k=K_SHRINK):
    """est = (n_cell*v_cell + k*v_role) / (n_cell + k); the role value alone when
    the cell has no sample, the cell value alone when the role level has none."""
    if v_role is None:
        return v_cell
    if not n_cell or v_cell is None:
        return v_role
    return (n_cell * v_cell + k * v_role) / (n_cell + k)


def share(rows, pred):
    return (sum(1 for r in rows if pred(r)) / len(rows)) if rows else None


def role_sample(rows, role):
    """(rows, model): `role`'s rows under its CURRENT model tag. A role's history
    under an earlier model (C was GLM, then three Codex variants) says nothing
    about the model behind it now. Rows without a model tag are kept."""
    model = current_model(role)
    return [r for r in rows if r.get("role") == role
            and r.get("model") in (None, model)], model


# Worker lineup for `rank` without --roles. Mirror of the worker keys of $ModelTag
# in ipc_wezterm_launch.ps1 (fleet 1: B-F) plus fleet 2's X; the legacy channels
# CODEX / DS are left out. Update together with the launcher.
LINEUP = ("B", "C", "D", "E", "F", "X")
LEGACY_ALIASES = ("CODEX", "DS")   # same models as E / D; ranked only when named in --roles


def default_roles(rows):
    """LINEUP plus every worker role present in the ledger rows, minus the hub and
    the legacy aliases."""
    hub = os.environ.get("IPC_HUB", "A")
    return sorted((set(LINEUP) | {r.get("role") for r in rows if r.get("role")})
                  - {hub} - set(LEGACY_ALIASES))


def rank(task_type, open_task=False, roles=None, k=K_SHRINK):
    """One line per candidate role for one task type, best first. Columns:
    n(timed) (rows of this role and type with a minute value, fail and cancel
    excluded: the sample the duration shrink uses), n_role (same, all the role's
    types), exec p50/p80 in
    minutes, first-pass rate (verdict pass), redispatch rate, and with --open:
    new = NEW provide rate = rows with new_items > 0 among the reply rows written
    live under the NEW protocol (outcome reply, carrying `anchor`; fail rows,
    backfilled and pre-2026-10-17 rows are not in the denominator); adopt = NEW adoption rate = rows with used_new != none among the
    same live NEW-protocol rows that have new_items > 0 (symmetric with provide).
    Every value is shrunk toward the role level: est = (n_cell*v_cell +
    k*v_role) / (n_cell + k), k = 10. score = (1-w)*first_pass +
    w*mean(provide, adoption) (0.5, neutral, when the role has no NEW data at
    all), w = 0.3 with --open, else 0; ties go to the lower
    p80. Cancel rows are left out (the hub's decision, not a role outcome).
    n(timed) < 3 = small-sample (short pieces only, <= 5 min); n_role = 0 = no
    basis. A picks; nothing is enforced."""
    w = OPEN_WEIGHT if open_task else 0.0
    rows = [r for r in load() if not_cancel(r)]           # one ledger read per command
    out = []
    for role in roles or default_roles(rows):
        rs, model = role_sample(rows, role)
        cell = [r for r in rs if row_type(r) == task_type]
        tr = [minutes(r) for r in rs if timed(r)]
        tc = [minutes(r) for r in cell if timed(r)]
        p50 = shrink(len(tc), pct(tc, 50), pct(tr, 50), k)
        p80 = shrink(len(tc), pct(tc, 80), pct(tr, 80), k)

        def shrunk_share(pred, among=lambda r: True):
            c = [r for r in cell if among(r)]
            a = [r for r in rs if among(r)]
            return shrink(len(c), share(c, pred), share(a, pred), k)

        def has_new(r):
            return (r.get("new_items") or 0) > 0

        def new_protocol(r):     # a reply written live by send() since 2026-10-17
            return ("anchor" in r and r.get("source") == "live"
                    and r.get("outcome") == "reply")   # fail rows can never carry NEW

        first = shrunk_share(lambda r: verdict_of(r) == "pass")
        redisp = shrunk_share(lambda r: verdict_of(r) == "redispatch")
        provide = shrunk_share(has_new, among=new_protocol)
        adopt = shrunk_share(lambda r: (r.get("used_new") or "none") != "none",
                             among=lambda r: has_new(r) and new_protocol(r))
        new_parts = [v for v in (provide, adopt) if v is not None]
        new_score = sum(new_parts) / len(new_parts) if new_parts else NEW_NEUTRAL
        score = None if first is None else (1 - w) * first + w * new_score
        # Counts are the timed sample the duration shrink uses (rows with an
        # exec/total minute value, fail and cancel excluded), as in _cell_estimate.
        note = ("no basis" if not tr else
                "small-sample: short pieces only (<=5 min)" if len(tc) < SMALL_CELL else "")
        out.append((role, model, len(tc), len(tr), p50, p80, first, redisp,
                    provide, adopt, score, note))
    out.sort(key=lambda o: (o[10] is None, -(o[10] or 0), o[5] if o[5] is not None else 1e9))

    def f(v, pctg=False):
        if v is None:
            return "-"
        return f"{v:.0%}" if pctg else f"{v:.1f}"

    print(f"RANK type={task_type} open={'yes' if open_task else 'no'} (NEW weight {w}) "
          f"k={k}: {SHRINK_HELP}")
    head = (f"{'role':<6}{'model':<18}{'n(timed)':>9}{'n_role':>7}{'p50m':>7}{'p80m':>7}"
            f"{'pass':>6}{'redisp':>7}" + (f"{'new':>6}{'adopt':>6}" if open_task else "")
            + f"{'score':>7}  note")
    print(head)
    for (role, model, nc, nr, p50, p80, first, redisp, provide, adopt, score, note) in out:
        print(f"{role:<6}{(model or '-')[:17]:<18}{nc:>9}{nr:>7}{f(p50):>7}{f(p80):>7}"
              f"{f(first, True):>6}{f(redisp, True):>7}"
              + (f"{f(provide, True):>6}{f(adopt, True):>6}" if open_task else "")
              + f"{f(score, True) if score is not None else '-':>7}  {note}")


def _cell_estimate(rows, role, task_type, units, chars, k=K_SHRINK):
    """estimate --type: budget from the (role, type) cell shrunk toward the role
    level (SHRINK_HELP), per unit when units are known. `rows` = the timed rows
    estimate() already loaded. False = role level too thin (< MIN_N), the caller
    falls back to the model tiers."""
    rs, model = role_sample(rows, role)
    cell = [r for r in rs if row_type(r) == task_type]
    small = "  small-sample: short pieces only (<=5 min)" if len(cell) < SMALL_CELL else ""
    name = f"cell-shrunk/{model}×{task_type}"
    if units:
        pr = [minutes(r) / r["units"] for r in rs if r.get("units")]
        pc = [minutes(r) / r["units"] for r in cell if r.get("units")]
        if len(pr) >= MIN_N:
            p50 = units * shrink(len(pc), pct(pc, 50), pct(pr, 50), k)
            p80 = units * shrink(len(pc), pct(pc, 80), pct(pr, 80), k)
            print(f"EST role={role} model={model} type={task_type} units={units} "
                  f"chars={chars or '?'} basis={name} per-unit n_cell={len(pc)} "
                  f"n_role={len(pr)} k={k} p50={p50:.0f}m p80={p80:.0f}m "
                  f"-> 预算建议: {max(1, round(p80))} 分钟{small}")
            return True
        print(f"  {name} per-unit: n_role={len(pr)} INSUFFICIENT (< {MIN_N}), falling back")
    tr = [minutes(r) for r in rs]
    tc = [minutes(r) for r in cell]
    if len(tr) >= MIN_N:
        p50 = shrink(len(tc), pct(tc, 50), pct(tr, 50), k)
        p80 = shrink(len(tc), pct(tc, 80), pct(tr, 80), k)
        print(f"EST role={role} model={model} type={task_type} units={units or '?'} "
              f"chars={chars or '?'} basis={name} total n_cell={len(tc)} n_role={len(tr)} "
              f"k={k} p50={p50:.0f}m p80={p80:.0f}m -> 预算建议: {max(1, round(p80))} 分钟{small}"
              + ("" if not units else "  (no per-unit history for this role yet)"))
        return True
    print(f"  {name} total: n_role={len(tr)} INSUFFICIENT (< {MIN_N}), falling back")
    return False


def estimate(role, units=None, chars=None, brief=None, task_type=None):
    if brief:
        with open(brief, encoding="utf-8") as f:
            text = f.read()
        m = UNITS.search(text) or FEN.search(text.splitlines()[0] if text else "")
        units = units or (int(m.group(1)) if m else None)
        chars = chars or len(text)
    rows = [r for r in load() if timed(r)]                # one ledger read per command
    if task_type and _cell_estimate(rows, role, task_type, units, chars):
        return
    keys = _keys(role)
    label = keys[0][1]
    tiers = []
    for field, key in keys:
        mine = [r for r in rows if r.get(field) == key]
        if units:
            per_u = [minutes(r) / r["units"] for r in mine if r.get("units")]
            tiers.append((f"per-unit/{key}", [units * v for v in per_u]))
        tiers.append((f"total/{key}", [minutes(r) for r in mine]))
    tiers.append(("total/all-models", [minutes(r) for r in rows]))
    for name, vals in tiers:
        if len(vals) >= MIN_N:
            p50, p80 = pct(vals, 50), pct(vals, 80)
            print(f"EST role={role} model={label} units={units or '?'} chars={chars or '?'} "
                  f"basis={name} n={len(vals)} p50={p50:.0f}m p80={p80:.0f}m -> 预算建议: {max(1, round(p80))} 分钟"
                  + ("" if name.startswith("per-unit") else "  (no per-unit history at this tier yet)"))
            return
        print(f"  {name}: n={len(vals)} INSUFFICIENT (< {MIN_N}), falling back")
    print(f"EST role={role} model={label}: no history -> use the 15-minute default and slice")


def accuracy(since=None):
    """Per model: review replies whose findings A checked one by one (grade --findings).
    precision = held / (held + false); checked = (held + false) / all findings."""
    acc = {}
    for r in load():
        f = r.get("findings")
        if not isinstance(f, dict) or (since and (r.get("dispatch_ts") or "") < since):
            continue
        a = acc.setdefault(r.get("model") or "?", {"n": 0, "held": 0, "false": 0, "unchecked": 0})
        a["n"] += 1
        for k in ("held", "false", "unchecked"):
            a[k] += int(f.get(k) or 0)
    if not acc:
        print("ACCURACY  no graded findings yet (ipc.py grade --task N --findings HELD,FALSE,UNCHECKED)")
        return
    print(f"{'model':<18} {'reviews':>7} {'held':>5} {'false':>5} {'unchk':>5} {'precision':>9} {'checked':>7}")
    for model, a in sorted(acc.items()):
        judged = a["held"] + a["false"]
        total = judged + a["unchecked"]
        prec = f"{a['held'] / judged:.0%}" if judged else "-"
        print(f"{model:<18} {a['n']:>7} {a['held']:>5} {a['false']:>5} {a['unchecked']:>5} "
              f"{prec:>9} {judged / total:>7.0%}")


def main():
    types = ipc.TASK_TYPES + (UNTYPED,)   # single authority for the 类型 whitelist
    ap = argparse.ArgumentParser(description="Task-duration statistics, budget estimates "
                                             "and per-role routing ranks from timing.jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="append rows for answered tasks the ledger lacks "
                                         "(never touches existing rows or their corrections)")
    bf.add_argument("--db", action="append", default=None,
                    help="scan only this _ipc.db (repeatable; read-only). Default: every "
                         "~/.claude/projects/*/ipc/_ipc.db")
    s = sub.add_parser("stats")
    s.add_argument("--by", choices=("variant", "model", "role", "project", "type", "cell"),
                   default="model", help="cell = role×type")
    s.add_argument("--since", default=None)
    e = sub.add_parser("estimate")
    e.add_argument("--role", required=True)
    e.add_argument("--brief", default=None)
    e.add_argument("--units", type=int, default=None)
    e.add_argument("--chars", type=int, default=None)
    e.add_argument("--type", dest="task_type", choices=types, default=None,
                   help="budget from the (role, type) cell shrunk toward the role level: "
                        + SHRINK_HELP)
    rk = sub.add_parser("rank", help="one line per candidate role for one task type",
                        description=rank.__doc__)
    rk.add_argument("--type", dest="task_type", choices=types, required=True)
    rk.add_argument("--open", dest="open_task", action="store_true",
                    help="open-ended task (检索核验, 评审, root cause, a brief that may be "
                         f"wrong): NEW provide + adoption rates enter the score, weight {OPEN_WEIGHT}")
    rk.add_argument("--roles", default=None,
                    help="comma list of candidate roles (default: LINEUP B C D E F X plus the "
                         "ledger's roles, minus the hub)")
    ac = sub.add_parser("accuracy", help="per model: review findings A checked one by one "
                                         "(grade --findings): held / false / unchecked, precision")
    ac.add_argument("--since", default=None, help="dispatch date floor, e.g. 2026-10-18")
    a = ap.parse_args()
    if a.cmd == "accuracy":
        accuracy(a.since)
    elif a.cmd == "backfill":
        backfill(a.db)
    elif a.cmd == "stats":
        stats(a.by, a.since)
    elif a.cmd == "rank":
        rank(a.task_type, a.open_task,
             [x.strip() for x in a.roles.split(",") if x.strip()] if a.roles else None)
    else:
        estimate(a.role, a.units, a.chars, a.brief, a.task_type)


if __name__ == "__main__":
    main()
