-- Fleet control plane — WezTerm front-end for many Claude Code sessions.
--
-- Consumes the (terminal-agnostic) data layer built in ~/.claude:
--   * $HOME/.claude/bin/claude-sessions --json   -> urgency-sorted session records
--   * $HOME/.claude/state/<sid>.json             -> {status, since, wezterm_pane}
--
-- Provides four things on top of WezTerm:
--   1. Tab colors that ESCALATE the longer a session waits (format-tab-title).
--   2. A session PICKER decoupled from the tab strip (CTRL+SHIFT+Space),
--      including transcript-content search (its '🔍' row).
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
local bar_cols_by_window = {}

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
local TAB_ORDER_CACHE = HOME .. '/.cache/wezterm-fleet-tab-order.json'
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

-- ---------------------------------------------------------------------------
-- Hidden sessions (CTRL+SHIFT+S → "keep running"): sid -> { notify, notified, label }.
-- Kept in wezterm.GLOBAL, not a file, because it describes panes that live and die
-- with THIS GUI process — it survives a config hot-reload (which wipes module-local
-- tables) but not a restart, exactly like the panes it tracks. GLOBAL reads return a
-- COPY, so every mutation reads the whole map, edits it, and writes it back.
-- ---------------------------------------------------------------------------
local HIDDEN_WS = '__hidden__'          -- background workspace where hidden panes park
local function hidden_map() return wezterm.GLOBAL.hidden or {} end
local function set_hidden_map(m) wezterm.GLOBAL.hidden = m end

-- ---------------------------------------------------------------------------
-- Project acronyms: collision-free 1–2 char codes for every folder in ~/projects,
-- so a tab can read as [acronym][n] (project code + which instance in that project).
-- The code set is computed over ALL project dirs at once (a folder's code depends
-- on what else exists), memoized, and rebuilt only when ~/projects changes.
-- ---------------------------------------------------------------------------
local PROJECTS    = HOME .. '/projects'
local PROJ_PREFIX = PROJECTS .. '/'
local ACR_ALPHA   = 'abcdefghijklmnopqrstuvwxyz'
local ACR_DIGITS  = '0123456789'

