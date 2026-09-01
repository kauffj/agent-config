-- Pure data decisions for wezterm.lua's session picker and shared tab identity.
-- WezTerm I/O and pane mutation stay in the config; these rules can run in a
-- scratch config without creating, focusing, or moving real user tabs.

local M = {}

local function record_with_id(records, session_id)
  for _, record in ipairs(records or {}) do
    if record.session_id == session_id then return record end
  end
  return nil
end

function M.refresh_record(record, live_records, scheduled_records, schedule_err)
  local fresh = record_with_id(live_records, record.session_id)
  if fresh then return fresh, nil, 'live' end
  if not record.scheduled then
    -- The agent can exit while its picker is open. The stale row still carries
    -- the registry-generated native resume command and cwd; preserve it so the
    -- caller can reopen the now-closed session through the single-writer lease.
    -- A synthetic/search row without both fields still fails closed.
    if type(record.resume_command) ~= 'string' or record.resume_command == ''
       or type(record.cwd) ~= 'string' or record.cwd == '' then
      return nil, 'cannot safely resume this session'
    end
    return record, nil, 'closed'
  end

  if not scheduled_records then return nil, schedule_err end
  fresh = record_with_id(scheduled_records, record.session_id)
  if not fresh then return nil, 'scheduled session is no longer available' end
  return fresh, nil, 'scheduled'
end

-- A registry snapshot proves which native process owned a pane when the
-- snapshot was taken. Re-reading it after capturing the pane object gives the
-- caller a safe fallback when the terminal cannot publish OSC 1337 user vars:
-- the same session, process, and pane must still be live. The captured object
-- cannot turn into a later pane merely because its numeric id is recycled.
function M.same_live_process(record, live_records)
  if type(record) ~= 'table' or type(record.session_id) ~= 'string'
     or record.session_id == '' or type(record.pid) ~= 'number'
     or record.pid <= 0 or record.wezterm_pane == nil then
    return false
  end
  local fresh = record_with_id(live_records, record.session_id)
  return type(fresh) == 'table'
    and fresh.pid == record.pid
    and fresh.wezterm_pane ~= nil
    and tostring(fresh.wezterm_pane) == tostring(record.wezterm_pane)
end

-- The picker must show the same navigation key that the tab bar renders. Agent
-- tabs use a stable project acronym + per-project instance; ordinary tabs keep
-- WezTerm's one-based positional fallback.
function M.tab_tag(state, tab_index)
  if type(state) == 'table' and state.acr ~= nil and state.n ~= nil then
    return tostring(state.acr) .. tostring(state.n)
  end
  return tostring((tonumber(tab_index) or 0) + 1)
end

-- A refreshed closed classification outranks any stale pane marker left behind
-- on the shell. This is deliberately pure so the resume-vs-activate ordering is
-- executable without creating or focusing real tabs.
function M.direct_action(selection_state, pane_available)
  if selection_state == 'closed' then return 'resume' end
  if pane_available then return 'activate' end
  return nil
end

-- Registry-generated labels end in a short, unique session tag. The picker
-- caps this column, so leaving the tag at the right edge turns sibling rows
-- such as "canonical-prose-view ·d126" into the same truncated label. Lead
-- with the tag there; custom labels (which have no generated suffix) stay put.
function M.distinguishing_label(label)
  if type(label) ~= 'string' then return label end
  local stem, tag = label:match('^(.-)%s+(·[%w._%-]+)%s*$')
  if not stem or stem == '' then return label end
  return tag .. ' ' .. stem
end

return M
