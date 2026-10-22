# -*- coding: utf-8 -*-
"""Per-role machine state for the IPC fleet, read from files instead of the pane screen.

Why (2026-10-20): ipc_pane_probe.py matched five status-line formats by regex and broke
twice on 10-18 when a format changed. The same numbers exist as data:
  claude roles (A, B, D, F, X): ~/.claude/ipc/state/<ROLE>.json, written by
      ~/.claude/hooks/statusline.js on every status-line run when IPC_ROLE is set in the
      pane (settings.json statusLine.refreshInterval keeps it ticking while idle).
      Fields: ts (ms), pid, session_id, transcript_path, cwd, model, vendor,
      ctx {used_pct, size}, rate_limits {five_hour/seven_day: used, resets_at}
      (Anthropic subscription only), prompt_cache {warm, expires_at, ttl}, cost_usd.
      Kimi Code and Zhipu Coding Plan roles (vendor kimi-code / zhipu-cn) get their windows
      from the vendor usage API instead (ipc_quota.cached_windows, 2026-10-21).
  codex roles (C, E): the session's rollout ~/.codex/sessions/Y/M/D/rollout-*-<sid>.jsonl,
      found through the role registry (codex-compat.js registers the thread id as the
      session_id; claude roles register "manual", so they are keyed by IPC_ROLE instead).
      Its last token_count event carries last_token_usage / model_context_window and
      rate_limits (limit_id "codex", shared by C and E).
Activity = mtime of the transcript / rollout: it moves only when a turn completes or a
tool returns. The pane tail hash moves on spinners and retry countdowns too, so "claimed
task, file quiet for N minutes" is the stuck signal (hub clock, ipc.py _clock_task).

Usage:  python ~/.claude/ipc/ipc_state.py A E          # one JSON object per role
        python ~/.claude/ipc/ipc_state.py --all        # every role in the pane map
Exit 0 always; a role without data reports source null.
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "ipc", "state")
STATE_FRESH_S = 120            # statusline refreshInterval 30 s; older than 4 ticks = pane or hook gone
PANE_MAP = os.path.join(HOME, ".claude", "ipc", "wezterm_panes.json")
REGISTRIES = os.path.join(HOME, ".claude", "projects", "*", "ipc", "ipc_roles.json")
ROLLOUTS = os.path.join(HOME, ".codex", "sessions", "*", "*", "*", "rollout-*.jsonl")
CODEX_ROLES = {"C", "E", "CODEX"}   # keep in sync with $CodexRoles in ipc_wezterm_launch.ps1
TAIL_BYTES = 1 << 20           # a rollout grows to tens of MB; token_count events sit near the end


def read_state(role):
    """statusline.js state for a claude role, with age_s added; None when missing/unreadable."""
    p = os.path.join(STATE_DIR, f"{role.upper()}.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        ts = float(d.get("ts") or 0) / 1000.0
        d["age_s"] = round(time.time() - ts, 1) if ts else None
        d["path"] = p
        return d
    except (OSError, ValueError):
        return None


def registry_session(role):
    """Newest real session_id registered for `role` across every project registry
    ("manual"/"unknown" placeholders skipped). None when no project has one."""
    best = None
    for reg in glob.glob(REGISTRIES):
        try:
            with open(reg, encoding="utf-8") as f:
                entry = (json.load(f) or {}).get(role.upper())
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        sid = entry.get("session_id")
        if not sid or sid in ("manual", "unknown"):
            continue
        ts = entry.get("ts") or ""
        if best is None or ts > best[0]:
            best = (ts, sid)
    return best[1] if best else None


def _session_meta_ids(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = json.loads(f.readline())
        pl = first.get("payload") or {}
        return {pl.get("id"), pl.get("session_id")} - {None}
    except (OSError, ValueError):
        return set()


def codex_rollout(role):
    """Rollout file of the codex role's registered thread: filename match first, else the
    newest 60 rollouts' session_meta (a forked sub-thread names the parent in session_id).
    Newest mtime wins when several match. None when the role has no registered thread."""
    sid = registry_session(role)
    if not sid:
        return None
    files = sorted(glob.glob(ROLLOUTS), key=os.path.getmtime, reverse=True)
    hits = [f for f in files if sid in os.path.basename(f)]
    if not hits:
        hits = [f for f in files[:60] if sid in _session_meta_ids(f)]
    return hits[0] if hits else None


def codex_token_count(path):
    """Last token_count event of a rollout -> {ts, ctx_used_pct, ctx_window, last_total,
    rate_limits}; None when the tail holds none."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    out = None
    for line in reversed(tail.splitlines()):
        if '"token_count"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        pl = ev.get("payload") or {}
        if pl.get("type") != "token_count":
            continue
        rl = pl.get("rate_limits") or {}
        has_rl = any((rl.get(k) or {}).get("used_percent") is not None for k in ("primary", "secondary"))
        if out is None:
            info = pl.get("info") or {}
            last = info.get("last_token_usage") or {}
            window = info.get("model_context_window")
            total = last.get("total_tokens")
            pct = round(100.0 * total / window, 1) if window and total is not None else None
            out = {"ts": ev.get("timestamp"), "ctx_used_pct": pct, "ctx_window": window,
                   "last_total": total, "rate_limits": rl if has_rl else None, "rate_limits_ts": None}
            if has_rl:
                out["rate_limits_ts"] = ev.get("timestamp")
                return out
            continue
        if has_rl:                      # the newest event may carry empty limits: walk back to one that does
            out["rate_limits"], out["rate_limits_ts"] = rl, ev.get("timestamp")
            return out
    return out


