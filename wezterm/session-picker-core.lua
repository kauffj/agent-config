-- Pure data decisions for wezterm.lua's session picker. WezTerm I/O and pane
-- mutation stay in the config; these rules can run in a scratch config without
-- creating, focusing, or moving real user tabs.

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

function M.matches_session(user_vars, session_id)
  return type(session_id) == 'string' and session_id ~= ''
    and type(user_vars) == 'table'
    and user_vars.agent_session == session_id
end

return M
