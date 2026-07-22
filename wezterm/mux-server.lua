-- Config for the PERSISTENT mux server (wezterm-mux-server), run by the
-- systemd --user unit systemd/user/wezterm-mux.service. Deliberately minimal and
-- SEPARATE from the fleet GUI config (wezterm.lua): the server only needs to own
-- ptys and expose the unix domain, and must never do GUI/network work — in
-- particular it must NOT load the wezterm-sessions plugin (a github fetch that
-- could hang or fail server startup).
--
-- The socket_path here MUST match the `mux` unix_domain in wezterm.lua exactly,
-- or clients won't find the server.
local wezterm = require 'wezterm'
local config = wezterm.config_builder()

local runtime = os.getenv('XDG_RUNTIME_DIR') or (wezterm.home_dir .. '/.local/state')
config.unix_domains = {
  { name = 'mux', socket_path = runtime .. '/wezterm/muxsvr-sock' },
}

-- Panes spawned without an explicit program get a login shell.
config.default_prog = { 'bash', '-l' }

return config
