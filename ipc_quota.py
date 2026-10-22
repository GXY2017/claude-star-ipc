# -*- coding: utf-8 -*-
"""Pre-dispatch quota snapshot for the IPC fleet (read-only, stdlib only).

Reads, per vendor, what can be read without a TUI:
  anthropic  GET /api/oauth/usage with the OAuth token Claude Code holds (never refreshed here)
  codex      the newest ~/.codex/sessions rollout event carrying rate_limits (limit_id "codex";
             C and E share these windows: a task to one spends the other's remainder)
  deepseek   GET /user/balance (pay-per-token: a balance, not a window)
  kimi       GET https://api.kimi.com/coding/v1/usages, Bearer $KIMI_CODE_API_KEY: usages.limit_5h /
             limit_7d used_ratio + reset_time; older count fields (limits[] 300-minute window,
             usage = weekly) as fallback. Endpoint from CodexBar docs/kimi.md, live 200 2026-10-21
  glm        GET https://open.bigmodel.cn/api/monitor/usage/quota/limit, Authorization $GLM_API_KEY
             (no Bearer needed, accepted either way): data.limits[] percentage + nextResetTime (ms);
             unit 3 x 5 = 5h, unit 6 x 1 = weekly (opencode-glm-quota src/utils/token-limits.ts),
             TIME_LIMIT = monthly MCP calls, skipped. Endpoint from zai-org/zai-coding-plugins
             glm-plan-usage, live 200 2026-10-21 (this account returns type CREDIT_LIMIT)
             Both are cached 60 s in ~/.claude/cache/ipc_quota_api_<vendor>.json for ipc_state,
             which the hub clock reads every tick; this script's own run always fetches fresh.
  ollama     G's free-period deadline ($RoleFreeUntil in ipc_wezterm_launch.ps1): OK before, HOLD after
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
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipc_state  # noqa: E402  (same dir; role -> rollout mapping, token_count reader)

HOME = os.path.expanduser("~")
ROLES = {"A": "anthropic", "X": "anthropic", "B": "kimi", "C": "codex", "E": "codex",
         "D": "deepseek", "F": "glm", "G": "ollama"}
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


def codex():
    """C and E share one window, so the freshest rate_limits across their own rollouts wins
    (ipc_state.codex_rollout maps role -> registered thread; 2026-10-20). The newest rollouts
    overall are the fallback for a thread that never registered. Per-role context % rides along."""
    cands = {ipc_state.codex_rollout(r) for r in ("C", "E")} - {None}
    cands |= set(sorted(glob.glob(ipc_state.ROLLOUTS), key=os.path.getmtime, reverse=True)[:3])
    best = None
    for f in cands:
        tc = ipc_state.codex_token_count(f)
        if tc and tc.get("rate_limits") and (best is None or (tc["rate_limits_ts"] or "") > (best[0]["rate_limits_ts"] or "")):
            best = (tc, f)
    ctx = {r: ipc_state.snapshot(r)["ctx_used_pct"] for r in ("C", "E")}
    if not best:
        return {"error": "no codex rate_limits event in ~/.codex/sessions", "ctx": ctx}
    tc, f = best
    rl = tc["rate_limits"]
    w = []
    for key in ("primary", "secondary"):
        p = rl.get(key) or {}
        mins = p.get("window_minutes")
        label = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m" if mins else key
        w.append({"name": label, "used": p.get("used_percent"), "resets": local(p.get("resets_at"))})
    return {"windows": w, "plan": rl.get("plan_type"), "as_of": local(tc["rate_limits_ts"]),
            "source": os.path.basename(f), "shared": "C+E", "ctx": ctx}


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


def _epoch(ts):
    """ISO string or epoch s/ms -> epoch seconds; None on anything odd."""
    try:
        if isinstance(ts, str):
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()
        v = float(ts)
        return v / 1000.0 if v > 1e11 else v
    except (TypeError, ValueError, OverflowError):
        return None


def _window(name, used, reset):
    at = _epoch(reset)
    return {"name": name, "used": round(float(used), 1), "resets_at": at, "resets": local(at)}


def parse_kimi(d):
    """Kimi Code /coding/v1/usages body -> windows. Ratio pools first; count fields fill a
    window the ratios do not carry (older response shape)."""
    out = {}
    for key, name in (("limit_5h", "5h"), ("limit_7d", "7d")):
        p = ((d.get("usages") or {}).get(key)) or {}
        if p.get("used_ratio") is not None:
            out[name] = _window(name, 100.0 * float(p["used_ratio"]), p.get("reset_time"))
    counts = [("5h", lim.get("detail") or {}) for lim in d.get("limits") or []
              if (lim.get("window") or {}).get("duration") == 300
              and (lim.get("window") or {}).get("timeUnit") == "TIME_UNIT_MINUTE"]
    counts.append(("7d", d.get("usage") or {}))
    for name, c in counts:
        try:
            if name not in out and float(c["limit"]) > 0:
                out[name] = _window(name, 100.0 * float(c["used"]) / float(c["limit"]), c.get("resetTime"))
        except (KeyError, TypeError, ValueError):
            pass
    return [out[k] for k in ("5h", "7d") if k in out]


GLM_WINDOWS = {(3, 5): "5h", (6, 1): "7d"}   # (unit, number); opencode-glm-quota token-limits.ts


def parse_glm(d):
    """Zhipu /api/monitor/usage/quota/limit body -> windows; TIME_LIMIT (monthly MCP calls) skipped."""
    out = []
    for lim in ((d.get("data") or {}).get("limits")) or []:
        if lim.get("type") == "TIME_LIMIT" or lim.get("percentage") is None:
            continue
        name = GLM_WINDOWS.get((lim.get("unit"), lim.get("number")), f"u{lim.get('unit')}x{lim.get('number')}")
        out.append(_window(name, lim["percentage"], lim.get("nextResetTime")))
    return out


def _get_json(url, headers, timeout=10):
    req = urllib.request.Request(url, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def kimi():
    key = os.environ.get("KIMI_CODE_API_KEY")
    if not key:
        return {"error": "KIMI_CODE_API_KEY not set"}
    try:
        w = parse_kimi(_get_json("https://api.kimi.com/coding/v1/usages", {"Authorization": f"Bearer {key}"}))
    except Exception as e:  # network / 401 / 429 / shape
        return {"error": str(e)[:80]}
    return {"windows": w} if w else {"error": "no window in the usages response"}


def glm():
    key = os.environ.get("GLM_API_KEY")
    if not key:
        return {"error": "GLM_API_KEY not set"}
    try:
        d = _get_json("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                      {"Authorization": key, "Accept-Language": "en-US,en"})
    except Exception as e:
        return {"error": str(e)[:80]}
    if not d.get("success", True) or d.get("code") not in (None, 200):
        return {"error": f"code {d.get('code')}: {str(d.get('msg'))[:60]}"}
    w = parse_glm(d)
    return {"windows": w, "plan": (d.get("data") or {}).get("level")} if w else {"error": "no window in the quota response"}


API_VENDORS = {"kimi": kimi, "glm": glm}
STATE_VENDOR = {"kimi-code": "kimi", "zhipu-cn": "glm"}   # statusline.js `vendor` -> reader here
API_TTL_S = 60.0


def cached_windows(vendor):
    """Windows for an API vendor, at most API_TTL_S old (errors are cached too, so a dead
    endpoint costs one request a minute). [] on error or an unknown vendor. Never raises:
    ipc_state.snapshot calls it from the hub clock."""
    fn = API_VENDORS.get(vendor)
    if not fn:
        return []
    path = os.path.join(HOME, ".claude", "cache", f"ipc_quota_api_{vendor}.json")
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if 0 <= time.time() - float(c["at"]) < API_TTL_S:
            return c.get("windows") or []
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        info = fn()
    except Exception as e:  # noqa: BLE001
        info = {"error": str(e)[:80]}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "windows": info.get("windows") or [], "error": info.get("error")}, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return info.get("windows") or []


def ollama():
    """G's free-period end, parsed from $RoleFreeUntil in the launcher (the one copy)."""
    try:
        with open(os.path.join(HOME, ".claude", "ipc", "ipc_wezterm_launch.ps1"), encoding="utf-8") as f:
            m = re.search(r"^\s*G\s*=\s*'([^']+)'", re.search(r"\$RoleFreeUntil\s*=\s*@\{(.*?)\n\}", f.read(), re.S).group(1), re.M)
        return {"free_until": m.group(1)}
    except (OSError, AttributeError) as e:
        return {"error": f"no G deadline in launcher: {e}"[:80]}


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
    if vendor == "ollama":
        end = info["free_until"]
        if dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(end.replace("Z", "+00:00")):
            return "OK", f"Ollama Cloud free until {local(end)}"
        return "HOLD", f"free period ended {local(end)}, paid per token now: ipc_pane_exit.ps1 -Role G"
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
    sys.stdout.reconfigure(encoding="utf-8")   # here, not at import: ipc_state imports this module from the hub clock
    roles = [r.strip().upper() for r in a.roles.split(",") if r.strip()]

    v = {"anthropic": anthropic(), "codex": codex(), "deepseek": deepseek(),
         "kimi": kimi(), "glm": glm(), "ollama": ollama()}

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
        if cx.get("ctx"):
            print("-- codex context used: " + " ".join(f"{r}={'?' if v is None else f'{v:.0f}%'}" for r, v in cx["ctx"].items()))
    if a.strict and any(r["verdict"] != "OK" for r in rows):
        sys.exit(2)


if __name__ == "__main__":
    main()
