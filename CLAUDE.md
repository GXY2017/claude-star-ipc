# Protocol → skills/multi-terminal-ipc/SKILL.md

> **Single authority (2026-08-21):** the full multi-terminal IPC protocol —
> topology, command surface, task lifecycle, watcher & recovery rules,
> orchestration patterns, capacity-aware dispatch — lives in
> [`skills/multi-terminal-ipc/SKILL.md`](skills/multi-terminal-ipc/SKILL.md).
> `install_user.py` deploys that skill user-level (`~/.claude/skills/`), so every
> opted-in project loads the same copy on demand; project `CLAUDE.md` files keep
> only a one-line pointer plus deployment-specific rules. This file no longer
> restates the protocol (the last full ~360-line spec is commit `a092dde`).

Repo facts (the only content that belongs here):

- **This repo is the machinery itself**, not a deployment: `ipc.py` (mailbox
  CLI), `.claude/hooks/ipc_role.py` (role registry + SessionStart/End hook
  handler), fleet/pane PowerShell scripts, mux configs, installer, tests,
  examples. Terminals collaborating in some project follow the skill; working
  *on this repo* is ordinary single-terminal development.
- **Deployment model (current):** USER-LEVEL. One copy of the machinery at
  `~/.claude/ipc/`; per-project mailbox/registry/heartbeats under
  `~/.claude/projects/<key>/ipc/`; a project opts in via the gate file
  `.claude/ipc.enabled` (`python ~/.claude/ipc/ipc_role.py enable`). The
  SessionStart/SessionEnd hooks are guarded (2026-08-09): claim/release run only
  when `WEZTERM_PANE` or `IPC_ROLE` is set, never in child sessions.
  In all docs, `ipc.py` is shorthand for `python ~/.claude/ipc/ipc.py`.
- **Editing rules:** protocol changes go to the skill (and the code); keep
  `README.md`'s files table and `install_user.py`'s deploy manifest in step with
  new scripts. The slash commands `.claude/commands/main.md` / `ipc-recover.md`
  are authoritative for hub-wait and post-/clear recovery — the skill points at
  them, don't fork their content.
- Legacy per-project topology (superseded) and its migration are described at
  the end of the skill; one-shot installers are archived under `_archive/`.
