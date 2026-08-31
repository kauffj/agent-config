local wezterm = require 'wezterm'
local root = assert(os.getenv('AGENT_CONFIG_TEST_ROOT'), 'AGENT_CONFIG_TEST_ROOT is required')
local picker = dofile(root .. '/wezterm/session-picker-core.lua')

local selected, err = picker.refresh_record(
  { session_id = 'chosen' },
  { { session_id = 'chosen', wezterm_pane = '22' } },
  nil, nil)
assert(selected.wezterm_pane == '22' and err == nil,
       'fresh live selection was not used')

selected, err = picker.refresh_record(
  { session_id = 'gone' }, {}, nil, nil)
assert(selected == nil and err == 'session is no longer open',
       'missing live selection did not fail closed')

selected, err = picker.refresh_record(
  { session_id = 'snoozed', scheduled = true }, {},
  { { session_id = 'snoozed', scheduled = true } }, nil)
assert(selected and selected.scheduled and err == nil,
       'current snooze record was not preserved')

selected, err = picker.refresh_record(
  { session_id = 'expired', scheduled = true }, {}, {}, nil)
assert(selected == nil and err == 'scheduled session is no longer available',
       'expired snooze did not fail closed')

selected, err = picker.refresh_record(
  { session_id = 'broken', scheduled = true }, {}, nil, 'schedule unavailable')
assert(selected == nil and err == 'schedule unavailable',
       'schedule read failure was not preserved')

assert(picker.matches_session({ agent_session = 'chosen' }, 'chosen'),
       'exact pane identity was rejected')
assert(not picker.matches_session({ agent_session = 'other' }, 'chosen'),
       'recycled pane identity was accepted')
assert(not picker.matches_session({ agent_session = 'chosen' }, ''),
       'empty selected identity was accepted')
assert(not picker.matches_session(nil, 'chosen'),
       'missing pane identity was accepted')

io.stderr:write('session picker behavior: all cases pass\n')
return wezterm.config_builder()
