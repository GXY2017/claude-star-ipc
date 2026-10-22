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
# references/ (2026-09-14): onboarding + orchestration/dispatch patterns split out of
# SKILL.md so the always-loaded body stays small; SKILL.md links them by relative path.
SKILL_REF_SRC = os.path.join(SRC, "skills", "multi-terminal-ipc", "references")
SKILL_REF_DST = os.path.join(os.path.dirname(SKILL_DST), "references")
# Optional WezTerm fleet-control scripts (2026-08-03; mux-domain world 2026-08-17):
# launch all role windows as panes under a per-fleet wezterm-mux-server, batch
# /clear + /ipc-recover them between projects, wake/dispatch/exit single panes.
WEZTERM_SCRIPTS = ("ipc_wezterm_common.ps1", "ipc_wezterm_launch.ps1",
                   "ipc_newcycle.ps1", "ipc_fleet1_restart.ps1",
                   "ipc_fleet2_launch.ps1", "ipc_dispatch.ps1",
                   "ipc_wake_pane.ps1", "ipc_pane_exit.ps1",
                   "mux-fleet1.lua", "mux-fleet2.lua")
# Hub-side tooling (2026-08-29..31 postmortems): brief lint before dispatch, pane
# liveness probe (context % / stall flags), dispatch->reply timing, pre-dispatch
# quota snapshot, read-only pane listing/tail, and the fleet triage checklist they
# implement. The 15-min reply clock is built into `ipc.py watch --me A` (2026-10-18;
# stall_watch.py removed).
TOOL_SCRIPTS = ("ipc_brief_lint.py", "ipc_pane_probe.py", "ipc_timing.py",
                "ipc_estimate.py",   # 2026-09-19: per-model duration ledger + budget estimates
                "ipc_quota.py",      # 2026-10-17: per-vendor quota verdicts before dispatch
                "ipc_state.py",      # 2026-10-20: per-role ctx/quota state from files (statusline state + codex rollouts), pane regex fallback
                "ipc_supervisor.py", # 2026-10-20: out-of-session hub reply clock (started by ipc_wezterm_launch.ps1)
                "fleet_list_panes.py", "fleet_get_text.py",
                "FLEET-TRIAGE.md")

# (event, command, idempotency marker substring)
# Guarded (user rule 2026-08-09): claim/release run only inside a WezTerm pane or a
# window with an explicit IPC_ROLE — a bare terminal never claims a role — and never
# in harness child sessions. Requires "shell": "bash" on the hook entry.
_GUARD = '{ [ -n "$WEZTERM_PANE" ] || [ -n "$IPC_ROLE" ]; } && [ -z "$CLAUDE_CODE_CHILD_SESSION" ]'
HOOK_SPECS = [
    ("SessionStart", f'if {_GUARD}; then python "{ROLE_DST}" claim; fi', "ipc_role.py\" claim"),
    ("SessionEnd", f'if {_GUARD}; then python "{ROLE_DST}" release; fi', "ipc_role.py\" release"),
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
        if os.path.isdir(SKILL_REF_SRC):
            os.makedirs(SKILL_REF_DST, exist_ok=True)
            for name in sorted(os.listdir(SKILL_REF_SRC)):
                if name.endswith(".md"):
                    shutil.copyfile(os.path.join(SKILL_REF_SRC, name),
                                    os.path.join(SKILL_REF_DST, name))
                    print(f"  + {os.path.join(SKILL_REF_DST, name)}")
    for name in WEZTERM_SCRIPTS + TOOL_SCRIPTS:
        src = os.path.join(SRC, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(DST, name))
            print(f"  + {os.path.join(DST, name)}")


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
        groups.append({"hooks": [{"type": "command", "command": command, "shell": "bash"}]})
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
