#!/usr/bin/env bash
# Set the terminal tab title with a state marker.
#
# Hooks run with NO controlling terminal (`tty` => "not a tty", /dev/tty is
# "No such device"), so a hook CANNOT printf an OSC title escape itself — bytes
# written to stdout/stderr are captured by Claude Code as text and never reach
# the terminal emulator. The supported path is the `terminalSequence` hook-output
# field: we print {"terminalSequence": "<escape>"} to stdout and Claude Code,
# which owns the PTY, writes the escape to the real terminal for us.
#
# Arg $1 = trailing marker glyph: '●' = waiting on you, '' = working/clean.
# The marker is a SUFFIX because Tilix left-trims long tab titles.

marker="$1"
INPUT=$(cat)

# The marker reflects ONLY the main agent's idle state. Events fired from
# inside a dispatched subagent (SubagentStop, or a subagent's own tool use)
# carry agent_id/agent_type — ignore those so a background subagent can't
# flip the tab marker while the main agent's state is unchanged.
event=$(jq -r '.hook_event_name // empty' <<<"$INPUT")
agent_id=$(jq -r '.agent_id // empty' <<<"$INPUT")
if [[ -n "$agent_id" || "$event" == "SubagentStop" ]]; then
    exit 0
fi

# Stop hooks can re-fire recursively; bail on the recursive invocation.
if [[ "$(jq -r '.stop_hook_active // false' <<<"$INPUT")" == "true" ]]; then
    exit 0
fi

CWD=$(jq -r '.cwd // empty' <<<"$INPUT")
session_id=$(jq -r '.session_id // empty' <<<"$INPUT")
project="${CWD##*/}"

# Label resolution. Multiple tabs in the same repo on the same branch would
# otherwise collapse to an identical "project branch" title, so:
#   1. A manual per-tab name (keyed by session) wins and is shown verbatim —
#      set one with:  echo my-name > /tmp/.tab-label-$CLAUDE_CODE_SESSION_ID
#   2. Otherwise use the git branch plus a short session-id tag, which keeps
#      same-repo/same-branch tabs distinguishable automatically.
label_file="/tmp/.tab-label-${session_id}"
if [[ -n "$session_id" && -f "$label_file" ]]; then
    label=$(< "$label_file")
else
    branch=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null)
    label="${branch:-$project}"
    [[ -n "$session_id" ]] && label="$label ·${session_id:0:4}"
fi

title="$project $label"
[[ -n "$marker" ]] && title="$title $marker"

seq=$(printf '\033]0;%s\007' "$title")
jq -nc --arg seq "$seq" '{terminalSequence: $seq}'

exit 0