-- Ordered, de-duplicated candidate codes for one name (most→least preferred):
-- the bare first letter, then first-letter + {true prefix, later-word initials,
-- remaining chars, then every a-z/0-9} as the 2nd char — the tail guarantees a
-- free 2-char code always exists (36 >> any first-letter cluster here).
local function acr_candidates(name)
  local n = name:lower()
  local chars, initials = {}, {}
  for w in n:gmatch('[a-z0-9]+') do
    initials[#initials + 1] = w:sub(1, 1)
    for i = 1, #w do chars[#chars + 1] = w:sub(i, i) end
  end
  if #chars == 0 then chars = { 'x' } end
  local bi = 1
  for i = 1, #chars do
    local c = chars[i]
    if c >= 'a' and c <= 'z' then bi = i; break end   -- prefer a letter as the base
  end
  local base = chars[bi]

  local seconds = {}
  if chars[bi + 1] then seconds[#seconds + 1] = chars[bi + 1] end   -- true prefix
  for i = 2, #initials do seconds[#seconds + 1] = initials[i] end   -- later-word initials
  for i = bi + 1, #chars do seconds[#seconds + 1] = chars[i] end    -- remaining scan chars
  for i = 1, #ACR_ALPHA do seconds[#seconds + 1] = ACR_ALPHA:sub(i, i) end
  for i = 1, #ACR_DIGITS do seconds[#seconds + 1] = ACR_DIGITS:sub(i, i) end

  local out, seen = { base }, { [base] = true }
  for _, s in ipairs(seconds) do
    local code = base .. s
    if not seen[code] then seen[code] = true; out[#out + 1] = code end
  end
  return out
end

-- Codes are STICKY: once a folder is assigned a code it keeps it forever, so a
-- project's letters never change regardless of tabs OR of new folders appearing.
-- The map is persisted to disk; a folder is only ever ADDED (taking a still-free
-- code), never reassigned. On a cold start (empty cache) the first assignment pass
-- reproduces the "shortest name wins the bare letter" table, then freezes it.
local ACR_CACHE   = HOME .. '/.cache/wezterm-fleet-acronyms.json'
local acronym_map = nil     -- nil = not loaded from disk yet
local acronym_sig = nil

local function save_acronyms()
  local ok, body = pcall(wezterm.json_encode, acronym_map)
  if not ok then return end
  local f = io.open(ACR_CACHE, 'w')
  if f then f:write(body); f:close() end
end

-- Assign codes to any folders that don't have one yet, without disturbing existing
-- assignments. Runs only when ~/projects changes (gated by a subprocess-free glob
-- signature), so the per-tick cost stays a glob + string compare.
local function refresh_acronyms()
  local entries = wezterm.glob(PROJECTS .. '/*')
  table.sort(entries)
  local sig = table.concat(entries, '\n')
  if acronym_map and sig == acronym_sig then return end
  if not acronym_map then                       -- first refresh: load sticky map
    acronym_map = read_json_file(ACR_CACHE)
    if type(acronym_map) ~= 'table' then acronym_map = {} end
  end

  local ok, stdout = wezterm.run_child_process({
    'find', PROJECTS, '-maxdepth', '1', '-mindepth', '1',
    '-type', 'd', '-not', '-name', '.*', '-printf', '%f\n' })
  if not ok then return end                     -- keep last good map; retry on change
  acronym_sig = sig

  -- Reserve every code already handed out (across all remembered folders, even ones
  -- since deleted — so a delete+recreate keeps its old code and nothing is reused).
  local taken = {}
  for _, code in pairs(acronym_map) do taken[code] = true end

  -- New folders only, in (length, alpha) order so a cold start matches the batch
  -- table; each takes its first still-free candidate. Existing codes are untouched.
  local newones = {}
  for line in stdout:gmatch('[^\n]+') do
    if not acronym_map[line] then newones[#newones + 1] = line end
  end
  table.sort(newones, function(a, b)
    if #a ~= #b then return #a < #b end
    return a < b
  end)
  local dirty = false
  for _, nm in ipairs(newones) do
    for _, cand in ipairs(acr_candidates(nm)) do
      if not taken[cand] then
        taken[cand] = true; acronym_map[nm] = cand; dirty = true; break
      end
    end
  end
  if dirty then save_acronyms() end
end

-- Top-level ~/projects folder for a cwd (or nil if the session isn't under it).
local function project_of(cwd)
  if type(cwd) ~= 'string' or cwd:sub(1, #PROJ_PREFIX) ~= PROJ_PREFIX then return nil end
  return cwd:sub(#PROJ_PREFIX + 1):match('^([^/]+)')
end

-- Display code for a cwd outside ~/projects (out of the collision-free scope):
-- first 1–2 alnum of the basename, '~' for home. Best effort, not guaranteed unique.
local function fallback_code(cwd)
  if type(cwd) ~= 'string' or cwd == '' then return '?' end
  if cwd == HOME then return '~' end
  local a = cwd:gsub('/+$', ''):gsub('.*/', ''):lower():gsub('[^a-z0-9]', '')
  if a == '' then return '?' end
  return a:sub(1, 2)
end

local function save_wait_cache()
  local ok, body = pcall(wezterm.json_encode, { awake = awake, starts = wait_awake_start })
  if not ok then return end
  local f = io.open(WAIT_CACHE, 'w')
  if f then f:write(body); f:close() end
end

-- Publish { [pane_id] = {window_id, tab_index} } for claude-snapshot, which joins it
-- to sessions by pid so claude-resume can respawn tabs in their remembered order.
-- Full overwrite (never merged) so a closed tab leaves no stale slot; skipped unless
-- the encoding actually changed, keeping the ~1/sec tick free of pointless writes.
local last_tab_order = nil
local function save_tab_order(order)
  if not next(order) then return end   -- a failed mux walk must not erase the file
  local ok, body = pcall(wezterm.json_encode, order)
  if not ok or body == last_tab_order then return end
  local f = io.open(TAB_ORDER_CACHE, 'w')
  if f then f:write(body); f:close(); last_tab_order = body end
end

-- update-status is also scheduled by pane-title changes, not just its ~1/sec timer.
-- A title animation can therefore invoke it many times per second and across every
-- GUI window. Keep the cheap window-width observation responsive, but run the
-- module-global mux/state scan at most once per wall-clock second.
local last_status_refresh = nil
wezterm.on('update-status', function(window, pane)
  local ok, dim = pcall(function() return pane:get_dimensions() end)
  if ok and dim and dim.cols and dim.cols > 0 then
    local window_id = tostring(window:window_id())
    local cur = bar_cols_by_window[window_id] or 0
    if math.abs(dim.cols - cur) > 1 then
      bar_cols_by_window[window_id] = dim.cols
    end
  end

  -- Advance the awake clock, freezing any gap big enough to be a suspend.
  local now = os.time()
  if now == last_status_refresh then return end
  last_status_refresh = now
  refresh_acronyms()   -- cheap unless ~/projects changed

  local delta = awake_last and (now - awake_last) or 0
  if delta < 0 or delta > AWAKE_GAP_CAP then delta = 0 end
  awake = awake + delta
  awake_last = now

  -- The set of pane ids that are actually OPEN right now (active pane of every tab
  -- across all windows). The gradient ceiling is computed only over these, so
  -- lingering orphan state files from dead sessions can't blow out the scale.
  -- If the mux walk fails, `live` stays empty and we fall back to counting all.
  -- The same walk records each pane's VISUAL position: ipairs order over w:tabs()
  -- is the on-screen order (it's what a drag-reorder changes), and it's only
  -- observable here — `wezterm cli list` returns panes unordered.
  local live, order = {}, {}
  pcall(function()
    for _, w in ipairs(wezterm.mux.all_windows()) do
      local wid = w:window_id()
      for i, t in ipairs(w:tabs()) do
        local ap = t:active_pane()
        if ap then
          local pid = tostring(ap:pane_id())
          live[pid] = true
          order[pid] = { wid, i - 1 }
        end
      end
    end
  end)
  local restrict = next(live) ~= nil
  save_tab_order(order)

  -- Rebuild pane_state (keyed by pane id, for render lookup), computing each
  -- waiting session's downtime-free age and the longest OPEN wait (the ceiling).
  -- Wait-starts are keyed by session_id so they survive pane-id churn across reboots.
  local fresh, longest, seen = {}, 0, {}
  for _, path in ipairs(wezterm.glob(HOME .. '/.claude/state/*.json')) do
    local d = read_json_file(path)
    if d and d.wezterm_pane then
      local pane_id = tostring(d.wezterm_pane)
      local upd = tonumber(d.updated) or 0
      -- Pane ids get reused and dead sessions' state files linger, so two files can
      -- claim one pane. The live session keeps writing, so newest `updated` wins —
      -- this is the stale-pane guard the old ●-in-title check used to provide.
      local prev = fresh[pane_id]
      if not prev or upd >= prev.updated then
        local sid = tostring(d.session_id or d.since or pane_id)
        local age = 0
        if d.status == 'waiting' and d.since then
          local w = wait_awake_start[sid]
          if not w or w.since ~= d.since then      -- a new wait (or `since` changed)
            -- Seed from the REAL wall age so an existing spread of waits keeps its
            -- relative order on first sight (fresh start / reboot), instead of every
            -- tab collapsing to age 0. From here the awake clock freezes downtime.
            w = { since = d.since, start = awake - math.max(0, now - d.since) }
            wait_awake_start[sid] = w
          end
          age = math.max(0, awake - w.start)
        end
        -- Project code + a per-folder group key (folder name, or the cwd for
        -- non-project sessions) so instances in one folder can be numbered below.
        local folder = project_of(d.cwd)
        local fkey = folder or ('#' .. tostring(d.cwd or pane_id))
        local acr = (folder and acronym_map[folder]) or fallback_code(d.cwd)
        fresh[pane_id] = { status = d.status, since = d.since, age = age,
                           updated = upd, sid = sid, fkey = fkey, acr = acr }
      end
    end
  end
  -- Ceiling + wait-start reaping over WINNERS only, so a dead duplicate for a reused
  -- pane can't inflate the gradient ceiling or keep a stale wait-start alive.
  for pane_id, e in pairs(fresh) do
    if e.status == 'waiting' then
      seen[e.sid] = true
      if (not restrict or live[pane_id]) and e.age > longest then longest = e.age end
    end
  end

  -- Per-folder instance number: 1-based rank among OPEN tabs sharing a folder,
  -- ordered by pane id, so two sessions in one project read f1/f2 and a lone one
  -- reads y1. Dead/lingering state files are excluded (same live-set as the ceiling).
  local groups = {}
  for pane_id, e in pairs(fresh) do
    if (not restrict) or live[pane_id] then
      local g = groups[e.fkey]
      if not g then g = {}; groups[e.fkey] = g end
      g[#g + 1] = pane_id
    end
  end
  for _, ids in pairs(groups) do
    table.sort(ids, function(a, b) return (tonumber(a) or 0) < (tonumber(b) or 0) end)
    for i, pid in ipairs(ids) do fresh[pid].n = i end
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

  -- Hidden "until idle" watcher. A parked pane still writes its state file, so the
  -- `fresh` scan above already holds its live status: ping once when it flips to
  -- waiting, and GC any hidden record whose pane has since closed (dropped from the
  -- `live` set). The single-threaded event loop makes this read-mutate-write atomic
  -- across windows, so exactly ONE window's tick fires the toast even though every
  -- window runs this — which is what we want, since it must also fire while WezTerm
  -- is unfocused. Fully pcall-guarded: a hiccup here must
  -- never wedge the tab bar.
  pcall(function()
    local hid = wezterm.GLOBAL.hidden
    if not (hid and next(hid)) then return end
    local by_sid = {}
    for pid, e in pairs(fresh) do by_sid[e.sid] = { status = e.status, pane = pid } end
    local changed = false
    for sid, h in pairs(hid) do
      local st = by_sid[sid]
      if not st or (restrict and not live[st.pane]) then
        hid[sid] = nil; changed = true                          -- pane gone -> GC
      elseif h.notify and not h.notified and st.status == 'waiting' then
        h.notified = true; changed = true
        window:toast_notification('fleet', (h.label or sid) .. ' is idle', nil, 4000)
      end
    end
    if changed then wezterm.GLOBAL.hidden = hid end
  end)
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

-- A one-char status glyph. The hook-owned STATE is authoritative (waiting -> '●',
-- working -> '✳'); we fall back to parsing the title only when there's no state,
-- because Claude Code rewrites the title ('✳ <task>') and can't be trusted to carry
-- the '●' marker. Surfaced right after the tab number so right-edge truncation can
-- never swallow it. Idle/clean/unknown -> a dim dot.
--
-- NOTE: '✳'/'●' are 3-byte UTF-8 chars and MUST be matched as whole literals,
-- never inside a '[...]' class — a class matches individual bytes, which splits
-- the glyph and yields invalid UTF-8 that WezTerm rejects (the tab then silently
-- falls back to its default 'N:' rendering).
local function status_glyph(st, t)
  if st and st.status == 'waiting' then return '●' end
  if st and st.status == 'working' then return '✳' end
  t = t or ''
  if t:match('●%s*$') then return '●' end
  if t:match('^%s*✳') or t:match('^%s*%*') then return '✳' end
  return '·'
end

-- Strip the status glyphs so the label spends its columns on real text.
local function compact_label(t)
  return (t or ''):gsub('%s*●%s*$', ''):gsub('^%s*✳%s*', ''):gsub('^%s*%*%s*', '')
end

-- Branch names that carry no signal when every tab is on them.
local DEFAULT_BRANCHES = { main = true, master = true }

-- Build a label that spends its columns on what DISTINGUISHES this session from
-- its siblings, and adapts to how much room the tab has:
--   * working tabs -> the task text (already unique + informative)
--   * otherwise the title is "project branch ·tag"; drop a default branch, and
--     when the tab is tight LEAD with the distinguishing bit (feature branch,
--     else the ·tag) so right-edge truncation can't eat it — instead of a
--     repeated "project main" that truncates to a useless "fsp-a…".
local SMART_TIGHT = 18
local function smart_label(pt, target)
  if pt:match('^%s*✳') or pt:match('^%s*%*') then
    return compact_label(pt)                          -- working: task text
  end
  local base = compact_label(pt)
  local project = base:match('^(%S+)') or base
  local tag = base:match('·(%w+)')
  local branch = base:gsub('^%S+%s*', ''):gsub('%s*·%w+%s*$', ''):gsub('%s+$', '')
  if DEFAULT_BRANCHES[branch] or branch == project then branch = '' end

  local distinct = branch ~= '' and branch or (tag and ('·' .. tag)) or ''
  if target and target <= SMART_TIGHT and distinct ~= '' then
    return distinct .. ' ' .. project                 -- tight: what differs, first
  end
  local parts = { project }
  if branch ~= '' then parts[#parts + 1] = branch end
  if tag then parts[#parts + 1] = '·' .. tag end
  return table.concat(parts, ' ')
end

-- The clicked-into tab gets this highlight so it's unmistakable in the row.
local ACTIVE = { bg = '#7aa2f7', fg = '#1a1b26' }   -- Tokyo Night blue / dark ink

-- Columns held back from the tab row for the '+' new-tab button (right edge) plus
-- a margin, so the summed tab widths stay inside the window (the hard overflow limit).
local TAB_BAR_RESERVE = 8

-- CRITICAL WezTerm quirk: the retro tab bar EQUALIZES every tab to a uniform width
-- (discarding our per-tab widths) once the COLORED (non-collapsed) tabs fill past
-- ~this fraction of the window. So the real budget for shown tabs is this fraction,
-- NOT the whole bar; collapsed dots fill the rest. Measured empirically: ~92% full
-- equalizes, comfortably honored well below — 0.80 leaves margin. (Verified by
-- screenshotting the live window and reading back rendered vs. requested widths.)
local EQUALIZE_FRAC = 0.80

-- How much width the ACTIVE tab may RESERVE for its label before the other tabs
-- get their turn. Deliberately well under tab_max_width so a busy row spends its
-- columns on code tabs instead of one fat active tab. When few tabs are open the
-- active tab still absorbs the leftover (up to tab_max_width), so it fills nicely
-- and shows more of its title — the reduction only bites once the row is contended.
local ACTIVE_MAX_RESERVE = 30

-- Overflow mode: when tabs don't all fit, the rightmost ones collapse to a thin
-- sliver and the collapse boundary shows a ›N marker (N = how many are hidden;
-- reach them via the picker, Ctrl+Shift+Space). These are the assumed rendered
-- widths of a collapsed sliver and of that marker, reserved so the slivers WezTerm
-- still draws can't squeeze the tabs that ARE shown.
local COLLAPSE_W       = 4
local OVERFLOW_MARKER_W = 3
local COLLAPSE_BG = '#1a1b26'   -- tab-bar bg (Tokyo Night)
local COLLAPSE_FG = '#565f89'   -- dim grey

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

-- A tab's essential unit and its width: ` [acronym][n] [glyph] `. This is the hard
-- floor — a tab is never sized (or truncated) below this. Works off any tab in the
-- `tabs` list, so format-tab-title can budget across the whole row.
local function tab_essential(t)
  local p = t.active_pane
  local st = p and pane_state[tostring(p.pane_id)] or nil
  local glyph = status_glyph(st, p and p.title or '')
  local tag = (st and st.acr and st.n) and (st.acr .. st.n) or tostring((t.tab_index or 0) + 1)
  return dispw(' ' .. tag .. ' ' .. glyph .. ' '), tag, glyph
end

-- A stable label for a session's folder, from pane_state's group key: the project
-- folder name, or the basename for a non-project cwd. Used as the active tab's
-- fallback when its live title is momentarily empty (foreground Claude sessions).
local function fkey_display(fk)
  if not fk or fk == '' then return nil end
  if fk:sub(1, 1) == '#' then
    local b = fk:sub(2):gsub('/+$', ''):gsub('.*/', '')
    return b ~= '' and b or nil
  end
  return fk
end

-- With Claude's animated terminal title disabled, its startup title is just its
-- version number. That is not a useful active-tab label; fall back to the stable
-- project/folder name from pane_state instead. Preserve version-only titles from
-- ordinary shells and applications, which have no Claude state entry.
local function useful_pane_title(title, state)
  if not title or title == '' or (state and title:match('^%d+%.%d+%.%d+$')) then return nil end
  return title
end

-- Cost of collapsing k tabs: one ›N marker cell plus a sliver for each of the rest.
local function collapse_cost(k)
  if k <= 0 then return 0 end
  return OVERFLOW_MARKER_W + (k - 1) * COLLAPSE_W
end

-- Overflow layout over the whole row. The active tab always gets its full label
-- (` [code][n] [icon] project/branch `, capped at tab_max_width); every other tab
-- gets exactly its essential (` [code][n] [icon] `), allocated left→right; whatever
-- no longer fits collapses off the right. Pure + deterministic — same result in
-- every per-tab call, so the tabs agree on one layout. Returns:
--   widths          -> { [tab_index] = width }  (absent key = collapsed)
--   first_collapsed -> tab_index that carries the ›N marker (or nil)
--   collapsed       -> how many tabs are hidden
local function compute_tab_layout(tabs, bar, safe, cap)
  local active_reserve, active_idx = 0, nil
  for _, t in ipairs(tabs) do
    if t.is_active then
      local am, atag, ag = tab_essential(t)
      local ap = t.active_pane
      local ast = ap and pane_state[tostring(ap.pane_id)] or nil
      local raw = useful_pane_title(ap and ap.title, ast) or fkey_display(ast and ast.fkey) or ('tab ' .. (t.tab_index + 1))
      local alabel = smart_label(raw, nil)   -- drops a project==branch duplicate, etc.
      active_reserve = math.min(ACTIVE_MAX_RESERVE, math.max(am, dispw(' ' .. atag .. ' ' .. ag .. ' ' .. alabel .. ' ')))
      active_idx = t.tab_index
    end
  end

  local ess = {}
  for _, t in ipairs(tabs) do
    if not t.is_active then
      local w = tab_essential(t)
      ess[#ess + 1] = { idx = t.tab_index, w = w }
    end
  end

  -- Show a tab only while the COLORED total stays under `safe` (past which WezTerm
  -- equalizes) AND the whole row (colored + collapsed dots + marker) stays under
  -- `bar` (past which it overflows). The rest collapse to dots that fill toward the
  -- right edge.
  local widths = {}
  local used = active_reserve
  local shown = 0
  for i, e in ipairs(ess) do
    local remaining = #ess - i
    if used + e.w <= safe and used + e.w + collapse_cost(remaining) <= bar then
      used = used + e.w
      widths[e.idx] = e.w
      shown = shown + 1
    else
      break
    end
  end

  local collapsed = #ess - shown
  local first_collapsed = (collapsed > 0) and ess[shown + 1].idx or nil

  -- The active tab absorbs slack, but only up to the colored-`safe` ceiling — never
  -- past it, or the row would equalize. So it fills nicely when few tabs are open
  -- and stays modest (near its reserve) when the row is busy.
  if active_idx then
    widths[active_idx] = math.min(cap, safe - (used - active_reserve))
  end

  return widths, first_collapsed, collapsed
end

-- WezTerm calls format-tab-title synchronously in tab order, twice per tab-bar
-- rebuild (measure, then render). Recomputing the whole-row layout in every
-- callback made that O(tab_count²). Tab zero starts each pass, so compute once
-- there and let the remaining callbacks do stable tab-id lookups. Rebuilding at
-- the start of both passes deliberately avoids fragile phase/invalidation state:
-- resize, activation, reorder, add/close, title, and pane-state changes are fresh.
local tab_layout_by_window = {}

local function build_tab_layout(tab, tabs, conf)
  local window_id = tostring(tab.window_id)
  local n = math.max(1, #tabs)
  local barcols = bar_cols_by_window[window_id] or (n * 16)
  local bar = barcols - TAB_BAR_RESERVE
  local safe = math.floor(barcols * EQUALIZE_FRAC)
  local cap = conf.tab_max_width or 64
  local widths, first_collapsed, collapsed = compute_tab_layout(tabs, bar, safe, cap)
  local by_tab_id = {}

  for _, current in ipairs(tabs) do
    by_tab_id[tostring(current.tab_id)] = {
      target = widths[current.tab_index] or false,
      marker = current.tab_index == first_collapsed,
      collapsed = collapsed,
    }
  end

  local row = { by_tab_id = by_tab_id }
  tab_layout_by_window[window_id] = row
  return row
end

-- A clicked link opens in the browser of the account THIS pane's session runs on.
-- Without this, every link goes through xdg-open to the default Brave profile — right
-- for a default-account session, wrong for every other one (it opens claude.ai signed
-- in as the other account). claude-open does the pane -> session -> profile lookup and
-- falls back to xdg-open for a pane that is not a Claude session, so a click in a plain
-- shell behaves exactly as it did before.
wezterm.on('open-uri', function(_, pane, uri)
  if not uri:match('^https?://') then return true end   -- mailto:, file:, ... unchanged
  wezterm.background_child_process({
    wezterm.home_dir .. '/.claude/bin/claude-open',
    '--pane', tostring(pane:pane_id()), uri,
  })
  return false
end)

wezterm.on('format-tab-title', function(tab, tabs, panes, conf, hover, max_width)
  local idx = tab.tab_index + 1
  local pt = tab.active_pane and tab.active_pane.title or ''
  local st = tab.active_pane and pane_state[tostring(tab.active_pane.pane_id)] or nil
  -- Color comes from the hook-owned STATE FILE (via pane_state), not the live title.
  -- Claude Code also writes the title ('✳ <task>'), so it can't be trusted to carry
  -- the '●' waiting marker — half the waiting panes lose it and would render black.
  -- The state file has a single writer (the hook), and update-status resolves
  -- reused-pane collisions by newest `updated`, so pane_state is the authority.
  local c = wait_colors(st)

  -- [acronym][n] (project code + instance) replaces the bare tab number; a tab with
  -- no Claude session (plain shell) keeps the sequential idx.
  local _, tag, glyph = tab_essential(tab)

  -- Overflow layout is computed once for the row and served by stable tab id.
  local window_id = tostring(tab.window_id)
  local row = tab_layout_by_window[window_id]
  if tab.tab_index == 0 or not row then
    row = build_tab_layout(tab, tabs, conf)
  end
  local entry = row.by_tab_id[tostring(tab.tab_id)]
  if not entry then
    -- Defensive recovery if WezTerm ever presents an unexpected callback order.
    row = build_tab_layout(tab, tabs, conf)
    entry = row.by_tab_id[tostring(tab.tab_id)]
  end
  local target = entry.target

  -- Collapsed tab: the boundary one carries the ›N marker; the rest render as an
  -- empty sliver. All remain reachable via the picker (Ctrl+Shift+Space).
  if not target then
    if entry.marker then
      return {
        { Background = { Color = COLLAPSE_BG } },
        { Foreground = { Color = COLLAPSE_FG } },
        { Text = '›' .. entry.collapsed },
      }
    end
    return { { Background = { Color = COLLAPSE_BG } }, { Foreground = { Color = COLLAPSE_FG } }, { Text = '·' } }
  end

  -- Active shows its cleaned title (project/branch/task, de-duplicated); every other
  -- tab is exactly its essential code+icon.
  local text
  if tab.is_active then
    local raw = useful_pane_title(pt, st) or fkey_display(st and st.fkey) or ('tab ' .. idx)
    text = fit(string.format(' %s %s %s ', tag, glyph, smart_label(raw, nil)), target)
  else
    text = fit(' ' .. tag .. ' ' .. glyph .. ' ', target)
  end

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
-- Fuzzy matching covers names/branches/topics; the trailing '🔍' row hands off
-- to session_search for transcript-content grep.
-- ---------------------------------------------------------------------------
local function run_registry()
  local ok, stdout, stderr = wezterm.run_child_process({ HOME .. '/.claude/bin/claude-sessions', '--json' })
  if not ok then return nil, (stderr or 'claude-sessions failed') end
  local okj, recs = pcall(wezterm.json_parse, stdout)
  if not okj then return nil, 'could not parse registry JSON' end
  return recs, nil
end

-- The mux pane a session is REALLY on right now, or nil if it's gone. Two ways to
-- miss: get_pane RAISES on a closed/unknown pane id (e.g. a snoozed tab), and
-- WezTerm RECYCLES pane ids, so a closed session's recorded pane can now belong to
-- a DIFFERENT session — trusting it would land you on the wrong tab (or right where
-- you are). A cwd mismatch is what identifies that reuse. One definition, so the
-- tab number the picker SHOWS and the pane it JUMPS to can never disagree.
local function session_pane(paneid, cwd)
  if not paneid then return nil end
  local ok, mux_pane = pcall(wezterm.mux.get_pane, tonumber(paneid))
  if not ok or not mux_pane then return nil end
  if cwd and cwd ~= '' then
    local okc, cur = pcall(function()
      local url = mux_pane:get_current_working_dir()
      if not url then return nil end
      if type(url) == 'string' then return url end
      return url.file_path or url.path or tostring(url)
    end)
    if okc and cur then
      cur = cur:gsub('^file://[^/]*', ''):gsub('/+$', '')
      if cur ~= cwd:gsub('/+$', '') then return nil end   -- reused pane, wrong session
    end
  end
  return mux_pane
end

local function activate_or_resume(win, pane, paneid, sid, cwd)
  local mux_pane = session_pane(paneid, cwd)
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
    act.SpawnCommandInNewTab { cwd = cwd, args = { 'bash', '-lic', 'unset CLAUDE_CODE_CHILD_SESSION; claude --resume ' .. sid .. '; exec bash' } },
    pane)
end

-- Resurface a hidden (still-running) session and clear its hidden record. Preferred
-- path: reattach the live pane as a TAB in the window you're looking at now. The Lua
-- pane API can only move a pane into a *new* window, but `wezterm cli
-- move-pane-to-new-tab --window-id` can target an EXISTING one. If that CLI move
-- fails (wezterm not on PATH, a cross-workspace refusal, any non-zero exit), fall
-- back to move_to_new_window so the session still comes back — just standalone. If
-- the pane is gone (WezTerm restarted, tab manually closed), drop the record and
-- fall through to the normal resume path. Distinct from activate_or_resume: this
-- RELOCATES a known-live pane; that one FINDS-or-RESTARTS.
local function resurface_hidden(win, pane, paneid, sid, cwd)
  local m = hidden_map()
  m[sid] = nil
  set_hidden_map(m)
  local mux_pane
  if paneid then
    local ok, p = pcall(wezterm.mux.get_pane, tonumber(paneid))
    if ok then mux_pane = p end
  end
  if mux_pane then
    -- run_child_process returns success=false (it does NOT raise) on a non-zero
    -- exit, and raises only if the binary is missing — so a reattach counts only
    -- when the pcall did NOT raise AND the command reported success.
    local ok, reattached = pcall(function()
      return wezterm.run_child_process({
        'wezterm', 'cli', 'move-pane-to-new-tab',
        '--pane-id', tostring(paneid),
        '--window-id', tostring(win:mux_window():window_id()),
      })
    end)
    if not (ok and reattached) then
      pcall(function() mux_pane:move_to_new_window(win:active_workspace()) end)  -- fallback: standalone window
    end
    pcall(function() mux_pane:activate() end)
    return
  end
  activate_or_resume(win, pane, paneid, sid, cwd)
end

-- Content search (the picker's '🔍 search transcripts…' row): prompt for text,
-- grep LIVE session transcripts via claude-search, then hand the matches to
-- the same jump/resume flow as the picker. claude-search is the live-scoped
-- sibling of the /find-session skill — and the picker can only jump to
-- sessions that are actually open, which is exactly what claude-search scopes
-- to. Results are ranked by match count.
local function session_search(window, pane)
  window:perform_action(act.PromptInputLine {
    description = 'Find live session — transcript contains:',
    action = wezterm.action_callback(function(win, p, line)
      if not line or line == '' then return end       -- cancelled / empty
      local ok, stdout, stderr = wezterm.run_child_process({ HOME .. '/.claude/bin/claude-search', line })
      if not ok then
        win:toast_notification('fleet', stderr or 'claude-search failed', nil, 4000); return
      end
      -- claude-search prints "count sid[:8] cwd"; join to the registry (full
      -- session_id + pane + label) by 8-char prefix so we can jump/resume.
      local recs = run_registry() or {}
      local by_prefix = {}
      for _, r in ipairs(recs) do by_prefix[tostring(r.session_id):sub(1, 8)] = r end

      local choices, pane_by_id, cwd_by_id = {}, {}, {}
      for l in stdout:gmatch('[^\n]+') do
        local count, sid8, cwd = l:match('^%s*(%d+)%s+(%x+)%s+(.+)$')
        if sid8 then
          local r = by_prefix[sid8]
          local id = r and r.session_id or sid8
          table.insert(choices, {
            id = id,
            label = string.format('%3s×  %-28s %s', count, (r and r.label) or sid8, (r and r.project) or cwd),
          })
          pane_by_id[id] = r and r.wezterm_pane
          cwd_by_id[id] = (r and r.cwd) or cwd
        end
      end
      if #choices == 0 then
        win:toast_notification('fleet', 'no live session matches "' .. line .. '"', nil, 3000); return
      end
      win:perform_action(act.InputSelector {
        title = 'Fleet — search: ' .. line,
        fuzzy = true,
        choices = choices,
        action = wezterm.action_callback(function(w2, p2, id)
          if not id then return end
          if hidden_map()[id] then
            resurface_hidden(w2, p2, pane_by_id[id], id, cwd_by_id[id])
          else
            activate_or_resume(w2, p2, pane_by_id[id], id, cwd_by_id[id])
          end
        end),
      }, p)
    end),
  }, pane)
end

local function session_picker(window, pane)
  local recs, err = run_registry()
  if not recs then window:toast_notification('fleet', err, nil, 4000); return end
  local hid = hidden_map()   -- sessions parked by "keep running" resurface, not jump-in-place

  -- Size the folder and label columns to their actual content (capped so one long
  -- name can't blow out the row), so we use the width when it's there and stay
  -- tight when it isn't — instead of truncating names that would have fit.
  local fw, lw = 10, 6
  for _, r in ipairs(recs) do
    fw = math.max(fw, dispw(r.project or ''))
    lw = math.max(lw, dispw(r.label or r.session_id or ''))
  end
  fw, lw = math.min(fw, 26), math.min(lw, 22)

  -- Which tab each live pane sits in, so a row can lead with the number you'd press
  -- to get there. Position within its window (0-based -> 1-based), matching both the
  -- tab bar and ActivateTab. tabs_with_info carries the index explicitly; plain
  -- tabs() doesn't document its ordering, and a confidently WRONG number is worse
  -- than none — so on any failure the map stays empty and the column goes blank.
  local tab_of_pane, tabw = {}, 1
  pcall(function()
    for _, w in ipairs(wezterm.mux.all_windows()) do
      for _, e in ipairs(w:tabs_with_info()) do
        local n = e.index + 1
        tabw = math.max(tabw, #tostring(n))
        for _, p in ipairs(e.tab:panes()) do tab_of_pane[tostring(p:pane_id())] = n end
      end
    end
  end)

  -- Right-aligned so the digits line up; blank (but still padded) for a session with
  -- no open tab — snoozed, closed, or running under another mux.
  local function tabcell(paneid, cwd)
    local n = session_pane(paneid, cwd) and tab_of_pane[tostring(paneid)] or nil
    local s = n and tostring(n) or ''
    return string.rep(' ', tabw - #s) .. s .. ' '
  end
  local blankcell = string.rep(' ', tabw + 1)

  local choices, pane_by_id, cwd_by_id, seen_ids, scheduled_ids = {}, {}, {}, {}, {}
  for _, r in ipairs(recs) do
    local grp = r.group and (' [' .. r.group .. ']') or ''
    -- Columns are padded by DISPLAY width (fit/column_width), not bytes, so the
    -- 2-cell colored glyphs and the multibyte ·tags don't drift the alignment the
    -- way string.format's byte-based %-Ns padding does. Order leads with the FOLDER
    -- (the real identity), then the branch/·tag, age, and finally the topic — kept
    -- untruncated so it stays fully fuzzy-searchable and distinguishes siblings.
    local row = tabcell(r.wezterm_pane, r.cwd)
      .. fit(r.glyph or '·', 2) .. ' '
      .. fit(r.project or '', fw) .. ' '
      .. fit(r.label or r.session_id, lw) .. ' '
      .. fit(r.age_str or '', 4) .. '  '
      .. (r.topic or '') .. grp
      .. (hid[r.session_id] and '  💤 hidden' or '')
    table.insert(choices, { id = r.session_id, label = row })
    pane_by_id[r.session_id] = r.wezterm_pane
    cwd_by_id[r.session_id] = r.cwd
    seen_ids[r.session_id] = true
  end

  -- Union in SNOOZED (closed, scheduled-to-reopen) sessions so you can reopen one
  -- early. They have no live pane, so activate_or_resume resumes them; selecting
  -- one also cancels its schedule. See bin/claude-schedule.
  local sok, sched = wezterm.run_child_process({ HOME .. '/.claude/bin/claude-schedule', 'list', '--json' })
  if sok and sched and sched ~= '' then
    local okj, list = pcall(wezterm.json_parse, sched)
    if okj and type(list) == 'table' then
      for _, e in ipairs(list) do
        local id = e.session_id
        if id and not seen_ids[id] then
          table.insert(choices, {
            id = id,
            label = blankcell .. fit('⏰', 2) .. ' ' .. fit(e.label or id, fw + lw + 1)
              .. '  reopens in ' .. (e.wakes_in or '?'),
          })
          cwd_by_id[id] = e.cwd
          scheduled_ids[id] = true
        end
      end
    end
  end

  if #choices == 0 then window:toast_notification('fleet', 'no live or snoozed sessions', nil, 3000); return end

  -- Deeper search as a picker row (same pattern as snooze's 'Custom…'): the
  -- rows above fuzzy-match on names/topics only, so offer the transcript-
  -- content grep when the session you want isn't findable by label.
  table.insert(choices, { id = '__search__', label = blankcell .. fit('🔍', 2) .. ' search transcripts…' })

  window:perform_action(act.InputSelector {
    title = 'Fleet — sessions',
    fuzzy = true,
    choices = choices,
    action = wezterm.action_callback(function(win, p, id)
      if not id then return end       -- cancelled
      if id == '__search__' then return session_search(win, p) end
      if hidden_map()[id] then
        resurface_hidden(win, p, pane_by_id[id], id, cwd_by_id[id])
      else
        activate_or_resume(win, p, pane_by_id[id], id, cwd_by_id[id])
      end
      if scheduled_ids[id] then
        wezterm.run_child_process({ HOME .. '/.claude/bin/claude-schedule', 'cancel', '--sid', id })
      end
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
        args = { 'bash', '-lic', 'unset CLAUDE_CODE_CHILD_SESSION; ' .. (first.cmd or 'claude') .. '; exec bash' },
      }
      for i = 2, #entries do
        local e = entries[i]
        mux_win:spawn_tab { cwd = e.cwd, args = { 'bash', '-lic', 'unset CLAUDE_CODE_CHILD_SESSION; ' .. (e.cmd or 'claude') .. '; exec bash' } }
      end
      win:perform_action(act.SwitchToWorkspace { name = id }, p)
    end),
  }, pane)
end

-- ---------------------------------------------------------------------------
-- Sleep this tab (CTRL+SHIFT+S): one flat picker (SLEEP_CHOICES) with three ways to
-- park it — pick any in a single selection (1–9 quick-keys).
--   • Kill & reopen — schedule a reopen (bin/claude-schedule) then CLOSE the tab.
--     Ends the process, but the transcript survives so `claude --resume` brings it
--     back exactly — which is why we fsync it BEFORE closing (below). Reopen fires
--     via the snapshot timer; resume early from the session picker. Each preset time
--     is its own row; "custom time…" chains one text prompt (free text only).
--   • Keep running, hide — move the LIVE pane to the __hidden__ workspace; the
--     process keeps running off-screen. Resurface from the picker (CTRL+SHIFT+␣).
--   • Keep running, hide until idle — as above, plus a one-shot toast when it next
--     goes idle (update-status watcher). See hide_do / resurface_hidden.
-- ---------------------------------------------------------------------------

-- Flush the session's transcript(s) to disk before closing the tab. Closing sends
-- the process SIGHUP; if the kernel is still holding the last append in the page
-- cache, an ill-timed shutdown afterward could roll it back. An idle session (the
-- normal snooze case) has already flushed via the Stop hook, so this is mostly
-- belt-and-suspenders — but it makes snooze durable by construction rather than by
-- timing. (It cannot save content a mid-turn session hasn't written yet.)
local FSYNC_PY = [[
import os, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)
except OSError:
    pass
]]
local function fsync_transcript(sid)
  if not sid or sid == '' then return end
  for _, p in ipairs(wezterm.glob(HOME .. '/.claude/projects/*/' .. sid .. '.jsonl')) do
    pcall(wezterm.run_child_process, { 'python3', '-c', FSYNC_PY, p })
  end
end

local function snooze_do(win, pane, rec, when)
  local disp = ((rec.project or '') .. ' ' .. (rec.label or '')):gsub('^%s+', ''):gsub('%s+$', '')
  local ok, stdout, stderr = wezterm.run_child_process({
    HOME .. '/.claude/bin/claude-schedule', 'add',
    '--sid', rec.session_id, '--cwd', rec.cwd or '',
    '--when', when, '--label', disp ~= '' and disp or rec.session_id,
  })
  if not ok then
    win:toast_notification('fleet', 'snooze failed: ' .. (stderr or ''), nil, 4000); return
  end
  -- Echo the resolved time ("scheduled <sid> -> 2026-07-13 09:00") back in the
  -- toast: free-form phrases may have been interpreted by an LLM, so show what
  -- was actually understood before the tab disappears.
  local at = (stdout or ''):match('%-> *(%d[%d%- :]+%d)')
  win:toast_notification('fleet', 'snoozed ' .. disp .. (at and (' — back ' .. at) or ''), nil, 2500)
  fsync_transcript(rec.session_id)   -- make the transcript durable before we close
  win:perform_action(act.CloseCurrentTab { confirm = false }, pane)
end

-- Keep running, hide (CTRL+SHIFT+S → option 2/3): move the LIVE pane into a
-- background workspace so the process keeps churning off-screen, then switch back
-- to where you were. move_to_new_window preserves the pane id, so the registry and
-- the state hook keep resolving this session to its (now hidden) pane — it stays a
-- normal live row in the picker, from which resurface_hidden brings it back.
-- `notify` = ping once when it next goes idle (option A: no focus stealing).
local function hide_do(win, pane, rec, notify)
  local disp = ((rec.project or '') .. ' ' .. (rec.label or '')):gsub('^%s+', ''):gsub('%s+$', '')
  if disp == '' then disp = rec.session_id end
  -- Seed `notified` from the CURRENT status: if the session is already idle at hide
  -- time, don't fire a pointless "is idle" ping a second later — only ping on its
  -- next transition into idle. (pane_state is the same hook-owned status the tab bar
  -- reads; a missing entry defaults to not-idle, i.e. ping on next wait.)
  local pid = pane and tostring(pane:pane_id())
  local cur = pid and pane_state[pid]
  local already_idle = cur ~= nil and cur.status == 'waiting'
  local return_ws = win:active_workspace()
  local ok, err = pcall(function() pane:move_to_new_window(HIDDEN_WS) end)
  if not ok then
    win:toast_notification('fleet', 'hide failed: ' .. tostring(err), nil, 4000); return
  end
  local m = hidden_map()
  m[rec.session_id] = { notify = notify and true or false, notified = already_idle, label = disp }
  set_hidden_map(m)
  -- Return to where we were. If hiding emptied this GUI window (the sole tab of a
  -- sole window on the workspace), the move closed it and `win` is now stale — pcall
  -- so a raised action can't strand us on __hidden__ (recover via Ctrl+Shift+O).
  pcall(function()
    win:perform_action(act.SwitchToWorkspace { name = return_ws }, pane)
    win:toast_notification('fleet',
      'hidden ' .. disp .. (notify and ' — will ping when idle' or ' — still running'),
      nil, 2500)
  end)
end

local SNOOZE_PRESETS = {
  { id = '1h',           label = 'in 1 hour' },
  { id = '4h',           label = 'in 4 hours' },
  { id = 'tonight',      label = 'tonight (8pm)' },
  { id = 'tomorrow 9am', label = 'tomorrow 9am' },
  { id = 'monday 9am',   label = 'Monday at 9am' },
  { id = '1w',           label = 'in 1 week' },
  { id = '__custom__',   label = 'Custom…' },
}

-- Flat "Sleep this tab…" menu (CTRL+SHIFT+S): hide-until-idle first, then every
-- kill-&-reopen time, then plain hide last — all ONE selection deep, no nested
-- sub-menu. The `hide —` / `kill & reopen —` prefixes group them for the eye and for
-- fuzzy typing; with fuzzy off (default), the leading 1–9 quick-keys select a row
-- instantly, no Enter. Only a custom time still needs a follow-up text prompt (free
-- text has nowhere to live in a selector). Reopen times come from SNOOZE_PRESETS so
-- they stay defined in exactly one place.
local SLEEP_CHOICES = {
  { id = 'hide_idle', label = 'hide until idle — keep running' },
}
for _, p in ipairs(SNOOZE_PRESETS) do
  local lbl = (p.id == '__custom__') and 'custom time…' or p.label
  SLEEP_CHOICES[#SLEEP_CHOICES + 1] = { id = p.id, label = 'kill & reopen — ' .. lbl }
end
SLEEP_CHOICES[#SLEEP_CHOICES + 1] = { id = 'hide', label = 'hide — keep running' }

local function session_snooze(window, pane)
  local recs = run_registry() or {}
  local pid = pane and tostring(pane:pane_id())
  local rec
  for _, r in ipairs(recs) do
    if tostring(r.wezterm_pane) == pid then rec = r; break end
  end
  if not rec then
    window:toast_notification('fleet', 'no Claude session in this tab', nil, 3000); return
  end
  window:perform_action(act.InputSelector {
    title = 'Sleep this tab…',
    choices = SLEEP_CHOICES,
    action = wezterm.action_callback(function(win, p, id)
      if not id then return end                          -- cancelled
      if id == 'hide' then hide_do(win, p, rec, false)
      elseif id == 'hide_idle' then hide_do(win, p, rec, true)
      elseif id == '__custom__' then
        win:perform_action(act.PromptInputLine {
          description = 'Reopen when?  e.g. "tomorrow 14:00", "monday morning", "3 days", "2026-07-10 09:00"',
          action = wezterm.action_callback(function(w2, p2, line)
            if line and line ~= '' then snooze_do(w2, p2, rec, line) end
          end),
        }, p)
      else
        snooze_do(win, p, rec, id)                       -- id is a preset time token
      end
    end),
  }, pane)
end

-- ---------------------------------------------------------------------------
-- Keys. Letters chosen to avoid clobbering core WezTerm defaults (T/W/C/V/N/P).
-- Rebind freely. If 'Space' errors at load, use a letter or 'phys:Space'.
-- ---------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- Persistent mux domain (durability). The pty host lives in wezterm-mux-server
-- (a lingering systemd --user service), NOT in this GUI process — so a GUI/X/gdm
-- death no longer cascades into every child `claude`. Defined but never
-- auto-connected (no connect_automatically), so the running GUI picking this up
-- on a config reload is a NO-OP: sessions only enter the mux when you spawn into
-- it (CTRL+SHIFT+U) or, at full cutover, when you set config.default_domain.
-- socket_path MUST match wezterm/mux-server.lua exactly.
-- ---------------------------------------------------------------------------
do
  local runtime = os.getenv('XDG_RUNTIME_DIR') or (HOME .. '/.local/state')
  config.unix_domains = { { name = 'mux', socket_path = runtime .. '/wezterm/muxsvr-sock' } }
end

config.keys = {
  { key = 'Space', mods = 'CTRL|SHIFT', action = wezterm.action_callback(session_picker) },
  -- Durable session: open a new tab whose pty lives in the persistent mux, so it
  -- survives a GUI/gdm restart. pcall-guarded — if the mux server is down it
  -- toasts instead of erroring. Full cutover is config.default_domain = 'mux'.
  { key = 'u',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(function(win, pane)
      local ok, err = pcall(function()
        win:perform_action(act.SpawnCommandInNewTab {
          domain = { DomainName = 'mux' }, args = { 'bash', '-l' },
        }, pane)
      end)
      if not ok then
        win:toast_notification('WezTerm', 'mux domain unavailable: ' .. tostring(err), nil, 4000)
      end
    end) },
  -- CTRL+SHIFT+F deliberately unbound: transcript search lives in the picker
  -- ('🔍' row), and F reverts to WezTerm's native scrollback search.
  { key = 's',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(session_snooze) },
  { key = 'o',     mods = 'CTRL|SHIFT', action = act.ShowLauncherArgs { flags = 'FUZZY|WORKSPACES' } },
  { key = 'g',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(launch_family) },
  -- Readable history: open the focused pane's Claude session transcript in a
  -- new tab, printed as logical lines so WezTerm does the wrapping — reflows on
  -- resize, scrollbar gauges it, selections copy unbroken. The tab drops into a
  -- shell after printing (Ctrl+D closes). See bin/claude-transcript.
  { key = 'h',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(function(win, pane)
      win:perform_action(act.SpawnCommandInNewTab {
        args = { HOME .. '/.claude/bin/claude-transcript',
                 '--pane', tostring(pane:pane_id()), '--hold' },
      }, pane)
    end) },
  -- Attend: jump to the left-most tab that is NOT working — a waiting '●' or idle
  -- '·' tab (same status_glyph the tab bar renders). If already on it, advance to
  -- the next one, so repeated presses skip past tabs you've decided to leave.
  { key = 'a',     mods = 'CTRL|SHIFT', action = wezterm.action_callback(function(win, pane)
      local active_id = win:active_tab() and win:active_tab():tab_id() or nil
      local idle, on_first = {}, false
      for i, t in ipairs(win:mux_window():tabs()) do
        local ap = t:active_pane()
        local st = ap and pane_state[tostring(ap:pane_id())] or nil
        if status_glyph(st, ap and ap:get_title() or '') ~= '✳' then
          idle[#idle + 1] = i - 1
          if #idle == 1 and t:tab_id() == active_id then on_first = true end
        end
      end
      if #idle == 0 then
        win:toast_notification('WezTerm', 'all tabs are working', nil, 2000)
        return
      end
      win:perform_action(act.ActivateTab(on_first and idle[2] or idle[1]), pane)
    end) },
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
