# -*- coding: utf-8 -*-
"""List panes on each wezterm socket with a hard timeout and --no-auto-start."""
import json, os, subprocess, sys
sys.stdout.reconfigure(encoding='utf-8')
W = r"C:\Program Files\WezTerm\wezterm.exe"
D = os.path.join(os.path.expanduser("~"), ".local", "share", "wezterm")
def _default_socks():
    """fleet sockets + the two pinned GUI sockets (pids from the launcher state files)."""
    ipc = os.path.join(os.path.expanduser("~"), ".claude", "ipc")
    out = ["fleet1.sock", "fleet2.sock"]
    try:
        out.append("gui-sock-" + open(os.path.join(ipc, "wezterm_gui_pid.txt"), encoding="utf-8").read().strip())
    except OSError:
        pass
    try:
        out.append("gui-sock-%s" % json.load(open(os.path.join(ipc, "wezterm_fleet2.json"), encoding="utf-8"))["gui_pid"])
    except (OSError, KeyError, ValueError):
        pass
    return out


socks = sys.argv[1:] or _default_socks()
for s in socks:
    env = dict(os.environ, WEZTERM_UNIX_SOCKET=os.path.join(D, s))
    print(f"=== {s}")
    try:
        r = subprocess.run([W, "cli", "--no-auto-start", "list", "--format", "json"],
                           env=env, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("TIMEOUT 15s"); continue
    if r.returncode != 0:
        print("ERR:", r.stderr.decode("utf-8", "replace").strip()[:300]); continue
    try:
        panes = json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception as e:
        print("BAD JSON:", e, r.stdout[:200]); continue
    if not panes:
        print("(0 panes)")
    for p in panes:
        print(f"pane={p['pane_id']} win={p['window_id']} tab={p['tab_id']} "
              f"title={p.get('title','')[:50]!r} cwd=...{p.get('cwd','')[-35:]}")