def activity_path(role):
    """File whose mtime tracks the role's progress: claude transcript or codex rollout."""
    role = role.upper()
    if role in CODEX_ROLES:
        return codex_rollout(role)
    st = read_state(role)
    return (st or {}).get("transcript_path")


def activity_age(role):
    """(seconds since the role's transcript/rollout last grew, path); (None, None) if unknown."""
    p = activity_path(role)
    if not p:
        return None, None
    try:
        return round(time.time() - os.path.getmtime(p), 1), p
    except OSError:
        return None, p


def _local(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _api_quota(vendor):
    """Windows from a vendor usage API (Kimi Code, Zhipu Coding Plan; 2026-10-21) for a claude
    role whose statusline state names that vendor; [] otherwise. ipc_quota caches each answer
    60 s, so the hub clock's per-tick reads cost one request a minute per vendor."""
    try:
        import ipc_quota  # noqa: WPS433 — lazy: ipc_quota imports this module at load
        v = ipc_quota.STATE_VENDOR.get(vendor)
        return ipc_quota.cached_windows(v) if v else []
    except Exception:  # noqa: BLE001 — optional signal
        return []


def snapshot(role):
    """Unified per-role view for probe/quota/clock. ctx_* None when no fresh file exists."""
    role = role.upper()
    out = {"role": role, "kind": "codex" if role in CODEX_ROLES else "claude", "source": None,
           "state_age_s": None, "ctx_used_pct": None, "ctx_left": None, "quota": [],
           "activity_age_s": None, "activity_path": None}
    if out["kind"] == "codex":
        rp = codex_rollout(role)
        tc = codex_token_count(rp) if rp else None
        if tc:
            out["source"] = rp
            out["ctx_used_pct"] = tc["ctx_used_pct"]
            rl = tc.get("rate_limits") or {}
            for key in ("primary", "secondary"):
                w = rl.get(key) or {}
                mins = w.get("window_minutes")
                name = "5h" if mins == 300 else "7d" if mins == 10080 else (f"{mins}m" if mins else key)
                out["quota"].append({"name": name, "used": w.get("used_percent"),
                                     "resets_at": w.get("resets_at"), "resets": _local(w.get("resets_at"))})
    else:
        st = read_state(role)
        if st:
            out["state_age_s"] = st.get("age_s")
            if st["age_s"] is not None and st["age_s"] <= STATE_FRESH_S:
                out["source"] = st["path"]
                out["ctx_used_pct"] = (st.get("ctx") or {}).get("used_pct")
                for key, name in (("five_hour", "5h"), ("seven_day", "7d")):
                    w = (st.get("rate_limits") or {}).get(key)
                    if w and w.get("used") is not None:
                        out["quota"].append({"name": name, "used": w["used"], "resets_at": w.get("resets_at"),
                                             "resets": _local(w.get("resets_at"))})
                if not out["quota"]:
                    out["quota"] = _api_quota(st.get("vendor"))
                out["prompt_cache"] = st.get("prompt_cache")
                out["model"] = st.get("model")
                out["vendor"] = st.get("vendor")
    if out["ctx_used_pct"] is not None:
        out["ctx_left"] = max(0, round(100 - float(out["ctx_used_pct"])))
    out["activity_age_s"], out["activity_path"] = activity_age(role)
    return out


def main():
    ap = argparse.ArgumentParser(description="IPC per-role state from files (no pane read)")
    ap.add_argument("roles", nargs="*")
    ap.add_argument("--all", action="store_true", help="every role in wezterm_panes.json plus X")
    a = ap.parse_args()
    roles = [r.upper() for r in a.roles]
    if a.all or not roles:
        try:
            with open(PANE_MAP, encoding="utf-8") as f:
                roles = sorted(json.load(f).keys()) + ["X"]
        except (OSError, ValueError):
            roles = ["A", "B", "C", "D", "E", "F", "G", "X"]
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for r in roles:
        print(json.dumps(snapshot(r), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
