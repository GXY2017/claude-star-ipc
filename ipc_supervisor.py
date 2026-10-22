# -*- coding: utf-8 -*-
"""Out-of-session supervisor for the IPC hub clock (2026-10-20, user: "做成独立进程，减少 token 消耗").

Before: the 15/30/45-minute reply clock ticked inside the hub's `watch --me A`, hosted by
the Monitor tool in A's Claude session. Detection was script, but the loop lived in a model
session (Monitor re-arm = model turns) and every clock line cost A a turn to read.
Now: this process runs ipc.clock_tick() itself, every --interval seconds, with no model in
the loop. What it finds goes to two files in the hub project's IPC state dir:
  _supervisor_A.log     every clock line (audit)
  _supervisor_A.events  only lines the hub must act on; `watch --me A` relays them verbatim
                        (ipc.py _relay_supervisor), so A spends a turn per real event, none
                        for the ticking. _supervisor_A.json is the heartbeat; while it is
                        fresh, watch stops running its own clock and `ipc.py clock` refuses.
Mechanical actions taken here, logged, never relayed when they resolve the verdict:
  BLOCKED-NOTICE  -> Enter (claude pane) / Esc (codex pane) into the pane, re-probe.
Everything else (QUOTA, BLOCKED-PROMPT, DEAD-*, idle & unchanged, 45-min reassign) needs a
decision: relayed to A as before.

Usage:
  python ~/.claude/ipc/ipc_supervisor.py start [--hub A] [--interval 60] [--project DIR]
  python ~/.claude/ipc/ipc_supervisor.py stop | status
  python ~/.claude/ipc/ipc_supervisor.py run [--once]      # foreground (start spawns this)
--project defaults to A's project in wezterm_panes.json; it is exported as CLAUDE_PROJECT_DIR
so ipc.py resolves the same state dir as the fleet. Stdlib only. ipc_wezterm_launch.ps1 runs
`start` after the panes are up; a running supervisor is reused (single instance by heartbeat).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOME = os.path.expanduser("~")
PANE_MAP = os.path.join(HOME, ".claude", "ipc", "wezterm_panes.json")
WEZTERM = r"C:\Program Files\WezTerm\wezterm.exe"
CODEX_ROLES = {"C", "E", "CODEX"}          # keep in sync with $CodexRoles in ipc_wezterm_launch.ps1
ACT_ROLE = re.compile(r"#\d+ -> ([A-Z]+) ")
FLEET_DOWN_SLEEP = 300.0


def fleet_project(hub):
    try:
        with open(PANE_MAP, encoding="utf-8") as f:
            m = json.load(f)
        return (m.get(hub) or next(iter(m.values())))["project"]
    except (OSError, ValueError, StopIteration, KeyError, TypeError):
        return None


def pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return str(int(pid)) in out


class Supervisor:
    def __init__(self, ipc, probe, hub, interval):
        self.ipc, self.probe, self.hub, self.interval = ipc, probe, hub, interval
        self.hb = ipc._supervisor_path(hub)
        self.events = ipc._supervisor_events_path(hub)
        self.log = os.path.join(ipc._STATE_DIR, f"_supervisor_{hub}.log")
        self.tick_lines = []
        ipc._ensure_state_dir()

    def _append(self, path, line):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    def logln(self, line):
        self._append(self.log, f"{time.strftime('%m-%d %H:%M:%S')} {line}")

    def emit(self, line):
        """Replaces ipc._emit for the clock: collect per tick, never touch stdout."""
        self.tick_lines.append(line)
        self.logln(line)
        return True

    def beat(self):
        tmp = f"{self.hb}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": time.time(), "interval": self.interval,
                           "hub": self.hub, "project": os.environ.get("CLAUDE_PROJECT_DIR")}, f)
            os.replace(tmp, self.hb)
        except OSError:
            pass

    def dismiss_notice(self, role):
        """Enter (claude) / Esc (codex) into the role's pane, then re-probe. Returns the new verdict."""
        fleet, pane = self.probe.pane_for(role)
        sock, eff = self.probe.resolve(fleet, pane)
        key = "\x1b" if role in CODEX_ROLES else "\r"
        subprocess.run([WEZTERM, "cli", "--no-auto-start", "send-text", "--pane-id", str(eff), "--no-paste", "--", key],
                       env=dict(os.environ, WEZTERM_UNIX_SOCKET=sock), capture_output=True, timeout=25)
        time.sleep(4)
        return self.probe.probe(role)["verdict"]

    def handle(self, line):
        """True when this process resolved the line itself (log only, not relayed)."""
        if "ACT NOW: BLOCKED-NOTICE" not in line:
            return False
        m = ACT_ROLE.search(line)
        if not m:
            return False
        role = m.group(1)
        try:
            verdict = self.dismiss_notice(role)
        except Exception as exc:  # noqa: BLE001 — a failed dismissal is relayed, with the reason
            self.logln(f"SUP AUTO: dismiss notice on {role} FAILED: {type(exc).__name__}: {exc}")
            return False
        self.logln(f"SUP AUTO: dismissed notice on {role}; re-probe verdict={verdict}")
        return not verdict.startswith(("BLOCKED", "DEAD", "QUOTA", "UNKNOWN"))

    def tick(self):
        self.tick_lines = []
        self.ipc.clock_tick(self.hub)
        for line in self.tick_lines:
            if not self.handle(line):
                self._append(self.events, line)
        return len(self.tick_lines)

    def run(self, once=False):
        self.ipc._emit = self.emit
        self.beat()
        self.logln(f"SUP online pid={os.getpid()} hub={self.hub} interval={self.interval}s project={os.environ.get('CLAUDE_PROJECT_DIR')}")
        if not once:
            time.sleep(self.ipc._CLOCK_EVERY + 5)   # handover: the hub watch sees the heartbeat and stops its own clock
        sock = self.probe.SOCK.get("fleet1")
        while True:
            self.beat()
            if sock and not os.path.exists(sock) and not os.path.exists(self.probe.OVERRIDE):
                self.logln("SUP fleet1 socket absent - sleeping")
                if once:
                    return
                time.sleep(FLEET_DOWN_SLEEP)
                continue
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the supervisor never dies on one tick
                self.logln(f"SUP TICK-ERROR {type(exc).__name__}: {exc}")
            if once:
                return
            time.sleep(self.interval)


