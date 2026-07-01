-- Fleet control plane — WezTerm front-end for many Claude Code sessions.
--
-- Consumes the (terminal-agnostic) data layer built in ~/.claude:
--   * $HOME/.claude/bin/claude-sessions --json   -> urgency-sorted session records
--   * $HOME/.claude/state/<sid>.json             -> {status, since, wezterm_pane}
--
-- Provides four things on top of WezTerm:
--   1. Tab colors that ESCALATE the longer a session waits (format-tab-title).
--   2. A session PICKER decoupled from the tab strip (CTRL+SHIFT+Space).
--   3. A workspace switcher for task clusters (CTRL+SHIFT+O).
--   4. launch-family: spawn a saved cluster of sessions at once (CTRL+SHIFT+G).
--
-- Source of truth lives in the config repo; ~/.config/wezterm/wezterm.lua is a
-- symlink to here. Validate after install:  wezterm --config-file <this> show-keys

local wezterm = require 'wezterm'
local act = wezterm.action
local config = wezterm.config_builder()
local HOME = wezterm.home_dir

-- ---------------------------------------------------------------------------
-- Base config — deliberately conservative so it loads on a fresh box. Font is
-- left to WezTerm's bundled default (JetBrains Mono) rather than risking a
-- missing family. The non-fancy tab bar honors explicit per-tab fg/bg colors.
-- ---------------------------------------------------------------------------
config.color_scheme = 'Tokyo Night'          -- bundled; swap freely
config.scrollback_lines = 100000
config.use_fancy_tab_bar = false
config.hide_tab_bar_if_only_one_tab = false
config.tab_bar_at_bottom = false
config.tab_max_width = 64   -- ceiling only; real per-tab sizing is in format-tab-title
config.audible_bell = 'Disabled'             -- waiting-sound is driven by hooks, not the bell

-- Grabbable scrollbar (the original garble-relief trial). Right padding leaves
-- room for the thumb; keep it big enough to grab. A contrasting thumb color
-- keeps it ALWAYS visible: with no scrollback the thumb fills the whole track,
-- so coloring it draws a permanent vertical bar instead of a background-colored
-- (invisible) one. `colors` overrides only this key; the rest come from the scheme.
config.enable_scroll_bar = true
config.min_scroll_bar_height = '1.5cell'
config.colors = { scrollbar_thumb = '#565f89' }   -- Tokyo Night grey; visible on the dark bg
config.window_padding = { left = 4, right = 16, top = 2, bottom = 2 }
config.window_close_confirmation = 'NeverPrompt'

-- Manual workspace save/restore via wezterm-sessions, pcall-guarded so a plugin
-- or network hiccup can never break the terminal. Auto-save left OFF (an empty
-- post-reboot window would otherwise clobber a good save before you restore).
--   Alt+s save · Alt+r restore · Alt+l load/switch · Alt+a toggle auto-save
local sess_ok, sessions = pcall(function()
  return wezterm.plugin.require('https://github.com/abidibo/wezterm-sessions')
end)
if sess_ok and sessions then
  sessions.apply_to_config(config, {
    auto_save_interval_s = 30,             -- only used if you toggle auto-save on (Alt+a)
    save_state_dir = 'default-user-owned', -- ~/.local/share/wezterm-sessions/state/
  })
else
  wezterm.log_error('wezterm-sessions failed to load: ' .. tostring(sessions))
end

-- ---------------------------------------------------------------------------
-- Live state cache: refreshed ~1/sec by update-status, read by format-tab-title.
-- Keyed by pane-id STRING -> {status, since}. We map a tab to its session via the
-- active pane id matched against each state file's wezterm_pane.
-- ---------------------------------------------------------------------------
local pane_state = {}

-- Wait ages run on an AWAKE clock that freezes during suspend/downtime, so
-- resuming the machine preserves each tab's color instead of inflating every wait
-- by the time you were away. `awake` accumulates real seconds between
-- update-status ticks but ignores any gap longer than AWAKE_GAP_CAP (a suspend or
-- a clock jump). wait_awake_start[session_id] = the awake value when that session's
-- current wait began; its age is `awake - start`. The gradient is then normalized
-- against a MOVING ceiling, max(WAIT_FLOOR_S, longest current wait), so the reddest
-- tab is always the most-neglected relative to the rest and nothing maxes before 2h.
--
-- Keyed by SESSION_ID (stable across restarts; pane ids are not) and persisted to
-- WAIT_CACHE, so relative coloring survives a full REBOOT too: `awake` resumes from
-- its saved value, freezing the reboot's downtime just like a suspend's. Best
-- effort — a missing/garbled cache just means a calm fresh start.
local WAIT_FLOOR_S  = 7200           -- 2h floor for the gradient ceiling
local AWAKE_GAP_CAP = 120            -- inter-tick gap over this = suspend; not counted
local WAIT_CACHE    = HOME .. '/.cache/wezterm-fleet-wait.json'
local SAVE_EVERY    = 15             -- persist the awake clock at most this often (s)
local awake = 0
local awake_last = nil
local last_save = 0
local wait_awake_start = {}
local wait_norm = WAIT_FLOOR_S

