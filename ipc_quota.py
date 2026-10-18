# -*- coding: utf-8 -*-
"""Pre-dispatch quota snapshot for the IPC fleet (read-only, stdlib only).

Reads, per vendor, what can be read without a TUI:
  anthropic  GET /api/oauth/usage with the OAuth token Claude Code holds (never refreshed here)
  codex      the newest ~/.codex/sessions rollout event carrying rate_limits (limit_id "codex";
             C and E share these windows: a task to one spends the other's remainder)
  deepseek   GET /user/balance (pay-per-token: a balance, not a window)
  kimi, glm  subscription windows (Kimi Code, Zhipu Coding Plan); no usage API: only the pane
             banner after a hit (FLEET-TRIAGE T6), so they stay UNKNOWN here
Verdict per role: OK / THROTTLE (only pieces that fit the remainder) / HOLD (nothing but an
approved exception) / UNKNOWN. Thresholds differ per window: a 7d window at 80% is far worse
than a 5h one. A's 5h HOLD is lower because the user's own session shares that window.
Usage:
  python ~/.claude/ipc/ipc_quota.py [--json] [--roles A,D,F] [--strict] [--log]
  --strict  exit 2 when any listed role is not OK (dispatch-gate use); UNKNOWN is not OK
  --log     append the JSON snapshot to ~/.claude/ipc/quota_snapshots.jsonl (before/after a
            round: the utilization delta is the only way to learn a task's cost in %)
Reviewed by DeepSeek 2026-10-17 (regex JSON, local(), balance currency, X/Fable, thresholds).
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
HOME = os.path.expanduser("~")
ROLES = {"A": "anthropic", "X": "anthropic", "B": "kimi", "C": "codex", "E": "codex",
         "D": "deepseek", "F": "glm"}
CACHE = os.path.join(HOME, ".claude", "cache", "ipc_quota_last.json")
LOG = os.path.join(HOME, ".claude", "ipc", "quota_snapshots.jsonl")
# (throttle, hold) percent used, by window label
TH = {"5h": (60.0, 80.0), "7d": (70.0, 85.0)}
TH_A = {"5h": (40.0, 60.0), "7d": (70.0, 85.0)}   # hub shares the 5h window with the user's session


def local(ts):
    """Epoch seconds/ms or ISO string -> 'MM-DD HH:MM' in the machine's zone; '-' on anything odd."""
    try:
        if ts in (None, "", 0):
            return "-"
        if isinstance(ts, str):
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
        else:
            v = float(ts)
            if v > 1e11:
                v /= 1000.0
            t = dt.datetime.fromtimestamp(v, dt.timezone.utc)
        return t.astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return "?"


def anthropic():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    try:
        cred = json.load(open(os.path.join(cfg, ".credentials.json"), encoding="utf-8")).get("claudeAiOauth", {})
    except (OSError, ValueError):
        return {"error": "no credentials"}
    tok = cred.get("accessToken")
    if not tok or (cred.get("expiresAt") and cred["expiresAt"] <= dt.datetime.now().timestamp() * 1000):
        return {"error": "token expired; Claude Code renews it"}
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    except Exception as e:  # network / 401 / 429
        return {"error": str(e)[:80]}
    out = {"windows": [], "missing": []}
    for k, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = d.get(k) or {}
        if w.get("utilization") is None:
            out["missing"].append(k)
        out["windows"].append({"name": label, "used": w.get("utilization"), "resets": local(w.get("resets_at"))})
    for lim in d.get("limits") or []:
        if lim.get("kind") == "weekly_scoped":
            name = (lim.get("scope") or {}).get("model", {}).get("display_name") or "model"
            if lim.get("percent") is None:
                out["missing"].append(f"weekly_scoped:{name}")
            out["windows"].append({"name": f"{name} 7d", "used": lim.get("percent"), "resets": local(lim.get("resets_at")),
                                   "model": name})
    eu = d.get("extra_usage") or {}
    out["extra_usage"] = bool(eu.get("is_enabled"))
    return out


