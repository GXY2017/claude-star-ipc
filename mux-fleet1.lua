-- Fleet-1 mux server config (wezterm-mux-server --config-file this).
-- socket_path MUST match the fleet1 entry in ~/.wezterm.lua (GUI connect side).
-- default_prog pins the MSI pwsh by absolute path - never bare `pwsh` (Store
-- MSIX servicing force-kills Store-pwsh panes; memory store-pwsh-update-kills-fleet).
local wezterm = require 'wezterm'
local config = wezterm.config_builder and wezterm.config_builder() or {}
config.unix_domains = {
  { name = 'fleet1', socket_path = wezterm.home_dir .. '/.local/share/wezterm/fleet1.sock' },
}
config.default_prog = { 'C:\\Program Files\\PowerShell\\7\\pwsh.exe' }
return config