local function read_json_file(path)
  local f = io.open(path, 'r')
  if not f then return nil end
  local body = f:read('a')
  f:close()
  local ok, data = pcall(wezterm.json_parse, body)
  if ok then return data end
  return nil
end

-- Restore the awake clock + per-session wait starts from the last run, so the wait
-- gradient picks up where it left off after a reboot. Best-effort and guarded.
do
  local cached = read_json_file(WAIT_CACHE)
  if cached then
    awake = tonumber(cached.awake) or 0
    last_save = awake
    if type(cached.starts) == 'table' then wait_awake_start = cached.starts end
  end
end

local function save_wait_cache()
  local ok, body = pcall(wezterm.json_encode, { awake = awake, starts = wait_awake_start })
  if not ok then return end
  local f = io.open(WAIT_CACHE, 'w')
  if f then f:write(body); f:close() end
end

wezterm.on('update-status', function(window, pane)
  -- Advance the awake clock, freezing any gap big enough to be a suspend.
  local now = os.time()
  local delta = awake_last and (now - awake_last) or 0
  if delta < 0 or delta > AWAKE_GAP_CAP then delta = 0 end
  awake = awake + delta
  awake_last = now

  -- The set of pane ids that are actually OPEN right now (active pane of every tab
  -- across all windows). The gradient ceiling is computed only over these, so
  -- lingering orphan state files from dead sessions can't blow out the scale.
  -- If the mux walk fails, `live` stays empty and we fall back to counting all.
  local live = {}
  pcall(function()
    for _, w in ipairs(wezterm.mux.all_windows()) do
      for _, t in ipairs(w:tabs()) do
        local ap = t:active_pane()
        if ap then live[tostring(ap:pane_id())] = true end
      end
    end
  end)
  local restrict = next(live) ~= nil

  -- Rebuild pane_state (keyed by pane id, for render lookup), computing each
  -- waiting session's downtime-free age and the longest OPEN wait (the ceiling).
  -- Wait-starts are keyed by session_id so they survive pane-id churn across reboots.
  local fresh, longest, seen = {}, 0, {}
  for _, path in ipairs(wezterm.glob(HOME .. '/.claude/state/*.json')) do
    local d = read_json_file(path)
    if d and d.wezterm_pane then
      local pane_id = tostring(d.wezterm_pane)
      local sid = tostring(d.session_id or d.since or pane_id)
      local age = 0
      if d.status == 'waiting' and d.since then
        local w = wait_awake_start[sid]
        if not w or w.since ~= d.since then        -- a new wait (or `since` changed)
          -- Seed from the REAL wall age so an existing spread of waits keeps its
          -- relative order on first sight (fresh start / reboot), instead of every
          -- tab collapsing to age 0. From here the awake clock freezes downtime.
          w = { since = d.since, start = awake - math.max(0, now - d.since) }
          wait_awake_start[sid] = w
        end
        seen[sid] = true
        age = math.max(0, awake - w.start)
        if age > longest and (not restrict or live[pane_id]) then longest = age end
      end
      fresh[pane_id] = { status = d.status, since = d.since, age = age }
    end
  end
  pane_state = fresh
  wait_norm = math.max(WAIT_FLOOR_S, longest)

  -- Forget awake-start for sessions no longer waiting; persist on the throttle.
  for sid in pairs(wait_awake_start) do
    if not seen[sid] then wait_awake_start[sid] = nil end
  end
  if awake - last_save >= SAVE_EVERY then
    last_save = awake
    save_wait_cache()
  end

  -- Cache the tab-bar width (in cells) so format-tab-title can pad tabs to fill
  -- it. The bar width isn't passed to that event, and WezTerm's retro bar won't
  -- stretch tabs on its own (wezterm/wezterm#7702), so we size them ourselves.
  -- pane:get_dimensions().cols == window text width for unsplit tabs (ours).
  local ok, dim = pcall(function() return pane:get_dimensions() end)
  if ok and dim and dim.cols and dim.cols > 0 then
    wezterm.GLOBAL.bar_cols = dim.cols
  end
end)

-- Wait escalation is a continuous gradient over wait age, not hard steps. Stops
-- run coolest (just started waiting) -> burning, and we interpolate between them
-- for a granular ramp. {pos 0..1, r, g, b}.
local WAIT_STOPS = {
  { 0.00, 0x3d, 0x5a, 0x55 },  -- calm teal-grey: just asked, low urgency
  { 0.22, 0x4f, 0x80, 0x57 },  -- green
  { 0.42, 0x84, 0xa3, 0x44 },  -- green-yellow
  { 0.58, 0xc2, 0xb3, 0x4a },  -- yellow
  { 0.74, 0xd6, 0x8a, 0x2e },  -- orange
  { 0.88, 0xcc, 0x52, 0x2e },  -- red-orange
  { 1.00, 0x9c, 0x16, 0x16 },  -- deep red: burning
}

-- Ease-in exponent (>1) keeps the FIRST stretch slow ("slow start"). It's applied
-- on top of a LOGARITHMIC age scale (see wait_colors) so that minutes, hours, and
-- days each get their own slice of the gradient — otherwise a few days-old waits
-- blow out a linear scale and crush every recent tab into the same calm color.
local WAIT_GAMMA = 1.8

local function lerp(a, b, f) return a + (b - a) * f end
local function hex2(n) return string.format('%02x', math.min(255, math.max(0, math.floor(n + 0.5)))) end

-- Interpolate the stops at position t (0..1) -> { bg, fg, bold }.
local function gradient_at(t)
  t = math.min(1, math.max(0, t))
  local lo, hi = WAIT_STOPS[1], WAIT_STOPS[#WAIT_STOPS]
  for i = 1, #WAIT_STOPS - 1 do
    if t >= WAIT_STOPS[i][1] and t <= WAIT_STOPS[i + 1][1] then
      lo, hi = WAIT_STOPS[i], WAIT_STOPS[i + 1]
      break
    end
  end
  local span = hi[1] - lo[1]
  local f = span > 0 and (t - lo[1]) / span or 0
  local r, g, b = lerp(lo[2], hi[2], f), lerp(lo[3], hi[3], f), lerp(lo[4], hi[4], f)
  -- Perceived luminance picks readable text; hot/deep stops get white.
  local lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return {
    bg = '#' .. hex2(r) .. hex2(g) .. hex2(b),
    fg = lum > 0.6 and '#1a1a1a' or '#ffffff',
    bold = t >= 0.85,
  }
end

-- Working = normal (nil). Waiting = a gradient on this tab's AWAKE wait age
-- (precomputed in update-status, so downtime never counts) on a LOG scale relative
-- to the current ceiling (longest wait, floored at 2h). Log scale spreads a
-- minutes-to-days range across the palette instead of bunching it at the cool end.
local function wait_colors(st)
  if not st or st.status ~= 'waiting' or not st.age then return nil end
  local frac = math.log(1 + st.age) / math.log(1 + wait_norm)
  return gradient_at(frac ^ WAIT_GAMMA)
end

local function trunc(s, n)
  if #s <= n then return s end
  return s:sub(1, math.max(1, n - 1)) .. '…'
end

-- A one-char status glyph derived from the pane title. session-state.sh appends
-- '●' (waiting on you) as a SUFFIX, and Claude Code prefixes a '✳'/'*' spinner
-- while working. We surface it right after the tab number so truncation (which
-- eats the right edge) can never swallow it. Idle/clean -> a dim dot.
--
-- NOTE: '✳'/'●' are 3-byte UTF-8 chars and MUST be matched as whole literals,
-- never inside a '[...]' class — a class matches individual bytes, which splits
-- the glyph and yields invalid UTF-8 that WezTerm rejects (the tab then silently
-- falls back to its default 'N:' rendering).
local function status_glyph(t)
  t = t or ''
  if t:match('●%s*$') then return '●' end
  if t:match('^%s*✳') or t:match('^%s*%*') then return '✳' end
  return '·'
end

-- Strip the status glyphs so the label spends its columns on real text. We keep
-- the full label (project included) — it truncates fine, and dropping the first
-- word mangles Claude's working-spinner titles, which are task text, not labels.
local function compact_label(t)
  return (t or ''):gsub('%s*●%s*$', ''):gsub('^%s*✳%s*', ''):gsub('^%s*%*%s*', '')
end

-- The clicked-into tab gets this highlight so it's unmistakable in the row.
local ACTIVE = { bg = '#7aa2f7', fg = '#1a1b26' }   -- Tokyo Night blue / dark ink

-- The active tab is sized this many "shares" vs. one share per other tab, so it
-- stays the widest while the whole row still sums to the bar width.
local ACTIVE_WEIGHT = 3.0

-- Left-pad-right (or truncate) `s` to exactly `target` display columns. The
-- trailing padding is what makes the row reach the right edge; WezTerm keeps it
-- (it doesn't trim), and flush-left text reads fuller than centered stubs.
local function dispw(s)
  if wezterm.column_width then return wezterm.column_width(s) end
  return #s   -- byte-length fallback; off by a couple cols on multibyte glyphs
end

local function fit(s, target)
  target = math.max(1, target)
  if dispw(s) > target then
    if wezterm.truncate_right then
      s = wezterm.truncate_right(s, math.max(1, target - 1)) .. '…'
    else
      s = trunc(s, target)
    end
  end
  local pad = target - dispw(s)
  if pad > 0 then s = s .. string.rep(' ', pad) end
  return s
end

wezterm.on('format-tab-title', function(tab, tabs, panes, conf, hover, max_width)
  local idx = tab.tab_index + 1
  local pt = tab.active_pane and tab.active_pane.title or ''
  local st = tab.active_pane and pane_state[tostring(tab.active_pane.pane_id)] or nil
  -- The pane's LIVE title is authoritative for waiting-vs-working (● suffix), so
  -- we only trust a wait color when it's actually present. Pane ids get reused and
  -- dead sessions' state files linger, so pane_state alone can hand a working tab a
  -- stale wait color from a long-gone session that once held this pane id.
  local c = pt:match('●%s*$') and wait_colors(st) or nil

  -- Divide the bar into shares so every tab's width sums to ~full width. Reserve
  -- a couple cols of safety so we undershoot (a tiny gap) rather than overshoot
  -- (which would make WezTerm truncate the rightmost tab).
  local n = math.max(1, #tabs)
  local bar = (wezterm.GLOBAL.bar_cols or (n * 16)) - 1
  local cap = conf.tab_max_width or 64
  local inactive_w = math.max(6, math.floor(bar / (n - 1 + ACTIVE_WEIGHT)))
  local active_w = math.max(inactive_w, math.min(cap, bar - inactive_w * (n - 1)))
  local target = math.min(cap, tab.is_active and active_w or inactive_w)

  -- Active and waiting tabs show the full title; others a compact label. Glyph
  -- sits right after the number so truncation (right edge) can't swallow it.
  local label = (tab.is_active or c) and (pt ~= '' and pt or ('tab ' .. idx)) or compact_label(pt)
  local text = fit(string.format(' %d %s %s ', idx, status_glyph(pt), label), target)

  -- Active wins the styling so "which tab am I in" is never ambiguous; a waiting
  -- active tab is one you're already looking at, so its escalation color can wait.
  if tab.is_active then
    return {
      { Background = { Color = ACTIVE.bg } },
      { Foreground = { Color = ACTIVE.fg } },
      { Attribute = { Intensity = 'Bold' } },
      { Text = text },
    }
  end
  if c then
    return {
      { Background = { Color = c.bg } },
      { Foreground = { Color = c.fg } },
      { Attribute = { Intensity = c.bold and 'Bold' or 'Normal' } },
      { Text = text },
    }
  end
  return text
end)

-- ---------------------------------------------------------------------------
-- Session picker (CTRL+SHIFT+Space). Reads the registry fresh, shows a fuzzy
-- list sorted by urgency, jumps to the chosen session's pane (or resumes it).
-- ---------------------------------------------------------------------------
local function run_registry()
  local ok, stdout, stderr = wezterm.run_child_process({ HOME .. '/.claude/bin/claude-sessions', '--json' })
  if not ok then return nil, (stderr or 'claude-sessions failed') end
  local okj, recs = pcall(wezterm.json_parse, stdout)
  if not okj then return nil, 'could not parse registry JSON' end
  return recs, nil
end

local function activate_or_resume(win, pane, paneid, sid, cwd)
  local mux_pane = paneid and wezterm.mux.get_pane(tonumber(paneid)) or nil
  if mux_pane then
    -- MuxPane:activate() focuses pane + its tab + window (recent WezTerm).
    -- pcall + tab-activate fallback keeps it working on older builds.
    if not pcall(function() mux_pane:activate() end) then
      pcall(function()
        local t = mux_pane:tab()
        if t then t:activate() end
      end)
    end
    return
  end
  -- Live in the registry but no WezTerm pane (closed, or running elsewhere): resume.
  win:perform_action(
    act.SpawnCommandInNewTab { cwd = cwd, args = { 'bash', '-lic', 'claude --resume ' .. sid .. '; exec bash' } },
    pane)
end

local function session_picker(window, pane)
  local recs, err = run_registry()
  if not recs then window:toast_notification('fleet', err, nil, 4000); return end
  if #recs == 0 then window:toast_notification('fleet', 'no live sessions', nil, 3000); return end

  local choices, pane_by_id, cwd_by_id = {}, {}, {}
  for _, r in ipairs(recs) do
    local grp = r.group and (' [' .. r.group .. ']') or ''
    table.insert(choices, {
      id = r.session_id,
      label = string.format('%s  %-28s %5s  %s%s', r.glyph or '·', r.label or r.session_id,
        r.age_str or '', r.project or '', grp),
    })
    pane_by_id[r.session_id] = r.wezterm_pane
    cwd_by_id[r.session_id] = r.cwd
  end

  window:perform_action(act.InputSelector {
    title = 'Fleet — sessions',
    fuzzy = true,
    choices = choices,
    action = wezterm.action_callback(function(win, p, id)
      if not id then return end       -- cancelled
      activate_or_resume(win, p, pane_by_id[id], id, cwd_by_id[id])
    end),
  }, pane)
end

-- ---------------------------------------------------------------------------
-- launch-family (CTRL+SHIFT+G): spawn a saved cluster into its own workspace.
-- ~/.claude/fleet/families.json = { "name": [ {cwd, label?, cmd?}, ... ], ... }
-- ---------------------------------------------------------------------------
local function launch_family(window, pane)
  local fams = read_json_file(HOME .. '/.claude/fleet/families.json')
  if not fams then window:toast_notification('fleet', 'no ~/.claude/fleet/families.json', nil, 3500); return end

  local choices = {}
  for name, _ in pairs(fams) do table.insert(choices, { id = name, label = name }) end
  table.sort(choices, function(a, b) return a.label < b.label end)
  if #choices == 0 then window:toast_notification('fleet', 'families.json is empty', nil, 3000); return end

  window:perform_action(act.InputSelector {
    title = 'Launch family',
    fuzzy = true,
    choices = choices,
    action = wezterm.action_callback(function(win, p, id)
      if not id then return end
      local entries = fams[id]
      if not entries or #entries == 0 then return end
      local first = entries[1]
      local _, _, mux_win = wezterm.mux.spawn_window {
        workspace = id,
        cwd = first.cwd,
        args = { 'bash', '-lic', (first.cmd or 'claude') .. '; exec bash' },
      }
      for i = 2, #entries do
        local e = entries[i]
        mux_win:spawn_tab { cwd = e.cwd, args = { 'bash', '-lic', (e.cmd or 'claude') .. '; exec bash' } }
      end
      win:perform_action(act.SwitchToWorkspace { name = id }, p)
    end),
  }, pane)
end

-- ---------------------------------------------------------------------------
-- Keys. Letters chosen to avoid clobbering core WezTerm defaults (T/W/C/V/N/P).
-- Rebind freely. If 'Space' errors at load, use a letter or 'phys:Space'.
-- ---------------------------------------------------------------------------
config.keys = {
  { key = 'Space', mods = 'CTRL|SHIFT', action = wezterm.action_callback(session_picker) },
  { key = 'o',     mods = 'CTRL|SHIFT', action = act.ShowLauncherArgs { flags = 'FUZZY|WORKSPACES' } },
  { key = 'g',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(launch_family) },
  -- Reorder the active tab. Left/Right shift it one slot; Home/End send it to the ends.
  { key = 'LeftArrow',  mods = 'CTRL|SHIFT', action = act.MoveTabRelative(-1) },
  { key = 'RightArrow', mods = 'CTRL|SHIFT', action = act.MoveTabRelative(1) },
  { key = 'Home',       mods = 'CTRL|SHIFT', action = act.MoveTab(0) },
  { key = 'End',        mods = 'CTRL|SHIFT', action = wezterm.action_callback(function(win, _)
      local tabs = win:mux_window():tabs()
      win:perform_action(act.MoveTab(#tabs - 1), win:active_pane())
    end) },
}

return config