def read_hb(ipc, hub):
    try:
        with open(ipc._supervisor_path(hub), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="out-of-session IPC hub clock")
    ap.add_argument("cmd", choices=["run", "start", "stop", "status"])
    ap.add_argument("--hub", default="A")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--project", default=None, help="fleet project dir (default: A's in wezterm_panes.json)")
    ap.add_argument("--once", action="store_true", help="run: one tick, then exit")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    project = a.project or fleet_project(a.hub)
    if not project:
        print("ERROR kind=env next=launch the fleet first (wezterm_panes.json has no project)")
        return 2
    os.environ["CLAUDE_PROJECT_DIR"] = project
    import ipc            # noqa: E402  resolves _STATE_DIR from CLAUDE_PROJECT_DIR
    import ipc_pane_probe  # noqa: E402

    hb = read_hb(ipc, a.hub)
    fresh = hb and 0 <= time.time() - float(hb.get("ts", 0)) < 3 * float(hb.get("interval", 60)) + 30

    if a.cmd == "status":
        if not hb:
            print(f"supervisor {a.hub}: no heartbeat ({ipc._supervisor_path(a.hub)})")
            return 1
        alive = pid_alive(hb.get("pid"))
        print(f"supervisor {a.hub}: pid={hb.get('pid')} alive={alive} heartbeat_age={time.time() - float(hb['ts']):.0f}s "
              f"fresh={bool(fresh)} project={hb.get('project')} log={os.path.join(ipc._STATE_DIR, f'_supervisor_{a.hub}.log')}")
        return 0 if (alive and fresh) else 1
    if a.cmd == "stop":
        if hb and pid_alive(hb.get("pid")):
            subprocess.run(["taskkill", "/PID", str(int(hb["pid"])), "/F"], capture_output=True, timeout=15)
            print(f"stopped pid {hb['pid']}")
        else:
            print("not running")
        try:
            os.remove(ipc._supervisor_path(a.hub))
        except OSError:
            pass
        return 0
    if a.cmd == "start":
        if fresh and pid_alive(hb.get("pid")):
            print(f"already running: pid {hb['pid']}")
            return 0
        log = open(os.path.join(ipc._STATE_DIR, f"_supervisor_{a.hub}.stderr"), "a", encoding="utf-8")
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", "--hub", a.hub,
                              "--interval", str(a.interval), "--project", project],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True)
        print(f"started pid {p.pid} (hub {a.hub}, every {a.interval:.0f}s, project {project})")
        return 0
    # run
    if not a.once and fresh and hb.get("pid") != os.getpid() and pid_alive(hb.get("pid")):
        print(f"ERROR kind=env next=stop pid {hb['pid']} first (another supervisor is live)")
        return 1
    Supervisor(ipc, ipc_pane_probe, a.hub, a.interval).run(once=a.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
