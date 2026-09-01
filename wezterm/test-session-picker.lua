local wezterm = require 'wezterm'
local root = assert(os.getenv('AGENT_CONFIG_TEST_ROOT'), 'AGENT_CONFIG_TEST_ROOT is required')
local picker = dofile(root .. '/wezterm/session-picker-core.lua')

local selected, err, state = picker.refresh_record(
  { session_id = 'chosen' },
  { { session_id = 'chosen', wezterm_pane = '22' } },
  nil, nil)
assert(selected.wezterm_pane == '22' and err == nil and state == 'live',
       'fresh live selection was not used')

selected, err, state = picker.refresh_record(
  { session_id = 'gone', cwd = '/repo', resume_command = 'claude --resume gone' },
  {}, nil, nil)
assert(selected and selected.session_id == 'gone' and err == nil and state == 'closed',
       'session that exited while the picker was open was not preserved for resume')

selected, err, state = picker.refresh_record(
  { session_id = 'unsafe', cwd = '/repo' }, {}, nil, nil)
assert(selected == nil and err == 'cannot safely resume this session' and state == nil,
       'stale selection without a validated resume command did not fail closed')

selected, err, state = picker.refresh_record(
  { session_id = 'snoozed', scheduled = true }, {},
  { { session_id = 'snoozed', scheduled = true } }, nil)
assert(selected and selected.scheduled and err == nil and state == 'scheduled',
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

assert(picker.distinguishing_label('canonical-prose-view ·d126')
         == '·d126 canonical-prose-view',
       'generated session tag was left at the truncated edge')
assert(picker.distinguishing_label('site refresh') == 'site refresh',
       'custom picker label was reordered')
assert(picker.distinguishing_label(nil) == nil,
       'missing picker label was not preserved')

io.stderr:write('session picker behavior: all cases pass\n')
return wezterm.config_builder()
