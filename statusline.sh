#!/usr/bin/env bash
# Claude Code status line: project context at bottom of TUI + tab title update

input=$(cat)

# Extract fields
project_dir=$(echo "$input" | jq -r '.workspace.project_dir // empty')
current_dir=$(echo "$input" | jq -r '.workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0')
cost=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')

# Derived values
project="${project_dir##*/}"
branch=$(git -C "$project_dir" symbolic-ref --short HEAD 2>/dev/null || git -C "$project_dir" rev-parse --short HEAD 2>/dev/null)
rel_dir="${current_dir#"$project_dir"}"
rel_dir="${rel_dir:-/}"

# Update tab title as side effect
label_file="/tmp/.tab-label-$(echo "$project_dir" | md5sum | cut -c1-8)"
if [[ -f "$label_file" ]]; then
    label=$(< "$label_file")
else
    label="$branch"
fi
printf '\e]0;%s %s\a' "$label" "$project" >&2

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
echo -e "\033[2m${project}\033[0m  ${branch}  \033[2m${rel_dir}\033[0m  ${model}  ctx:${ctx}${cost_str}"
