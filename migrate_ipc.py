#!/usr/bin/env python3
"""Migrate a project from a per-project IPC install to the user-level install.

File-ALLOWLIST uninstall of the per-project copy so only the user-level machinery
runs — eliminating N-copy drift and the double-claim that happens when a global
hook AND a project-local hook both fire. Never removes whole directories; touches
only the named items below.

IDLE ONLY: refuses if any watcher heartbeat is fresh (terminals still listening),
because deleting the live mailbox out from under active terminals corrupts the run.
Close/clear all terminals in the target first.

Touches, in <target>:
  - .claude/settings.local.json : remove ONLY hook groups whose command contains
    'ipc_role.py claim' / 'ipc_role.py release' (every other key/hook preserved)
  - delete: ipc.py, .claude/hooks/ipc_role.py
  - delete runtime state: _ipc.db (+ -wal/-shm), .claude/ipc_roles.json,
    _watcher_*.alive
  - write .claude/ipc.enabled so the user-level global hook activates here

Usage:
    python migrate_ipc.py <target-project-dir> [--dry-run]
    python migrate_ipc.py .                       # migrate the current project
"""
import glob
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WATCHER_FRESH = 60  # seconds: a heartbeat newer than this => a terminal is live


def _watchers_live(target):
    live = []
    for p in glob.glob(os.path.join(target, "_watcher_*.alive")):
        try:
            if time.time() - os.path.getmtime(p) <= WATCHER_FRESH:
                live.append(os.path.basename(p))
        except OSError:
            pass
    return live


def _strip_hooks(settings_path, dry):
    if not os.path.exists(settings_path):
        return
    try:
        with open(settings_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        print(f"  ! {settings_path} not valid JSON — left untouched. Remove hooks manually.")
        return
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return
    changed = False
    for event in ("SessionStart", "SessionEnd"):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if "ipc_role.py" not in h.get("command", "")]
            if hs:
                kept.append({**g, "hooks": hs})
            if len(hs) != len(g.get("hooks", [])):
                changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
            changed = True
    if changed:
        print(f"  - remove ipc_role hooks from {settings_path}")
        if not dry:
            tmp = settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, settings_path)
    else:
        print(f"  = no ipc_role hooks in {settings_path}")


def _rm(path, dry):
    if os.path.exists(path):
        print(f"  - delete {path}")
        if not dry:
            os.remove(path)


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        raise SystemExit("usage: python migrate_ipc.py <target-project-dir> [--dry-run]")
    target = os.path.abspath(args[0])
    if not os.path.isdir(target):
        raise SystemExit(f"! not a directory: {target}")

    live = _watchers_live(target)
    if live and not dry:
        raise SystemExit(f"! REFUSING: live watcher(s) {live} — close/clear all terminals "
                         f"in {target} first (or re-run with --dry-run to preview).")

    print(f"{'[dry-run] ' if dry else ''}Migrating {target} to user-level IPC")
    _strip_hooks(os.path.join(target, ".claude", "settings.local.json"), dry)
    _rm(os.path.join(target, "ipc.py"), dry)
    _rm(os.path.join(target, ".claude", "hooks", "ipc_role.py"), dry)
    for name in ("_ipc.db", "_ipc.db-wal", "_ipc.db-shm"):
        _rm(os.path.join(target, name), dry)
    _rm(os.path.join(target, ".claude", "ipc_roles.json"), dry)
    for p in glob.glob(os.path.join(target, "_watcher_*.alive")):
        _rm(p, dry)

    marker = os.path.join(target, ".claude", "ipc.enabled")
    print(f"  + write {marker}")
    if not dry:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("opted into user-level IPC; the global hook claims a role here\n")

    print("\nDone." if not dry else "\n[dry-run] no changes made.")
    print("After this, the user-level global hook (install_user.py) drives IPC here; a "
          "fresh per-cwd mailbox is created under ~/.claude/projects/<key>/ipc/ on next use.")


if __name__ == "__main__":
    main()
