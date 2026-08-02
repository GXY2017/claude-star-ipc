@echo off
rem Boot autostart for IPC keepalive daemon(s). Drop a copy into
rem   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
rem (user-level, no elevation needed — unlike Register-ScheduledTask).
rem
rem -WorkRoot MUST point at the opted-in project root. At logon the cmd runs with
rem cwd = System32, and ipc.py resolves the mailbox by walking UP from cwd to a
rem project marker (CLAUDE.md/.claude/.git; the home dir is deliberately skipped
rem to prevent cross-wiring) — without the ps1's cwd pin the walk falls back to
rem cwd and the daemon beats a STRAY mailbox with no error: every slot shows DOWN
rem after reboot while the daemon looks healthy.
rem
rem Verify the boot chain without rebooting (clean registry env + System32 cwd):
rem   Start-Process cmd.exe -ArgumentList '/c','"<this file>"' -UseNewEnvironment `
rem     -WorkingDirectory C:\Windows\System32 -WindowStyle Hidden
rem then from the project root: python %USERPROFILE%\.claude\ipc\ipc.py status --watch CODEX
start "" pwsh -NoProfile -WindowStyle Hidden -File "%USERPROFILE%\.claude\ipc\codex_ipc_worker.ps1" -Role CODEX,DS -WorkRoot "C:\path\to\your\project"
