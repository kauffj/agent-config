#!/usr/bin/env bash
# Claude Code status line: project context at bottom of TUI + tab title update

input=$(cat)

# Extract fields
project_dir=$(echo "$input" | jq -r '.workspace.project_dir // empty')
current_dir=$(echo "$input" | jq -r '.workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0')
cost=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')
session_id=$(echo "$input" | jq -r '.session_id // empty')

# Derived values
project="${project_dir##*/}"
branch=$(git -C "$current_dir" symbolic-ref --short HEAD 2>/dev/null || git -C "$current_dir" rev-parse --short HEAD 2>/dev/null)
rel_dir="${current_dir#"$project_dir"}"
rel_dir="${rel_dir:-/}"

# The tab title is owned entirely by hooks/session-state.sh (which also handles
# the idle marker and per-tab uniqueness); the statusline no longer writes it.

# Wait-escalation glyph, read from the session's state file (written by
# hooks/session-state.sh). The longer a session sits waiting on you, the louder
# it gets:  ○ waiting <2m  →  ● yellow <10m  →  ● red ≥10m. Working/unknown shows
# nothing, so a live session stays quiet.
wait_glyph=""
state_file="$HOME/.claude/state/${session_id}.json"
if [[ -n "$session_id" && -f "$state_file" ]]; then
    read -r st since < <(jq -r '"\(.status // "") \(.since // 0)"' "$state_file" 2>/dev/null)
    if [[ "$st" == "waiting" && -n "$since" && "$since" != "0" ]]; then
        age=$(( $(printf '%(%s)T' -1) - since ))
        if   (( age >= 600 )); then wait_glyph="\033[31m●\033[0m "   # red ≥10m
        elif (( age >= 120 )); then wait_glyph="\033[33m●\033[0m "   # yellow <10m
        else                        wait_glyph="○ "                  # waiting <2m
        fi
    fi
fi

# Format cost
cost_str=""
if [[ "$cost" != "0" && "$cost" != "null" ]]; then
    cost_str=$(printf ' $%.2f' "$cost")
fi

# Format context with color
used_int=${used_pct%.*}
if (( used_int > 80 )); then
    ctx="\033[31m${used_pct}%\033[0m"
elif (( used_int > 50 )); then
    ctx="\033[33m${used_pct}%\033[0m"
else
    ctx="${used_pct}%"
fi

# Single line: project  branch  cwd  model  context  cost
echo -e "${wait_glyph}\033[2m${project}\033[0m  ${branch}  \033[2m${rel_dir}\033[0m  ${model}  ctx:${ctx}${cost_str}"
