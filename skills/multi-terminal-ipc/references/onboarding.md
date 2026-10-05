# Enable IPC in a new project (onboarding)

> Split out of `SKILL.md` on 2026-09-07 (lint-skills: progressive disclosure — onboarding is a one-time flow). Triggers: 在新项目启用 IPC / enable IPC in this project / fresh machine.

The machinery is USER-LEVEL (one copy at `~/.claude/ipc/`, global hooks in
`~/.claude/settings.json`); a new project needs exactly ONE per-project artifact —
the opt-in gate file. One command creates it, with guards:

```
CLAUDE_PROJECT_DIR='<absolute project root>' python ~/.claude/ipc/ipc_role.py enable                # bash / Git Bash
$env:CLAUDE_PROJECT_DIR='<absolute project root>'; python "$HOME/.claude/ipc/ipc_role.py" enable   # PowerShell — a quoted "~" is never expanded
```

`enable` resolves the root as `ipc.py` does (`CLAUDE_PROJECT_DIR` if set, else
the nearest ancestor of cwd holding `AGENTS.md` / `CLAUDE.md` / `.claude` / `.git`, else cwd),
so the variable replaces a `cd`. It validates the user-level install (ipc.py importable, claim hook
registered), **refuses the home dir and `~/.claude`** (every launch dir becomes a
registered project; user config must never be one), and creates
`.claude/ipc.enabled` at the project root. Idempotent. Manual equivalent: create
that empty gate file yourself.

Then bring terminals online:
1. Open each terminal with cwd = the project root. **Claim is guarded (user rule
   2026-08-09): the SessionStart hook runs only when `WEZTERM_PANE` or
   `IPC_ROLE` is set** (never in child sessions) — a bare terminal outside
   WezTerm never claims a role. Launch via the fleet script (presets `IPC_ROLE`
   per pane) or pin a window's role with `IPC_ROLE=B claude`; among
   guard-passing sessions without a preset role, first in = A/hub, the rest
   take the lowest free slot.
2. The manual floor: the fleet launcher types `/ipc-recover <role>` (Codex:
   `$ipc-recover <role>`) into each pane once its TUI is ready; type it yourself
   only in a window opened by hand. Its watcher parks and any backlog arrives
   as signals.
3. Verify: `ipc_role.py status` shows the roles live, then one A→B round-trip
   (`send` a test task, worker replies, `pending --hub A` returns to empty).

What you do NOT need: no per-project copy of `ipc.py`, no per-project hooks, no
mailbox setup — state auto-creates under `~/.claude/projects/<key>/ipc/` on first
use, one isolated mailbox per project. In the project's own AGENTS.md (its only
instruction file, user rule 2026-10-04) add only a
one-line pointer ("协作协议见 `multi-terminal-ipc` skill") plus any
deployment-specific rules — never paste the protocol (user rule 2026-08-21:
project docs must not restate user-level content).

Fresh **machine** rather than fresh project? Run `python install_user.py` from the
claude-star-ipc repo once (deploys machinery + slash commands + this skill), then
per-project `enable` as above.
