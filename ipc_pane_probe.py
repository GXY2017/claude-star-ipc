# -*- coding: utf-8 -*-
"""Mechanical liveness probe of a fleet pane: context-left %, activity flags, tail hash.

Why it exists (2026-08-29 postmortem, memory codex-context-zero-recv-loop): the role
registry / heartbeat said E was live while its pane showed `Context 0% left` and an
endless `recv --block` loop; A dispatched three tasks into that hole. Heartbeat is the
wrong lens — the pane status line is the only truthful capacity signal. This probe reads
it so ipc_dispatch.ps1 (gate) and stall_watch.py (15-min reply clock) never rely on a
human reading a percentage.

Status-line formats handled (verified 2026-08-30 on fleet1):
  codex          : "gpt-6-luna max · Context 79% left · ~\\Downloads\\..."
  kimi / glm     : "[k3[1m]] pdfpeek | Ctx 62% left"
  claude+statusline.js: "ctx 12.3% (25k/200k)"   <- USED %, converted to left

Usage:
  python ~/.claude/ipc/ipc_pane_probe.py E            # one role, human line
  python ~/.claude/ipc/ipc_pane_probe.py E F --json   # NDJSON, one object per role
  python ~/.claude/ipc/ipc_pane_probe.py --all
Exit 0 always (a probe failure is data, reported as verdict UNKNOWN).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
WEZTERM = r"C:\Program Files\WezTerm\wezterm.exe"
PANE_MAP = os.path.join(HOME, ".claude", "ipc", "wezterm_panes.json")
FLEET2_MAP = os.path.join(HOME, ".claude", "ipc", "wezterm_fleet2.json")
SOCK = {
    "fleet1": os.path.join(HOME, ".local", "share", "wezterm", "fleet1.sock"),
    "fleet2": os.path.join(HOME, ".local", "share", "wezterm", "fleet2.sock"),
}
OVERRIDE = os.path.join(HOME, ".claude", "ipc", "wezterm_sock_override.json")
DEAD_CTX = 20      # <= this % left: refuse dispatch
WARN_CTX = 70      # <  this % left: heavy batches not allowed (capacity policy 2026-08-19)

CTX_LEFT = re.compile(r"\b(?:Context|Ctx)\s+(\d{1,3})%\s+left", re.I)
CTX_USED = re.compile(r"\bctx\s+(\d{1,3}(?:\.\d+)?)%\s*\(", re.I)
RECV_CALL = re.compile(r"ipc\.py\"?\s+recv\s+--me\s+\w+\s+--block")
RECV_NONE = re.compile(r"NONE \(timeout\)")
WORKING = re.compile(r"(Working \(|esc to interrupt|Esc to cancel|…\s*\(\d+[ms]|Sketching|Thinking|Brewing|Pondering)", re.I)
IDLE = re.compile(r"(^\s*[❯›>]\s*$|› Ask Codex|Worked for \d)", re.M)
# Banner/prompt regexes are deliberately NARROW: the tail also contains ordinary
# conversation text ("配额", "403", "permission" show up in briefs and replies), and a
# false QUOTA verdict would refuse a healthy worker (seen on B/A in the first probe run).
PERMISSION = re.compile(r"(Do you want to proceed|Yes, and don't ask again|❯\s*1\.\s*Yes|Allow (once|always)|Trust this folder\?)", re.I)
QUOTA = re.compile(r"(hit your (usage |5-hour |weekly )?limit|usage limit reached|You've reached your|resets? (at|in) \d+\s*(:|h|m|am|pm)|HTTP(/\d\.\d)? 403|status(_code)?[ =:]+403|Request rejected \(429\)|usage limit|quota will reset|reached your.{0,20}limit|402 Insufficient Balance|已达到.{0,12}使用上限|限额将在.{0,30}重置)", re.I)
ERROR = re.compile(r"(Traceback \(most recent|Exception:)", re.M)   # info only; never an ACT trigger
# SGR mouse-report junk: a TUI died without restoring terminal modes, mouse motion echoes into the
# shell as `[555;110;4M[555;110;5M...` and floods the pane (2026-08-30 F, #1416). Three+ reports on
# one line is nothing a human or program prints on purpose.
MOUSE_JUNK = re.compile(r"(\[\d{1,4};\d{1,4};\d{1,4}[Mm]){3,}")
# Banner SHAPE (2026-08-30 #1259): quota/permission words are only trusted inside an API-error banner
# block - a line starting with the assistant bullet that carries "API Error", plus its wrapped
# continuation lines. Body text that merely TALKS about quotas (X reporting "B、F 均在限额中",
# "quota will reset") made the dispatch gate refuse X itself at 14:32. Shape, not word blacklists.
BANNER_START = re.compile(r"^\s*[●•]\s.*\bAPI Error\b")
BANNER_STOP = re.compile(r"^(\s*[●•✻❯›>─]|\S)")         # next marker line (any indent) or a COLUMN-0 line ends the block; indented text continues it
# codex (2026-08-30 #1294, samples quota_codex_C/E_credits.txt): the notice starts with `■ ` at
# column 0 and its continuation lines are UNINDENTED, ending at the `› Ask Codex` prompt. `■` is
# codex's system-notice marker; assistant prose uses `•`, tool denials `✗` - so the marker alone is
# the shape, no keyword blacklist needed.
CODEX_BANNER_START = re.compile(r"^\s*■\s")
CODEX_BANNER_STOP = re.compile(r"^\s*[■•✗›❯─]")
# Reset-time hints as vendors print them (raw text, no arithmetic): codex "try again at 9:05 PM",
# glm "限额将在 2026-08-30 15:39:19 重置", claude-style "resets at 3pm". kimi / deepseek print none.
RESET_HINT = re.compile(r"(?:try\s+again\s+at|resets?\s+(?:at|in))\s+([0-9]{1,2}:[0-9]{2}\s*(?:[AP]M)?|[0-9]+\s*(?:h|m|hours?|min)[^.\n]*)|限额将在\s*([0-9\-]+ [0-9:]+)\s*重置", re.I)


def banner_blocks(lines):
    """Yield each error/notice banner block as one string: claude `●…API Error` + indented continuation,
    or codex `■ …` + unindented continuation up to the next marker/prompt."""
    i = 0
    while i < len(lines):
        if BANNER_START.match(lines[i]):
            block = [lines[i]]
            i += 1
            while i < len(lines) and not BANNER_STOP.match(lines[i]):
                block.append(lines[i])
                i += 1
            yield "\n".join(block)
        elif CODEX_BANNER_START.match(lines[i]):
            block = [lines[i]]
            i += 1
            while i < len(lines) and not CODEX_BANNER_STOP.match(lines[i]) and not CTX_LEFT.search(lines[i]):
                block.append(lines[i])
                i += 1
            yield "\n".join(block)
        else:
            i += 1


def reset_hint(banners):
    for b in banners:
        m = RESET_HINT.search(b)
        if m:
            return (m.group(1) or m.group(2)).strip()
    return None


def pane_for(role):
    """THE role -> (fleet, pane_id) resolver for Python. Mirror of Get-PaneTarget in
    ipc_wezterm_common.ps1 - two registries by design (wezterm_panes.json = fleet1,
    wezterm_fleet2.json = X); change both readers together (2026-08-30 #1262)."""
    role = role.upper()
    if role == "X":
        with open(FLEET2_MAP, encoding="utf-8") as f:
            return "fleet2", int(json.load(f)["pane_id"])
    with open(PANE_MAP, encoding="utf-8") as f:
        m = json.load(f)
    if role not in m:
        raise KeyError(f"role {role} not in {PANE_MAP}")
    return "fleet1", int(m[role]["pane_id"])


def _override(fleet):
    """Temporary socket detour (2026-08-30 #1235): {sock, pane_offset, expect_pane_ids}. None = no detour."""
    try:
        with open(OVERRIDE, encoding="utf-8") as f:
            return json.load(f).get(fleet)
    except (OSError, ValueError):
        return None


def _wez(sock, *args, timeout=25):
    env = dict(os.environ)
    env["WEZTERM_UNIX_SOCKET"] = sock
    r = subprocess.run([WEZTERM, "cli", "--no-auto-start", *args], env=env, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:200])
    return r.stdout


