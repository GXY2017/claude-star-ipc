# -*- coding: utf-8 -*-
"""Read-only tail of a pane's screen via a given socket, hard timeout, no auto-start.
usage: get_text.py <sock-file-name> <pane_id> [tail_lines]"""
import os, subprocess, sys
sys.stdout.reconfigure(encoding='utf-8')
W = r"C:\Program Files\WezTerm\wezterm.exe"
D = os.path.join(os.path.expanduser("~"), ".local", "share", "wezterm")
sock, pane = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 25
env = dict(os.environ, WEZTERM_UNIX_SOCKET=os.path.join(D, sock))
try:
    r = subprocess.run([W, "cli", "--no-auto-start", "get-text", "--pane-id", pane],
                       env=env, capture_output=True, timeout=15)
except subprocess.TimeoutExpired:
    print("TIMEOUT"); sys.exit(0)
if r.returncode != 0:
    print("ERR:", r.stderr.decode("utf-8", "replace").strip()[:300]); sys.exit(0)
lines = [l.rstrip() for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
print(f"--- {sock} pane {pane}: {len(lines)} non-empty lines, last {n}:")
print("\n".join(lines[-n:]))
