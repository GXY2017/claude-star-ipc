#!/usr/bin/env python3
"""Install the IPC kit at USER LEVEL — one copy of the machinery for every project.

Deploys ipc.py + ipc_role.py to ~/.claude/ipc/ and registers the
SessionStart(claim)/SessionEnd(release) hooks ONCE in ~/.claude/settings.json,
pointing at the absolute ipc_role.py path. Idempotent (safe to re-run).

Why this is safe to register globally: a project only PARTICIPATES if it opts in
(a .claude/ipc.enabled marker, IPC_ROLE env, an existing project-local install, or
an existing mailbox — see ipc_role._opted_in). In every other project the hook is
inert: it claims no role and injects nothing. Per-project STATE
(mailbox/registry/heartbeats) is never created here — ipc.py resolves it per-cwd to
~/.claude/projects/<key>/ipc/ at runtime, fully isolated between projects.

Usage:
    python install_user.py               # deploy files + register the global hook
    python install_user.py --deploy-only # copy files only, do NOT touch settings.json
"""
import json
import os
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SRC = os.path.dirname(os.path.abspath(__file__))
DST = os.path.join(os.path.expanduser("~"), ".claude", "ipc")
USER_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
ROLE_DST = os.path.join(DST, "ipc_role.py")
# Slash commands are USER-LEVEL too (2026-07-02): /ipc-recover must be available in
# every opted-in project — a cleared worker recovering in project X can't rely on
# project X having its own copy.
CMD_SRC = os.path.join(SRC, ".claude", "commands")
CMD_DST = os.path.join(os.path.expanduser("~"), ".claude", "commands")
COMMANDS = ("ipc-recover.md", "main.md")
# The operating skill ships with the kit (2026-07-02): onboarding + operations
# reference that stays version-locked to the code instead of drifting on one machine.
SKILL_SRC = os.path.join(SRC, "skills", "multi-terminal-ipc", "SKILL.md")
SKILL_DST = os.path.join(os.path.expanduser("~"), ".claude", "skills",
                         "multi-terminal-ipc", "SKILL.md")

# (event, command, idempotency marker substring)
HOOK_SPECS = [
    ("SessionStart", f'python "{ROLE_DST}" claim', "ipc_role.py\" claim"),
    ("SessionEnd", f'python "{ROLE_DST}" release', "ipc_role.py\" release"),
]


def _deploy():
    os.makedirs(DST, exist_ok=True)
    shutil.copyfile(os.path.join(SRC, "ipc.py"), os.path.join(DST, "ipc.py"))
    shutil.copyfile(os.path.join(SRC, ".claude", "hooks", "ipc_role.py"), ROLE_DST)
    print(f"  + {os.path.join(DST, 'ipc.py')}")
    print(f"  + {ROLE_DST}")
    os.makedirs(CMD_DST, exist_ok=True)
    for name in COMMANDS:
        src = os.path.join(CMD_SRC, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(CMD_DST, name))
            print(f"  + {os.path.join(CMD_DST, name)}")
    if os.path.exists(SKILL_SRC):
        os.makedirs(os.path.dirname(SKILL_DST), exist_ok=True)
        shutil.copyfile(SKILL_SRC, SKILL_DST)
        print(f"  + {SKILL_DST}")


def _merge_hooks():
    if os.path.exists(USER_SETTINGS):
        try:
            with open(USER_SETTINGS, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            print(f"  ! {USER_SETTINGS} is not valid JSON — left untouched. Add hooks manually.")
            return
    else:
        cfg = {}
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("  ! settings 'hooks' is not an object — left untouched. Add manually.")
        return
    added = []
    for event, command, marker in HOOK_SPECS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            print(f"  ! hooks.{event} is not a list — skipped. Add manually.")
            continue
        present = any(marker in h.get("command", "")
                     for g in groups for h in g.get("hooks", []))
        if present:
            continue
        groups.append({"hooks": [{"type": "command", "command": command}]})
        added.append(event)
    os.makedirs(os.path.dirname(USER_SETTINGS), exist_ok=True)
    tmp = USER_SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_SETTINGS)
    print(f"  + hooks registered in {USER_SETTINGS}: {', '.join(added)}"
          if added else f"  = hooks already present in {USER_SETTINGS} (unchanged)")


def main():
    deploy_only = "--deploy-only" in sys.argv
    if os.path.abspath(SRC) == os.path.abspath(DST):
        raise SystemExit("! source == destination; nothing to do.")
    print(f"Installing IPC kit USER-LEVEL  {SRC}  ->  {DST}")
    _deploy()
    if deploy_only:
        print("\nDeployed (settings.json untouched, --deploy-only). To activate, add the "
              "SessionStart/SessionEnd hooks pointing at the path above.")
        return
    _merge_hooks()
    print("\nDone. The global hook is OPT-IN: it only claims a role in a project that "
          "has a .claude/ipc.enabled marker (or IPC_ROLE set / a local install / an "
          "existing mailbox). Migrate an existing per-project install with migrate_ipc.py.")


if __name__ == "__main__":
    main()