def resolve(fleet, pane_id):
    """(socket, effective pane id). With an override: identity check first (P2: a socket has no
    identity, the pane-id SET is the identity) - mismatch is a hard failure, never a fallback."""
    ov = _override(fleet)
    if not ov:
        return SOCK[fleet], pane_id
    sock = ov["sock"]
    expect = sorted(int(x) for x in ov.get("expect_pane_ids", []))
    got = sorted(int(p["pane_id"]) for p in json.loads(_wez(sock, "list", "--format", "json").decode("utf-8", "replace")))
    if expect and got != expect:
        raise RuntimeError(f"override identity check FAILED on {os.path.basename(sock)}: panes {got} != expected {expect} "
                           f"(a pane was opened/closed; offset {ov.get('pane_offset')} no longer trusted - delete {OVERRIDE} or restart fleet1)")
    return sock, pane_id + int(ov.get("pane_offset", 0))


def pane_text(fleet, pane_id):
    sock, eff = resolve(fleet, pane_id)
    r = subprocess.run([WEZTERM, "cli", "--no-auto-start", "get-text", "--pane-id", str(eff)],
                       env=dict(os.environ, WEZTERM_UNIX_SOCKET=sock), capture_output=True, timeout=25)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:200])
    return r.stdout.decode("utf-8", "replace")


