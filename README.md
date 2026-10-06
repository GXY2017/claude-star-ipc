# claude-star-ipc

## Install

Requires Windows, Python 3.8+, Git, PowerShell 7, WezTerm nightly, and an
authenticated Claude Code CLI. Install and authenticate any other worker CLIs
you plan to use.

```powershell
git clone https://github.com/GXY2017/claude-star-ipc.git
Set-Location claude-star-ipc
python install_user.py
```

Set `$LaunchCmd` in `~/.claude/ipc/ipc_wezterm_launch.ps1` to the CLI commands
available on your machine for each role you want to launch.

## Use

Enable IPC in your project:

```powershell
Set-Location "D:/path/to/project"
python "$HOME/.claude/ipc/ipc_role.py" enable
```

Launch hub A and worker B in that project:

```powershell
pwsh "$HOME/.claude/ipc/ipc_wezterm_launch.ps1" -Project "D:/path/to/project" -Roles A,B
```

The launcher starts each pane and submits its recovery command automatically.
No manual input is needed in the worker windows. An AI session can run the
launcher and control the panes through WezTerm.

In hub A, prepare `task.md` using the
[brief format](skills/multi-terminal-ipc/references/orchestration-and-dispatch.md),
then dispatch it and read replies:

```powershell
pwsh "$HOME/.claude/ipc/ipc_dispatch.ps1" -Role B -BodyFile task.md
python "$HOME/.claude/ipc/ipc.py" recv --me A --block
```

Start a fresh task cycle:

```powershell
pwsh "$HOME/.claude/ipc/ipc_newcycle.ps1"
```

For dispatch, replies, and recovery, see the
[operating guide](skills/multi-terminal-ipc/SKILL.md).
