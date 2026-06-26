#!/usr/bin/env python3
"""Install the star-topology IPC kit (ipc.py + role hook + protocol) into another
Claude Code project.

Usage:
    python install_ipc.py <target-project-dir>

What it does (idempotent — safe to re-run):
  1. copies ipc.py            -> <target>/ipc.py
  2. copies ipc_role.py       -> <target>/.claude/hooks/ipc_role.py
  3. merges the SessionStart(claim)/SessionEnd(release) hooks into
     <target>/.claude/settings.local.json (preserving everything already there)
  4. appends the IPC protocol section to <target>/CLAUDE.md (creates it if absent)

What it deliberately does NOT copy (per-project runtime state — copying it would
cross-wire two projects' mailboxes): _ipc.db (+ -wal/-shm), _watcher_*.alive,
.claude/ipc_roles.json. Those are recreated fresh on first use in the target.

The protocol text is path-agnostic ("project root = where ipc.py lives"), so there
is nothing to hand-edit after install. Launch every terminal with cwd = target root.
"""
import json
import os
import re
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SRC = os.path.dirname(os.path.abspath(__file__))
IPC_HEADER = "## Multi-Terminal IPC Protocol"  # start of the copyable protocol section

# The two hook entries this kit needs. Identified for idempotency by the unique
# substring in their command, so a re-run won't duplicate them.
HOOK_SPECS = [
    ("SessionStart", "python .claude/hooks/ipc_role.py claim", "ipc_role.py claim"),
    ("SessionEnd", "python .claude/hooks/ipc_role.py release", "ipc_role.py release"),
]


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def _merge_hooks(settings_path):
    """Add the claim/release hooks to settings.local.json without clobbering."""
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            print(f"  ! {settings_path} is not valid JSON — leaving it untouched. "
                  f"Add the hooks manually.")
            return False
    else:
        cfg = {}
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("  ! settings 'hooks' is not an object — leaving untouched. Add manually.")
        return False
    added = []
    for event, command, marker in HOOK_SPECS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):  # user set this event to null/dict: don't crash
            print(f"  ! settings hooks.{event} is not a list — skipped. Add manually.")
            continue
        # already present? (scan every command string under this event)
        present = any(
            marker in h.get("command", "")
            for g in groups for h in g.get("hooks", [])
        )
        if present:
            continue
        groups.append({"hooks": [{"type": "command", "command": command}]})
        added.append(event)
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, settings_path)
    return added


def _protocol_section():
    """Extract the IPC protocol section from the source CLAUDE.md: from its header
    up to the next level-1 (`# `) heading, or EOF if none follows."""
    with open(os.path.join(SRC, "CLAUDE.md"), encoding="utf-8") as f:
        text = f.read()
    i = text.find(IPC_HEADER)
    if i < 0:
        raise SystemExit("! source CLAUDE.md has no IPC section header; aborting.")
    rest = text[i:]
    nxt = re.search(r"\n# [^\n]", rest)  # next top-level section ends the IPC block
    if nxt:
        rest = rest[:nxt.start()]
    return rest.rstrip() + "\n"


def _append_protocol(claude_md_path):
    section = _protocol_section()
    if os.path.exists(claude_md_path):
        with open(claude_md_path, encoding="utf-8") as f:
            existing = f.read()
        if IPC_HEADER in existing:
            return "skip"  # already documented (match the section header only)
        with open(claude_md_path, "a", encoding="utf-8") as f:
            f.write("\n" + section)
        return "append"
    with open(claude_md_path, "w", encoding="utf-8") as f:
        f.write("# Project protocol\n\n" + section)
    return "create"


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python install_ipc.py <target-project-dir>")
    target = os.path.abspath(sys.argv[1])
    if not os.path.isdir(target):
        raise SystemExit(f"! target is not a directory: {target}")
    if os.path.abspath(target) == SRC:
        raise SystemExit("! target is the source project itself; nothing to do.")

    print(f"Installing IPC kit  {SRC}  ->  {target}")

    _copy(os.path.join(SRC, "ipc.py"), os.path.join(target, "ipc.py"))
    print("  + ipc.py")

    _copy(os.path.join(SRC, ".claude", "hooks", "ipc_role.py"),
          os.path.join(target, ".claude", "hooks", "ipc_role.py"))
    print("  + .claude/hooks/ipc_role.py")

    added = _merge_hooks(os.path.join(target, ".claude", "settings.local.json"))
    if added is False:
        pass
    elif added:
        print(f"  + hooks merged into settings.local.json: {', '.join(added)}")
    else:
        print("  = hooks already present in settings.local.json (unchanged)")

    how = _append_protocol(os.path.join(target, "CLAUDE.md"))
    print({"create": "  + CLAUDE.md created with protocol section",
           "append": "  + protocol section appended to CLAUDE.md",
           "skip": "  = CLAUDE.md already documents IPC (unchanged)"}[how])

    print("\nDone. Runtime state (_ipc.db / _watcher_*.alive / ipc_roles.json) was "
          "NOT copied — it is created fresh on first use.")
    print("Next: launch each terminal with cwd = target root; the first becomes A, "
          "the rest B/C/D. Have every worker say one line so it parks its watcher.")


if __name__ == "__main__":
    main()