def parse(text):
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    tail = lines[-40:]
    joined = "\n".join(tail)
    ctx, src = None, None
    for l in reversed(tail):                     # status line sits at the bottom
        m = CTX_LEFT.search(l)
        if m:
            ctx, src = int(m.group(1)), "left"
            break
        m = CTX_USED.search(l)
        if m:
            ctx, src = max(0, round(100 - float(m.group(1)))), "used->left"
            break
    flags = []
    if len(RECV_CALL.findall(joined)) >= 2 and len(RECV_NONE.findall(joined)) >= 2:
        flags.append("recv_loop")
    if WORKING.search(joined):
        flags.append("working")
    elif IDLE.search(joined):
        flags.append("idle")
    banners = list(banner_blocks(tail))          # shape gate: only error banners can carry these words
    if PERMISSION.search(joined):
        flags.append("permission_prompt")         # prompts are their own shape (dialog lines), unchanged
    if NOTICE.search(joined):
        flags.append("notice_overlay")            # harness notice / update prompt over the TUI; Enter (Codex update prompt: Esc) dismisses it (2026-10-17)
    if any(QUOTA.search(b) for b in banners):
        flags.append("quota_banner")
    if ERROR.search(joined):
        flags.append("error")
    if any(MOUSE_JUNK.search(l) for l in tail):   # one line of 3+ consecutive reports is already unambiguous
        flags.append("mouse_junk")
    tail_hash = hashlib.sha1("\n".join(lines[-15:]).encode("utf-8", "replace")).hexdigest()[:12]
    parse.last_reset_hint = reset_hint(banners) if "quota_banner" in flags else None
    return ctx, src, flags, tail_hash, (tail[-1][:120] if tail else "")


# Notice overlays seen so far: Claude Code release notices ("We're changing auto mode ..."), the Codex
# update prompt ("enter continue · esc skip" / "Update now"), "Press Enter to continue". The pane
# looks alive (spinner, last command) but the model never gets the next tool result until dismissed.
NOTICE = re.compile(r"We're changing|Press Enter to continue|enter continue|esc skip|Update now \(runs|Skip until next version", re.I)


def verdict(ctx, flags):
    if "mouse_junk" in flags:
        return "DEAD-TUI"          # TUI gone, terminal modes unrestored - nothing dispatchable here
    # A banner is a verdict on its own - a just-launched claude pane has no ctx % yet (2026-08-30 14:33
    # restart: B/D/F showed 403/402/429 banners and were reported UNKNOWN instead of QUOTA).
    if "quota_banner" in flags:
        return "QUOTA"
    if "permission_prompt" in flags:
        return "BLOCKED-PROMPT"
    if "notice_overlay" in flags:
        return "BLOCKED-NOTICE"      # dismiss with Enter (Codex update prompt: Esc), then re-probe
    if ctx is None:
        return "UNKNOWN"
    if ctx <= DEAD_CTX and "recv_loop" in flags:
        return "DEAD-LOOP"
    if ctx <= DEAD_CTX:
        return "DEAD-CTX"
    if "quota_banner" in flags:
        return "QUOTA"
    if "permission_prompt" in flags:
        return "BLOCKED-PROMPT"
    if ctx < WARN_CTX:
        return "LOW-CTX"
    return "OK"


def probe(role):
    out = {"role": role.upper(), "fleet": None, "pane_id": None, "ctx_left": None, "ctx_src": None,
           "flags": [], "verdict": "UNKNOWN", "reset_hint": None, "tail_hash": None, "last_line": "", "error": None}
    try:
        fleet, pid = pane_for(role)
        out["fleet"], out["pane_id"] = fleet, pid
        ctx, src, flags, th, last = parse(pane_text(fleet, pid))
        out.update(ctx_left=ctx, ctx_src=src, flags=flags, tail_hash=th, last_line=last,
                   verdict=verdict(ctx, flags), reset_hint=getattr(parse, "last_reset_hint", None))
    except Exception as exc:              # probe failure is data, never a crash
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def human(p):
    ctx = "?" if p["ctx_left"] is None else f'{p["ctx_left"]}%'
    fl = ",".join(p["flags"]) or "-"
    err = f'  err={p["error"]}' if p["error"] else ""
    rh = f'  reset={p["reset_hint"]}' if p.get("reset_hint") else ""
    return f'{p["role"]:<2} pane={p["pane_id"]} ctx_left={ctx:<4} verdict={p["verdict"]:<14} flags={fl}{rh}{err}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roles", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    roles = a.roles
    if a.all or not roles:
        with open(PANE_MAP, encoding="utf-8") as f:
            roles = sorted(json.load(f).keys())
    for r in roles:
        p = probe(r)
        print(json.dumps(p, ensure_ascii=False) if a.json else human(p), flush=True)


if __name__ == "__main__":
    main()
