#!/usr/bin/env bash
# Hook: Stop — Claude finished responding, now waiting for user input

INPUT=$(cat)

# Prevent loops
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [[ "$STOP_ACTIVE" == "true" ]]; then
    exit 0
fi

CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
project="${CWD##*/}"

# Resolve label: manual override > git branch > project name
label_file="/tmp/.tab-label-$(echo "$CWD" | md5sum | cut -c1-8)"
if [[ -f "$label_file" ]]; then
    label=$(< "$label_file")
else
    label=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null)
fi

# Set tab title with ~ prefix (idle/done)
printf '\e]0;~ %s %s\a' "${label:-$project}" "$project" >&2

exit 0