def _find_codex_limits(obj):
    """Depth-first search for a rate_limits dict with limit_id == 'codex'."""
    if isinstance(obj, dict):
        if obj.get("limit_id") == "codex" and ("primary" in obj or "secondary" in obj):
            return obj
        for v in obj.values():
            r = _find_codex_limits(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_codex_limits(v)
            if r:
                return r
    return None


def codex():
    files = sorted(glob.glob(os.path.join(HOME, ".codex", "sessions", "*", "*", "*", "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    for f in files:
        last = None
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    rl = _find_codex_limits(ev)
                    if rl:
                        last = (rl, ev.get("timestamp"))
        except OSError:
            continue
        if last:
            rl, ts = last
            w = []
            for key in ("primary", "secondary"):
                p = rl.get(key) or {}
                mins = p.get("window_minutes")
                label = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m" if mins else key
                w.append({"name": label, "used": p.get("used_percent"), "resets": local(p.get("resets_at"))})
            return {"windows": w, "plan": rl.get("plan_type"), "as_of": local(ts), "source": os.path.basename(f),
                    "shared": "C+E"}
    return {"error": "no codex rate_limits event in ~/.codex/sessions"}


def deepseek():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return {"error": "DEEPSEEK_API_KEY not set"}
    req = urllib.request.Request("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)[:80]}
    bal = {}
    for b in d.get("balance_infos", []):
        try:
            bal[b["currency"]] = float(b["total_balance"])
        except (KeyError, ValueError):
            pass
    now_cn = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    peak = now_cn.weekday() < 5 and (9 <= now_cn.hour < 12 or 14 <= now_cn.hour < 18)
    return {"available": d.get("is_available"), "balance": bal, "peak": peak}


def verdict(role, vendor, info, a):
    if "error" in info:
        return "UNKNOWN", info["error"]
    if vendor == "deepseek":
        usd = info["balance"].get("USD", 0.0)
        cny = info["balance"].get("CNY", 0.0)
        money = usd + cny / 7.0
        tag = "peak x2" if info.get("peak") else "off-peak"
        desc = f"balance ${usd:.2f}" + (f" + CNY {cny:.2f}" if cny else "") + f" ({tag})"
        if not info.get("available") or money < a.min_balance:
            return "HOLD", desc
        if money < a.review_only_balance:
            return "THROTTLE", desc + " review-only floor"
        return "OK", desc
    if "windows" in info:
        th = TH_A if role == "A" else TH
        wins = info["windows"]
        if role == "X":   # fleet 2 runs Fable: its own weekly window is the primary signal
            wins = [w for w in wins if w.get("model") == "Fable"] or wins
        elif role == "A":
            wins = [w for w in wins if not w.get("model")]
        worst = "OK"
        parts = []
        for w in wins:
            if w.get("used") is None:
                parts.append(f"{w['name']}=missing")
                continue
            key = "7d" if "7d" in w["name"] else "5h"
            thr, hold = th.get(key, TH["5h"])
            lvl = "HOLD" if w["used"] >= hold else "THROTTLE" if w["used"] >= thr else "OK"
            if ("OK", "THROTTLE", "HOLD").index(lvl) > ("OK", "THROTTLE", "HOLD").index(worst):
                worst = lvl
            parts.append(f"{w['name']}={w['used']}%@{w['resets']}")
        if info.get("missing"):
            parts.append("missing:" + ",".join(info["missing"]))
        desc = " ".join(parts) + (f" [shared {info['shared']}]" if info.get("shared") else "")
        return worst, desc
    return "UNKNOWN", info.get("note", "")


def main():
    ap = argparse.ArgumentParser(description="IPC fleet quota snapshot (read-only)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--roles", default=",".join(ROLES), help="comma list, default all")
    ap.add_argument("--strict", action="store_true", help="exit 2 unless every listed role is OK")
    ap.add_argument("--log", action="store_true", help=f"append the snapshot to {LOG}")
    ap.add_argument("--min-balance", type=float, default=5.0, help="USD-equivalent below which DeepSeek is HOLD")
    ap.add_argument("--review-only-balance", type=float, default=8.0, help="below this D takes short pieces only")
    a = ap.parse_args()
    roles = [r.strip().upper() for r in a.roles.split(",") if r.strip()]

    v = {"anthropic": anthropic(), "codex": codex(), "deepseek": deepseek(),
         "kimi": {"note": "Kimi Code subscription: 5h window (403 seen 2026-08-30); no usage API, pane banner only"},
         "glm": {"note": "Zhipu Coding Plan (user 2026-10-17): 5h window, 429 code 1308 at the cap; no usage API, pane banner only"}}

    # stale fallback for anthropic: keep the last good snapshot, show it marked stale
    prev = {}
    try:
        prev = json.load(open(CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if "error" in v["anthropic"] and prev.get("anthropic"):
        v["anthropic"] = dict(prev["anthropic"], stale=prev.get("at"), error_now=v["anthropic"]["error"])
        del v["anthropic"]["error_now"]
    else:
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump({"anthropic": v["anthropic"], "at": dt.datetime.now().isoformat(timespec="minutes")},
                      open(CACHE, "w", encoding="utf-8"))
        except OSError:
            pass

    rows = []
    for role in roles:
        vendor = ROLES.get(role)
        if not vendor:
            rows.append({"role": role, "vendor": "?", "verdict": "UNKNOWN", "detail": "unmapped role"})
            continue
        s, d = verdict(role, vendor, v[vendor], a)
        if v[vendor].get("stale"):
            d += f" (stale {v[vendor]['stale']})"
        rows.append({"role": role, "vendor": vendor, "verdict": s, "detail": d})

    snap = {"at": dt.datetime.now().isoformat(timespec="seconds"), "roles": rows, "vendors": v}
    if a.log:
        try:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"log write failed: {e}", file=sys.stderr)

    if a.json:
        print(json.dumps(snap, ensure_ascii=False))
    else:
        for r in rows:
            print(f"{r['role']}  {r['vendor']:9s} {r['verdict']:8s} {r['detail']}")
        ant = v["anthropic"]
        if "extra_usage" in ant:
            print(f"-- anthropic extra usage: {'on' if ant['extra_usage'] else 'OFF (a hit stops A dead)'}; A thresholds 5h {TH_A['5h']} 7d {TH_A['7d']}, others 5h {TH['5h']} 7d {TH['7d']} (throttle, hold)")
        cx = v["codex"]
        if "as_of" in cx:
            print(f"-- codex snapshot as of {cx['as_of']} from {cx['source']} (plan {cx.get('plan')}); stale if no Codex turn ran since")
    if a.strict and any(r["verdict"] != "OK" for r in rows):
        sys.exit(2)


if __name__ == "__main__":
    main()
