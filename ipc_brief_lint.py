# -*- coding: utf-8 -*-
"""Mechanical lint of a dispatch brief BEFORE it is sent (called by ipc_dispatch.ps1).

Each check is a 2026-08-29 incident turned into a rule (memory
hub-manage-not-operate-15min-slices, fleet-worker-capacity):
  H  header `# A -> <ROLE>` must name the role being dispatched to     (#1144: F's brief sent to C)
  B  a `预算: N 分钟` line is required and N <= --max-min (15)          (#1138: 18 reports, no budget)
  U  optional `UNITS: n`; with a known per-unit rate the estimate must fit the budget
  P  every absolute path mentioned must exist, or its parent must      (stale/typo paths)
  D  no dangling product target like "追加进 。" / "写入:" + nothing    (#1097: product path lost)
  D2 a declared product must be an ABSOLUTE path, never a bare `name.md`  (#1167/#1190: ground truth
     picked an old input because the product was only named, not pathed)
  N  "N 份" in the title vs the first "N 份" in the body must agree     (#1079: title 12, body 8) [WARN]
  G  code blocks must not use `cd X && ...` / heredoc / brace expansion (guard-safe shell rule) [WARN]

Usage:  python ~/.claude/ipc/ipc_brief_lint.py <brief.md> --role E [--max-min 15] [--rate E=3.0]
Exit 0 = pass (warnings allowed), 2 = FAIL (dispatch must stop unless -Force).
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Measured minutes per unit (2026-08-29 mailbox timing, X's recount): E ~3.0 min/report
# for distillation; F ~0.6 min/card for review. Unknown role -> no estimate.
DEFAULT_RATE = {"E": 3.0, "F": 0.6}

HEADER = re.compile(r"^#\s*(\w+)\s*(?:->|→|=>|—>|-->)\s*(\w+)")
BUDGET = re.compile(r"(?:预算|时间预算|BUDGET)\s*[:：]\s*(\d+)\s*(?:分钟|min)?", re.I)
UNITS = re.compile(r"UNITS\s*[:：]\s*(\d+)", re.I)
FEN = re.compile(r"(\d+)\s*份")
ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s`'\"()（）,，。;；|<>]+")
DANGLING = re.compile(r"(追加进|追加到|写进|写入|输出到|产物)\s*[:：]?\s*(?:[。.]|$)", re.M)
NEXT_LINE_ABS = re.compile(r"^[`*]*[A-Za-z]:\\")
PRODUCT_KW = re.compile(r"(产物|追加进|追加到|写进|写入|新建|落盘|输出到|产出)")
# a file-looking token that is NOT part of an absolute path (absolute paths are removed first)
# bare `name.md` or relative `_staging\name.md` (absolute paths are removed first)
BARE_FILE = re.compile(r"(?<![\w\\/.])((?:[\w\-\u4e00-\u9fff<>*]+[\\/])*[\w\-\u4e00-\u9fff<>*]+\.(?:md|tsv|txt|py|json|csv|log|xlsx|docx|pdf))(?![\w\\/.])(:\d+)?")
NEGATED_BEFORE = re.compile(r"(不要|不得|不准|不能|不许|禁止|别|勿|只读|不)\s*(?:直接|再|去|往|向|改|动)?\s*$")
CD_COMPOUND = re.compile(r"\bcd\s+[^\n]*&&")
HEREDOC = re.compile(r"<<\s*'?[A-Z_]+'?")
BRACE = re.compile(r"\{[^{}\n]*,[^{}\n]*\}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--role", required=True)
    ap.add_argument("--max-min", type=int, default=15)
    ap.add_argument("--rate", action="append", default=[], help="ROLE=min_per_unit override")
    a = ap.parse_args()
    role = a.role.upper()
    rate = dict(DEFAULT_RATE)
    rate_src = "static table"
    try:  # 2026-09-19: ledger-derived p50 min/unit for the role's CURRENT model beats the static table
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ipc_estimate import rate_for
        learned = rate_for(role)
        if learned:
            rate[role] = round(learned[0], 2)
            rate_src = f"timing.jsonl p50, n={learned[1]}"
    except Exception as e:  # noqa: BLE001 — lint must run without the ledger
        rate_src = f"static table (ledger unavailable: {e})"
    for kv in a.rate:
        k, v = kv.split("=", 1)
        rate[k.upper()] = float(v)

    text = open(a.brief, encoding="utf-8").read()
    lines = text.splitlines()
    fails, warns = [], []

    # H header
    first = next((l for l in lines if l.strip()), "")
    m = HEADER.match(first.strip())
    if not m:
        fails.append(f"H 首行不是 `# <from> -> <role>` 标题: {first[:60]!r}")
    elif m.group(2).upper() != role:
        fails.append(f"H 标题写的是 -> {m.group(2)}，但正在派给 {role}")

    # B budget
    mb = BUDGET.search(text)
    budget = None
    if not mb:
        fails.append(f"B 缺 `预算: N 分钟` 行（规则：每单 <= {a.max_min} 分钟）")
    else:
        budget = int(mb.group(1))
        if budget > a.max_min:
            fails.append(f"B 预算 {budget} 分钟 > 上限 {a.max_min}，拆单")

    # U units vs rate
    mu = UNITS.search(text)
    units = int(mu.group(1)) if mu else None
    if units and role in rate:
        est = units * rate[role]
        if est > a.max_min:
            fails.append(f"U UNITS={units} × {rate[role]} 分钟/份（{role} {rate_src}）≈ {est:.0f} 分钟 > {a.max_min}，拆单")
        elif budget is not None and est > budget:
            warns.append(f"U 估算 {est:.0f} 分钟（{rate[role]} 分钟/份，{rate_src}）> 你写的预算 {budget} 分钟")
    elif not units:
        warns.append("U 没写 `UNITS: n`，无法按实测吞吐校验单量")

    # P paths
    for tok in sorted(set(ABS_PATH.findall(text))):
        p = tok.rstrip("\\.:")
        if os.path.exists(p) or os.path.isdir(os.path.dirname(p)):
            continue
        fails.append(f"P 路径不存在且父目录也不存在: {p}")

    # D dangling product target
    for md in DANGLING.finditer(text):
        ln = text.count("\n", 0, md.start()) + 1
        seg = lines[ln - 1].strip()
        if "```" in seg or seg.startswith("#") or seg.startswith("|"):
            continue   # headings ("## 产物") and table rows are not targets
        # keyword at end of line + the ABSOLUTE path alone on the next line is a legitimate layout
        # (2026-08-30 #1209, A's b2s4 brief); anything else on the next line is still dangling.
        nxt = next((x.strip() for x in lines[ln:] if x.strip()), "")
        if NEXT_LINE_ABS.match(nxt):
            continue
        fails.append(f"D 第 {ln} 行产物/写入目标为空: {seg[:60]!r}（下一行也不是以绝对路径开头）")

    # D2 product declared as a bare filename (must be absolute)
    for i, l in enumerate(lines, 1):
        if l.strip().startswith("#"):
            continue
        kw = PRODUCT_KW.search(l)
        if not kw:
            continue
        if NEGATED_BEFORE.search(l[:kw.start()]):   # "不写入 分析素材.md": a prohibition, not a product
            continue
        # same line, and only AFTER the keyword ("不要改 `x.py`…写进回复" has its token before it)
        stripped = ABS_PATH.sub(" ", l[kw.end():])
        for mm in BARE_FILE.finditer(stripped):
            name, cite = mm.group(1), mm.group(2)
            if cite:                       # `file.md:98` is an evidence citation, not a product
                continue
            if "<" in name or "*" in name:  # naming template, not a product
                continue
            base = name.replace("/", "\\").split("\\")[-1]
            fails.append(f"D2 第 {i} 行产物写成非绝对路径 `{name}`：改成绝对路径，例如 "
                         f"C:\\Users\\<user>\\Downloads\\<项目>\\_staging\\{base}（更正类简报也要写全）")

    # N title-count vs body-count
    mt = FEN.search(first)
    if mt:
        body = "\n".join(lines[1:])
        mbdy = FEN.search(body)
        if mbdy and mbdy.group(1) != mt.group(1):
            warns.append(f"N 标题说 {mt.group(1)} 份，正文第一次出现的是 {mbdy.group(1)} 份")
        if units and int(mt.group(1)) != units:
            warns.append(f"N 标题说 {mt.group(1)} 份，UNITS={units}")

    # G guard-safe shell shapes inside code fences
    in_code = False
    for i, l in enumerate(lines, 1):
        if l.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            continue
        if CD_COMPOUND.search(l):
            warns.append(f"G 第 {i} 行 `cd … &&` 复合命令（guard-safe 规则禁止）")
        if HEREDOC.search(l):
            warns.append(f"G 第 {i} 行 heredoc（含 CJK 时会卡权限判定，改 .py 文件）")
        if BRACE.search(l):
            warns.append(f"G 第 {i} 行大括号展开（guard-safe 规则禁止）")

    for w in warns:
        print("WARN", w)
    for f in fails:
        print("FAIL", f)
    tag = f"budget={budget if budget is not None else '-'} units={units if units else '-'}"
    if fails:
        print(f"LINT FAIL ({len(fails)} fail, {len(warns)} warn) {tag}")
        sys.exit(2)
    print(f"LINT OK ({len(warns)} warn) {tag}")


if __name__ == "__main__":
    main()
